# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import APIToken, BotTypeEnum, IssueAssignee, Project, ProjectMember, State, User, WorkspaceMember


def _ai_bot_url(slug):
    return f"/api/workspaces/{slug}/ai-bot-members/"


def _workspace_members_url(slug):
    return f"/api/workspaces/{slug}/members/"


def _project_members_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/members/"


def _issues_url(slug, project_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/issues/"


def _api_work_items_url(slug, project_id):
    return f"/api/v1/workspaces/{slug}/projects/{project_id}/work-items/"


def _api_states_url(slug, project_id):
    return f"/api/v1/workspaces/{slug}/projects/{project_id}/states/"


def _make_user(email):
    username = email.split("@")[0]
    user = User.objects.create(email=email, username=username, first_name=username, display_name=username)
    user.set_password("test-password")
    user.save()
    return user


def _stub_issue_activity_tasks(monkeypatch):
    monkeypatch.setattr("plane.app.views.issue.base.issue_activity.delay", lambda **kwargs: None)
    monkeypatch.setattr("plane.app.views.issue.base.model_activity.delay", lambda **kwargs: None)
    monkeypatch.setattr("plane.app.views.issue.base.issue_description_version_task.delay", lambda **kwargs: None)
    monkeypatch.setattr("plane.api.views.issue.issue_activity.delay", lambda **kwargs: None)
    monkeypatch.setattr("plane.api.views.issue.model_activity.delay", lambda **kwargs: None)


@pytest.fixture
def project(db, workspace, create_user):
    project = Project.objects.create(
        name="AI Agent Project",
        identifier="AIA",
        workspace=workspace,
        created_by=create_user,
        updated_by=create_user,
    )
    ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=20, is_active=True)
    State.objects.create(name="Todo", group="unstarted", project=project, workspace=workspace)
    return project


@pytest.mark.contract
@pytest.mark.django_db
class TestAIBotMembers:
    def test_workspace_admin_can_create_ai_bot_member_with_token(self, session_client, workspace, create_user):
        session_client.force_authenticate(user=create_user)

        response = session_client.post(
            _ai_bot_url(workspace.slug),
            {
                "display_name": "Planner AI",
                "email": "planner-ai@example.com",
                "token_label": "Planner AI local token",
                "expired_at": None,
            },
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED
        workspace_member = response.data["workspace_member"]
        api_token = response.data["api_token"]
        bot_id = workspace_member["member"]["id"]

        bot_user = User.objects.get(pk=bot_id)
        assert bot_user.is_bot is True
        assert bot_user.bot_type == BotTypeEnum.AI_AGENT
        assert not bot_user.has_usable_password()
        assert workspace_member["role"] == 15
        assert workspace_member["member"]["bot_type"] == BotTypeEnum.AI_AGENT
        assert api_token["token"].startswith("plane_api_")
        assert api_token["user_type"] == 1

        token = APIToken.objects.get(pk=api_token["id"])
        assert token.user == bot_user
        assert token.workspace == workspace
        assert token.is_service is True

    def test_non_admin_cannot_create_ai_bot_member(self, api_client, workspace):
        member_user = _make_user("workspace-member@example.com")
        WorkspaceMember.objects.create(workspace=workspace, member=member_user, role=15, is_active=True)
        api_client.force_authenticate(user=member_user)

        response = api_client.post(
            _ai_bot_url(workspace.slug),
            {"display_name": "Blocked AI"},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_duplicate_bot_email_is_rejected(self, session_client, workspace, create_user):
        session_client.force_authenticate(user=create_user)
        _make_user("duplicate@example.com")

        response = session_client.post(
            _ai_bot_url(workspace.slug),
            {"display_name": "Duplicate AI", "email": "duplicate@example.com"},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "email" in response.data

    def test_workspace_members_include_ai_agents_and_hide_seed_bots(self, session_client, workspace, create_user):
        session_client.force_authenticate(user=create_user)
        create_response = session_client.post(
            _ai_bot_url(workspace.slug),
            {"display_name": "Visible AI"},
            format="json",
        )
        ai_bot_id = str(create_response.data["workspace_member"]["member"]["id"])

        seed_bot = User.objects.create(
            email="workspace-seed@example.com",
            username="workspace_seed_bot",
            display_name="Workspace Seed Bot",
            first_name="Workspace Seed Bot",
            is_bot=True,
            bot_type=BotTypeEnum.WORKSPACE_SEED,
        )
        WorkspaceMember.objects.create(workspace=workspace, member=seed_bot, role=15, is_active=True)

        response = session_client.get(_workspace_members_url(workspace.slug))

        assert response.status_code == status.HTTP_200_OK
        member_ids = {str(member["member"]["id"]) for member in response.data}
        assert ai_bot_id in member_ids
        assert str(seed_bot.id) not in member_ids

    def test_ai_bot_cannot_be_added_to_project_members(self, session_client, workspace, project, create_user):
        session_client.force_authenticate(user=create_user)
        create_response = session_client.post(
            _ai_bot_url(workspace.slug),
            {"display_name": "Executor AI"},
            format="json",
        )
        bot_id = str(create_response.data["workspace_member"]["member"]["id"])

        add_response = session_client.post(
            _project_members_url(workspace.slug, project.id),
            {"members": [{"member_id": bot_id, "role": 15}]},
            format="json",
        )

        assert add_response.status_code == status.HTTP_400_BAD_REQUEST
        assert not ProjectMember.objects.filter(project=project, member_id=bot_id).exists()

        list_response = session_client.get(_project_members_url(workspace.slug, project.id))
        assert list_response.status_code == status.HTTP_200_OK
        assert bot_id not in {str(member["member"]) for member in list_response.data}

    def test_ai_bot_can_be_assigned_to_issue_without_project_membership(
        self, session_client, workspace, project, create_user, monkeypatch
    ):
        _stub_issue_activity_tasks(monkeypatch)
        session_client.force_authenticate(user=create_user)
        create_response = session_client.post(
            _ai_bot_url(workspace.slug),
            {"display_name": "Assignee AI"},
            format="json",
        )
        bot_id = str(create_response.data["workspace_member"]["member"]["id"])
        state = State.objects.get(project=project)
        issue_response = session_client.post(
            _issues_url(workspace.slug, project.id),
            {
                "name": "Run project analysis",
                "state_id": str(state.id),
                "assignee_ids": [bot_id],
            },
            format="json",
        )

        assert issue_response.status_code == status.HTTP_201_CREATED
        assert IssueAssignee.objects.filter(issue_id=issue_response.data["id"], assignee_id=bot_id).exists()
        assert not ProjectMember.objects.filter(project=project, member_id=bot_id).exists()

        detail_response = session_client.get(f"{_issues_url(workspace.slug, project.id)}{issue_response.data['id']}/")

        assert detail_response.status_code == status.HTTP_200_OK
        assert bot_id in {str(assignee_id) for assignee_id in detail_response.data["assignee_ids"]}

    def test_ai_bot_assignment_update_does_not_create_project_member(
        self, session_client, workspace, project, create_user, monkeypatch
    ):
        _stub_issue_activity_tasks(monkeypatch)
        session_client.force_authenticate(user=create_user)
        create_response = session_client.post(
            _ai_bot_url(workspace.slug),
            {"display_name": "Assignment AI"},
            format="json",
        )
        bot_id = str(create_response.data["workspace_member"]["member"]["id"])
        state = State.objects.get(project=project)
        issue_response = session_client.post(
            _issues_url(workspace.slug, project.id),
            {
                "name": "Refine task before execution",
                "state_id": str(state.id),
            },
            format="json",
        )

        assert issue_response.status_code == status.HTTP_201_CREATED
        assert not ProjectMember.objects.filter(project=project, member_id=bot_id).exists()

        update_response = session_client.patch(
            f"{_issues_url(workspace.slug, project.id)}{issue_response.data['id']}/",
            {"assignee_ids": [bot_id]},
            format="json",
        )

        assert update_response.status_code == status.HTTP_204_NO_CONTENT
        assert not ProjectMember.objects.filter(project=project, member_id=bot_id).exists()
        assert IssueAssignee.objects.filter(issue_id=issue_response.data["id"], assignee_id=bot_id).exists()

    def test_ai_bot_api_token_can_read_project_board_and_update_own_issue_description_and_state(
        self, session_client, workspace, project, create_user, monkeypatch
    ):
        _stub_issue_activity_tasks(monkeypatch)
        session_client.force_authenticate(user=create_user)
        create_response = session_client.post(
            _ai_bot_url(workspace.slug),
            {"display_name": "API Executor AI"},
            format="json",
        )
        bot_id = str(create_response.data["workspace_member"]["member"]["id"])
        bot_token = create_response.data["api_token"]["token"]
        todo_state = State.objects.get(project=project)
        started_state = State.objects.create(name="In Progress", group="started", project=project, workspace=workspace)
        issue_response = session_client.post(
            _issues_url(workspace.slug, project.id),
            {
                "name": "Execute assigned work",
                "state_id": str(todo_state.id),
                "assignee_ids": [bot_id],
            },
            format="json",
        )
        issue_id = issue_response.data["id"]

        bot_client = APIClient()
        bot_client.credentials(HTTP_X_API_KEY=bot_token)
        states_response = bot_client.get(_api_states_url(workspace.slug, project.id))
        list_response = bot_client.get(_api_work_items_url(workspace.slug, project.id))
        update_response = bot_client.patch(
            f"{_api_work_items_url(workspace.slug, project.id)}{issue_id}/",
            {"description_html": "<p>Refined by AI.</p>", "state": str(started_state.id)},
            format="json",
        )

        assert states_response.status_code == status.HTTP_200_OK
        assert str(started_state.id) in {str(state["id"]) for state in states_response.data["results"]}
        assert list_response.status_code == status.HTTP_200_OK
        assert update_response.status_code == status.HTTP_200_OK
        issue_response = bot_client.get(f"{_api_work_items_url(workspace.slug, project.id)}{issue_id}/")
        assert issue_response.status_code == status.HTTP_200_OK
        assert issue_response.data["description_html"] == "<p>Refined by AI.</p>"
        assert str(issue_response.data["state"]) == str(started_state.id)

    def test_ai_bot_api_token_cannot_update_human_issue(
        self, session_client, workspace, project, create_user, monkeypatch
    ):
        _stub_issue_activity_tasks(monkeypatch)
        session_client.force_authenticate(user=create_user)
        create_response = session_client.post(
            _ai_bot_url(workspace.slug),
            {"display_name": "Restricted AI"},
            format="json",
        )
        bot_id = str(create_response.data["workspace_member"]["member"]["id"])
        bot_token = create_response.data["api_token"]["token"]
        ProjectMember.objects.create(workspace=workspace, project=project, member_id=bot_id, role=15, is_active=True)
        state = State.objects.get(project=project)
        issue_response = session_client.post(
            _issues_url(workspace.slug, project.id),
            {
                "name": "Human owned work",
                "state_id": str(state.id),
                "assignee_ids": [str(create_user.id)],
            },
            format="json",
        )

        bot_client = APIClient()
        bot_client.credentials(HTTP_X_API_KEY=bot_token)
        update_response = bot_client.patch(
            f"{_api_work_items_url(workspace.slug, project.id)}{issue_response.data['id']}/",
            {"description_html": "<p>Should be denied.</p>"},
            format="json",
        )

        assert update_response.status_code == status.HTTP_403_FORBIDDEN

    def test_ai_bot_api_token_can_update_other_fields_of_assigned_issue(
        self, session_client, workspace, project, create_user, monkeypatch
    ):
        _stub_issue_activity_tasks(monkeypatch)
        session_client.force_authenticate(user=create_user)
        create_response = session_client.post(
            _ai_bot_url(workspace.slug),
            {"display_name": "Field Limited AI"},
            format="json",
        )
        bot_id = str(create_response.data["workspace_member"]["member"]["id"])
        bot_token = create_response.data["api_token"]["token"]
        state = State.objects.get(project=project)
        issue_response = session_client.post(
            _issues_url(workspace.slug, project.id),
            {
                "name": "Field limited work",
                "state_id": str(state.id),
                "assignee_ids": [bot_id],
            },
            format="json",
        )

        bot_client = APIClient()
        bot_client.credentials(HTTP_X_API_KEY=bot_token)
        update_response = bot_client.patch(
            f"{_api_work_items_url(workspace.slug, project.id)}{issue_response.data['id']}/",
            {"name": "Renamed by the assigned bot", "priority": "high"},
            format="json",
        )

        assert update_response.status_code == status.HTTP_200_OK, update_response.data
        assert update_response.data["name"] == "Renamed by the assigned bot"
        assert update_response.data["priority"] == "high"
