# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from importlib import import_module

import pytest
from django.apps import apps
from django.utils import timezone

from plane.db.models import (
    AWAITING_HUMAN_STATE_NAME,
    DEFAULT_STATES,
    HumanRequest,
    HumanRequestDecision,
    HumanRequestKind,
    Issue,
    Project,
    State,
    StateGroup,
    Workspace,
)

migration = import_module("plane.db.migrations.0127_awaiting_human")


@pytest.fixture
def workspace(create_user):
    return Workspace.objects.create(name="Human Workspace", slug="human-workspace", owner=create_user)


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
    ("Error", "started", 40000),
    ("Done", "completed", 45000),
    ("Cancelled", "cancelled", 55000),
]

ORDER = ["Backlog", "Todo", "In Progress", "Error", "Awaiting Human", "Done", "Cancelled"]


@pytest.mark.unit
@pytest.mark.django_db
class TestAddAwaitingHumanStateMigration:
    def test_ac2_adds_state_after_in_progress_and_error(self, workspace, create_user):
        project = make_project(workspace, create_user, "AWHA", DEFAULTS)

        migration.add_awaiting_human_state(apps, None)

        state = State.objects.get(project=project, name=AWAITING_HUMAN_STATE_NAME)
        assert state.group == "started"
        assert state.slug == "awaiting-human"
        assert state.sequence == 42500
        assert state.workspace_id == workspace.id
        assert state.default is False
        assert list(State.objects.filter(project=project).values_list("name", flat=True)) == ORDER

    def test_ac2_triage_and_custom_awaiting_human_state_no_duplicate(self, workspace, create_user):
        project = make_project(workspace, create_user, "AWHB", DEFAULTS + [("awaiting human", "unstarted", 60000)])
        State.all_state_objects.create(name="Triage", group="triage", project=project, workspace=workspace)
        before = {(s.id, s.name, s.group, s.sequence) for s in State.all_state_objects.filter(project=project)}

        migration.add_awaiting_human_state(apps, None)
        migration.add_awaiting_human_state(apps, None)

        after = {(s.id, s.name, s.group, s.sequence) for s in State.all_state_objects.filter(project=project)}
        assert after == before

    def test_ac2_triage_state_is_ignored_for_placement(self, workspace, create_user):
        project = make_project(workspace, create_user, "AWHC", DEFAULTS)
        State.all_state_objects.create(name="Triage", group="triage", project=project, workspace=workspace)
        State.all_state_objects.filter(project=project, name="Triage").update(sequence=65000)

        migration.add_awaiting_human_state(apps, None)

        assert State.objects.get(project=project, name=AWAITING_HUMAN_STATE_NAME).sequence == 42500
        triage = State.triage_objects.get(project=project)
        assert (triage.name, triage.sequence) == ("Triage", 65000)

    def test_ac2_changes_nothing_else(self, workspace, create_user):
        project = make_project(workspace, create_user, "AWHD", DEFAULTS)
        before = {(s.id, s.name, s.group, s.sequence, s.color) for s in State.objects.filter(project=project)}

        migration.add_awaiting_human_state(apps, None)

        after = {
            (s.id, s.name, s.group, s.sequence, s.color)
            for s in State.objects.filter(project=project).exclude(name=AWAITING_HUMAN_STATE_NAME)
        }
        assert after == before

    def test_ac2_skips_deleted_projects(self, workspace, create_user):
        project = make_project(workspace, create_user, "AWHE", DEFAULTS)
        project.delete()

        migration.add_awaiting_human_state(apps, None)

        assert not State.all_state_objects.filter(project_id=project.id, name=AWAITING_HUMAN_STATE_NAME).exists()

    def test_ac2_project_without_started_state(self, workspace, create_user):
        project = make_project(
            workspace, create_user, "AWHF", [("Backlog", "backlog", 15000), ("Done", "completed", 45000)]
        )

        migration.add_awaiting_human_state(apps, None)

        assert State.objects.get(project=project, name=AWAITING_HUMAN_STATE_NAME).sequence == 60000

    def test_ac4_reverse_is_noop(self, workspace, create_user):
        project = make_project(workspace, create_user, "AWHG", DEFAULTS)
        migration.add_awaiting_human_state(apps, None)

        reverse = next(op for op in migration.Migration.operations if hasattr(op, "reverse_code")).reverse_code
        assert reverse is migration.migrations.RunPython.noop
        assert State.objects.filter(project=project, name=AWAITING_HUMAN_STATE_NAME).exists()


@pytest.mark.unit
class TestAwaitingHumanDefaults:
    def test_ac1_migration_matches_default_states(self):
        default = next(state for state in DEFAULT_STATES if state["name"] == AWAITING_HUMAN_STATE_NAME)

        assert migration.AWAITING_HUMAN_STATE == {key: default[key] for key in ("name", "color", "group")}
        assert default["group"] == StateGroup.STARTED.value
        assert not default.get("default")

    def test_ac1_default_states_order(self):
        names = [state["name"] for state in sorted(DEFAULT_STATES, key=lambda state: state["sequence"])]

        # 0131_remove_error_state dropped Error from the defaults.
        assert names == [name for name in ORDER if name != "Error"] + ["Triage"]


@pytest.mark.unit
@pytest.mark.django_db
class TestHumanRequestModel:
    def test_ac3_stores_request_and_answer(self, workspace, create_user):
        project = make_project(workspace, create_user, "AWHH", DEFAULTS)
        in_progress = State.objects.get(project=project, name="In Progress")
        issue = Issue.objects.create(name="Ask", project=project, workspace=workspace, state=in_progress)

        request = HumanRequest.objects.create(
            issue=issue,
            project=project,
            workspace=workspace,
            kind=HumanRequestKind.APPROVAL,
            question="Deploy to the server?",
            requested_by=create_user,
            state_before=in_progress,
        )

        # AC3 example: decision empty means open
        assert request.decision == ""
        assert request.is_open
        assert request.answer == ""
        assert request.requested_at is not None
        assert request.resolved_at is None and request.resolved_by is None
        assert list(issue.human_requests.all()) == [request]

        request.decision = HumanRequestDecision.ACCEPT
        request.answer = "Yes, go ahead"
        request.resolved_by = create_user
        request.resolved_at = timezone.now()
        request.save()

        stored = HumanRequest.objects.get(pk=request.pk)
        assert not stored.is_open
        assert (stored.kind, stored.decision, stored.answer) == ("approval", "accept", "Yes, go ahead")
        assert stored.state_before_id == in_progress.id
        assert stored.requested_by_id == stored.resolved_by_id == create_user.id
        assert stored.resolved_at is not None

    def test_ac3_choices(self):
        assert set(HumanRequestKind.values) == {"approval", "question"}
        assert set(HumanRequestDecision.values) == {"accept", "deny", "answered"}
        decision = HumanRequest._meta.get_field("decision")
        assert decision.blank and decision.default == ""
