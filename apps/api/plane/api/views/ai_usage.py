# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Django imports
from django.db import transaction

# Third party imports
from drf_spectacular.utils import OpenApiRequest, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.api.serializers import WorkItemAIUsageSerializer
from plane.app.permissions import ProjectEntityOrAIBotAIUsagePermission
from plane.db.models import Issue, WorkItemAIUsage
from plane.utils.openapi import (
    CURSOR_PARAMETER,
    INVALID_REQUEST_RESPONSE,
    ISSUE_ID_PARAMETER,
    ISSUE_NOT_FOUND_RESPONSE,
    PER_PAGE_PARAMETER,
    PROJECT_ID_PARAMETER,
    WORKSPACE_SLUG_PARAMETER,
)

from .base import BaseAPIView
from .issue import project_visibility_q


class WorkItemAIUsageListCreateAPIEndpoint(BaseAPIView):
    """Work Item AI Usage List and Create Endpoint"""

    serializer_class = WorkItemAIUsageSerializer
    model = WorkItemAIUsage
    permission_classes = [ProjectEntityOrAIBotAIUsagePermission]
    use_read_replica = True

    def get_queryset(self):
        return (
            WorkItemAIUsage.objects.filter(workspace__slug=self.kwargs.get("slug"))
            .filter(project_id=self.kwargs.get("project_id"))
            .filter(issue_id=self.kwargs.get("issue_id"))
            .filter(project_visibility_q(self.request.user, self.kwargs.get("slug")))
            .filter(project__archived_at__isnull=True)
            .order_by("created_at")
            .distinct()
        )

    @extend_schema(
        operation_id="list_work_item_ai_usage",
        summary="List work item AI usage",
        description="Retrieve the token usage reported per AI model for a work item.",
        tags=["Work Item AI Usage"],
        parameters=[
            WORKSPACE_SLUG_PARAMETER,
            PROJECT_ID_PARAMETER,
            ISSUE_ID_PARAMETER,
            CURSOR_PARAMETER,
            PER_PAGE_PARAMETER,
        ],
        responses={200: WorkItemAIUsageSerializer(many=True)},
    )
    def get(self, request, slug, project_id, issue_id):
        """List the AI usage records of a work item."""
        return self.paginate(
            request=request,
            queryset=self.get_queryset(),
            on_results=lambda usages: WorkItemAIUsageSerializer(usages, many=True, fields=self.fields).data,
        )

    @extend_schema(
        operation_id="create_work_item_ai_usage",
        summary="Report work item AI usage",
        description=(
            "Report token usage for one AI model, or a list of them, on a work item. "
            "When session_id is set, a record with the same work item, session_id and model is updated instead of duplicated."  # noqa: E501
        ),
        tags=["Work Item AI Usage"],
        parameters=[WORKSPACE_SLUG_PARAMETER, PROJECT_ID_PARAMETER, ISSUE_ID_PARAMETER],
        request=OpenApiRequest(request=WorkItemAIUsageSerializer),
        responses={
            201: OpenApiResponse(description="AI usage stored", response=WorkItemAIUsageSerializer),
            400: INVALID_REQUEST_RESPONSE,
            404: ISSUE_NOT_FOUND_RESPONSE,
        },
    )
    def post(self, request, slug, project_id, issue_id):
        """Create or update AI usage records of a work item."""
        issue = Issue.objects.filter(pk=issue_id, project_id=project_id, workspace__slug=slug).first()
        if issue is None:
            return Response({"error": "Work item not found"}, status=status.HTTP_404_NOT_FOUND)

        many = isinstance(request.data, list)
        payload = request.data if many else [request.data]
        if not payload:
            return Response({"error": "At least one usage record is required"}, status=status.HTTP_400_BAD_REQUEST)

        serializers = [WorkItemAIUsageSerializer(data=item) for item in payload]
        errors = [serializer.errors if not serializer.is_valid() else {} for serializer in serializers]
        if any(errors):
            return Response(errors if many else errors[0], status=status.HTTP_400_BAD_REQUEST)

        usages = []
        with transaction.atomic():
            for serializer in serializers:
                data = dict(serializer.validated_data)
                session_id = data.get("session_id", "")
                instance = None
                if session_id:
                    instance = (
                        WorkItemAIUsage.objects.select_for_update()
                        .filter(issue=issue, session_id=session_id, model=data["model"])
                        .first()
                    )
                if instance is None:
                    instance = WorkItemAIUsage(issue=issue, project_id=project_id)
                for field, value in data.items():
                    setattr(instance, field, value)
                instance.save()
                usages.append(instance)

        response = WorkItemAIUsageSerializer(usages, many=True).data
        return Response(response if many else response[0], status=status.HTTP_201_CREATED)
