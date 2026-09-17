# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import pytest

from plane.bgtasks.workspace_seed_task import create_project_states
from plane.db.models import Project, State


@pytest.mark.unit
@pytest.mark.django_db
class TestWorkspaceSeedStates:
    def test_seeds_error_state_between_in_progress_and_done(self, workspace, create_user):
        project = Project.objects.create(name="Seed", identifier="SEED", workspace=workspace, created_by=create_user)

        state_map = create_project_states(workspace, {1: project.id}, create_user)

        names = list(State.objects.filter(project=project).values_list("name", flat=True))
        assert names == ["Backlog", "Todo", "In Progress", "Error", "Done", "Cancelled"]
        error = State.objects.get(project=project, name="Error")
        assert error.group == "started"
        assert error.default is False
        assert error.id in state_map.values()
        assert State.objects.filter(project=project, default=True).count() == 1
