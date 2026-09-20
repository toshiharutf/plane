# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
from decimal import Decimal

# Django imports
from django.db.models import Count, F, Q, Sum
from django.http import HttpRequest

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.permissions import ROLE, allow_permission
from plane.app.views.base import BaseAPIView
from plane.db.models import Issue, StateGroup, AIUsageRecord
from plane.utils.date_utils import get_analytics_filters

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class WorkspaceAIUsageAnalyticsEndpoint(BaseAPIView):
    """Token usage per AI model and completed work item.

    ``AIUsageRecord`` rows are summed per (model, work item). Cache writes (5m
    and 1h) are reported as ``cache_creation_input_tokens`` and cache hits as
    ``cache_read_input_tokens``; ``api_cost_usd`` is the summed API price
    (null when no row of the item has a known price). Only completed, non-archived,
    non-draft work items in active projects the requester is a member of are
    included; ``project_ids`` (comma separated) narrows the projects further.
    """

    @allow_permission([ROLE.ADMIN, ROLE.MEMBER], level="WORKSPACE")
    def get(self, request: HttpRequest, slug: str) -> Response:
        filters = get_analytics_filters(
            slug=slug,
            user=request.user,
            type="ai-usage",
            project_ids=request.GET.get("project_ids", None),
        )
        completed_issue_ids = (
            Issue.issue_objects.filter(**filters["base_filters"])
            .filter(state__group=StateGroup.COMPLETED.value)
            .values("id")
        )

        rows = (
            AIUsageRecord.objects.filter(issue_id__in=completed_issue_ids)
            .order_by()
            .values("model", "issue_id")
            .annotate(
                input_tokens=Sum("input_tokens"),
                output_tokens=Sum("output_tokens"),
                cache_creation_input_tokens=Sum(F("cache_write_5m_tokens") + F("cache_write_1h_tokens")),
                cache_read_input_tokens=Sum("cache_read_tokens"),
                cost_sum=Sum("api_cost_usd"),
                unknown_cost_count=Count("id", filter=Q(api_cost_usd__isnull=True)),
                duration_sum=Sum("duration_seconds"),
                unknown_duration_count=Count("id", filter=Q(duration_seconds__isnull=True)),
            )
        )

        issues = {
            issue["id"]: issue
            for issue in Issue.issue_objects.filter(id__in={row["issue_id"] for row in rows}).values(
                "id", "project_id", "sequence_id", "name", "completed_at", "project__identifier"
            )
        }

        models = {}
        categories = {}
        for usage in AIUsageRecord.objects.filter(issue_id__in=completed_issue_ids).values(
            "model", "issue_id", "cost_breakdown"
        ):
            key = (usage["model"], usage["issue_id"])
            bucket = categories.setdefault(key, {"known": {}, "unknown": 0})
            if usage["cost_breakdown"] is None:
                bucket["unknown"] += 1
            else:
                for category, value in usage["cost_breakdown"].items():
                    bucket["known"][category] = bucket["known"].get(category, Decimal(0)) + Decimal(value)
        for row in rows:
            issue = issues.get(row["issue_id"])
            if issue is None:
                continue
            bucket = categories[(row["model"], row["issue_id"])]
            entry = models.setdefault(
                row["model"],
                {"model": row["model"], "work_items": [], "totals": {field: 0 for field in TOKEN_FIELDS}},
            )
            entry["work_items"].append(
                {
                    "id": issue["id"],
                    "project_id": issue["project_id"],
                    "sequence_id": issue["sequence_id"],
                    "project_identifier": issue["project__identifier"],
                    "name": issue["name"],
                    "completed_at": issue["completed_at"],
                    **{field: row[field] or 0 for field in TOKEN_FIELDS},
                    "api_cost_usd": None if row["unknown_cost_count"] else row["cost_sum"],
                    "known_api_cost_usd": row["cost_sum"],
                    "unknown_cost_count": row["unknown_cost_count"],
                    "duration_seconds": None if row["unknown_duration_count"] else row["duration_sum"],
                    "unknown_duration_count": row["unknown_duration_count"],
                    "cost_categories_usd": None if bucket["unknown"] else bucket["known"],
                    "known_cost_categories_usd": bucket["known"],
                    "unknown_category_count": bucket["unknown"],
                }
            )
            for field in TOKEN_FIELDS:
                entry["totals"][field] += row[field] or 0

        for entry in models.values():
            # Items without completed_at (e.g. moved to Done before it was tracked) go last.
            entry["work_items"].sort(
                key=lambda item: (item["completed_at"] is None, item["completed_at"] or 0, item["sequence_id"])
            )

        return Response(
            {"models": [models[name] for name in sorted(models)]},
            status=status.HTTP_200_OK,
        )
