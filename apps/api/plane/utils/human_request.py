# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Human requests: a bot asks a question or an approval on a work item, a human answers in text.

Shared by the public API (``/api/v1``, where bots ask) and the web API (where
humans answer), so both follow the same rules:

- Opening a request remembers the work item state (``state_before``) and moves
  the item to the project's Awaiting Human state. That move follows the work
  item state machine (``plane.utils.work_item_state_rules``): only from
  Backlog, In Progress or In Review (or when the item already awaits a human);
  from any other state the request is refused with 400. A work item has at
  most one open request.
- Answering stores the text, sets the decision, moves the item out of Awaiting
  Human and adds a comment ``Answer: <text>``. The item goes to the project's
  Todo state (so the orchestrator picks it up again, with its history and the
  answer, when a worker slot is free), except that a request asked in In Review
  (a blocked merge) moves it back to In Review, where the review continues.
- An approval answer must start with the word yes/accept or no/deny
  (case-insensitive); the rest of the text is the note.
"""

import json
import re
from functools import partial

# Django imports
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape

# Module imports
from plane.bgtasks.issue_activities_task import issue_activity
from plane.db.models import (
    AWAITING_HUMAN_STATE_NAME,
    HumanRequest,
    HumanRequestDecision,
    HumanRequestKind,
    Issue,
    IssueComment,
    State,
    StateGroup,
)
from plane.utils.work_item_state_rules import (
    AWAITING_HUMAN,
    IN_REVIEW,
    STATE_DISPLAY_NAMES,
    STATE_TRANSITIONS,
    TODO,
    state_key,
    transition_error,
)

# The states a work item may be in when a human request is opened (those that may move to Awaiting Human).
ASK_FROM_STATES = [STATE_DISPLAY_NAMES[key] for key in STATE_DISPLAY_NAMES if AWAITING_HUMAN in STATE_TRANSITIONS[key]]
# The same list as it reads in a sentence: "Backlog, In Progress or In Review".
ASK_FROM_STATES_TEXT = f"{', '.join(ASK_FROM_STATES[:-1])} or {ASK_FROM_STATES[-1]}"

# The first word decides an approval: "yes", "Accept!", "no - wait" match; "yesterday", "nope", "maybe" do not.
APPROVAL_ANSWER_PATTERN = re.compile(r"^\s*(yes|accept|no|deny)(?![A-Za-z0-9_])", re.IGNORECASE)
APPROVAL_DECISIONS = {
    "yes": HumanRequestDecision.ACCEPT.value,
    "accept": HumanRequestDecision.ACCEPT.value,
    "no": HumanRequestDecision.DENY.value,
    "deny": HumanRequestDecision.DENY.value,
}
# Separators between the decision word and the note, as in "no - wait for Monday" or "yes, go".
NOTE_SEPARATORS = " \t\r\n-–—:,.;!"

APPROVAL_ANSWER_ERROR = "An approval answer must start with yes or accept, or with no or deny"


class HumanRequestError(Exception):
    """A request that cannot be opened or answered; ``status_code`` is the HTTP status to answer with."""

    def __init__(self, message, status_code, **extra):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.extra = extra

    @property
    def payload(self):
        return {"error": self.message, **self.extra}


def parse_answer(kind, text):
    """Return ``(decision, note)`` for an answer text, or raise ``HumanRequestError`` (400).

    A question takes any non-empty text (decision ``answered``, no note). An approval
    needs a leading yes/accept or no/deny; the note is the text after that word.
    """
    text = (text or "").strip() if isinstance(text, str) else ""
    if not text:
        raise HumanRequestError("The answer text is required", 400)
    if kind != HumanRequestKind.APPROVAL.value:
        return HumanRequestDecision.ANSWERED.value, ""
    match = APPROVAL_ANSWER_PATTERN.match(text)
    if match is None:
        raise HumanRequestError(APPROVAL_ANSWER_ERROR, 400)
    return APPROVAL_DECISIONS[match.group(1).lower()], text[match.end() :].strip(NOTE_SEPARATORS)


def answer_text(data):
    """The ``answer`` of a request body; None when the body is not an object (a JSON list, for example)."""
    return data.get("answer") if isinstance(data, dict) else None


def answer_note(kind, answer):
    """The note of an answered approval (the answer without its leading yes/no word), else ``""``."""
    if kind != HumanRequestKind.APPROVAL.value or not answer:
        return ""
    match = APPROVAL_ANSWER_PATTERN.match(answer)
    return answer[match.end() :].strip(NOTE_SEPARATORS) if match else ""


def awaiting_human_state(project_id):
    return State.objects.filter(project_id=project_id, name__iexact=AWAITING_HUMAN_STATE_NAME).first()


def _dispatch_activity(**kwargs):
    """Queue the activity task once the transaction is committed, so the worker reads the final rows."""
    transaction.on_commit(partial(issue_activity.delay, **kwargs), robust=True)


def _move_issue(issue, state, actor_id):
    """Set the work item state and record it in the activity, as a state ``PATCH`` does."""
    if state is None or issue.state_id == state.id:
        return
    current_instance = json.dumps({"state_id": str(issue.state_id) if issue.state_id else None})
    issue.state = state
    issue.save()
    _dispatch_activity(
        type="issue.activity.updated",
        requested_data=json.dumps({"state_id": str(state.id)}),
        actor_id=str(actor_id),
        issue_id=str(issue.id),
        project_id=str(issue.project_id),
        current_instance=current_instance,
        epoch=int(timezone.now().timestamp()),
        notification=True,
    )


def open_human_request(issue, kind, question, requested_by):
    """Create an open request on ``issue`` and move the item to Awaiting Human.

    Raises ``HumanRequestError``: 409 when the item already has an open request (with its
    id as ``human_request``), 400 when the project has no Awaiting Human state or the item is
    in a state that may not move to Awaiting Human (anything but Backlog, In Progress and In Review).
    """
    with transaction.atomic():
        # The row lock serializes two bots (or a retry) asking on the same work item.
        issue = Issue.objects.select_for_update(of=("self",)).get(pk=issue.pk)
        existing = HumanRequest.objects.filter(issue=issue, decision="").first()
        if existing is not None:
            raise HumanRequestError(
                "The work item already has an open human request", 409, human_request=str(existing.id)
            )
        awaiting = awaiting_human_state(issue.project_id)
        if awaiting is None:
            raise HumanRequestError(f"The project has no {AWAITING_HUMAN_STATE_NAME} state", 400)
        # The move to Awaiting Human follows the state machine for everybody: from the ASK_FROM_STATES only.
        current = State.all_state_objects.filter(pk=issue.state_id).first() if issue.state_id else None
        if transition_error(current, awaiting, for_bot=True) is not None:
            raise HumanRequestError(
                f"A human request can only be opened on a work item in {ASK_FROM_STATES_TEXT}, "
                f"not in {current.name if current is not None else 'no state'}",
                400,
                current_state=current.name if current is not None else None,
                requested_state=awaiting.name,
                allowed_from_states=ASK_FROM_STATES,
            )
        human_request = HumanRequest.objects.create(
            issue=issue,
            project_id=issue.project_id,
            kind=kind,
            question=question,
            requested_by=requested_by,
            # Where the item was asked (the answer resumes In Review from it); None when it already awaited a human.
            state_before_id=issue.state_id if issue.state_id != awaiting.id else None,
        )
        _move_issue(issue, awaiting, requested_by.id)
    return human_request


def todo_state(project_id):
    """The project's Todo state: the state named Todo, else the default (then first) ``unstarted`` state."""
    states = State.objects.filter(project_id=project_id)
    named = [state for state in states.filter(name__iexact="todo") if state_key(state.name) == TODO]
    if named:
        return named[0]
    unstarted = states.filter(group=StateGroup.UNSTARTED.value)
    return unstarted.filter(default=True).first() or unstarted.order_by("sequence").first()


def resume_state(human_request, project_id):
    """The state the answer moves the work item to: In Review when it was asked there, else Todo.

    ``state_before`` counts only while it is a live state of the project named In Review; a state
    that was deleted or renamed since the request was opened falls back to Todo.
    """
    before = State.objects.filter(pk=human_request.state_before_id, project_id=project_id).first()
    if before is not None and state_key(before.name) == IN_REVIEW:
        return before
    return todo_state(project_id)


def answer_human_request(human_request_id, text, resolved_by):
    """Store a human's answer, move the work item to Todo (In Review when asked there) and comment the answer.

    Raises ``HumanRequestError``: 403 for a bot, 409 when the request is already
    answered, 400 for an empty text or an approval without a leading yes/no.
    """
    # Bots ask, humans answer: a bot must never resolve a request, whatever let it reach this call.
    if getattr(resolved_by, "is_bot", False):
        raise HumanRequestError("Only a human member can answer a human request", 403)

    with transaction.atomic():
        # The row lock makes the second of two concurrent answers see the first one (409).
        human_request = HumanRequest.objects.select_for_update(of=("self",)).get(pk=human_request_id)
        if not human_request.is_open:
            raise HumanRequestError("The human request is already answered", 409)
        decision, _note = parse_answer(human_request.kind, text)

        human_request.answer = text.strip()
        human_request.decision = decision
        human_request.resolved_by = resolved_by
        human_request.resolved_at = timezone.now()
        human_request.save()

        issue = Issue.objects.select_for_update(of=("self",)).get(pk=human_request.issue_id)
        awaiting = awaiting_human_state(issue.project_id)
        # Awaiting Human -> Todo: the orchestrator picks the item up again with the answer (a request asked
        # in In Review goes back there). Leave the state alone when somebody moved the item elsewhere (Todo,
        # Cancelled) in the meantime.
        if awaiting is not None and issue.state_id == awaiting.id:
            _move_issue(issue, resume_state(human_request, issue.project_id), resolved_by.id)

        answer_html = "<br>".join(escape(line) for line in human_request.answer.splitlines())
        comment = IssueComment.objects.create(
            issue=issue,
            project_id=issue.project_id,
            workspace_id=issue.workspace_id,
            comment_html=f"<p>Answer: {answer_html}</p>",
            actor=resolved_by,
            created_by=resolved_by,
            updated_by=resolved_by,
        )
        _dispatch_activity(
            type="comment.activity.created",
            requested_data=json.dumps(
                {"id": str(comment.id), "comment_html": comment.comment_html}, cls=DjangoJSONEncoder
            ),
            actor_id=str(resolved_by.id),
            issue_id=str(issue.id),
            project_id=str(issue.project_id),
            current_instance=None,
            epoch=int(timezone.now().timestamp()),
        )
    return human_request
