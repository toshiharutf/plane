# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework import status

from plane.db.models import (
    BotTypeEnum,
    Issue,
    IssueActivity,
    IssueAssignee,
    Project,
    ProjectMember,
    State,
    User,
    WorkspaceMember,
)


def _ai_status_url(slug):
    return f"/api/workspaces/{slug}/ai-status/"


def _make_user(email, is_ai_bot=False):
    username = email.split("@")[0]
    return User.objects.create(
        email=email,
        username=username,
        first_name=username,
        display_name=username,
        is_bot=is_ai_bot,
        bot_type=BotTypeEnum.AI_AGENT if is_ai_bot else None,
    )


def _make_project(workspace, owner, identifier):
    project = Project.objects.create(
        name=f"Project {identifier}",
        identifier=identifier,
        workspace=workspace,
        created_by=owner,
        updated_by=owner,
    )
    ProjectMember.objects.create(workspace=workspace, project=project, member=owner, role=20, is_active=True)
    return project


def _states(project):
    return {
        group: State.objects.create(name=group.title(), group=group, project=project, workspace=project.workspace)
        for group in ("unstarted", "started", "completed")
    }


def _make_issue(project, state, assignees, name, created_at, completed_at=None):
    issue = Issue.objects.create(name=name, project=project, workspace=project.workspace, state=state)
    for assignee in assignees:
        IssueAssignee.objects.create(issue=issue, assignee=assignee, project=project, workspace=project.workspace)
    Issue.objects.filter(pk=issue.pk).update(created_at=created_at, completed_at=completed_at)
    return issue


def _state_change(issue, state, at):
    activity = IssueActivity.objects.create(
        issue=issue,
        project=issue.project,
        workspace=issue.workspace,
        verb="updated",
        field="state",
        new_value=state.name,
        new_identifier=state.id,
    )
    IssueActivity.objects.filter(pk=activity.pk).update(created_at=at)


@pytest.fixture
def ai_bot(db, workspace):
    bot = _make_user("status-ai@bots.local.plane", is_ai_bot=True)
    WorkspaceMember.objects.create(workspace=workspace, member=bot, role=15, is_active=True)
    return bot


@pytest.fixture
def project(db, workspace, create_user):
    return _make_project(workspace, create_user, "AIS")


@pytest.mark.contract
@pytest.mark.django_db
class TestWorkspaceAIStatus:
    def test_returns_in_progress_and_completed_bot_items(self, session_client, workspace, create_user, project, ai_bot):
        states = _states(project)
        human = _make_user("human@example.com")
        now = timezone.now().replace(second=0, microsecond=0)

        running = _make_issue(project, states["started"], [ai_bot, human], "Running", now - timedelta(hours=3))
        _state_change(running, states["started"], now - timedelta(hours=1))
        _make_issue(project, states["started"], [human], "Human only", now - timedelta(hours=2))

        done = _make_issue(
            project,
            states["completed"],
            [ai_bot],
            "Done",
            created_at=now - timedelta(hours=5),
            completed_at=now - timedelta(minutes=15),
        )
        _state_change(done, states["started"], now - timedelta(hours=2))
        _state_change(done, states["started"], now - timedelta(minutes=60))
        # A transition after completion must not be used as the start.
        _state_change(done, states["started"], now - timedelta(minutes=5))

        no_activity = _make_issue(
            project,
            states["completed"],
            [ai_bot],
            "No activity",
            created_at=now - timedelta(minutes=30),
            completed_at=now - timedelta(minutes=10),
        )

        response = session_client.get(_ai_status_url(workspace.slug))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["bots"] == [
            {"id": str(ai_bot.id), "display_name": ai_bot.display_name, "avatar_url": None}
        ]

        assert [item["id"] for item in response.data["in_progress"]] == [running.id]
        in_progress = response.data["in_progress"][0]
        assert in_progress["assignee_ids"] == [str(ai_bot.id)]
        assert in_progress["started_at"] == now - timedelta(hours=1)
        assert in_progress["project_identifier"] == "AIS"
        assert in_progress["state_name"] == states["started"].name
        assert {"sequence_id", "state_id", "state_color", "priority", "updated_at"} <= set(in_progress)

        completed = {item["id"]: item for item in response.data["completed"]}
        assert set(completed) == {done.id, no_activity.id}
        assert completed[done.id]["started_at"] == now - timedelta(minutes=60)
        assert completed[done.id]["duration_minutes"] == 45.0
        assert completed[no_activity.id]["started_at"] == now - timedelta(minutes=30)
        assert completed[no_activity.id]["duration_minutes"] == 20.0

    def test_excludes_projects_the_user_is_not_a_member_of(self, api_client, workspace, create_user, project, ai_bot):
        member = _make_user("member@example.com")
        WorkspaceMember.objects.create(workspace=workspace, member=member, role=15, is_active=True)
        ProjectMember.objects.create(workspace=workspace, project=project, member=member, role=15, is_active=True)
        other_project = _make_project(workspace, create_user, "OTH")
        now = timezone.now()
        visible = _make_issue(project, _states(project)["started"], [ai_bot], "Visible", now)
        _make_issue(other_project, _states(other_project)["started"], [ai_bot], "Hidden", now)

        api_client.force_authenticate(user=member)
        response = api_client.get(_ai_status_url(workspace.slug))

        assert response.status_code == status.HTTP_200_OK
        assert [item["id"] for item in response.data["in_progress"]] == [visible.id]

    def test_days_limits_completed_items(self, session_client, workspace, project, ai_bot):
        states = _states(project)
        now = timezone.now()
        recent = _make_issue(project, states["completed"], [ai_bot], "Recent", now - timedelta(days=2), now)
        old = _make_issue(
            project, states["completed"], [ai_bot], "Old", now - timedelta(days=201), now - timedelta(days=200)
        )

        default_response = session_client.get(_ai_status_url(workspace.slug))
        all_time_response = session_client.get(_ai_status_url(workspace.slug), {"days": 0})
        invalid_response = session_client.get(_ai_status_url(workspace.slug), {"days": "abc"})

        assert [item["id"] for item in default_response.data["completed"]] == [recent.id]
        assert {item["id"] for item in all_time_response.data["completed"]} == {recent.id, old.id}
        assert invalid_response.status_code == status.HTTP_400_BAD_REQUEST

    def test_inactive_bots_and_removed_assignees_are_ignored(self, session_client, workspace, project, ai_bot):
        states = _states(project)
        WorkspaceMember.objects.filter(member=ai_bot).update(is_active=False)
        _make_issue(project, states["started"], [ai_bot], "Inactive bot", timezone.now())

        response = session_client.get(_ai_status_url(workspace.slug))

        assert response.data == {"bots": [], "in_progress": [], "completed": []}

        WorkspaceMember.objects.filter(member=ai_bot).update(is_active=True)
        IssueAssignee.objects.filter(assignee=ai_bot).update(deleted_at=timezone.now())

        response = session_client.get(_ai_status_url(workspace.slug))

        assert response.data["in_progress"] == []

    def test_without_ai_bots_everything_is_empty(self, session_client, workspace, project, create_user):
        states = _states(project)
        _make_issue(project, states["started"], [create_user], "Human task", timezone.now())

        response = session_client.get(_ai_status_url(workspace.slug))

        assert response.status_code == status.HTTP_200_OK
        assert response.data == {"bots": [], "in_progress": [], "completed": []}

    def test_negative_days_is_rejected(self, session_client, workspace, project, ai_bot):
        response = session_client.get(_ai_status_url(workspace.slug), {"days": -1})

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_other_bot_types_and_other_state_groups_are_ignored(self, session_client, workspace, project, ai_bot):
        states = _states(project)
        plain_bot = User.objects.create(
            email="plain-bot@bots.local.plane", username="plain-bot", display_name="plain-bot", is_bot=True
        )
        WorkspaceMember.objects.create(workspace=workspace, member=plain_bot, role=15, is_active=True)
        now = timezone.now()
        _make_issue(project, states["started"], [plain_bot], "Other bot", now)
        _make_issue(project, states["unstarted"], [ai_bot], "Not started", now)

        response = session_client.get(_ai_status_url(workspace.slug))

        assert [bot["id"] for bot in response.data["bots"]] == [str(ai_bot.id)]
        assert response.data["in_progress"] == []
        assert response.data["completed"] == []

    def test_archived_and_deleted_items_are_hidden(self, session_client, workspace, project, ai_bot):
        states = _states(project)
        now = timezone.now()
        archived = _make_issue(project, states["completed"], [ai_bot], "Archived", now, now)
        Issue.objects.filter(pk=archived.pk).update(archived_at=now)
        _make_issue(project, states["started"], [ai_bot], "Deleted", now).delete()

        response = session_client.get(_ai_status_url(workspace.slug))

        assert response.data["in_progress"] == []
        assert response.data["completed"] == []

    def test_in_progress_is_ordered_by_latest_start(self, session_client, workspace, project, ai_bot):
        states = _states(project)
        now = timezone.now().replace(microsecond=0)
        older = _make_issue(project, states["started"], [ai_bot], "Older", now - timedelta(hours=6))
        newer = _make_issue(project, states["started"], [ai_bot], "Newer", now - timedelta(hours=8))
        _state_change(newer, states["started"], now - timedelta(minutes=5))

        response = session_client.get(_ai_status_url(workspace.slug))

        assert [item["id"] for item in response.data["in_progress"]] == [newer.id, older.id]

    def test_query_count_does_not_grow_with_items(self, session_client, workspace, project, ai_bot):
        states = _states(project)
        now = timezone.now()

        def query_count():
            with CaptureQueriesContext(connection) as context:
                response = session_client.get(_ai_status_url(workspace.slug))
            assert response.status_code == status.HTTP_200_OK
            return len(context.captured_queries)

        _make_issue(project, states["started"], [ai_bot], "One", now)
        _make_issue(project, states["completed"], [ai_bot], "Two", now, now)
        baseline = query_count()
        for index in range(5):
            _make_issue(project, states["started"], [ai_bot], f"Started {index}", now)
            _make_issue(project, states["completed"], [ai_bot], f"Completed {index}", now, now)

        assert query_count() == baseline

    @pytest.mark.parametrize("role", [5, None])
    def test_guests_and_non_members_are_forbidden(self, api_client, workspace, role):
        user = _make_user("outsider@example.com")
        if role is not None:
            WorkspaceMember.objects.create(workspace=workspace, member=user, role=role, is_active=True)
        api_client.force_authenticate(user=user)

        response = api_client.get(_ai_status_url(workspace.slug))

        assert response.status_code == status.HTTP_403_FORBIDDEN
