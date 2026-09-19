# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from importlib import import_module

import pytest
from django.apps import apps

from plane.db.models import DEFAULT_STATES, Project, State, Workspace

migration = import_module("plane.db.migrations.0126_add_error_state")


@pytest.fixture
def workspace(create_user):
    return Workspace.objects.create(name="Error Workspace", slug="error-workspace", owner=create_user)


def make_project(workspace, user, identifier, states):
    project = Project.objects.create(name=identifier, identifier=identifier, workspace=workspace, created_by=user)
    for name, group, sequence in states:
        State.objects.create(name=name, group=group, project=project, workspace=workspace)
        State.objects.filter(project=project, name=name).update(sequence=sequence)
    return project


DEFAULTS = [
    ("Backlog", "backlog", 15000),
    ("Todo", "unstarted", 25000),
    ("In Progress", "started", 35000),
    ("Done", "completed", 45000),
    ("Cancelled", "cancelled", 55000),
]


@pytest.mark.unit
@pytest.mark.django_db
class TestAddErrorStateMigration:
    def test_adds_error_state_between_started_and_completed(self, workspace, create_user):
        project = make_project(workspace, create_user, "ERRA", DEFAULTS)

        migration.add_error_state(apps, None)

        error = State.objects.get(project=project, name="Error")
        assert error.group == "started"
        assert error.slug == "error"
        assert error.sequence == 40000
        assert error.workspace_id == workspace.id
        assert error.default is False
        names = list(State.objects.filter(project=project).values_list("name", flat=True))
        assert names == ["Backlog", "Todo", "In Progress", "Error", "Done", "Cancelled"]

    def test_is_idempotent_and_keeps_existing_error_state(self, workspace, create_user):
        project = make_project(workspace, create_user, "ERRB", DEFAULTS + [("error", "cancelled", 60000)])

        migration.add_error_state(apps, None)
        migration.add_error_state(apps, None)

        assert State.objects.filter(project=project, name__iexact="error").count() == 1
        assert State.objects.get(project=project, name__iexact="error").group == "cancelled"

    def test_project_without_started_state(self, workspace, create_user):
        project = make_project(
            workspace, create_user, "ERRC", [("Backlog", "backlog", 15000), ("Done", "completed", 45000)]
        )

        migration.add_error_state(apps, None)

        assert State.objects.get(project=project, name="Error").sequence == 60000

    def test_skips_deleted_projects(self, workspace, create_user):
        project = make_project(workspace, create_user, "ERRD", DEFAULTS)
        project.delete()

        migration.add_error_state(apps, None)

        assert not State.all_state_objects.filter(project_id=project.id, name="Error").exists()

    def test_started_state_is_last(self, workspace, create_user):
        project = make_project(
            workspace, create_user, "ERRE", [("Backlog", "backlog", 15000), ("In Progress", "started", 35000)]
        )

        migration.add_error_state(apps, None)

        assert State.objects.get(project=project, name="Error").sequence == 50000

    def test_placed_after_the_last_started_state(self, workspace, create_user):
        project = make_project(
            workspace,
            create_user,
            "ERRF",
            [
                ("In Progress", "started", 35000),
                ("In Review", "started", 40000),
                ("Done", "completed", 45000),
            ],
        )

        migration.add_error_state(apps, None)

        names = list(State.objects.filter(project=project).values_list("name", flat=True))
        assert names == ["In Progress", "In Review", "Error", "Done"]
        assert State.objects.get(project=project, name="Error").sequence == 42500

    def test_soft_deleted_error_state_does_not_count(self, workspace, create_user):
        project = make_project(workspace, create_user, "ERRG", DEFAULTS + [("Error", "cancelled", 60000)])
        State.objects.get(project=project, name="Error").delete()

        migration.add_error_state(apps, None)

        error = State.objects.get(project=project, name="Error")
        assert error.group == "started"
        assert error.sequence == 40000

    def test_project_with_triage_state(self, workspace, create_user):
        project = make_project(workspace, create_user, "ERRI", DEFAULTS)
        State.all_state_objects.create(name="Triage", group="triage", project=project, workspace=workspace)
        State.all_state_objects.filter(project=project, name="Triage").update(sequence=65000)

        migration.add_error_state(apps, None)

        assert State.objects.get(project=project, name="Error").sequence == 40000
        triage = State.triage_objects.get(project=project)
        assert (triage.name, triage.sequence) == ("Triage", 65000)

    def test_project_without_states(self, workspace, create_user):
        project = make_project(workspace, create_user, "ERRH", [])

        migration.add_error_state(apps, None)

        assert State.objects.get(project=project, name="Error").sequence == 15000


@pytest.mark.unit
class TestErrorStateDefaults:
    def test_error_is_no_longer_a_default_state(self):
        # 0131_remove_error_state removed it; 0126 stays as history.
        assert "Error" not in [state["name"] for state in DEFAULT_STATES]

    def test_default_states_order(self):
        names = [state["name"] for state in sorted(DEFAULT_STATES, key=lambda state: state["sequence"])]

        assert names == ["Backlog", "Todo", "In Progress", "In Review", "Awaiting Human", "Done", "Cancelled", "Triage"]
