# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.db.models import Q

from plane.db.models import BotTypeEnum, IssueAssignee, ProjectMember, WorkspaceMember

ASSIGNABLE_PROJECT_MEMBER_ROLE = 15


def visible_user_q(prefix=""):
    return Q(**{f"{prefix}is_bot": False}) | Q(**{f"{prefix}bot_type": BotTypeEnum.AI_AGENT})


def visible_member_q(prefix="member__"):
    return visible_user_q(prefix=prefix)


def is_visible_user(user):
    return not user.is_bot or user.bot_type == BotTypeEnum.AI_AGENT


def is_ai_agent_user(user):
    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_bot", False)
        and getattr(user, "bot_type", None) == BotTypeEnum.AI_AGENT
    )


def is_workspace_ai_agent(user, workspace_slug):
    """True when ``user`` is an AI_AGENT bot that is an active, assignable member of the workspace."""
    return is_ai_agent_user(user) and WorkspaceMember.objects.filter(
        workspace__slug=workspace_slug,
        member=user,
        role__gte=ASSIGNABLE_PROJECT_MEMBER_ROLE,
        is_active=True,
    ).exists()


def is_issue_assigned_to_user(issue_id, project_id, workspace_slug, user_id):
    """True when ``user_id`` is one of the current assignees of the work item."""
    return IssueAssignee.objects.filter(
        issue_id=issue_id,
        project_id=project_id,
        workspace__slug=workspace_slug,
        assignee_id=user_id,
        deleted_at__isnull=True,
    ).exists()


def active_assignee_q(prefix="assignees__"):
    return Q(**{f"{prefix}member_project__is_active": True}) | Q(
        **{
            f"{prefix}is_bot": True,
            f"{prefix}bot_type": BotTypeEnum.AI_AGENT,
            f"{prefix}member_workspace__is_active": True,
        }
    )


def active_issue_assignee_q(prefix="assignee__"):
    return Q(**{f"{prefix}member_project__is_active": True}) | Q(
        **{
            f"{prefix}is_bot": True,
            f"{prefix}bot_type": BotTypeEnum.AI_AGENT,
            f"{prefix}member_workspace__is_active": True,
        }
    )


def get_assignable_issue_assignee_ids(project_id, workspace_id, member_ids):
    requested_member_ids = [str(getattr(member, "id", member)) for member in member_ids or []]
    if not requested_member_ids:
        return []

    assignable_member_ids = set(
        str(member_id)
        for member_id in ProjectMember.objects.filter(
            project_id=project_id,
            role__gte=ASSIGNABLE_PROJECT_MEMBER_ROLE,
            is_active=True,
            member_id__in=requested_member_ids,
        ).values_list("member_id", flat=True)
    )

    if workspace_id:
        assignable_member_ids.update(
            str(member_id)
            for member_id in WorkspaceMember.objects.filter(
                workspace_id=workspace_id,
                is_active=True,
                role__gte=ASSIGNABLE_PROJECT_MEMBER_ROLE,
                member_id__in=requested_member_ids,
                member__is_bot=True,
                member__bot_type=BotTypeEnum.AI_AGENT,
            ).values_list("member_id", flat=True)
        )

    return [member_id for member_id in requested_member_ids if member_id in assignable_member_ids]
