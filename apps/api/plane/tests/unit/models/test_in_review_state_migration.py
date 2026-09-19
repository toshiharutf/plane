# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from importlib import import_module

import pytest
from django.apps import apps
from django.utils import timezone

from plane.db.models import DEFAULT_STATES, HumanRequest, Issue, Project, State, StateGroup, Workspace
from plane.db.models.state import IN_REVIEW_STATE_NAME

migration = import_module("plane.db.migrations.0132_in_review_state")


@pytest.fixture
def workspace(create_user):
    return Workspace.objects.create(name="Review Workspace", slug="review-workspace", owner=create_user)


def make_project(workspace, user, identifier, states):
    project = Project.objects.create(name=identifier, identifier=identifier, workspace=workspace, created_by=user)
    for name, group, sequence in states:
        state = State.all_state_objects.create(name=name, group=group, project=project, workspace=workspace)
        # ``State.save`` numbers a new state itself; set the sequence (and the default) the test asks for.
        State.all_state_objects.filter(pk=state.pk).update(sequence=sequence, default=name == "Backlog")
    return project


def issue_in(project, workspace, state_name, name="Item"):
    state = State.objects.get(project=project, name=state_name)
    return Issue.objects.create(name=name, project=project, workspace=workspace, state=state)


def state_names(project):
    return list(State.objects.filter(project=project).values_list("name", flat=True))


def state_rows(project):
    states = State.all_state_objects.filter(project=project)
    return {(s.id, s.name, s.group, s.sequence, s.color, s.default, s.updated_at, s.deleted_at) for s in states}


def issue_rows(project):
    return set(Issue.all_objects.filter(project=project).values_list("id", "state_id", "updated_at", "deleted_at"))


DEFAULTS = [
    ("Backlog", "backlog", 15000),
    ("Todo", "unstarted", 25000),
    ("In Progress", "started", 35000),
    ("Awaiting Human", "started", 42500),
    ("Done", "completed", 45000),
    ("Cancelled", "cancelled", 55000),
]
TRIAGE = ("Triage", "triage", 65000)
MIGRATED = DEFAULTS[:3] + [("In Review", "started", 38750)] + DEFAULTS[3:]

ORDER = ["Backlog", "Todo", "In Progress", "In Review", "Awaiting Human", "Done", "Cancelled"]


@pytest.mark.unit
@pytest.mark.django_db
class TestAddInReviewStateMigration:
    def test_ac1_adds_in_review_between_in_progress_and_the_next_state(self, workspace, create_user):
        project = make_project(workspace, create_user, "INRA", DEFAULTS + [TRIAGE])
        # Outside a request the audit fields stay empty; the new state takes the project's creator.
        Project.objects.filter(pk=project.pk).update(created_by=create_user)

        migration.add_in_review_state(apps, None)

        state = State.objects.get(project=project, name="In Review")
        assert state.group == "started"
        assert state.slug == "in-review"
        assert state.color == migration.IN_REVIEW_STATE["color"]
        assert state.sequence == 38750
        assert state.default is False
        assert state.workspace_id == workspace.id
        assert state.created_by_id == create_user.id
        assert state_names(project) == ORDER
        in_progress = State.objects.get(project=project, name="In Progress")
        done = State.objects.get(project=project, name="Done")
        assert in_progress.sequence < state.sequence < done.sequence

    def test_ac1_writes_no_other_change(self, workspace, create_user):
        project = make_project(workspace, create_user, "INRB", DEFAULTS + [TRIAGE])
        for name, _group, _sequence in DEFAULTS:
            issue_in(project, workspace, name, f"In {name}")
        states_before = state_rows(project)
        issues_before = issue_rows(project)

        migration.add_in_review_state(apps, None)

        added = State.objects.get(project=project, name="In Review")
        assert {row for row in state_rows(project) if row[0] != added.id} == states_before
        assert issue_rows(project) == issues_before
        assert not Issue.all_objects.filter(state=added).exists()

    @pytest.mark.parametrize(
        "custom", [("in review", "unstarted", 60000), ("IN REVIEW", "started", 38000), ("In Review", "triage", 65000)]
    )
    def test_ac1_project_with_a_state_named_in_review_is_left_alone(self, workspace, create_user, custom):
        project = make_project(workspace, create_user, "INRC", DEFAULTS + [custom])
        before = state_rows(project)

        migration.add_in_review_state(apps, None)

        assert state_rows(project) == before

    def test_ac1_is_idempotent(self, workspace, create_user):
        project = make_project(workspace, create_user, "INRD", DEFAULTS)

        migration.add_in_review_state(apps, None)
        once = state_rows(project)
        migration.add_in_review_state(apps, None)

        assert state_rows(project) == once
        assert State.objects.filter(project=project, name__iexact="in review").count() == 1

    def test_ac1_skips_deleted_projects_and_covers_archived_ones(self, workspace, create_user):
        deleted = make_project(workspace, create_user, "INRE", DEFAULTS)
        deleted.delete()
        archived = make_project(workspace, create_user, "INRF", DEFAULTS)
        Project.objects.filter(pk=archived.pk).update(archived_at=timezone.now())

        migration.add_in_review_state(apps, None)

        assert not State.all_state_objects.filter(project_id=deleted.id, name="In Review").exists()
        assert State.all_state_objects.filter(project_id=archived.id, name="In Review", deleted_at=None).exists()

    @pytest.mark.parametrize(
        "states,sequence",
        [
            # After the last started state that is not Awaiting Human.
            ([("Doing", "started", 30000), ("Awaiting Human", "started", 42500), ("Done", "completed", 45000)], 36250),
            # No started state: after the last backlog or unstarted state.
            ([("Backlog", "backlog", 15000), ("Todo", "unstarted", 25000), ("Done", "completed", 45000)], 35000),
            ([("Backlog", "backlog", 15000), ("Awaiting Human", "started", 42500)], 28750),
            # Nothing to come after: before the first state.
            ([("Done", "completed", 45000)], 22500),
            # No following state: plus 15000, triage states do not count.
            ([("Backlog", "backlog", 15000), ("In Progress", "started", 35000), TRIAGE], 50000),
            ([], 15000),
        ],
    )
    def test_ac1_project_without_the_default_states(self, workspace, create_user, states, sequence):
        project = make_project(workspace, create_user, "INRG", states)

        migration.add_in_review_state(apps, None)

        assert State.objects.get(project=project, name="In Review").sequence == sequence

    def test_ac1_soft_deleted_in_review_state_does_not_count(self, workspace, create_user):
        project = make_project(workspace, create_user, "INRH", MIGRATED)
        State.objects.filter(project=project, name="In Review").update(deleted_at=timezone.now())

        migration.add_in_review_state(apps, None)

        assert state_names(project) == ORDER
        assert State.all_state_objects.filter(project=project, name="In Review").count() == 2


@pytest.mark.unit
class TestInReviewDefaults:
    def test_ac1_migration_matches_default_states(self):
        default = next(state for state in DEFAULT_STATES if state["name"] == IN_REVIEW_STATE_NAME)

        assert IN_REVIEW_STATE_NAME == "In Review"
        assert migration.IN_REVIEW_STATE == {key: default[key] for key in ("name", "color", "group")}
        assert default["group"] == StateGroup.STARTED.value
        assert default["sequence"] == 40000
        assert not default.get("default")

    def test_ac1_default_states_order(self):
        names = [state["name"] for state in sorted(DEFAULT_STATES, key=lambda state: state["sequence"])]

        assert names == ORDER + ["Triage"]

    def test_ac1_migration_is_a_data_migration_with_a_reverse(self):
        (operation,) = migration.Migration.operations

        assert migration.Migration.dependencies == [("db", "0131_remove_error_state")]
        assert isinstance(operation, migration.migrations.RunPython)
        assert operation.code is migration.add_in_review_state
        # AC4: the reverse is real, not a noop.
        assert operation.reverse_code is migration.remove_in_review_state


@pytest.mark.unit
@pytest.mark.django_db
class TestRemoveInReviewStateMigration:
    def test_ac4_moves_items_to_in_progress_then_soft_deletes_the_state(self, workspace, create_user):
        project = make_project(workspace, create_user, "INRI", MIGRATED + [TRIAGE])
        in_review = State.objects.get(project=project, name="In Review")
        in_progress = State.objects.get(project=project, name="In Progress")
        live = issue_in(project, workspace, "In Review", "Waits for its merge")
        archived = issue_in(project, workspace, "In Review", "Archived in review")
        Issue.objects.filter(pk=archived.pk).update(archived_at=timezone.now().date())
        deleted = issue_in(project, workspace, "In Review", "Deleted in review")
        Issue.all_objects.filter(pk=deleted.pk).update(deleted_at=timezone.now())
        others = [issue_in(project, workspace, name, f"In {name}") for name, _group, _sequence in DEFAULTS]
        asked = HumanRequest.objects.create(
            issue=others[3], project=project, workspace=workspace, kind="question", question="?", state_before=in_review
        )
        others_before = {(issue.id, issue.state_id) for issue in others}
        states_before = {row for row in state_rows(project) if row[0] != in_review.id}

        migration.remove_in_review_state(apps, None)

        for issue in (live, archived, deleted):
            assert Issue.all_objects.get(pk=issue.pk).state_id == in_progress.id
        assert {(issue.id, Issue.all_objects.get(pk=issue.pk).state_id) for issue in others} == others_before
        assert not State.objects.filter(project=project, name="In Review").exists()
        removed = State.all_state_objects.get(pk=in_review.pk)
        assert removed.deleted_at is not None and removed.default is False
        assert not Issue.all_objects.filter(state_id=in_review.id).exists()
        # History keeps pointing at the (soft-deleted) In Review state.
        assert HumanRequest.objects.get(pk=asked.pk).state_before_id == in_review.id
        assert {row for row in state_rows(project) if row[0] != in_review.id} == states_before
        assert state_names(project) == [name for name in ORDER if name != "In Review"]

    def test_ac4_matches_in_review_case_insensitively(self, workspace, create_user):
        project = make_project(workspace, create_user, "INRJ", DEFAULTS + [("in review", "started", 38750)])
        item = issue_in(project, workspace, "in review")

        migration.remove_in_review_state(apps, None)

        assert not State.objects.filter(project=project, name__iexact="in review").exists()
        assert Issue.objects.get(pk=item.pk).state.name == "In Progress"

    @pytest.mark.parametrize(
        "states,target",
        [
            # No In Progress: the first other started state that is not Awaiting Human.
            (
                [
                    ("Backlog", "backlog", 15000),
                    ("Awaiting Human", "started", 20000),
                    ("Doing", "started", 30000),
                    ("In Review", "started", 40000),
                ],
                "Doing",
            ),
            # No such state either: the project's default state, never Awaiting Human.
            (
                [("Backlog", "backlog", 15000), ("Awaiting Human", "started", 20000), ("In Review", "started", 40000)],
                "Backlog",
            ),
        ],
    )
    def test_ac4_project_without_in_progress(self, workspace, create_user, states, target):
        project = make_project(workspace, create_user, "INRK", states)
        item = issue_in(project, workspace, "In Review")

        migration.remove_in_review_state(apps, None)

        assert Issue.objects.get(pk=item.pk).state.name == target
        assert "In Review" not in state_names(project)

    def test_ac4_default_in_review_state_hands_the_default_to_backlog(self, workspace, create_user):
        project = make_project(workspace, create_user, "INRL", MIGRATED)
        State.objects.filter(project=project).update(default=False)
        State.objects.filter(project=project, name="In Review").update(default=True)

        migration.remove_in_review_state(apps, None)

        assert [state.name for state in State.objects.filter(project=project, default=True)] == ["Backlog"]
        assert State.all_state_objects.get(project=project, name="In Review").default is False

    def test_ac4_items_with_nowhere_to_go_keep_the_state(self, workspace, create_user):
        project = make_project(workspace, create_user, "INRM", [("In Review", "started", 40000), TRIAGE])
        item = issue_in(project, workspace, "In Review")
        empty = make_project(workspace, create_user, "INRN", [("In Review", "started", 40000)])

        migration.remove_in_review_state(apps, None)

        assert state_names(project) == ["In Review"]
        assert Issue.objects.get(pk=item.pk).state.name == "In Review"
        assert state_names(empty) == []

    def test_ac4_triage_state_named_in_review_is_left_alone(self, workspace, create_user):
        project = make_project(workspace, create_user, "INRO", DEFAULTS + [("In Review", "triage", 65000)])
        before = state_rows(project)

        migration.remove_in_review_state(apps, None)

        assert state_rows(project) == before

    def test_ac4_reverse_twice_is_harmless_and_forward_adds_the_state_again(self, workspace, create_user):
        project = make_project(workspace, create_user, "INRP", DEFAULTS)
        migration.add_in_review_state(apps, None)
        item = issue_in(project, workspace, "In Review")

        migration.remove_in_review_state(apps, None)
        once = state_rows(project)
        migration.remove_in_review_state(apps, None)

        assert state_rows(project) == once
        assert Issue.objects.get(pk=item.pk).state.name == "In Progress"

        migration.add_in_review_state(apps, None)

        assert state_names(project) == ORDER
        assert State.objects.get(project=project, name="In Review").sequence == 38750
        assert Issue.objects.get(pk=item.pk).state.name == "In Progress"
