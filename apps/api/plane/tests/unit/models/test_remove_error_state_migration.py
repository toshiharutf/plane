# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from importlib import import_module

import pytest
from django.apps import apps
from django.utils import timezone

from plane.db.models import HumanRequest, Issue, Project, State, Workspace

migration = import_module("plane.db.migrations.0131_remove_error_state")


@pytest.fixture
def workspace(create_user):
    return Workspace.objects.create(name="No Error Workspace", slug="no-error-workspace", owner=create_user)


def make_project(workspace, user, identifier, names):
    project = Project.objects.create(name=identifier, identifier=identifier, workspace=workspace, created_by=user)
    groups = {
        "Backlog": "backlog",
        "Todo": "unstarted",
        "In Progress": "started",
        "Error": "started",
        "error": "started",
        "Awaiting Human": "started",
        "Done": "completed",
    }
    for sequence, name in enumerate(names, start=1):
        State.objects.create(
            name=name,
            group=groups[name],
            project=project,
            workspace=workspace,
            default=name == "Backlog",
            sequence=sequence * 10000,
        )
    return project


def issue_in(project, workspace, state_name, name="Item"):
    state = State.objects.get(project=project, name=state_name)
    return Issue.objects.create(name=name, project=project, workspace=workspace, state=state)


ALL = ["Backlog", "Todo", "In Progress", "Error", "Awaiting Human", "Done"]


@pytest.mark.unit
@pytest.mark.django_db
class TestRemoveErrorStateMigration:
    def test_moves_items_to_awaiting_human_and_soft_deletes_error(self, workspace, create_user):
        project = make_project(workspace, create_user, "NOEA", ALL)
        error = State.objects.get(project=project, name="Error")
        awaiting = State.objects.get(project=project, name="Awaiting Human")
        failed = issue_in(project, workspace, "Error", "Failed run")
        archived = issue_in(project, workspace, "Error", "Archived failed run")
        Issue.objects.filter(pk=archived.pk).update(archived_at=timezone.now().date())
        deleted = issue_in(project, workspace, "Error", "Deleted failed run")
        Issue.all_objects.filter(pk=deleted.pk).update(deleted_at=timezone.now())
        other = issue_in(project, workspace, "In Progress", "Running")
        asked = HumanRequest.objects.create(
            issue=other, project=project, workspace=workspace, kind="question", question="?", state_before=error
        )

        migration.remove_error_state(apps, None)

        for issue in (failed, archived, deleted):
            assert Issue.all_objects.get(pk=issue.pk).state_id == awaiting.id
        assert Issue.objects.get(pk=other.pk).state.name == "In Progress"
        assert not State.objects.filter(project=project, name="Error").exists()
        assert State.all_state_objects.get(pk=error.pk).deleted_at is not None
        # History keeps pointing at the (soft-deleted) Error state.
        assert HumanRequest.objects.get(pk=asked.pk).state_before_id == error.id
        names = list(State.objects.filter(project=project).values_list("name", flat=True))
        assert names == ["Backlog", "Todo", "In Progress", "Awaiting Human", "Done"]

    def test_matches_error_case_insensitively(self, workspace, create_user):
        project = make_project(workspace, create_user, "NOEB", ["Backlog", "error", "Awaiting Human"])
        item = issue_in(project, workspace, "error")

        migration.remove_error_state(apps, None)

        assert not State.objects.filter(project=project, name__iexact="error").exists()
        assert Issue.objects.get(pk=item.pk).state.name == "Awaiting Human"

    def test_default_error_state_hands_the_default_to_backlog(self, workspace, create_user):
        project = make_project(workspace, create_user, "NOEC", ALL)
        State.objects.filter(project=project).update(default=False)
        State.objects.filter(project=project, name="Error").update(default=True)

        migration.remove_error_state(apps, None)

        defaults = State.objects.filter(project=project, default=True)
        assert [state.name for state in defaults] == ["Backlog"]
        assert State.all_state_objects.get(project=project, name="Error").default is False

    def test_project_without_awaiting_human_keeps_an_error_state_in_use(self, workspace, create_user):
        project = make_project(workspace, create_user, "NOED", ["Backlog", "In Progress", "Error", "Done"])
        item = issue_in(project, workspace, "Error")

        migration.remove_error_state(apps, None)

        assert State.objects.filter(project=project, name="Error").exists()
        assert Issue.objects.get(pk=item.pk).state.name == "Error"
        assert not State.objects.filter(project=project, name="Awaiting Human").exists()

    def test_project_without_awaiting_human_drops_an_empty_error_state(self, workspace, create_user):
        project = make_project(workspace, create_user, "NOEE", ["Backlog", "Error", "Done"])

        migration.remove_error_state(apps, None)

        assert not State.objects.filter(project=project, name="Error").exists()
        assert list(State.objects.filter(project=project).values_list("name", flat=True)) == ["Backlog", "Done"]

    def test_is_idempotent_and_leaves_projects_without_error_alone(self, workspace, create_user):
        project = make_project(workspace, create_user, "NOEF", ["Backlog", "Todo", "Awaiting Human"])
        item = issue_in(project, workspace, "Todo")

        migration.remove_error_state(apps, None)
        migration.remove_error_state(apps, None)

        assert State.objects.filter(project=project).count() == 3
        assert Issue.objects.get(pk=item.pk).state.name == "Todo"

    def test_reverse_is_a_noop(self):
        reverse = migration.Migration.operations[0].reverse_code
        assert reverse is migration.migrations.RunPython.noop
