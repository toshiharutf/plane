# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Public API (``/api/v1``) for reporting AI token usage on a work item."""

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import Issue, IssueAssignee, Project, ProjectMember, State, WorkItemAIUsage


USAGE = {
    "model": "claude-opus-5",
    "input_tokens": 1200,
    "output_tokens": 3400,
    "cache_creation_input_tokens": 56000,
    "cache_read_input_tokens": 789000,
    "session_id": "session-1",
}


TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def _url(slug, project_id, issue_id):
    return f"/api/v1/workspaces/{slug}/projects/{project_id}/work-items/{issue_id}/ai-usage/"


def _create_bot(session_client, workspace, display_name):
    response = session_client.post(
        f"/api/workspaces/{workspace.slug}/ai-bot-members/",
        {"display_name": display_name},
        format="json",
    )
    assert response.status_code == status.HTTP_201_CREATED, response.data
    bot_id = str(response.data["workspace_member"]["member"]["id"])
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=response.data["api_token"]["token"])
    return bot_id, client


def _create_issue(project, workspace, state, user, name, assignee_ids=()):
    issue = Issue.objects.create(
        name=name, project=project, workspace=workspace, state=state, created_by=user, updated_by=user
    )
    for assignee_id in assignee_ids:
        IssueAssignee.objects.create(issue=issue, assignee_id=assignee_id, project=project, workspace=workspace)
    return issue


@pytest.fixture
def project(db, workspace, create_user):
    project = Project.objects.create(
        name="AI Usage Project", identifier="AIU", workspace=workspace, created_by=create_user, updated_by=create_user
    )
    ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=20, is_active=True)
    return project


@pytest.fixture
def state(db, workspace, project):
    return State.objects.create(name="Todo", group="unstarted", project=project, workspace=workspace)


@pytest.fixture
def human_client(api_client, api_token):
    api_client.credentials(HTTP_X_API_KEY=api_token.token)
    return api_client


@pytest.mark.contract
@pytest.mark.django_db
class TestWorkItemAIUsage:
    def test_member_posts_then_lists_usage(self, workspace, project, state, create_user, human_client):
        issue = _create_issue(project, workspace, state, create_user, "Task")
        url = _url(workspace.slug, project.id, issue.id)

        created = human_client.post(url, USAGE, format="json")
        assert created.status_code == status.HTTP_201_CREATED, created.data
        for field, value in USAGE.items():
            assert created.data[field] == value
        assert str(created.data["issue"]) == str(issue.id)
        assert str(created.data["project"]) == str(project.id)
        assert str(created.data["workspace"]) == str(workspace.id)

        listed = human_client.get(url)
        assert listed.status_code == status.HTTP_200_OK
        assert [row["id"] for row in listed.data["results"]] == [created.data["id"]]

    def test_list_payload_creates_one_row_per_model(self, workspace, project, state, create_user, human_client):
        issue = _create_issue(project, workspace, state, create_user, "Task")
        payload = [USAGE, {**USAGE, "model": "claude-haiku-4-5-20251001", "input_tokens": 10}]

        response = human_client.post(_url(workspace.slug, project.id, issue.id), payload, format="json")

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert len(response.data) == 2
        assert set(WorkItemAIUsage.objects.filter(issue=issue).values_list("model", flat=True)) == {
            "claude-opus-5",
            "claude-haiku-4-5-20251001",
        }

    def test_same_session_and_model_updates_instead_of_duplicating(
        self, workspace, project, state, create_user, human_client
    ):
        issue = _create_issue(project, workspace, state, create_user, "Task")
        url = _url(workspace.slug, project.id, issue.id)

        first = human_client.post(url, USAGE, format="json")
        second = human_client.post(url, {**USAGE, "output_tokens": 9999}, format="json")

        assert second.status_code == status.HTTP_201_CREATED, second.data
        assert second.data["id"] == first.data["id"]
        rows = WorkItemAIUsage.objects.filter(issue=issue)
        assert rows.count() == 1
        assert rows.get().output_tokens == 9999

    def test_rows_without_session_id_are_never_merged(self, workspace, project, state, create_user, human_client):
        issue = _create_issue(project, workspace, state, create_user, "Task")
        url = _url(workspace.slug, project.id, issue.id)
        payload = {key: value for key, value in USAGE.items() if key != "session_id"}

        human_client.post(url, payload, format="json")
        human_client.post(url, payload, format="json")

        assert WorkItemAIUsage.objects.filter(issue=issue).count() == 2

    def test_rejects_negative_tokens_and_missing_model(self, workspace, project, state, create_user, human_client):
        issue = _create_issue(project, workspace, state, create_user, "Task")
        url = _url(workspace.slug, project.id, issue.id)

        negative = human_client.post(url, {**USAGE, "cache_read_input_tokens": -1}, format="json")
        missing_model = human_client.post(url, {"input_tokens": 1}, format="json")
        mixed_list = human_client.post(url, [USAGE, {**USAGE, "model": "", "session_id": "s2"}], format="json")

        assert negative.status_code == status.HTTP_400_BAD_REQUEST
        assert "cache_read_input_tokens" in negative.data
        assert missing_model.status_code == status.HTTP_400_BAD_REQUEST
        assert "model" in missing_model.data
        assert mixed_list.status_code == status.HTTP_400_BAD_REQUEST
        assert not WorkItemAIUsage.objects.filter(issue=issue).exists()

    def test_same_session_with_another_model_adds_a_row(self, workspace, project, state, create_user, human_client):
        issue = _create_issue(project, workspace, state, create_user, "Task")
        url = _url(workspace.slug, project.id, issue.id)

        human_client.post(url, USAGE, format="json")
        response = human_client.post(url, {**USAGE, "model": "claude-haiku-4-5", "input_tokens": 7}, format="json")

        assert response.status_code == status.HTTP_201_CREATED, response.data
        rows = WorkItemAIUsage.objects.filter(issue=issue, session_id=USAGE["session_id"])
        assert {row.model: row.input_tokens for row in rows} == {"claude-opus-5": 1200, "claude-haiku-4-5": 7}

    def test_same_session_on_another_work_item_is_not_merged(
        self, workspace, project, state, create_user, human_client
    ):
        first = _create_issue(project, workspace, state, create_user, "First")
        second = _create_issue(project, workspace, state, create_user, "Second")

        human_client.post(_url(workspace.slug, project.id, first.id), USAGE, format="json")
        response = human_client.post(
            _url(workspace.slug, project.id, second.id), {**USAGE, "output_tokens": 1}, format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert WorkItemAIUsage.objects.get(issue=first).output_tokens == USAGE["output_tokens"]
        assert WorkItemAIUsage.objects.get(issue=second).output_tokens == 1

    def test_empty_list_payload_is_rejected(self, workspace, project, state, create_user, human_client):
        issue = _create_issue(project, workspace, state, create_user, "Task")

        response = human_client.post(_url(workspace.slug, project.id, issue.id), [], format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert not WorkItemAIUsage.objects.filter(issue=issue).exists()

    def test_token_counts_default_to_zero(self, workspace, project, state, create_user, human_client):
        issue = _create_issue(project, workspace, state, create_user, "Task")

        response = human_client.post(
            _url(workspace.slug, project.id, issue.id), {"model": "claude-opus-5"}, format="json"
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert [response.data[field] for field in TOKEN_FIELDS] == [0, 0, 0, 0]
        assert response.data["session_id"] == ""

    def test_work_item_of_another_project_returns_404(self, workspace, project, state, create_user, human_client):
        other = Project.objects.create(
            name="Other Project", identifier="AIO", workspace=workspace, created_by=create_user, updated_by=create_user
        )
        ProjectMember.objects.create(workspace=workspace, project=other, member=create_user, role=20, is_active=True)
        issue = _create_issue(project, workspace, state, create_user, "Task")

        response = human_client.post(_url(workspace.slug, other.id, issue.id), USAGE, format="json")

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert not WorkItemAIUsage.objects.filter(issue=issue).exists()

    def test_unknown_work_item_returns_404(self, workspace, project, human_client):
        response = human_client.post(
            _url(workspace.slug, project.id, "00000000-0000-0000-0000-000000000000"), USAGE, format="json"
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_unauthenticated_gets_401(self, workspace, project, state, create_user):
        issue = _create_issue(project, workspace, state, create_user, "Task")
        url = _url(workspace.slug, project.id, issue.id)

        assert APIClient().get(url).status_code == status.HTTP_401_UNAUTHORIZED
        assert APIClient().post(url, USAGE, format="json").status_code == status.HTTP_401_UNAUTHORIZED

    def test_assigned_bot_reports_usage(self, workspace, project, state, create_user, session_client):
        bot_id, bot_client = _create_bot(session_client, workspace, "Usage AI")
        issue = _create_issue(project, workspace, state, create_user, "Bot task", [bot_id])

        response = bot_client.post(_url(workspace.slug, project.id, issue.id), USAGE, format="json")

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert str(WorkItemAIUsage.objects.get(issue=issue).created_by_id) == bot_id

    def test_non_assigned_bot_gets_403_but_can_read(self, workspace, project, state, create_user, session_client):
        _bot_id, bot_client = _create_bot(session_client, workspace, "Other AI")
        issue = _create_issue(project, workspace, state, create_user, "Human task", [str(create_user.id)])
        WorkItemAIUsage.objects.create(issue=issue, project=project, model="claude-opus-5", input_tokens=5)
        url = _url(workspace.slug, project.id, issue.id)

        write = bot_client.post(url, USAGE, format="json")
        read = bot_client.get(url)

        assert write.status_code == status.HTTP_403_FORBIDDEN
        assert WorkItemAIUsage.objects.filter(issue=issue).count() == 1
        assert read.status_code == status.HTTP_200_OK
        assert len(read.data["results"]) == 1
