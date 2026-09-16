# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Contract tests for ``IssueSyncStateEndpoint``.

The web app polls ``/workspaces/<slug>/projects/<project_id>/issues-sync-state/``
and refetches open work item views when the returned token changes. Changes
made by other actors (API keys, AI bots, other users) must move the token;
changes made by the requesting user must not.
"""

from uuid import uuid4

import pytest
from rest_framework import status

from plane.db.models import Issue, IssueActivity, Project, ProjectMember, User

SYNC_URL = "/api/workspaces/{slug}/projects/{project_id}/issues-sync-state/"


@pytest.fixture
def project(db, workspace, create_user):
    project = Project.objects.create(
        name="Sync Project",
        identifier="SYNC",
        workspace=workspace,
        created_by=create_user,
    )
    ProjectMember.objects.create(project=project, member=create_user, workspace=workspace, role=20)
    return project


@pytest.fixture
def other_user(db):
    unique_id = uuid4().hex[:8]
    return User.objects.create(
        email=f"other-{unique_id}@plane.so",
        username=f"other_{unique_id}",
    )


def _issue_updated_by(project, workspace, user):
    issue = Issue(name="Work item", project=project, workspace=workspace)
    issue.save(created_by_id=user.id)
    issue.updated_by = user
    issue.save(disable_auto_set_user=True)
    return issue


def _get(client, workspace, project):
    response = client.get(SYNC_URL.format(slug=workspace.slug, project_id=project.id))
    assert response.status_code == status.HTTP_200_OK
    return response.data


@pytest.mark.contract
class TestIssueSyncState:
    @pytest.mark.django_db
    def test_empty_project(self, session_client, workspace, project):
        data = _get(session_client, workspace, project)
        assert data == {"latest_issue_updated_at": None, "latest_activity_at": None}

    @pytest.mark.django_db
    def test_changes_by_other_actor_move_token(self, session_client, workspace, project, other_user):
        issue = _issue_updated_by(project, workspace, other_user)
        data = _get(session_client, workspace, project)
        assert data["latest_issue_updated_at"] == issue.updated_at

        activity = IssueActivity.objects.create(
            issue=issue, project=project, workspace=workspace, actor=other_user, verb="updated"
        )
        data = _get(session_client, workspace, project)
        assert data["latest_activity_at"] == activity.created_at

    @pytest.mark.django_db
    def test_own_changes_are_ignored(self, session_client, workspace, project, create_user):
        issue = _issue_updated_by(project, workspace, create_user)
        IssueActivity.objects.create(
            issue=issue, project=project, workspace=workspace, actor=create_user, verb="updated"
        )
        data = _get(session_client, workspace, project)
        assert data == {"latest_issue_updated_at": None, "latest_activity_at": None}

    @pytest.mark.django_db
    def test_soft_deleted_issue_moves_token(self, session_client, workspace, project, other_user):
        issue = _issue_updated_by(project, workspace, other_user)
        before = _get(session_client, workspace, project)["latest_issue_updated_at"]
        issue.delete()
        # the soft delete is saved without a request user, so updated_by becomes None
        after = _get(session_client, workspace, project)["latest_issue_updated_at"]
        assert after is not None and after > before

    @pytest.mark.django_db
    def test_non_member_is_forbidden(self, api_client, workspace, project, other_user):
        api_client.force_authenticate(user=other_user)
        response = api_client.get(SYNC_URL.format(slug=workspace.slug, project_id=project.id))
        assert response.status_code == status.HTTP_403_FORBIDDEN
