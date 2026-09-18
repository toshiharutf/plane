# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""App API aggregate of AI token usage per model and completed work item."""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework import status

from plane.db.models import (
    AIUsageRecord,
    Issue,
    Project,
    ProjectMember,
    State,
    User,
    Workspace,
    WorkspaceMember,
)


URL = "/api/workspaces/{slug}/analytics/ai-usage/"


def _project(workspace, user, name, identifier, member=True):
    project = Project.objects.create(
        name=name, identifier=identifier, workspace=workspace, created_by=user, updated_by=user
    )
    if member:
        ProjectMember.objects.create(workspace=workspace, project=project, member=user, role=20, is_active=True)
    return project


def _issue(project, state, user, name, completed_at=None):
    issue = Issue.objects.create(
        name=name, project=project, workspace=project.workspace, state=state, created_by=user, updated_by=user
    )
    if completed_at is not None:
        Issue.objects.filter(id=issue.id).update(completed_at=completed_at)
    return issue


LEGACY_FIELDS = {"cache_creation_input_tokens": "cache_write_1h_tokens", "cache_read_input_tokens": "cache_read_tokens"}


def _usage(issue, model, session_id="", **tokens):
    tokens = {LEGACY_FIELDS.get(field, field): value for field, value in tokens.items()}
    return AIUsageRecord.objects.create(
        issue=issue, project=issue.project, workspace=issue.workspace, model=model, session_id=session_id, **tokens
    )


@pytest.fixture
def project(db, workspace, create_user):
    return _project(workspace, create_user, "AI Usage Project", "AIU")


@pytest.fixture
def done(db, project):
    return State.objects.create(name="Done", group="completed", project=project, workspace=project.workspace)


@pytest.fixture
def todo(db, project):
    return State.objects.create(name="Todo", group="unstarted", project=project, workspace=project.workspace)


def _by_model(response):
    assert response.status_code == status.HTTP_200_OK, response.data
    return {entry["model"]: entry for entry in response.data["models"]}


@pytest.mark.contract
@pytest.mark.django_db
class TestAIUsageAnalytics:
    def test_sums_sessions_per_model_and_work_item(self, session_client, workspace, project, done, create_user):
        issue = _issue(project, done, create_user, "Done task")
        _usage(issue, "claude-opus-5", "s1", input_tokens=10, output_tokens=20, cache_creation_input_tokens=30)
        _usage(issue, "claude-opus-5", "s2", input_tokens=1, output_tokens=2, cache_read_input_tokens=400)
        _usage(issue, "claude-sonnet-5", "s1", input_tokens=5, output_tokens=6)

        models = _by_model(session_client.get(URL.format(slug=workspace.slug)))

        assert list(models) == ["claude-opus-5", "claude-sonnet-5"]
        opus = models["claude-opus-5"]
        assert len(opus["work_items"]) == 1
        item = opus["work_items"][0]
        assert item["id"] == issue.id
        assert item["project_id"] == project.id
        assert item["project_identifier"] == "AIU"
        assert item["sequence_id"] == issue.sequence_id
        assert item["name"] == "Done task"
        assert item["completed_at"] is not None
        assert (
            item["input_tokens"],
            item["output_tokens"],
            item["cache_creation_input_tokens"],
            item["cache_read_input_tokens"],
        ) == (11, 22, 30, 400)
        assert opus["totals"] == {
            "input_tokens": 11,
            "output_tokens": 22,
            "cache_creation_input_tokens": 30,
            "cache_read_input_tokens": 400,
        }
        sonnet_item = models["claude-sonnet-5"]["work_items"][0]
        assert sonnet_item["id"] == issue.id
        assert (sonnet_item["input_tokens"], sonnet_item["output_tokens"]) == (5, 6)

    def test_ac3_reads_ai_usage_records_with_cache_splits_and_cost(
        self, session_client, workspace, project, done, create_user
    ):
        issue = _issue(project, done, create_user, "Priced task")
        _usage(issue, "claude-opus-5", "s1", input_tokens=1_000_000, cache_write_5m_tokens=10, cache_write_1h_tokens=5)
        _usage(issue, "claude-opus-5", "s2", output_tokens=100_000, cache_read_tokens=7)

        item = _by_model(session_client.get(URL.format(slug=workspace.slug)))["claude-opus-5"]["work_items"][0]

        assert (item["cache_creation_input_tokens"], item["cache_read_input_tokens"]) == (15, 7)
        assert item["api_cost_usd"] == sum(record.api_cost_usd for record in AIUsageRecord.objects.filter(issue=issue))

    def test_excludes_open_archived_and_items_without_usage(
        self, session_client, workspace, project, done, todo, create_user
    ):
        _issue(project, done, create_user, "Done without usage")
        open_issue = _issue(project, todo, create_user, "Open")
        _usage(open_issue, "claude-opus-5", input_tokens=100)
        archived = _issue(project, done, create_user, "Archived")
        _usage(archived, "claude-opus-5", input_tokens=100)
        Issue.objects.filter(id=archived.id).update(archived_at=timezone.now().date())
        deleted_usage = _usage(_issue(project, done, create_user, "Deleted usage"), "claude-opus-5", input_tokens=7)
        AIUsageRecord.objects.filter(id=deleted_usage.id).update(deleted_at=timezone.now())
        kept = _issue(project, done, create_user, "Kept")
        _usage(kept, "claude-opus-5", input_tokens=3)

        models = _by_model(session_client.get(URL.format(slug=workspace.slug)))

        assert [item["id"] for item in models["claude-opus-5"]["work_items"]] == [kept.id]
        assert models["claude-opus-5"]["totals"]["input_tokens"] == 3

    def test_orders_work_items_by_completed_at(self, session_client, workspace, project, done, create_user):
        now = timezone.now()
        later = _issue(project, done, create_user, "Later", completed_at=now)
        earlier = _issue(project, done, create_user, "Earlier", completed_at=now - timedelta(days=1))
        for issue in (later, earlier):
            _usage(issue, "claude-opus-5", output_tokens=1)

        models = _by_model(session_client.get(URL.format(slug=workspace.slug)))

        assert [item["id"] for item in models["claude-opus-5"]["work_items"]] == [earlier.id, later.id]

    def test_non_member_projects_hidden_and_project_filter(self, session_client, workspace, project, done, create_user):
        mine = _issue(project, done, create_user, "Mine")
        _usage(mine, "claude-opus-5", input_tokens=1)
        other_project = _project(workspace, create_user, "Other", "OTH", member=False)
        other_done = State.objects.create(name="Done", group="completed", project=other_project, workspace=workspace)
        _usage(_issue(other_project, other_done, create_user, "Hidden"), "claude-opus-5", input_tokens=1)
        second = _project(workspace, create_user, "Second", "SEC")
        second_done = State.objects.create(name="Done", group="completed", project=second, workspace=workspace)
        second_issue = _issue(second, second_done, create_user, "Second")
        _usage(second_issue, "claude-opus-5", input_tokens=1)

        models = _by_model(session_client.get(URL.format(slug=workspace.slug)))
        assert {item["id"] for item in models["claude-opus-5"]["work_items"]} == {mine.id, second_issue.id}

        filtered = _by_model(session_client.get(URL.format(slug=workspace.slug), {"project_ids": str(second.id)}))
        assert [item["id"] for item in filtered["claude-opus-5"]["work_items"]] == [second_issue.id]

    def test_items_without_completed_at_go_last(self, session_client, workspace, project, done, create_user):
        untracked = _issue(project, done, create_user, "Untracked")
        Issue.objects.filter(id=untracked.id).update(completed_at=None)
        tracked = _issue(project, done, create_user, "Tracked", completed_at=timezone.now())
        for issue in (untracked, tracked):
            _usage(issue, "claude-opus-5", output_tokens=1)

        models = _by_model(session_client.get(URL.format(slug=workspace.slug)))

        items = models["claude-opus-5"]["work_items"]
        assert [item["id"] for item in items] == [tracked.id, untracked.id]
        assert items[1]["completed_at"] is None

    def test_totals_sum_all_work_items_of_a_model(self, session_client, workspace, project, done, create_user):
        first = _issue(project, done, create_user, "First")
        second = _issue(project, done, create_user, "Second")
        _usage(first, "claude-opus-5", input_tokens=1, output_tokens=2, cache_creation_input_tokens=3)
        _usage(second, "claude-opus-5", input_tokens=10, cache_read_input_tokens=40)
        _usage(second, "claude-sonnet-5", output_tokens=7)

        models = _by_model(session_client.get(URL.format(slug=workspace.slug)))

        assert len(models["claude-opus-5"]["work_items"]) == 2
        assert models["claude-opus-5"]["totals"] == {
            "input_tokens": 11,
            "output_tokens": 2,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 40,
        }
        assert models["claude-sonnet-5"]["totals"]["output_tokens"] == 7

    def test_excludes_drafts_deleted_items_and_archived_projects(
        self, session_client, workspace, project, done, create_user
    ):
        draft = _issue(project, done, create_user, "Draft")
        Issue.objects.filter(id=draft.id).update(is_draft=True)
        deleted = _issue(project, done, create_user, "Deleted")
        Issue.objects.filter(id=deleted.id).update(deleted_at=timezone.now())
        archived_project = _project(workspace, create_user, "Archived project", "ARC")
        archived_done = State.objects.create(
            name="Done", group="completed", project=archived_project, workspace=workspace
        )
        in_archived_project = _issue(archived_project, archived_done, create_user, "In archived project")
        Project.objects.filter(id=archived_project.id).update(archived_at=timezone.now())
        kept = _issue(project, done, create_user, "Kept")
        for issue in (draft, deleted, in_archived_project, kept):
            _usage(issue, "claude-opus-5", input_tokens=5)

        models = _by_model(session_client.get(URL.format(slug=workspace.slug)))

        assert [item["id"] for item in models["claude-opus-5"]["work_items"]] == [kept.id]
        assert models["claude-opus-5"]["totals"]["input_tokens"] == 5

    def test_multiple_project_ids_and_inactive_membership(self, session_client, workspace, project, done, create_user):
        mine = _issue(project, done, create_user, "Mine")
        _usage(mine, "claude-opus-5", input_tokens=1)
        second = _project(workspace, create_user, "Second", "SEC")
        second_done = State.objects.create(name="Done", group="completed", project=second, workspace=workspace)
        second_issue = _issue(second, second_done, create_user, "Second")
        _usage(second_issue, "claude-opus-5", input_tokens=1)
        left = _project(workspace, create_user, "Left", "LFT")
        left_done = State.objects.create(name="Done", group="completed", project=left, workspace=workspace)
        _usage(_issue(left, left_done, create_user, "Left"), "claude-opus-5", input_tokens=1)
        ProjectMember.objects.filter(project=left, member=create_user).update(is_active=False)

        both = _by_model(
            session_client.get(URL.format(slug=workspace.slug), {"project_ids": f"{project.id},{second.id},{left.id}"})
        )

        assert {item["id"] for item in both["claude-opus-5"]["work_items"]} == {mine.id, second_issue.id}

    def test_other_workspaces_are_not_included(self, session_client, workspace, project, done, create_user):
        other_workspace = Workspace.objects.create(name="Other Workspace", owner=create_user, slug="other-ai-usage")
        WorkspaceMember.objects.create(workspace=other_workspace, member=create_user, role=20)
        other_project = _project(other_workspace, create_user, "Elsewhere", "ELS")
        other_done = State.objects.create(
            name="Done", group="completed", project=other_project, workspace=other_workspace
        )
        _usage(_issue(other_project, other_done, create_user, "Elsewhere"), "claude-opus-5", input_tokens=9)
        mine = _issue(project, done, create_user, "Mine")
        _usage(mine, "claude-opus-5", input_tokens=1)

        models = _by_model(session_client.get(URL.format(slug=workspace.slug)))

        assert [item["id"] for item in models["claude-opus-5"]["work_items"]] == [mine.id]

    def test_empty_response(self, session_client, workspace, project):
        response = session_client.get(URL.format(slug=workspace.slug))
        assert response.status_code == status.HTTP_200_OK
        assert response.data == {"models": []}

    def test_guest_and_outsider_forbidden(self, api_client, workspace, project):
        guest = User.objects.create(email="guest@plane.so", username="guest-ai-usage")
        WorkspaceMember.objects.create(workspace=workspace, member=guest, role=5)
        api_client.force_authenticate(user=guest)
        assert api_client.get(URL.format(slug=workspace.slug)).status_code == status.HTTP_403_FORBIDDEN

        outsider = User.objects.create(email="outsider@plane.so", username="outsider-ai-usage")
        api_client.force_authenticate(user=outsider)
        assert api_client.get(URL.format(slug=workspace.slug)).status_code == status.HTTP_403_FORBIDDEN

    def test_unauthenticated(self, api_client, workspace):
        assert api_client.get(URL.format(slug=workspace.slug)).status_code in (
            status.HTTP_401_UNAUTHORIZED,
            status.HTTP_403_FORBIDDEN,
        )
