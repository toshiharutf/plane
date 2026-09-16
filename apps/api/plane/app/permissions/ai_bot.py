# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Permission classes that extend the human project permissions to workspace AI_AGENT bots.

Human users keep the exact behaviour of the base permission class. A workspace
``AI_AGENT`` bot (``is_bot=True``, ``bot_type=AI_AGENT``, active workspace member
with role >= member) authenticated through an API key gets an additional,
narrower set of rights:

- Work items: read everything, update only work items it is assigned to, and
  create sub work items under work items it is assigned to.
- Comments: read and create on any work item, update or delete only its own.
- Links: read everything, create only on work items it is assigned to, and
  update or delete only links it created.
- Relations: read and create between any work items.
- Pages: read public pages, create pages, update only pages it owns.
"""

import uuid

# Third Party imports
from rest_framework.permissions import SAFE_METHODS, BasePermission

# Module imports
from plane.db.models import IssueComment, IssueLink, Page
from plane.utils.members import is_issue_assigned_to_user, is_workspace_ai_agent

from .project import ProjectEntityPermission, ProjectLitePermission


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return False
    return True


class ProjectEntityOrAIBotReadOnlyPermission(ProjectEntityPermission):
    """Project members keep full access; AI bots may only use safe methods."""

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True
        return request.method in SAFE_METHODS and is_workspace_ai_agent(request.user, view.workspace_slug)


class ProjectEntityOrAIBotWorkItemPermission(ProjectEntityPermission):
    """Project members keep full access.

    AI bots may read every work item, ``PATCH`` work items they are assigned to,
    and ``POST`` new work items only as children (``parent``) of work items they
    are assigned to.
    """

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True

        if not is_workspace_ai_agent(request.user, view.workspace_slug):
            return False

        if request.method in SAFE_METHODS:
            return True

        project_id = view.project_id
        if not project_id:
            return False

        if request.method == "PATCH":
            issue_id = view.kwargs.get("pk")
            return bool(
                issue_id and is_issue_assigned_to_user(issue_id, project_id, view.workspace_slug, request.user.id)
            )

        if request.method == "POST":
            parent_id = request.data.get("parent")
            return bool(
                parent_id
                and _is_uuid(parent_id)
                and is_issue_assigned_to_user(parent_id, project_id, view.workspace_slug, request.user.id)
            )

        return False


class ProjectLiteOrAIBotCommentPermission(ProjectLitePermission):
    """Project members keep full access.

    AI bots may read and create comments on any work item, and update or delete
    only the comments they authored.
    """

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True

        if not is_workspace_ai_agent(request.user, view.workspace_slug):
            return False

        if request.method in SAFE_METHODS or request.method == "POST":
            return True

        if request.method in ("PATCH", "PUT", "DELETE"):
            comment_id = view.kwargs.get("pk")
            return bool(
                comment_id
                and IssueComment.objects.filter(
                    pk=comment_id,
                    workspace__slug=view.workspace_slug,
                    project_id=view.project_id,
                    issue_id=view.kwargs.get("issue_id"),
                    actor_id=request.user.id,
                ).exists()
            )

        return False


class ProjectEntityOrAIBotLinkPermission(ProjectEntityPermission):
    """Project members keep full access.

    AI bots may read every link, ``POST`` links only on work items they are
    assigned to, and update or delete only the links they created.
    """

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True

        if not is_workspace_ai_agent(request.user, view.workspace_slug):
            return False

        if request.method in SAFE_METHODS:
            return True

        project_id = view.project_id
        issue_id = view.kwargs.get("issue_id")
        if not project_id or not issue_id:
            return False

        if request.method == "POST":
            return is_issue_assigned_to_user(issue_id, project_id, view.workspace_slug, request.user.id)

        if request.method in ("PATCH", "PUT", "DELETE"):
            link_id = view.kwargs.get("pk")
            return bool(
                link_id
                and IssueLink.objects.filter(
                    pk=link_id,
                    workspace__slug=view.workspace_slug,
                    project_id=project_id,
                    issue_id=issue_id,
                    created_by_id=request.user.id,
                ).exists()
            )

        return False


class ProjectEntityOrAIBotRelationPermission(ProjectEntityPermission):
    """Project members keep full access; AI bots may read and create relations."""

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True

        if not is_workspace_ai_agent(request.user, view.workspace_slug):
            return False

        return request.method in SAFE_METHODS or request.method == "POST"


class ProjectEntityOrAIBotPagePermission(BasePermission):
    """Page access for the public API.

    Humans follow ``ProjectEntityPermission`` (project members read, members and
    admins write). AI bots may read, create pages, and update only pages they own.
    Private-page visibility is enforced by the view querysets.
    """

    def has_permission(self, request, view):
        if request.user.is_anonymous:
            return False

        if not is_workspace_ai_agent(request.user, view.workspace_slug):
            return ProjectEntityPermission().has_permission(request, view)

        if request.method in SAFE_METHODS or request.method == "POST":
            return True

        if request.method in ("PATCH", "PUT"):
            page_id = view.kwargs.get("pk")
            return bool(
                page_id
                and Page.objects.filter(
                    pk=page_id,
                    workspace__slug=view.workspace_slug,
                    project_pages__project_id=view.project_id,
                    project_pages__deleted_at__isnull=True,
                    owned_by_id=request.user.id,
                ).exists()
            )

        return False
