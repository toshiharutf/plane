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

Who is checked:

- A workspace ``AI_AGENT`` bot, on every work item: it may only use the table
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


def transition_error(current_state, target_state, for_bot=True):
    """``None`` when moving from ``current_state`` to ``target_state`` is allowed, else the error.

    ``for_bot`` applies the bot rules (only the table); otherwise the human exceptions apply
    (a ``cancelled`` group target, or a state outside the table on either side, is allowed).
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

    if current is None:
        # A work item without a state enters the workflow like a new one.
        allowed = INITIAL_STATES
    else:
        allowed = STATE_TRANSITIONS.get(current, frozenset())
    if target in allowed:
        return None

    allowed_names = _display(allowed)
    who = "An AI agent bot" if for_bot else "A work item assigned to an AI agent bot"
    targets = ", ".join(allowed_names) if allowed_names else "none (final state)"
    if for_bot:
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

    Bots follow the table on every work item. Other actors follow it (with the human
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
    error = transition_error(current_state, target_state, for_bot=for_bot)
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
