# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Human requests: a bot asks a question or an approval on a work item, a human answers in text.

Shared by the public API (``/api/v1``, where bots ask) and the web API (where
humans answer), so both follow the same rules:

- Opening a request remembers the work item state and moves the item to the
  project's Awaiting Human state. A work item has at most one open request.
- Answering stores the text, sets the decision, moves the item back to the
  remembered state and adds a comment ``Answer: <text>``.
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
    id as ``human_request``), 400 when the project has no Awaiting Human state.
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
        human_request = HumanRequest.objects.create(
            issue=issue,
            project_id=issue.project_id,
            kind=kind,
            question=question,
            requested_by=requested_by,
            # An item that already sits in Awaiting Human has no state worth going back to.
            state_before_id=issue.state_id if issue.state_id != awaiting.id else None,
        )
        _move_issue(issue, awaiting, requested_by.id)
    return human_request


def _resume_state(human_request):
    """The state to go back to: the remembered one, else the first other ``started`` state, else the default."""
    if human_request.state_before_id and State.objects.filter(pk=human_request.state_before_id).exists():
        return human_request.state_before
    states = State.objects.filter(project_id=human_request.project_id)
    return (
        states.filter(group=StateGroup.STARTED.value)
        .exclude(name__iexact=AWAITING_HUMAN_STATE_NAME)
        .order_by("sequence")
        .first()
        or states.filter(default=True).first()
    )


def answer_human_request(human_request_id, text, resolved_by):
    """Store a human's answer, move the work item back and add the answer as a comment.

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
        # Leave the state alone when somebody moved the item elsewhere (Done, Cancelled) in the meantime.
        if awaiting is not None and issue.state_id == awaiting.id:
            _move_issue(issue, _resume_state(human_request), resolved_by.id)

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
