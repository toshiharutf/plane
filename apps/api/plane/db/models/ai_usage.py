# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.db import models
from django.db.models import Q

# Module imports
from plane.utils.ai_pricing import compute_cost

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


class AIUsageRecord(ProjectBaseModel):
    """One AI session's token, cache and plan-usage metrics with its API price.

    ``api_cost_usd`` and ``price_version`` are computed from
    ``plane.utils.ai_pricing`` on every ``save()``; ``api_cost_usd`` stays null
    for models without a known price. ``issue_title`` keeps the work item name
    at report time so the record stays readable after the work item is gone.
    ``split_unknown`` marks rows whose cache writes could not be split into 5m
    and 1h (copied from ``WorkItemAIUsage``; all writes are counted as 1h).
    """

    issue = models.ForeignKey(
        "db.Issue", on_delete=models.SET_NULL, null=True, blank=True, related_name="ai_usage_records"
    )
    issue_title = models.CharField(max_length=255, blank=True, default="")
    session_id = models.CharField(max_length=255, blank=True, default="")
    model = models.CharField(max_length=255)
    effort = models.CharField(max_length=32, blank=True, default="")
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    input_tokens = models.PositiveBigIntegerField(default=0)
    cache_write_5m_tokens = models.PositiveBigIntegerField(default=0)
    cache_write_1h_tokens = models.PositiveBigIntegerField(default=0)
    cache_read_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    usage_5h_pct = models.FloatField(null=True, blank=True)
    usage_weekly_pct = models.FloatField(null=True, blank=True)
    api_cost_usd = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    price_version = models.CharField(max_length=32, blank=True, default="")
    split_unknown = models.BooleanField(default=False)
    external_source = models.CharField(max_length=255, null=True, blank=True)
    external_id = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        verbose_name = "AI Usage Record"
        verbose_name_plural = "AI Usage Records"
        db_table = "ai_usage_records"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["created_at"], name="ai_usage_record_created_idx"),
            models.Index(fields=["project", "created_at"], name="ai_usage_record_proj_crt_idx"),
        ]

    def apply_pricing(self):
        self.api_cost_usd, self.price_version = compute_cost(
            self.model,
            input_tokens=self.input_tokens,
            cache_write_5m_tokens=self.cache_write_5m_tokens,
            cache_write_1h_tokens=self.cache_write_1h_tokens,
            cache_read_tokens=self.cache_read_tokens,
            output_tokens=self.output_tokens,
        )

    def save(self, *args, **kwargs):
        if self.issue_id and not self.issue_title:
            self.issue_title = (self.issue.name or "")[:255]
        self.apply_pricing()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = {*update_fields, "api_cost_usd", "price_version"}
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.issue_id} {self.model} {self.created_at}"
