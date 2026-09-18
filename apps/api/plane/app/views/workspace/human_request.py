# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import uuid

# Django imports
from django.db.models import F

# Third party modules
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.permissions import ROLE, allow_permission
from plane.app.views.base import BaseAPIView
from plane.db.models import HumanRequest, ProjectMember
from plane.utils.human_request import HumanRequestError, answer_human_request, answer_note


def _rows(queryset):
    """Human requests as plain rows with the work item and project identifiers the web app shows."""
    rows = list(
        queryset.order_by("-requested_at", "-created_at").values(
            "id",
            "kind",
            "question",
            "requested_by_id",
            "requested_at",
            "answer",
            "decision",
            "resolved_by_id",
            "resolved_at",
            "issue_id",
            "project_id",
            "state_before_id",
            issue_name=F("issue__name"),
            issue_sequence_id=F("issue__sequence_id"),
            project_identifier=F("project__identifier"),
            requested_by_display_name=F("requested_by__display_name"),
        )
    )
    for row in rows:
        row["is_open"] = not row["decision"]
        row["note"] = answer_note(row["kind"], row["answer"])
    return rows


def _visible_requests(slug, user):
    """Human requests on live work items of the projects ``user`` is an active member of."""
    return HumanRequest.objects.filter(
        workspace__slug=slug,
        project_id__in=ProjectMember.objects.filter(workspace__slug=slug, member=user, is_active=True).values(
            "project_id"
        ),
        project__archived_at__isnull=True,
        issue__deleted_at__isnull=True,
        issue__archived_at__isnull=True,
    )


class WorkspaceHumanRequestEndpoint(BaseAPIView):
    """Open human requests in the user's projects, newest first (AI Status, work item banner).

    ``issue_id`` limits the list to one work item.
    """

    @allow_permission(allowed_roles=[ROLE.ADMIN, ROLE.MEMBER], level="WORKSPACE")
    def get(self, request, slug):
        queryset = _visible_requests(slug, request.user).filter(decision="")
        issue_id = request.GET.get("issue_id")
        if issue_id:
            try:
                queryset = queryset.filter(issue_id=uuid.UUID(issue_id))
            except ValueError:
                return Response({"error": "issue_id must be a UUID"}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_rows(queryset), status=status.HTTP_200_OK)


class WorkspaceHumanRequestAnswerEndpoint(BaseAPIView):
    """Answer a human request with a text; only human admins and members of its project, never bots."""

    @allow_permission(allowed_roles=[ROLE.ADMIN, ROLE.MEMBER], level="WORKSPACE")
    def post(self, request, slug, pk):
        human_request = _visible_requests(slug, request.user).filter(pk=pk).first()
        if human_request is None:
            return Response({"error": "Human request not found"}, status=status.HTTP_404_NOT_FOUND)
        if (
            request.user.is_bot
            or not ProjectMember.objects.filter(
                workspace__slug=slug,
                project_id=human_request.project_id,
                member=request.user,
                is_active=True,
                role__in=[ROLE.ADMIN.value, ROLE.MEMBER.value],
            ).exists()
        ):
            return Response(
                {"error": "Only a human member of the project can answer a human request"},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            answer_human_request(human_request.id, request.data.get("answer"), request.user)
        except HumanRequestError as exc:
            return Response(exc.payload, status=exc.status_code)
        return Response(_rows(HumanRequest.objects.filter(pk=human_request.id))[0], status=status.HTTP_200_OK)
