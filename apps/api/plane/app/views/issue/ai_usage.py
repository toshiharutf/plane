# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.db.models import Sum
from django.db.models.functions import Coalesce

# Third Party imports
from rest_framework.response import Response

# Module imports
from .. import BaseAPIView
from plane.app.permissions import ProjectEntityPermission
from plane.db.models import WorkItemAIUsage

TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


class IssueAIUsageSummaryEndpoint(BaseAPIView):
    """Token usage of a work item summed over all reports, in total and per AI model.

    ``cache_creation_input_tokens`` is the input cache miss and
    ``cache_read_input_tokens`` the input cache hit.
    """

    permission_classes = [ProjectEntityPermission]

    def get(self, request, slug, project_id, issue_id):
        usages = WorkItemAIUsage.objects.filter(
            workspace__slug=slug,
            project_id=project_id,
            issue_id=issue_id,
            issue__deleted_at__isnull=True,
        )
        sums = {field: Coalesce(Sum(field), 0) for field in TOKEN_FIELDS}
        models = list(usages.values("model").annotate(**sums).order_by("model"))
        totals = {field: sum(row[field] for row in models) for field in TOKEN_FIELDS}
        return Response({"totals": totals, "models": models})
