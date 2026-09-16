# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from collections import defaultdict
from datetime import timedelta

# Django imports
from django.db.models import Exists, F, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

# Third party modules
from rest_framework import status
from rest_framework.response import Response

# Module imports
from plane.app.permissions import ROLE, allow_permission
from plane.app.views.base import BaseAPIView
from plane.db.models import (
    BotTypeEnum,
    Issue,
    IssueActivity,
    IssueAssignee,
    ProjectMember,
    State,
    StateGroup,
    WorkspaceMember,
)

DEFAULT_COMPLETED_WINDOW_DAYS = 90


def _started_at_subquery(slug, before_completion):
    """Latest transition of the work item into a ``started`` group state."""
    activities = IssueActivity.objects.filter(
        issue_id=OuterRef("pk"),
        workspace__slug=slug,
        field="state",
        deleted_at__isnull=True,
        new_identifier__in=State.all_state_objects.filter(workspace__slug=slug, group=StateGroup.STARTED.value).values(
            "id"
        ),
    )
    if before_completion:
        activities = activities.filter(created_at__lte=OuterRef("completed_at"))
    return Coalesce(Subquery(activities.order_by("-created_at").values("created_at")[:1]), F("created_at"))


class WorkspaceAIStatusEndpoint(BaseAPIView):
    """In-progress and completed work items assigned to the workspace AI bots."""

    @allow_permission(allowed_roles=[ROLE.ADMIN, ROLE.MEMBER], level="WORKSPACE")
    def get(self, request, slug):
        days = request.GET.get("days", DEFAULT_COMPLETED_WINDOW_DAYS)
        try:
            days = int(days)
            if days < 0:
                raise ValueError
        except (TypeError, ValueError):
            return Response({"error": "days must be a non-negative integer"}, status=status.HTTP_400_BAD_REQUEST)

        bot_members = WorkspaceMember.objects.filter(
            workspace__slug=slug,
            is_active=True,
            member__is_bot=True,
            member__bot_type=BotTypeEnum.AI_AGENT,
        ).select_related("member", "member__avatar_asset")
        bots = [
            {
                "id": str(bot_member.member.id),
                "display_name": bot_member.member.display_name,
                "avatar_url": bot_member.member.avatar_url,
            }
            for bot_member in bot_members
        ]
        bot_ids = [bot["id"] for bot in bots]
        if not bot_ids:
            return Response({"bots": [], "in_progress": [], "completed": []}, status=status.HTTP_200_OK)

        project_ids = ProjectMember.objects.filter(workspace__slug=slug, member=request.user, is_active=True).values(
            "project_id"
        )
        issues = Issue.issue_objects.filter(workspace__slug=slug, project_id__in=project_ids).filter(
            Exists(
                IssueAssignee.objects.filter(issue_id=OuterRef("pk"), assignee_id__in=bot_ids, deleted_at__isnull=True)
            )
        )

        in_progress = list(
            issues.filter(state__group=StateGroup.STARTED.value)
            .annotate(started_at=_started_at_subquery(slug, before_completion=False))
            .order_by("-started_at")
            .values(
                "id",
                "name",
                "sequence_id",
                "project_id",
                "priority",
                "state_id",
                "started_at",
                "updated_at",
                project_identifier=F("project__identifier"),
                state_name=F("state__name"),
                state_color=F("state__color"),
            )
        )

        completed_issues = issues.filter(state__group=StateGroup.COMPLETED.value, completed_at__isnull=False)
        if days:
            completed_issues = completed_issues.filter(completed_at__gte=timezone.now() - timedelta(days=days))
        completed = list(
            completed_issues.annotate(started_at=_started_at_subquery(slug, before_completion=True))
            .order_by("-completed_at")
            .values(
                "id",
                "name",
                "sequence_id",
                "project_id",
                "started_at",
                "completed_at",
                project_identifier=F("project__identifier"),
            )
        )

        assignee_ids = defaultdict(list)
        for issue_id, assignee_id in IssueAssignee.objects.filter(
            issue_id__in=[item["id"] for item in in_progress + completed],
            assignee_id__in=bot_ids,
            deleted_at__isnull=True,
        ).values_list("issue_id", "assignee_id"):
            assignee_ids[issue_id].append(str(assignee_id))

        for item in in_progress + completed:
            item["assignee_ids"] = assignee_ids[item["id"]]
        for item in completed:
            duration = (item["completed_at"] - item["started_at"]).total_seconds() / 60
            item["duration_minutes"] = round(max(duration, 0), 2)

        return Response(
            {"bots": bots, "in_progress": in_progress, "completed": completed},
            status=status.HTTP_200_OK,
        )
