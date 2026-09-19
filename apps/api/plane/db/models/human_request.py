# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.conf import settings
from django.db import models
from django.utils import timezone

# Module imports
from .project import ProjectBaseModel


class HumanRequestKind(models.TextChoices):
    APPROVAL = "approval", "Approval"
    QUESTION = "question", "Question"


class HumanRequestDecision(models.TextChoices):
    ACCEPT = "accept", "Accept"
    DENY = "deny", "Deny"
    ANSWERED = "answered", "Answered"


class HumanRequest(ProjectBaseModel):
    """A question or approval a bot asks a human about a work item, and the answer.

    A row with an empty ``decision`` is open. ``state_before`` is the work item
    state before it moved to Awaiting Human: once the request is resolved
    (``resolved_by`` and ``resolved_at`` set) the item moves to the project's
    Todo state, not back to ``state_before``, unless that state is In Review
    (a request asked during the review resumes the review).
    """

    issue = models.ForeignKey("db.Issue", on_delete=models.CASCADE, related_name="human_requests")
    kind = models.CharField(max_length=20, choices=HumanRequestKind.choices)
    question = models.TextField()
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="human_requests_asked",
    )
    requested_at = models.DateTimeField(default=timezone.now)
    state_before = models.ForeignKey(
        "db.State",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="human_requests_before",
    )
    answer = models.TextField(blank=True, default="")
    decision = models.CharField(max_length=20, choices=HumanRequestDecision.choices, blank=True, default="")
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="human_requests_resolved",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Human Request"
        verbose_name_plural = "Human Requests"
        db_table = "human_requests"
        ordering = ("-requested_at",)

    @property
    def is_open(self):
        return not self.decision

    def __str__(self):
        return f"{self.issue_id} {self.kind}"
