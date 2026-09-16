# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.db import models
from django.db.models import Q

# Module imports
from .project import ProjectBaseModel


class WorkItemAIUsage(ProjectBaseModel):
    """Token usage of one AI model while working on a work item.

    One row per reporting unit (typically one agent session per model); totals
    per work item are computed by summing rows. ``cache_creation_input_tokens``
    is the input cache miss (cache write) and ``cache_read_input_tokens`` the
    input cache hit.
    """

    issue = models.ForeignKey("db.Issue", on_delete=models.CASCADE, related_name="ai_usages")
    model = models.CharField(max_length=255)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    cache_creation_input_tokens = models.PositiveBigIntegerField(default=0)
    cache_read_input_tokens = models.PositiveBigIntegerField(default=0)
    session_id = models.CharField(max_length=255, blank=True, default="")
    external_source = models.CharField(max_length=255, null=True, blank=True)
    external_id = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        verbose_name = "Work Item AI Usage"
        verbose_name_plural = "Work Item AI Usages"
        db_table = "work_item_ai_usages"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["issue", "session_id", "model"],
                condition=Q(deleted_at__isnull=True) & ~Q(session_id=""),
                name="work_item_ai_usage_unique_issue_session_model_when_deleted_at_null",
            )
        ]

    def __str__(self):
        return f"{self.issue_id} {self.model}"
