# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta
from uuid import uuid4

# Django imports
from django.db import transaction
from django.db.models import Count, Q, OuterRef, Subquery, IntegerField
from django.utils import timezone
from django.db.models.functions import Coalesce

# Third party modules
from rest_framework import status
from rest_framework.response import Response

from plane.app.permissions import WorkspaceEntityPermission, allow_permission, ROLE

# Module imports
from plane.app.serializers import (
    AIBotMemberCreateSerializer,
    APITokenSerializer,
    ProjectMemberRoleSerializer,
    WorkspaceMemberAdminSerializer,
    WorkspaceMemberMeSerializer,
    WorkSpaceMemberSerializer,
)
from plane.app.views.base import BaseAPIView
from plane.db.models import (
    APIToken,
    BotTypeEnum,
    DraftIssue,
    Profile,
    Project,
    ProjectMember,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.utils.cache import invalidate_cache
from plane.utils.members import visible_member_q

from .. import BaseViewSet


class WorkSpaceMemberViewSet(BaseViewSet):
    serializer_class = WorkspaceMemberAdminSerializer
    model = WorkspaceMember

    search_fields = ["member__display_name", "member__first_name"]
    use_read_replica = True

    def get_queryset(self):
        return self.filter_queryset(
            super()
            .get_queryset()
            .filter(workspace__slug=self.kwargs.get("slug"))
            .filter(visible_member_q())
            .select_related("member", "member__avatar_asset")
        )

    @allow_permission(allowed_roles=[ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def list(self, request, slug):
        workspace_member = WorkspaceMember.objects.get(member=request.user, workspace__slug=slug, is_active=True)

        # Get all active workspace members
        workspace_members = self.get_queryset()
        if workspace_member.role > 5:
            serializer = WorkspaceMemberAdminSerializer(workspace_members, fields=("id", "member", "role"), many=True)
        else:
            serializer = WorkSpaceMemberSerializer(workspace_members, fields=("id", "member", "role"), many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @allow_permission(allowed_roles=[ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def retrieve(self, request, slug, pk):
        workspace_member = WorkspaceMember.objects.get(member=request.user, workspace__slug=slug, is_active=True)

        try:
            # Get the specific workspace member by pk
            member = self.get_queryset().get(pk=pk)
        except WorkspaceMember.DoesNotExist:
            return Response(
                {"error": "Workspace member not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if workspace_member.role > ROLE.GUEST.value:
            serializer = WorkspaceMemberAdminSerializer(member, fields=("id", "member", "role"))
        else:
            serializer = WorkSpaceMemberSerializer(member, fields=("id", "member", "role"))
        return Response(serializer.data, status=status.HTTP_200_OK)

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def partial_update(self, request, slug, pk):
        workspace_member = WorkspaceMember.objects.get(visible_member_q(), pk=pk, workspace__slug=slug, is_active=True)
        if request.user.id == workspace_member.member_id:
            return Response(
                {"error": "You cannot update your own role"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if workspace_member.member.is_bot and "role" in request.data:
            return Response(
                {"error": "AI bot workspace roles cannot be changed"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # If a user is moved to a guest role he can't have any other role in projects
        if "role" in request.data and int(request.data.get("role")) == 5:
            ProjectMember.objects.filter(workspace__slug=slug, member_id=workspace_member.member_id).update(role=5)

        serializer = WorkSpaceMemberSerializer(workspace_member, data=request.data, partial=True)

        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def destroy(self, request, slug, pk):
        # Check the user role who is deleting the user
        workspace_member = WorkspaceMember.objects.get(visible_member_q(), workspace__slug=slug, pk=pk, is_active=True)

        # check requesting user role
        requesting_workspace_member = WorkspaceMember.objects.get(
            workspace__slug=slug, member=request.user, is_active=True
        )

        if str(workspace_member.id) == str(requesting_workspace_member.id):
            return Response(
                {"error": "You cannot remove yourself from the workspace. Please use leave workspace"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if requesting_workspace_member.role < workspace_member.role:
            return Response(
                {"error": "You cannot remove a user having role higher than you"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if (
            Project.objects.annotate(
                total_members=Count("project_projectmember"),
                member_with_role=Count(
                    "project_projectmember",
                    filter=Q(
                        project_projectmember__member_id=workspace_member.id,
                        project_projectmember__role=20,
                    ),
                ),
            )
            .filter(total_members=1, member_with_role=1, workspace__slug=slug)
            .exists()
        ):
            return Response(
                {
                    "error": "User is a part of some projects where they are the only admin, they should either leave that project or promote another user to admin."  # noqa: E501
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Deactivate the users from the projects where the user is part of
        _ = ProjectMember.objects.filter(
            workspace__slug=slug, member_id=workspace_member.member_id, is_active=True
        ).update(is_active=False, updated_at=timezone.now())

        workspace_member.is_active = False
        workspace_member.save()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @invalidate_cache(
        path="/api/workspaces/:slug/members/",
        url_params=True,
        user=False,
        multiple=True,
    )
    @invalidate_cache(path="/api/users/me/settings/")
    @invalidate_cache(path="api/users/me/workspaces/", user=False, multiple=True)
    @allow_permission(allowed_roles=[ROLE.ADMIN, ROLE.MEMBER, ROLE.GUEST], level="WORKSPACE")
    def leave(self, request, slug):
        workspace_member = WorkspaceMember.objects.get(workspace__slug=slug, member=request.user, is_active=True)

        # Check if the leaving user is the only admin of the workspace
        if (
            workspace_member.role == 20
            and not WorkspaceMember.objects.filter(workspace__slug=slug, role=20, is_active=True).count() > 1
        ):
            return Response(
                {
                    "error": "You cannot leave the workspace as you are the only admin of the workspace you will have to either delete the workspace or promote another user to admin."  # noqa: E501
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if (
            Project.objects.annotate(
                total_members=Count("project_projectmember"),
                member_with_role=Count(
                    "project_projectmember",
                    filter=Q(
                        project_projectmember__member_id=request.user.id,
                        project_projectmember__role=20,
                    ),
                ),
            )
            .filter(total_members=1, member_with_role=1, workspace__slug=slug)
            .exists()
        ):
            return Response(
                {
                    "error": "You are a part of some projects where you are the only admin, you should either leave the project or promote another user to admin."  # noqa: E501
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # # Deactivate the users from the projects where the user is part of
        _ = ProjectMember.objects.filter(
            workspace__slug=slug, member_id=workspace_member.member_id, is_active=True
        ).update(is_active=False, updated_at=timezone.now())

        # # Deactivate the user
        workspace_member.is_active = False
        workspace_member.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceMemberUserViewsEndpoint(BaseAPIView):
    def post(self, request, slug):
        workspace_member = WorkspaceMember.objects.get(workspace__slug=slug, member=request.user, is_active=True)
        workspace_member.view_props = request.data.get("view_props", {})
        workspace_member.save()

        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceMemberUserEndpoint(BaseAPIView):
    use_read_replica = True

    def get(self, request, slug):
        draft_issue_count = (
            DraftIssue.objects.filter(created_by=request.user, workspace_id=OuterRef("workspace_id"))
            .values("workspace_id")
            .annotate(count=Count("id"))
            .values("count")
        )

        workspace_member = (
            WorkspaceMember.objects.filter(member=request.user, workspace__slug=slug, is_active=True)
            .annotate(draft_issue_count=Coalesce(Subquery(draft_issue_count, output_field=IntegerField()), 0))
            .first()
        )
        serializer = WorkspaceMemberMeSerializer(workspace_member)
        return Response(serializer.data, status=status.HTTP_200_OK)


class WorkspaceAIBotMemberEndpoint(BaseAPIView):
    @invalidate_cache(
        path="/api/workspaces/:slug/members/",
        url_params=True,
        user=False,
        multiple=True,
    )
    @invalidate_cache(path="/api/users/me/settings/")
    @allow_permission(allowed_roles=[ROLE.ADMIN], level="WORKSPACE")
    def post(self, request, slug):
        serializer = AIBotMemberCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        display_name = data["display_name"].strip()
        email = data.get("email") or f"ai-bot-{uuid4().hex}@bots.local.plane"
        token_label = data.get("token_label") or f"{display_name} agent token"
        expired_at = data.get("expired_at", timezone.now() + timedelta(days=365))
        workspace = Workspace.objects.get(slug=slug)

        with transaction.atomic():
            bot_user = User(
                email=email,
                username=f"ai_bot_{uuid4().hex}",
                display_name=display_name,
                first_name=display_name,
                last_name="",
                is_bot=True,
                bot_type=BotTypeEnum.AI_AGENT,
                is_active=True,
                is_email_verified=True,
                is_email_valid=True,
                is_password_autoset=True,
                user_timezone=request.user.user_timezone,
                last_active=timezone.now(),
            )
            bot_user.set_unusable_password()
            bot_user.save()
            Profile.objects.create(user=bot_user, is_onboarded=True, last_workspace_id=workspace.id)

            workspace_member = WorkspaceMember.objects.create(
                workspace=workspace,
                member=bot_user,
                role=ROLE.MEMBER.value,
            )

            api_token = APIToken.objects.create(
                label=token_label,
                description=f"Service token for AI bot {display_name}",
                user=bot_user,
                user_type=1,
                workspace=workspace,
                expired_at=expired_at,
                is_service=True,
            )

        workspace_member = WorkspaceMember.objects.select_related("member", "member__avatar_asset").get(
            pk=workspace_member.pk
        )
        return Response(
            {
                "workspace_member": WorkspaceMemberAdminSerializer(
                    workspace_member, fields=("id", "member", "role", "is_active", "created_at")
                ).data,
                "api_token": APITokenSerializer(api_token).data,
            },
            status=status.HTTP_201_CREATED,
        )


class WorkspaceProjectMemberEndpoint(BaseAPIView):
    serializer_class = ProjectMemberRoleSerializer
    model = ProjectMember

    permission_classes = [WorkspaceEntityPermission]

    def get(self, request, slug):
        # Fetch all project IDs where the user is involved
        project_ids = (
            ProjectMember.objects.filter(member=request.user, is_active=True)
            .values_list("project_id", flat=True)
            .distinct()
        )

        # Get all the project members in which the user is involved
        project_members = ProjectMember.objects.filter(
            visible_member_q(), workspace__slug=slug, project_id__in=project_ids, is_active=True
        ).select_related("project", "member", "workspace")
        project_members = ProjectMemberRoleSerializer(project_members, many=True).data

        project_members_dict = dict()

        # Construct a dictionary with project_id as key and project_members as value
        for project_member in project_members:
            project_id = project_member.pop("project")
            if str(project_id) not in project_members_dict:
                project_members_dict[str(project_id)] = []
            project_members_dict[str(project_id)].append(project_member)

        return Response(project_members_dict, status=status.HTTP_200_OK)
