# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Permission classes that extend the human project permissions to workspace AI_AGENT bots.

Human users keep the exact behaviour of the base permission class. A workspace
``AI_AGENT`` bot (``is_bot=True``, ``bot_type=AI_AGENT``, active workspace member
with role >= member) authenticated through an API key gets an additional,
narrower set of rights (public ``/api/v1`` views only):

- Work items: read everything, update only work items it is assigned to,
  create sub work items under work items it is assigned to or under work items
  it created that are not ``[Human]`` tickets and have no assignee but itself
  (such sub items unassigned or assigned only to itself), and create
  top-level work items that are unassigned or assigned only to itself. A
  top-level ``[Human] ...`` ticket must be unassigned, so the bot never works
  on a human's decision. On unassigned work items it created, it may move the
  state back to Todo (a state of the ``unstarted`` group), e.g. to ask a human
  again. On work items it created (``created_by``, never ``external_source``)
  that are not ``[Human]`` tickets and have no assignee but itself, it may
  update the planning fields (name, description, priority, estimate point,
  labels, dates, AI model, parent, state and assignees): assignees only to
  nobody or to itself alone, the parent only to nothing or to a parent it may
  create sub items under, and the state never to a ``completed`` group state
  (the bot never closes planned items as Done).
  On top of these permissions, every state a bot sets follows the work item
  state machine (``plane.utils.work_item_state_rules``, enforced by the issue
  serializers with a 400): Backlog -> Todo or Awaiting Human, Todo -> In
  Progress, In Progress -> Awaiting Human or Done, Awaiting Human -> Todo, Done
  is final; a bot creates work items only in Backlog, Todo or Awaiting Human,
  and never moves one to Cancelled or to a custom state.
- Cycles: read, create, update the name, description and dates, add work
  items and transfer the open ones to another cycle; never delete or archive.
- Modules: read, create, update the name, description, status and dates, and
  add work items; never delete or archive.
- Labels: read and create; never update or delete.
- Estimates: read; create the project's estimate when it has none, add points
  to an estimate it created; never change or delete points or estimates.
- Project: only a ``PATCH`` whose body is exactly ``{"estimate": <id>}``,
  pointing at an estimate of the project, and only while the project's
  current estimate is unset or one the bot created.
- Comments: read and create on any work item, update or delete only its own.
- Links: read everything, create only on work items it is assigned to, and
  update or delete only links it created.
- Relations: read and create between any work items.
- AI usage: read everything, report usage only on work items it is assigned to.
- Human requests: read everything, ask a question or an approval only on work
  items it is assigned to that are in Backlog or In Progress; never answer one
  (only human members answer; the answer moves the item to Todo).
- Pages: read public pages, create pages, update only pages it owns.
"""

import uuid

# Third Party imports
from rest_framework.permissions import SAFE_METHODS, BasePermission

# Module imports
from plane.db.models import Estimate, Issue, IssueAssignee, IssueComment, IssueLink, Page, Project, State
from plane.utils.members import is_issue_assigned_to_user, is_workspace_ai_agent

from .project import ProjectBasePermission, ProjectEntityPermission, ProjectLitePermission, ProjectMemberPermission


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


def _requests_unassigned(data):
    """True when the request body explicitly sets ``assignees`` to an empty list."""
    if hasattr(data, "getlist"):
        return "assignees" in data and not [value for value in data.getlist("assignees") if value]
    return "assignees" in data and data.get("assignees") == []


def _requests_todo_on_own_unassigned_item(request, view, issue_id):
    """True for a ``PATCH`` that only sets ``state`` to a Todo state on an unassigned item the bot created.

    Todo is any state of the ``unstarted`` group in the same project. This lets the orchestrator
    reopen a ``[Human] ...`` ticket it created without giving it any other right on that ticket.
    """
    data = request.data
    if set(data.keys()) != {"state"}:
        return False
    state_id = data.get("state")
    if not _is_uuid(state_id) or not _is_uuid(issue_id):
        return False
    project_id = view.project_id
    workspace_slug = view.workspace_slug
    if not Issue.objects.filter(
        pk=issue_id, project_id=project_id, workspace__slug=workspace_slug, created_by_id=request.user.id
    ).exists():
        return False
    if IssueAssignee.objects.filter(issue_id=issue_id, deleted_at__isnull=True).exists():
        return False
    return State.objects.filter(
        pk=state_id, project_id=project_id, workspace__slug=workspace_slug, group="unstarted"
    ).exists()


HUMAN_ITEM_PREFIX = "[Human]"


def _is_human_ticket_name(name):
    """True for a ``[Human] ...`` name (any case): a ticket that asks a person for a decision or an action."""
    return str(name or "").strip().casefold().startswith(HUMAN_ITEM_PREFIX.casefold())


def _requested_assignees(data):
    """The ``assignees`` of the request body as a list of strings, ``None`` when absent.

    Returns ``False`` when the value is not a list (form data always is one).
    """
    if "assignees" not in data:
        return None
    values = data.getlist("assignees") if hasattr(data, "getlist") else data.get("assignees")
    if not isinstance(values, (list, tuple)):
        return False
    return [str(value) for value in values if value]


def _requests_only_self_assigned(data, user_id):
    """True when ``assignees`` is absent (the create view then assigns the bot) or exactly ``[bot]``."""
    values = _requested_assignees(data)
    return values is None or values == [str(user_id)]


def _requests_top_level_item(data, user_id):
    """True for a top-level work item that is unassigned, or assigned only to the bot and not a ``[Human]`` ticket."""
    if _requests_unassigned(data):
        return True
    return _requests_only_self_assigned(data, user_id) and not _is_human_ticket_name(data.get("name"))


# Fields a bot may change on a work item it created (see ``_requests_update_of_own_planned_item``).
OWN_ITEM_PATCH_FIELDS = {
    "name",
    "description_html",
    "priority",
    "estimate_point",
    "labels",
    "state",
    "assignees",
    "start_date",
    "target_date",
    "ai_model",
    "parent",
}


def _is_own_planned_item(issue_id, project_id, workspace_slug, user_id):
    """True for a work item of the project the bot created, that is not a ``[Human]`` ticket and has no assignee but the bot."""
    issue = Issue.objects.filter(
        pk=issue_id, project_id=project_id, workspace__slug=workspace_slug, created_by_id=user_id
    ).first()
    if issue is None or _is_human_ticket_name(issue.name):
        return False
    return (
        not IssueAssignee.objects.filter(issue_id=issue_id, deleted_at__isnull=True)
        .exclude(assignee_id=user_id)
        .exists()
    )


def _is_allowed_parent(parent_id, project_id, workspace_slug, user_id):
    """True when a bot may put a work item under ``parent_id``, by ``POST`` or by ``PATCH`` of ``parent``.

    The parent is a work item of the project that the bot is assigned to, or one of its own planned
    items (``_is_own_planned_item``): the planner writes sub items under unassigned future-cycle items.
    """
    if not _is_uuid(parent_id):
        return False
    return is_issue_assigned_to_user(parent_id, project_id, workspace_slug, user_id) or _is_own_planned_item(
        parent_id, project_id, workspace_slug, user_id
    )


def _requests_update_of_own_planned_item(request, view, issue_id):
    """True for a ``PATCH`` of planning fields on a work item the bot created.

    The item must be in the view's project, created by the bot (``created_by``), not a ``[Human]``
    ticket, and have no assignee but the bot. ``assignees`` may only become ``[]`` or ``[bot]``,
    ``state`` must be a non-``completed`` state of the project, ``parent`` must be null or another
    work item of the project that ``_is_allowed_parent`` accepts, and ``name`` may not turn the
    item into a ``[Human]`` ticket.
    """
    data = request.data
    keys = set(data.keys())
    if not keys or not keys <= OWN_ITEM_PATCH_FIELDS or not _is_uuid(issue_id):
        return False
    user_id = str(request.user.id)
    project_id = view.project_id
    workspace_slug = view.workspace_slug
    if not _is_own_planned_item(issue_id, project_id, workspace_slug, request.user.id):
        return False
    if "name" in keys and _is_human_ticket_name(data.get("name")):
        return False
    if "assignees" in keys and _requested_assignees(data) not in ([], [user_id]):
        return False
    if "state" in keys:
        state_id = data.get("state")
        if not _is_uuid(state_id):
            return False
        if (
            not State.objects.filter(pk=state_id, project_id=project_id, workspace__slug=workspace_slug)
            .exclude(group="completed")
            .exists()
        ):
            return False
    if "parent" in keys and data.get("parent") is not None:
        parent_id = data.get("parent")
        if str(parent_id) == str(issue_id):
            return False
        if not _is_allowed_parent(parent_id, project_id, workspace_slug, request.user.id):
            return False
    return True


class ProjectEntityOrAIBotWorkItemPermission(ProjectEntityPermission):
    """Project members keep full access.

    AI bots may read every work item, ``PATCH`` work items they are assigned to,
    move unassigned work items they created back to a Todo state, and update the
    planning fields of work items they created (see
    ``_requests_update_of_own_planned_item``). They may ``POST`` new work items
    either as children (``parent``) of work items they are assigned to, as
    unassigned or self-assigned children of their own planned items (the same
    parents ``_is_allowed_parent`` accepts for a ``PATCH`` of ``parent``), or as
    top-level work items that are unassigned (``assignees`` given explicitly as
    an empty list) or assigned only to themselves. A top-level ``[Human] ...``
    ticket must be unassigned, so the bot cannot work on a human's decision.
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
                issue_id
                and (
                    is_issue_assigned_to_user(issue_id, project_id, view.workspace_slug, request.user.id)
                    or _requests_todo_on_own_unassigned_item(request, view, issue_id)
                    or _requests_update_of_own_planned_item(request, view, issue_id)
                )
            )

        if request.method == "POST":
            parent_id = request.data.get("parent")
            if parent_id is None:
                return _requests_top_level_item(request.data, request.user.id)
            if not _is_uuid(parent_id):
                return False
            if is_issue_assigned_to_user(parent_id, project_id, view.workspace_slug, request.user.id):
                return True
            # Under its own planned item the sub item follows the top-level rules:
            # unassigned, or assigned only to the bot and not a ``[Human]`` ticket.
            return _is_own_planned_item(
                parent_id, project_id, view.workspace_slug, request.user.id
            ) and _requests_top_level_item(request.data, request.user.id)

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


class _ProjectEntityOrAIBotAssignedItemPermission(ProjectEntityPermission):
    """Project members keep full access.

    AI bots may read every record and ``POST`` only on work items (``issue_id``)
    they are assigned to.
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
        if request.method == "POST" and project_id and issue_id:
            return is_issue_assigned_to_user(issue_id, project_id, view.workspace_slug, request.user.id)

        return False


class ProjectEntityOrAIBotAIUsagePermission(_ProjectEntityOrAIBotAssignedItemPermission):
    """AI bots may read every usage record and ``POST`` usage only on work items they are assigned to."""


class ProjectEntityOrAIBotHumanRequestPermission(_ProjectEntityOrAIBotAssignedItemPermission):
    """AI bots may read every human request and open one only on work items they are assigned to.

    Answering is a separate view that bots are refused (``ProjectEntityPermission``).
    """


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


def _requests_only_fields(data, fields):
    """True when the request body sets at least one field and only fields of ``fields``."""
    keys = set(data.keys())
    return bool(keys) and keys <= fields


class _ProjectEntityOrAIBotPlanningPermission(ProjectEntityPermission):
    """Project members keep full access.

    AI bots may read, ``POST`` (create the entity, or add or transfer work items)
    and ``PATCH`` only the fields in ``bot_patch_fields``. Deletes stay refused;
    archive views keep ``ProjectEntityPermission``.
    """

    bot_patch_fields = frozenset()

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True

        if not view.project_id or not is_workspace_ai_agent(request.user, view.workspace_slug):
            return False

        if request.method in SAFE_METHODS or request.method == "POST":
            return True

        if request.method == "PATCH":
            return bool(view.kwargs.get("pk")) and _requests_only_fields(request.data, self.bot_patch_fields)

        return False


class ProjectEntityOrAIBotCyclePermission(_ProjectEntityOrAIBotPlanningPermission):
    """AI bots may read and create cycles, update their name, description and dates, and add or transfer work items."""

    bot_patch_fields = frozenset({"name", "description", "start_date", "end_date"})


class ProjectEntityOrAIBotModulePermission(_ProjectEntityOrAIBotPlanningPermission):
    """AI bots may read and create modules, update their name, description, status and dates, and add work items."""

    bot_patch_fields = frozenset({"name", "description", "status", "start_date", "target_date"})


class ProjectMemberOrAIBotLabelPermission(ProjectMemberPermission):
    """Project members keep ``ProjectMemberPermission``; AI bots may read and create labels only."""

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True

        if not view.project_id or not is_workspace_ai_agent(request.user, view.workspace_slug):
            return False

        return request.method in SAFE_METHODS or request.method == "POST"


def _estimate_created_by(estimate_id, project_id, workspace_slug, user_id):
    return bool(
        _is_uuid(estimate_id)
        and Estimate.objects.filter(
            pk=estimate_id, project_id=project_id, workspace__slug=workspace_slug, created_by_id=user_id
        ).exists()
    )


class ProjectEntityOrAIBotEstimatePermission(ProjectEntityPermission):
    """Project members keep full access.

    AI bots may read estimates and their points, ``POST`` the project's estimate
    while the project has none (or the existing one is theirs, which the view
    answers with 409 and its id), and ``POST`` points to an estimate they created.
    Updating or deleting estimates and points stays refused.
    """

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True

        project_id = view.project_id
        if not project_id or not is_workspace_ai_agent(request.user, view.workspace_slug):
            return False

        if request.method in SAFE_METHODS:
            return True

        if request.method != "POST":
            return False

        estimate_id = view.kwargs.get("estimate_id")
        if estimate_id:
            return _estimate_created_by(estimate_id, project_id, view.workspace_slug, request.user.id)

        return (
            not Estimate.objects.filter(project_id=project_id, workspace__slug=view.workspace_slug)
            .exclude(created_by_id=request.user.id)
            .exists()
        )


def _requests_project_estimate_by_bot(request, view):
    """True for a project ``PATCH`` whose body is exactly ``{"estimate": <id>}`` that a bot may send.

    The target estimate belongs to the project, and the project's current estimate is unset
    or one the bot created.
    """
    data = request.data
    if set(data.keys()) != {"estimate"}:
        return False
    project_id = view.project_id
    workspace_slug = view.workspace_slug
    if not _is_uuid(project_id):
        return False
    project = Project.objects.filter(pk=project_id, workspace__slug=workspace_slug).only("estimate_id").first()
    if project is None:
        return False
    if not Estimate.objects.filter(
        pk=data.get("estimate") if _is_uuid(data.get("estimate")) else None,
        project_id=project_id,
        workspace__slug=workspace_slug,
    ).exists():
        return False
    return project.estimate_id is None or _estimate_created_by(
        project.estimate_id, project_id, workspace_slug, request.user.id
    )


class ProjectBaseOrAIBotEstimatePermission(ProjectBasePermission):
    """Humans keep ``ProjectBasePermission``.

    AI bots may only ``PATCH`` the project with ``{"estimate": <id>}`` (see
    ``_requests_project_estimate_by_bot``); every other project request is refused.
    """

    def has_permission(self, request, view):
        if super().has_permission(request, view):
            return True

        if request.method != "PATCH" or not is_workspace_ai_agent(request.user, view.workspace_slug):
            return False

        return _requests_project_estimate_by_bot(request, view)
