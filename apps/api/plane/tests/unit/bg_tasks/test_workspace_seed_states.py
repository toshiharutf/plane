# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest

from plane.bgtasks.workspace_seed_task import create_project_states
from plane.db.models import Project, State


@pytest.mark.unit
@pytest.mark.django_db
class TestWorkspaceSeedStates:
    def test_seeds_the_state_machine_states_without_error(self, workspace, create_user):
        project = Project.objects.create(name="Seed", identifier="SEED", workspace=workspace, created_by=create_user)

        create_project_states(workspace, {1: project.id}, create_user)

        names = list(State.objects.filter(project=project).values_list("name", flat=True))
        assert names == ["Backlog", "Todo", "In Progress", "In Review", "Awaiting Human", "Done", "Cancelled"]
        assert State.objects.filter(project=project, default=True).count() == 1

    def test_ac1_seeds_awaiting_human_state_between_in_progress_and_done(self, workspace, create_user):
        project = Project.objects.create(name="Seed", identifier="SEED", workspace=workspace, created_by=create_user)

        state_map = create_project_states(workspace, {1: project.id}, create_user)

        awaiting = State.objects.get(project=project, name="Awaiting Human")
        assert awaiting.group == "started"
        assert awaiting.default is False
        in_progress = State.objects.get(project=project, name="In Progress")
        done = State.objects.get(project=project, name="Done")
        assert in_progress.sequence < awaiting.sequence < done.sequence
        assert awaiting.id in state_map.values()

    def test_ac1_seeds_in_review_state_between_in_progress_and_awaiting_human(self, workspace, create_user):
        project = Project.objects.create(name="Seed", identifier="SEED", workspace=workspace, created_by=create_user)

        state_map = create_project_states(workspace, {1: project.id}, create_user)

        in_review = State.objects.get(project=project, name="In Review")
        assert in_review.group == "started"
        assert in_review.default is False
        in_progress = State.objects.get(project=project, name="In Progress")
        awaiting = State.objects.get(project=project, name="Awaiting Human")
        done = State.objects.get(project=project, name="Done")
        assert in_progress.sequence < in_review.sequence < awaiting.sequence < done.sequence
        assert in_review.id in state_map.values()
