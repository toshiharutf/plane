# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from rest_framework import status

from plane.db.models import Issue, Project, ProjectMember, State
from plane.db.models.issue import AI_MODEL_CHOICES, AI_MODEL_NONE, AI_MODEL_VALUES


@pytest.fixture(autouse=True)
def no_background_tasks(monkeypatch):
    """Activity, webhook and notification tasks need a broker; they are not under test here."""
    monkeypatch.setattr("celery.app.task.Task.apply_async", lambda *args, **kwargs: None)


@pytest.fixture
def project(db, workspace, create_user):
    project = Project.objects.create(
        name="AI Model Project",
        identifier="AIM",
        workspace=workspace,
        created_by=create_user,
    )
    ProjectMember.objects.create(project=project, member=create_user, role=20, is_active=True)
    return project


@pytest.fixture
def state(db, workspace, project):
    return State.objects.create(name="Todo", project=project, workspace=workspace, group="backlog", default=True)


@pytest.fixture
def issue(db, workspace, project, state, create_user):
    return Issue.objects.create(
        name="AI model issue", workspace=workspace, project=project, state=state, created_by=create_user
    )


@pytest.mark.unit
def test_ai_model_values_cover_claude_and_openai():
    assert AI_MODEL_VALUES[0] == AI_MODEL_NONE == "None"
    assert len(AI_MODEL_VALUES) == len(set(AI_MODEL_VALUES))
    for value in ("opus-high", "fable-xhigh", "sonnet-medium", "haiku", "gpt-5.5-xhigh", "gpt-5.6-terra-ultra"):
        assert value in AI_MODEL_VALUES


@pytest.mark.unit
def test_ai_model_field_matches_the_value_list():
    field = Issue._meta.get_field("ai_model")
    assert field.default == AI_MODEL_NONE
    assert [value for value, _ in field.choices] == AI_MODEL_VALUES
    assert all(value == label for value, label in AI_MODEL_CHOICES)
    assert max(len(value) for value in AI_MODEL_VALUES) <= field.max_length
    # "haiku" has no effort levels, every other model always carries one
    assert "haiku" in AI_MODEL_VALUES
    assert not {"fable", "opus", "sonnet", "gpt-5.5", "gpt-5.6-terra", "gpt-5.6-luna"} & set(AI_MODEL_VALUES)


@pytest.mark.unit
@pytest.mark.django_db
def test_issue_created_in_code_defaults_to_none(issue):
    issue.refresh_from_db()
    assert issue.ai_model == AI_MODEL_NONE


@pytest.mark.contract
class TestIssueAIModelExternalAPI:
    def list_url(self, slug, project_id):
        return f"/api/v1/workspaces/{slug}/projects/{project_id}/work-items/"

    def detail_url(self, slug, project_id, issue_id):
        return f"/api/v1/workspaces/{slug}/projects/{project_id}/work-items/{issue_id}/"

    @pytest.mark.django_db
    def test_new_work_item_defaults_to_none(self, api_key_client, workspace, project, state):
        response = api_key_client.post(self.list_url(workspace.slug, project.id), {"name": "New"}, format="json")
        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert response.data["ai_model"] == "None"
        assert Issue.objects.get(pk=response.data["id"]).ai_model == "None"

    @pytest.mark.django_db
    def test_create_with_ai_model(self, api_key_client, workspace, project, state):
        response = api_key_client.post(
            self.list_url(workspace.slug, project.id), {"name": "With model", "ai_model": "fable-xhigh"}, format="json"
        )
        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert response.data["ai_model"] == "fable-xhigh"
        assert Issue.objects.get(pk=response.data["id"]).ai_model == "fable-xhigh"

    @pytest.mark.django_db
    def test_create_rejects_unknown_ai_model(self, api_key_client, workspace, project, state):
        response = api_key_client.post(
            self.list_url(workspace.slug, project.id), {"name": "Bad model", "ai_model": "opus"}, format="json"
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert not Issue.objects.filter(name="Bad model").exists()

    @pytest.mark.django_db
    def test_list_includes_ai_model(self, api_key_client, workspace, project, issue):
        Issue.objects.filter(pk=issue.pk).update(ai_model="sonnet-low")
        response = api_key_client.get(self.list_url(workspace.slug, project.id))
        assert response.status_code == status.HTTP_200_OK
        results = response.data["results"] if isinstance(response.data, dict) else response.data
        assert [item["ai_model"] for item in results if str(item["id"]) == str(issue.id)] == ["sonnet-low"]

    @pytest.mark.django_db
    def test_patch_back_to_none(self, api_key_client, workspace, project, issue):
        Issue.objects.filter(pk=issue.pk).update(ai_model="opus-high")
        url = self.detail_url(workspace.slug, project.id, issue.id)
        response = api_key_client.patch(url, {"ai_model": "None"}, format="json")
        assert response.status_code == status.HTTP_200_OK, response.data
        issue.refresh_from_db()
        assert issue.ai_model == "None"

    @pytest.mark.django_db
    def test_other_updates_keep_ai_model(self, api_key_client, workspace, project, issue):
        Issue.objects.filter(pk=issue.pk).update(ai_model="gpt-5.6-terra-ultra")
        url = self.detail_url(workspace.slug, project.id, issue.id)
        response = api_key_client.patch(url, {"name": "Renamed"}, format="json")
        assert response.status_code == status.HTTP_200_OK, response.data
        issue.refresh_from_db()
        assert (issue.name, issue.ai_model) == ("Renamed", "gpt-5.6-terra-ultra")

    @pytest.mark.django_db
    def test_get_and_patch_ai_model(self, api_key_client, workspace, project, issue):
        url = self.detail_url(workspace.slug, project.id, issue.id)
        response = api_key_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert response.data["ai_model"] == "None"

        response = api_key_client.patch(url, {"ai_model": "gpt-5.5-high"}, format="json")
        assert response.status_code == status.HTTP_200_OK, response.data
        issue.refresh_from_db()
        assert issue.ai_model == "gpt-5.5-high"

    @pytest.mark.django_db
    def test_patch_rejects_unknown_ai_model(self, api_key_client, workspace, project, issue):
        url = self.detail_url(workspace.slug, project.id, issue.id)
        response = api_key_client.patch(url, {"ai_model": "opus-turbo"}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        issue.refresh_from_db()
        assert issue.ai_model == "None"


@pytest.mark.contract
class TestIssueAIModelAppAPI:
    def detail_url(self, slug, project_id, issue_id):
        return f"/api/workspaces/{slug}/projects/{project_id}/issues/{issue_id}/"

    @pytest.mark.django_db
    def test_detail_returns_and_updates_ai_model(self, session_client, workspace, project, issue):
        url = self.detail_url(workspace.slug, project.id, issue.id)
        response = session_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert response.data["ai_model"] == "None"

        response = session_client.patch(url, {"ai_model": "opus-high"}, format="json")
        assert response.status_code == status.HTTP_204_NO_CONTENT, getattr(response, "data", None)
        issue.refresh_from_db()
        assert issue.ai_model == "opus-high"

        response = session_client.get(url)
        assert response.data["ai_model"] == "opus-high"

    @pytest.mark.django_db
    def test_list_includes_ai_model(self, session_client, workspace, project, issue):
        response = session_client.get(f"/api/workspaces/{workspace.slug}/projects/{project.id}/issues/")
        assert response.status_code == status.HTTP_200_OK
        results = response.data["results"] if isinstance(response.data, dict) else response.data
        assert any(str(item["id"]) == str(issue.id) and item["ai_model"] == "None" for item in results)

    @pytest.mark.django_db
    def test_create_with_ai_model(self, session_client, workspace, project, state):
        response = session_client.post(
            f"/api/workspaces/{workspace.slug}/projects/{project.id}/issues/",
            {"name": "App create", "ai_model": "gpt-5.6-luna-max"},
            format="json",
        )
        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert response.data["ai_model"] == "gpt-5.6-luna-max"
        assert Issue.objects.get(pk=response.data["id"]).ai_model == "gpt-5.6-luna-max"

    @pytest.mark.django_db
    def test_patch_rejects_unknown_ai_model(self, session_client, workspace, project, issue):
        url = self.detail_url(workspace.slug, project.id, issue.id)
        response = session_client.patch(url, {"ai_model": "opus-turbo"}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        issue.refresh_from_db()
        assert issue.ai_model == "None"
