# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""App API summary of AI token usage shown in the work item properties."""

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import Issue, Project, ProjectMember, State, User, WorkItemAIUsage


def _url(slug, project_id, issue_id):
    return f"/api/workspaces/{slug}/projects/{project_id}/issues/{issue_id}/ai-usage/"


@pytest.fixture
def project(db, workspace, create_user):
    project = Project.objects.create(
        name="AI Usage App", identifier="AIA", workspace=workspace, created_by=create_user, updated_by=create_user
    )
    ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=20, is_active=True)
    return project


@pytest.fixture
def issue(db, workspace, project, create_user):
    state = State.objects.create(name="Todo", group="unstarted", project=project, workspace=workspace)
    return Issue.objects.create(
        name="Task", project=project, workspace=workspace, state=state, created_by=create_user, updated_by=create_user
    )


def _usage(issue, model, session_id, **tokens):
    return WorkItemAIUsage.objects.create(
        issue=issue, project=issue.project, workspace=issue.workspace, model=model, session_id=session_id, **tokens
    )


@pytest.mark.contract
@pytest.mark.django_db
class TestIssueAIUsageSummary:
    def test_sums_totals_and_per_model(self, session_client, workspace, project, issue):
        _usage(issue, "claude-opus-5", "s1", input_tokens=10, output_tokens=20, cache_creation_input_tokens=30)
        _usage(issue, "claude-opus-5", "s2", input_tokens=1, output_tokens=2, cache_read_input_tokens=400)
        _usage(issue, "claude-haiku-4-5-20251001", "s3", input_tokens=5, cache_read_input_tokens=100)

        response = session_client.get(_url(workspace.slug, project.id, issue.id))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["totals"] == {
            "input_tokens": 16,
            "output_tokens": 22,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 500,
        }
        assert response.data["models"] == [
            {
                "model": "claude-haiku-4-5-20251001",
                "input_tokens": 5,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 100,
            },
            {
                "model": "claude-opus-5",
                "input_tokens": 11,
                "output_tokens": 22,
                "cache_creation_input_tokens": 30,
                "cache_read_input_tokens": 400,
            },
        ]

    def test_no_usage_returns_zero_totals(self, session_client, workspace, project, issue):
        response = session_client.get(_url(workspace.slug, project.id, issue.id))

        assert response.status_code == status.HTTP_200_OK
        assert response.data["models"] == []
        assert set(response.data["totals"].values()) == {0}

    def test_other_work_items_are_not_counted(self, session_client, workspace, project, issue, create_user):
        other = Issue.objects.create(
            name="Other", project=project, workspace=workspace, state=issue.state, created_by=create_user
        )
        _usage(other, "claude-opus-5", "s1", input_tokens=999)

        response = session_client.get(_url(workspace.slug, project.id, issue.id))

        assert response.data["totals"]["input_tokens"] == 0

    def test_non_member_is_forbidden(self, workspace, project, issue):
        outsider = User.objects.create(email="outsider-ai-usage@example.com", username="outsider-ai-usage")
        client = APIClient()
        client.force_authenticate(user=outsider)

        assert client.get(_url(workspace.slug, project.id, issue.id)).status_code == status.HTTP_403_FORBIDDEN

    def test_unauthenticated_is_rejected(self, workspace, project, issue):
        response = APIClient().get(_url(workspace.slug, project.id, issue.id))

        assert response.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN)
