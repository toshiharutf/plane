# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

# Third party imports
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiRequest, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.api.serializers.human_request import HumanRequestSerializer
from plane.app.permissions import ProjectEntityPermission
from plane.app.permissions.ai_bot import ProjectEntityOrAIBotHumanRequestPermission
from plane.db.models import HumanRequest, Issue
from plane.utils.human_request import HumanRequestError, answer_human_request, answer_text, open_human_request
from plane.utils.openapi import (
    CONFLICT_RESPONSE,
    CURSOR_PARAMETER,
    FORBIDDEN_RESPONSE,
    INVALID_REQUEST_RESPONSE,
    ISSUE_ID_PARAMETER,
    ISSUE_NOT_FOUND_RESPONSE,
    NOT_FOUND_RESPONSE,
    PER_PAGE_PARAMETER,
    PROJECT_ID_PARAMETER,
    WORKSPACE_SLUG_PARAMETER,
)

from .base import BaseAPIView
from .issue import project_visibility_q

HUMAN_REQUEST_ID_PARAMETER = OpenApiParameter(
    name="pk",
    description="Human request ID",
    required=True,
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.PATH,
)
HUMAN_REQUEST_STATUS_PARAMETER = OpenApiParameter(
    name="status",
    description="open (default): unanswered requests; answered; all",
    required=False,
    type=OpenApiTypes.STR,
    location=OpenApiParameter.QUERY,
)
HUMAN_REQUEST_STATUSES = ("open", "answered", "all")


class _WorkItemHumanRequestMixin:
    def get_queryset(self):
        return (
            HumanRequest.objects.filter(workspace__slug=self.kwargs.get("slug"))
            .filter(project_id=self.kwargs.get("project_id"))
            .filter(issue_id=self.kwargs.get("issue_id"))
            .filter(project_visibility_q(self.request.user, self.kwargs.get("slug")))
            .filter(project__archived_at__isnull=True)
            .order_by("-requested_at", "-created_at")
            .distinct()
        )


class WorkItemHumanRequestListCreateAPIEndpoint(_WorkItemHumanRequestMixin, BaseAPIView):
    """Work Item Human Request List and Create Endpoint"""

    serializer_class = HumanRequestSerializer
    model = HumanRequest
    permission_classes = [ProjectEntityOrAIBotHumanRequestPermission]

    @extend_schema(
        operation_id="list_work_item_human_requests",
        summary="List work item human requests",
        description="Retrieve the human requests of a work item, newest first. Only open ones unless status is given.",
        tags=["Work Item Human Requests"],
        parameters=[
            WORKSPACE_SLUG_PARAMETER,
            PROJECT_ID_PARAMETER,
            ISSUE_ID_PARAMETER,
            HUMAN_REQUEST_STATUS_PARAMETER,
            CURSOR_PARAMETER,
            PER_PAGE_PARAMETER,
        ],
        responses={200: HumanRequestSerializer(many=True), 400: INVALID_REQUEST_RESPONSE},
    )
    def get(self, request, slug, project_id, issue_id):
        """List the human requests of a work item."""
        request_status = request.GET.get("status", "open")
        if request_status not in HUMAN_REQUEST_STATUSES:
            return Response(
                {"error": f"status must be one of {', '.join(HUMAN_REQUEST_STATUSES)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = self.get_queryset()
        if request_status == "open":
            queryset = queryset.filter(decision="")
        elif request_status == "answered":
            queryset = queryset.exclude(decision="")
        return self.paginate(
            request=request,
            queryset=queryset,
            on_results=lambda rows: HumanRequestSerializer(rows, many=True, fields=self.fields).data,
        )

    @extend_schema(
        operation_id="create_work_item_human_request",
        summary="Ask a human",
        description=(
            "Open a human request (kind approval or question) on a work item. The work item state is "
            "remembered and the item moves to Awaiting Human until a human member answers."
        ),
        tags=["Work Item Human Requests"],
        parameters=[WORKSPACE_SLUG_PARAMETER, PROJECT_ID_PARAMETER, ISSUE_ID_PARAMETER],
        request=OpenApiRequest(request=HumanRequestSerializer),
        responses={
            201: OpenApiResponse(description="Human request opened", response=HumanRequestSerializer),
            400: INVALID_REQUEST_RESPONSE,
            403: FORBIDDEN_RESPONSE,
            404: ISSUE_NOT_FOUND_RESPONSE,
            409: CONFLICT_RESPONSE,
        },
    )
    def post(self, request, slug, project_id, issue_id):
        """Open a human request on a work item."""
        issue = Issue.issue_objects.filter(pk=issue_id, project_id=project_id, workspace__slug=slug).first()
        if issue is None:
            return Response({"error": "Work item not found"}, status=status.HTTP_404_NOT_FOUND)

        serializer = HumanRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            human_request = open_human_request(
                issue, serializer.validated_data["kind"], serializer.validated_data["question"], request.user
            )
        except HumanRequestError as exc:
            return Response(exc.payload, status=exc.status_code)
        return Response(HumanRequestSerializer(human_request).data, status=status.HTTP_201_CREATED)


class WorkItemHumanRequestDetailAPIEndpoint(_WorkItemHumanRequestMixin, BaseAPIView):
    """Work Item Human Request Detail Endpoint"""

    serializer_class = HumanRequestSerializer
    model = HumanRequest
    permission_classes = [ProjectEntityOrAIBotHumanRequestPermission]

    @extend_schema(
        operation_id="retrieve_work_item_human_request",
        summary="Retrieve a work item human request",
        description="Retrieve one human request, open or answered, with the answer and decision.",
        tags=["Work Item Human Requests"],
        parameters=[WORKSPACE_SLUG_PARAMETER, PROJECT_ID_PARAMETER, ISSUE_ID_PARAMETER, HUMAN_REQUEST_ID_PARAMETER],
        responses={200: HumanRequestSerializer, 404: NOT_FOUND_RESPONSE},
    )
    def get(self, request, slug, project_id, issue_id, pk):
        """Retrieve a human request of a work item."""
        human_request = self.get_queryset().filter(pk=pk).first()
        if human_request is None:
            return Response({"error": "Human request not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(HumanRequestSerializer(human_request, fields=self.fields).data, status=status.HTTP_200_OK)


class WorkItemHumanRequestAnswerAPIEndpoint(_WorkItemHumanRequestMixin, BaseAPIView):
    """Work Item Human Request Answer Endpoint: human project members only, never bots."""

    serializer_class = HumanRequestSerializer
    model = HumanRequest
    # ``ProjectEntityPermission`` refuses every bot (403), whatever its role.
    permission_classes = [ProjectEntityPermission]

    @extend_schema(
        operation_id="answer_work_item_human_request",
        summary="Answer a human request",
        description=(
            "Answer an open human request with a text. An approval answer must start with yes or accept "
            "(decision accept) or no or deny (decision deny); the rest is the note. The work item goes back "
            "to its state before the request and gets a comment with the answer. Bots are refused."
        ),
        tags=["Work Item Human Requests"],
        parameters=[WORKSPACE_SLUG_PARAMETER, PROJECT_ID_PARAMETER, ISSUE_ID_PARAMETER, HUMAN_REQUEST_ID_PARAMETER],
        request=OpenApiRequest(
            request={"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
        ),
        responses={
            200: OpenApiResponse(description="Human request answered", response=HumanRequestSerializer),
            400: INVALID_REQUEST_RESPONSE,
            403: FORBIDDEN_RESPONSE,
            404: NOT_FOUND_RESPONSE,
            409: CONFLICT_RESPONSE,
        },
    )
    def post(self, request, slug, project_id, issue_id, pk):
        """Answer a human request of a work item."""
        human_request = self.get_queryset().filter(pk=pk).first()
        if human_request is None:
            return Response({"error": "Human request not found"}, status=status.HTTP_404_NOT_FOUND)
        try:
            human_request = answer_human_request(human_request.id, answer_text(request.data), request.user)
        except HumanRequestError as exc:
            return Response(exc.payload, status=exc.status_code)
        return Response(HumanRequestSerializer(human_request).data, status=status.HTTP_200_OK)
