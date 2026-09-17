# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.db.models import Sum
from django.http import HttpRequest

# Third party imports
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.permissions import ROLE, allow_permission
from plane.app.views.base import BaseAPIView
from plane.db.models import Issue, StateGroup, WorkItemAIUsage
from plane.utils.date_utils import get_analytics_filters

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class WorkspaceAIUsageAnalyticsEndpoint(BaseAPIView):
    """Token usage per AI model and completed work item.

    Usage rows are summed per (model, work item). Only completed, non-archived,
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
            WorkItemAIUsage.objects.filter(issue_id__in=completed_issue_ids)
            .order_by()
            .values("model", "issue_id")
            .annotate(**{field: Sum(field) for field in TOKEN_FIELDS})
        )

        issues = {
            issue["id"]: issue
            for issue in Issue.issue_objects.filter(id__in={row["issue_id"] for row in rows}).values(
                "id", "project_id", "sequence_id", "name", "completed_at", "project__identifier"
            )
        }

        models = {}
        for row in rows:
            issue = issues.get(row["issue_id"])
            if issue is None:
                continue
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
