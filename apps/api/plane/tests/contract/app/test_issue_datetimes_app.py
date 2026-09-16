# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest
from rest_framework import status

from plane.db.models import Issue, Project, ProjectMember, State


@pytest.fixture(autouse=True)
def no_background_tasks(monkeypatch):
    # Activity, webhook and logging tasks need a broker; they are not under test here
    monkeypatch.setattr("celery.app.task.Task.apply_async", lambda *args, **kwargs: None)


@pytest.fixture
def project(db, workspace, create_user):
    project = Project.objects.create(
        name="Datetime API Project", identifier="DTA", workspace=workspace, created_by=create_user
    )
    ProjectMember.objects.create(project=project, member=create_user, role=20, is_active=True)
    return project


@pytest.fixture
def states(project):
    return {
        group: State.objects.create(
            name=group.title(), project=project, workspace=project.workspace, group=group, default=group == "backlog"
        )
        for group in ("backlog", "started", "completed")
    }


@pytest.fixture
def issue(project, states, create_user):
    return Issue.objects.create(
        name="API Item", project=project, workspace=project.workspace, state=states["backlog"], created_by=create_user
    )


def _app_url(issue):
    return f"/api/workspaces/{issue.workspace.slug}/projects/{issue.project_id}/issues/{issue.id}/"


def _v1_url(issue):
    return f"/api/v1/workspaces/{issue.workspace.slug}/projects/{issue.project_id}/work-items/{issue.id}/"


@pytest.mark.contract
class TestIssueDatetimesAppAPI:
    @pytest.mark.django_db
    def test_patch_datetime_updates_date(self, session_client, issue):
        response = session_client.patch(
            _app_url(issue), {"start_datetime": "2026-04-01T09:15:30Z"}, format="json"
        )
        assert response.status_code == status.HTTP_204_NO_CONTENT, response.data
        data = session_client.get(_app_url(issue)).data
        assert data["start_datetime"].startswith("2026-04-01T09:15:30")
        assert str(data["start_date"]) == "2026-04-01"

    @pytest.mark.django_db
    def test_patch_date_only_updates_datetime(self, session_client, issue):
        response = session_client.patch(_app_url(issue), {"start_date": "2026-06-07"}, format="json")
        assert response.status_code == status.HTTP_204_NO_CONTENT, response.data
        issue.refresh_from_db()
        assert issue.start_datetime is not None
        assert issue.start_datetime.date().isoformat() == "2026-06-07"

    @pytest.mark.django_db
    def test_start_after_target_rejected(self, session_client, issue):
        response = session_client.patch(
            _app_url(issue),
            {"start_datetime": "2026-04-02T10:00:00Z", "target_datetime": "2026-04-02T09:00:00Z"},
            format="json",
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.django_db
    def test_state_changes_fill_datetimes(self, session_client, issue, states):
        url = _app_url(issue)
        assert session_client.patch(url, {"state_id": str(states["started"].id)}, format="json").status_code == 204
        data = session_client.get(url).data
        assert data["start_datetime"] is not None and data["target_datetime"] is None

        assert session_client.patch(url, {"state_id": str(states["completed"].id)}, format="json").status_code == 204
        data = session_client.get(url).data
        assert data["target_datetime"] is not None
        assert data["start_date"] is not None and data["target_date"] is not None

        listed = session_client.get(f"/api/workspaces/{issue.workspace.slug}/projects/{issue.project_id}/issues/")
        assert listed.status_code == 200

    @pytest.mark.django_db
    def test_v1_api_returns_datetimes(self, api_key_client, issue, states):
        url = _v1_url(issue)
        response = api_key_client.patch(url, {"state": str(states["started"].id)}, format="json")
        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["start_datetime"] is not None
        data = api_key_client.get(url).data
        assert data["start_datetime"] is not None
        assert "target_datetime" in data
