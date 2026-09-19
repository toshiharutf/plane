# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The work item state machine that AI_AGENT bots (and people working on their items) follow.

States are matched by name, case-insensitively::

    Backlog        -> Todo, Awaiting Human        (initial state)
    Todo           -> In Progress                 (set when the item's cycle starts)
    In Progress    -> Awaiting Human, Done
    Awaiting Human -> Todo                        (a human answered; the orchestrator picks it up again)
    Done           -> (final)

Moving a work item to the state it is already in is always allowed (a no-op).

An unassigned work item whose name starts with ``[Human]`` (a ticket a bot wrote
for a person) follows its own table when a bot moves it: the bot hands it to the
person and never takes it back, starts it or closes it::

    Backlog        -> Todo, Awaiting Human
    Todo           -> Awaiting Human
    In Progress    -> Awaiting Human
    Awaiting Human -> (the person closes it)
    Done           -> (final)

On such a ticket a target state of the ``completed`` or ``cancelled`` group is
refused whatever its name is.

Who is checked:

- A workspace ``AI_AGENT`` bot, on every work item: it may only use the tables
  above, and it may create work items only in Backlog, Todo or Awaiting Human.
- Anybody else (people, in the web app or the API) only on a work item that has
  an AI_AGENT bot among its assignees (before or after the change). A person
  may still cancel such an item (any state of the ``cancelled`` group) and move
  it from or to a state outside the five names (a custom state) freely.

Every violation is answered with HTTP 400 and a body
``{"error": ..., "current_state": ..., "requested_state": ..., "allowed_states": [...]}``.

``check_state_change`` and ``check_initial_state`` are the only entry points;
the issue serializers of the public API and the web app call them, so every
endpoint that writes a work item state through them is covered.
"""

# Third party imports
from crum import get_current_user
from rest_framework import status
from rest_framework.exceptions import APIException

# Module imports
from plane.db.models import BotTypeEnum, IssueAssignee, State, StateGroup, User
from plane.utils.members import is_ai_agent_user

BACKLOG = "backlog"
TODO = "todo"
IN_PROGRESS = "in progress"
AWAITING_HUMAN = "awaiting human"
DONE = "done"

# Keep in sync with STATE_TRANSITIONS in the orchestrator's client (plane_api.py).
STATE_TRANSITIONS = {
    BACKLOG: frozenset({TODO, AWAITING_HUMAN}),
    TODO: frozenset({IN_PROGRESS}),
    IN_PROGRESS: frozenset({AWAITING_HUMAN, DONE}),
    AWAITING_HUMAN: frozenset({TODO}),
    DONE: frozenset(),
}

# The table of an unassigned ``[Human] ...`` ticket (see ``is_unassigned_human_ticket``): Todo -> Awaiting Human
# exists only here, and nothing leads out of Awaiting Human. Keep in sync with the orchestrator's client too.
HUMAN_TICKET_STATE_TRANSITIONS = {
    BACKLOG: frozenset({TODO, AWAITING_HUMAN}),
    TODO: frozenset({AWAITING_HUMAN}),
    IN_PROGRESS: frozenset({AWAITING_HUMAN}),
    AWAITING_HUMAN: frozenset(),
    DONE: frozenset(),
}

# State groups that close a work item; on a ``[Human]`` ticket a bot never reaches them, whatever the state is named.
CLOSED_STATE_GROUPS = (StateGroup.COMPLETED.value, StateGroup.CANCELLED.value)

HUMAN_ITEM_PREFIX = "[Human]"

# The states a bot may create a work item in.
INITIAL_STATES = frozenset({BACKLOG, TODO, AWAITING_HUMAN})

STATE_DISPLAY_NAMES = {
    BACKLOG: "Backlog",
    TODO: "Todo",
    IN_PROGRESS: "In Progress",
    AWAITING_HUMAN: "Awaiting Human",
    DONE: "Done",
}
# Display order of allowed targets in error messages.
_ORDER = [BACKLOG, TODO, IN_PROGRESS, AWAITING_HUMAN, DONE]


def state_key(name):
    """The comparison key of a state name: case-insensitive, surrounding and repeated spaces ignored."""
    return " ".join(str(name or "").split()).casefold()


def _display(keys):
    return [STATE_DISPLAY_NAMES[key] for key in _ORDER if key in keys]


def _name(state):
    return state.name if state is not None else "no state"


class WorkItemStateTransitionError(APIException):
    """A state change or an initial state the rules refuse (HTTP 400)."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "invalid_state_transition"

    def __init__(self, message, current_state, requested_state, allowed_states):
        self.message = message
        self.allowed_states = list(allowed_states)
        super().__init__(
            detail={
                "error": message,
                "current_state": current_state,
                "requested_state": requested_state,
                "allowed_states": self.allowed_states,
            }
        )


def current_actor():
    """The user of the request being served (``crum``); ``None`` outside a request (tasks, migrations)."""
    user = get_current_user()
    return user if getattr(user, "is_authenticated", False) else None


def _is_ai_bot_id_list(user_ids):
    ids = [str(getattr(user_id, "id", user_id)) for user_id in user_ids or () if user_id]
    return bool(ids) and User.objects.filter(pk__in=ids, is_bot=True, bot_type=BotTypeEnum.AI_AGENT).exists()


def has_ai_bot_assignee(issue_id, extra_assignee_ids=()):
    """True when the work item (or the requested assignee list) has an AI_AGENT bot among its assignees."""
    if (
        issue_id is not None
        and IssueAssignee.objects.filter(
            issue_id=issue_id,
            deleted_at__isnull=True,
            assignee__is_bot=True,
            assignee__bot_type=BotTypeEnum.AI_AGENT,
        ).exists()
    ):
        return True
    return _is_ai_bot_id_list(extra_assignee_ids)


def is_human_ticket_name(name):
    """True for a ``[Human] ...`` name (any case): a ticket that asks a person for a decision or an action."""
    return str(name or "").strip().casefold().startswith(HUMAN_ITEM_PREFIX.casefold())


def is_unassigned_human_ticket(issue, requested_assignee_ids=()):
    """True for a ``[Human] ...`` work item that has no assignee and gets none in the same request."""
    if issue is None or not is_human_ticket_name(issue.name):
        return False
    if [assignee_id for assignee_id in requested_assignee_ids or () if assignee_id]:
        return False
    return not IssueAssignee.objects.filter(issue_id=issue.pk, deleted_at__isnull=True).exists()


def transition_error(current_state, target_state, for_bot=True, human_ticket=False):
    """``None`` when moving from ``current_state`` to ``target_state`` is allowed, else the error.

    ``for_bot`` applies the bot rules (only the table); otherwise the human exceptions apply
    (a ``cancelled`` group target, or a state outside the table on either side, is allowed).
    ``human_ticket`` (bots only) applies the table of an unassigned ``[Human]`` ticket, where the
    state names never match a state of the ``completed`` or ``cancelled`` group.
    """
    if target_state is None:
        return None
    if current_state is not None and current_state.id == target_state.id:
        return None
    current = state_key(current_state.name) if current_state is not None else None
    target = state_key(target_state.name)

    if not for_bot:
        if target_state.group == StateGroup.CANCELLED.value:
            return None
        if current not in STATE_TRANSITIONS or target not in STATE_TRANSITIONS:
            return None

    human_ticket = bool(human_ticket and for_bot)
    table = HUMAN_TICKET_STATE_TRANSITIONS if human_ticket else STATE_TRANSITIONS
    if current is None:
        # A work item without a state enters the workflow like a new one.
        allowed = INITIAL_STATES
    else:
        allowed = table.get(current, frozenset())
    if target in allowed and not (human_ticket and target_state.group in CLOSED_STATE_GROUPS):
        return None

    allowed_names = _display(allowed)
    who = "An AI agent bot" if for_bot else "A work item assigned to an AI agent bot"
    none = "none (a person closes it)" if human_ticket and current == AWAITING_HUMAN else "none (final state)"
    targets = ", ".join(allowed_names) if allowed_names else none
    if human_ticket:
        message = (
            f"{who} cannot move an unassigned {HUMAN_ITEM_PREFIX} work item from {_name(current_state)} "
            f"to {target_state.name}"
        )
    elif for_bot:
        message = f"{who} cannot move a work item from {_name(current_state)} to {target_state.name}"
    else:
        message = f"{who} cannot move from {_name(current_state)} to {target_state.name}"
    message += f". Allowed next states: {targets}."
    if not for_bot:
        message += " A person may also cancel it."
    return WorkItemStateTransitionError(
        message,
        current_state=_name(current_state) if current_state is not None else None,
        requested_state=target_state.name,
        allowed_states=allowed_names,
    )


def check_state_change(actor, issue, target_state, requested_assignee_ids=()):
    """Raise ``WorkItemStateTransitionError`` when ``actor`` may not move ``issue`` to ``target_state``.

    Bots follow the table on every work item, and the ``[Human]`` ticket table on an
    unassigned ``[Human]`` work item. Other actors follow the table (with the human
    exceptions) only on work items that have an AI_AGENT bot among their current or
    requested assignees. ``actor`` ``None`` (a background task) is never checked.
    """
    if actor is None or target_state is None or issue is None:
        return
    if issue.state_id is not None and issue.state_id == target_state.id:
        return
    for_bot = is_ai_agent_user(actor)
    if not for_bot and not has_ai_bot_assignee(issue.pk, requested_assignee_ids):
        return
    current_state = State.all_state_objects.filter(pk=issue.state_id).first() if issue.state_id else None
    human_ticket = for_bot and is_unassigned_human_ticket(issue, requested_assignee_ids)
    error = transition_error(current_state, target_state, for_bot=for_bot, human_ticket=human_ticket)
    if error is not None:
        raise error


def default_state(project_id):
    """The state a new work item gets when none is given (as ``Issue._ensure_default_state``)."""
    states = State.objects.filter(project_id=project_id, is_triage=False)
    return states.filter(default=True).first() or states.first()


def check_initial_state(actor, state, project_id=None):
    """Raise ``WorkItemStateTransitionError`` when a bot creates a work item outside Backlog, Todo, Awaiting Human.

    ``state`` ``None`` means the project's default state (``project_id`` is then needed).
    People are never checked here.
    """
    if actor is None or not is_ai_agent_user(actor):
        return
    if state is None and project_id is not None:
        state = default_state(project_id)
    if state is None:
        return
    if state_key(state.name) in INITIAL_STATES:
        return
    allowed_names = _display(INITIAL_STATES)
    raise WorkItemStateTransitionError(
        f"An AI agent bot cannot create a work item in {state.name}. Allowed initial states: "
        f"{', '.join(allowed_names)}.",
        current_state=None,
        requested_state=state.name,
        allowed_states=allowed_names,
    )
