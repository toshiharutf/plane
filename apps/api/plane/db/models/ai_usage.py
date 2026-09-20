# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from decimal import Decimal, ROUND_DOWN

from django.db import models

# Module imports
from plane.utils.ai_pricing import COST_QUANTUM, MILLION, compute_cost, model_prices

from .project import ProjectBaseModel


class AIUsageRecord(ProjectBaseModel):
    """One AI session's token, cache and plan-usage metrics with its API price.

    ``api_cost_usd`` and ``price_version`` are computed from
    ``plane.utils.ai_pricing`` when token inputs change; metadata edits preserve
    the originally recorded price. ``api_cost_usd`` stays null
    for models without a known price. ``issue_title`` keeps the work item name
    at report time so the record stays readable after the work item is gone.
    ``split_unknown`` marks rows whose cache writes could not be split into 5m
    and 1h (legacy ``cache_creation_input_tokens`` reports and rows copied from the
    dropped ``WorkItemAIUsage`` table; all writes are counted as 1h).
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
    cost_breakdown = models.JSONField(null=True, blank=True, editable=False)
    split_unknown = models.BooleanField(default=False)
    external_source = models.CharField(max_length=255, null=True, blank=True)
    external_id = models.CharField(max_length=255, null=True, blank=True)
    # Duplicate legacy deliveries remain in soft-deleted audit history after migration.
    duplicate_of = models.UUIDField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "AI Usage Record"
        verbose_name_plural = "AI Usage Records"
        db_table = "ai_usage_records"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["created_at"], name="ai_usage_record_created_idx"),
            models.Index(fields=["project", "created_at"], name="ai_usage_record_proj_crt_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["issue", "session_id", "model"],
                condition=models.Q(deleted_at__isnull=True, issue__isnull=False) & ~models.Q(session_id=""),
                name="ai_usage_issue_session_model_uniq",
            ),
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
        prices = model_prices(self.model, input_tokens=self.input_tokens, cache_read_tokens=self.cache_read_tokens)
        self.cost_breakdown = None
        if self.api_cost_usd is not None and prices is not None:
            token_counts = {
                "input": self.input_tokens,
                "output": self.output_tokens,
                "cache_write_5m": self.cache_write_5m_tokens,
                "cache_write_1h": self.cache_write_1h_tokens,
                "cache_read": self.cache_read_tokens,
            }
            raw = {key: Decimal(count) * prices[key] / MILLION for key, count in token_counts.items()}
            parts = {key: value.quantize(COST_QUANTUM, rounding=ROUND_DOWN) for key, value in raw.items()}
            # Allocate sub-microdollar rounding to the largest category so both
            # charts and totals use exactly the stored cost without negative bars.
            largest = max(raw, key=raw.get)
            parts[largest] += self.api_cost_usd - sum(parts.values())
            self.cost_breakdown = {key: str(value) for key, value in parts.items()}

    def save(self, *args, **kwargs):
        if self.issue_id and not self.issue_title:
            self.issue_title = (self.issue.name or "")[:255]
        pricing_fields = {
            "model",
            "input_tokens",
            "output_tokens",
            "cache_write_5m_tokens",
            "cache_write_1h_tokens",
            "cache_read_tokens",
        }
        update_fields = kwargs.get("update_fields")
        reprice = self._state.adding
        if not reprice and (update_fields is None or pricing_fields.intersection(update_fields)):
            previous = type(self).objects.filter(pk=self.pk).values(*pricing_fields).first()
            reprice = previous is None or any(previous[key] != getattr(self, key) for key in pricing_fields)
        if reprice:
            self.apply_pricing()
        if update_fields is not None and reprice:
            kwargs["update_fields"] = {*update_fields, "api_cost_usd", "price_version", "cost_breakdown"}
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.issue_id} {self.model} {self.created_at}"
