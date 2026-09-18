# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Python imports
import uuid
from datetime import date

# Django imports
from django.db import transaction

# Third party imports
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiRequest, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.api.serializers import AIUsageHistorySerializer, WorkItemAIUsageSerializer
from plane.app.permissions import ProjectEntityOrAIBotAIUsagePermission, WorkspaceEntityPermission
from plane.db.models import AIUsageRecord, Issue, StateGroup
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
    """Work Item AI Usage List and Create Endpoint, backed by ``AIUsageRecord``."""

    serializer_class = WorkItemAIUsageSerializer
    model = AIUsageRecord
    permission_classes = [ProjectEntityOrAIBotAIUsagePermission]
    use_read_replica = True

    def get_queryset(self):
        return (
            AIUsageRecord.objects.filter(workspace__slug=self.kwargs.get("slug"))
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
                        AIUsageRecord.objects.select_for_update()
                        .filter(issue=issue, session_id=session_id, model=data["model"])
                        .first()
                    )
                if instance is None:
                    instance = AIUsageRecord(issue=issue, project_id=project_id)
                for field, value in data.items():
                    setattr(instance, field, value)
                instance.save()
                usages.append(instance)

        response = WorkItemAIUsageSerializer(usages, many=True).data
        return Response(response if many else response[0], status=status.HTTP_201_CREATED)


def _parse_date(value, name):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a date (YYYY-MM-DD)")


class AIUsageHistoryAPIEndpoint(BaseAPIView):
    """AI usage records of completed work items across the workspace, newest first."""

    serializer_class = AIUsageHistorySerializer
    model = AIUsageRecord
    permission_classes = [WorkspaceEntityPermission]
    use_read_replica = True

    @extend_schema(
        operation_id="list_ai_usage_history",
        summary="List AI usage history",
        description=(
            "List the AI usage records of completed work items: title, model, effort, duration, "
            "tokens by kind and api_cost_usd per session. Filter by project_ids (comma separated) "
            "and by created_at date with date_from and date_to (YYYY-MM-DD, inclusive)."
        ),
        tags=["Work Item AI Usage"],
        parameters=[
            WORKSPACE_SLUG_PARAMETER,
            OpenApiParameter(name="project_ids", type=OpenApiTypes.STR, location=OpenApiParameter.QUERY),
            OpenApiParameter(name="date_from", type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY),
            OpenApiParameter(name="date_to", type=OpenApiTypes.DATE, location=OpenApiParameter.QUERY),
            CURSOR_PARAMETER,
            PER_PAGE_PARAMETER,
        ],
        responses={200: AIUsageHistorySerializer(many=True), 400: INVALID_REQUEST_RESPONSE},
    )
    def get(self, request, slug):
        """List the AI usage history of completed work items."""
        try:
            project_ids = [uuid.UUID(pid) for pid in request.GET.get("project_ids", "").split(",") if pid.strip()]
            date_from = _parse_date(request.GET.get("date_from"), "date_from")
            date_to = _parse_date(request.GET.get("date_to"), "date_to")
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        queryset = (
            AIUsageRecord.objects.filter(workspace__slug=slug)
            .filter(project_visibility_q(request.user, slug))
            .filter(
                project__archived_at__isnull=True,
                issue__deleted_at__isnull=True,
                issue__state__group=StateGroup.COMPLETED.value,
            )
            .select_related("project", "issue")
        )
        if project_ids:
            queryset = queryset.filter(project_id__in=project_ids)
        if date_from:
            queryset = queryset.filter(created_at__date__gte=date_from)
        if date_to:
            queryset = queryset.filter(created_at__date__lte=date_to)

        return self.paginate(
            request=request,
            queryset=queryset.distinct(),
            order_by="-created_at",
            on_results=lambda records: AIUsageHistorySerializer(records, many=True, fields=self.fields).data,
        )
