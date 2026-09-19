# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The work item state machine (``plane.utils.work_item_state_rules``).

Backlog -> Todo | Awaiting Human; Todo -> In Progress; In Progress -> Awaiting Human | Done;
Awaiting Human -> Todo; Done is final. AI_AGENT bots follow it on every work item and create
work items only in Backlog, Todo or Awaiting Human. People follow it on work items assigned to
a bot, except that they may cancel them and use custom states freely. An unassigned ``[Human]``
work item has its own bot table: Todo -> Awaiting Human exists only there, and nothing leads out
of Awaiting Human.
"""

from types import SimpleNamespace

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import Issue, IssueAssignee, Project, ProjectMember, State
from plane.utils.work_item_state_rules import (
    HUMAN_TICKET_INITIAL_STATES,
    HUMAN_TICKET_STATE_TRANSITIONS,
    STATE_TRANSITIONS,
    is_unassigned_human_ticket,
    state_key,
    transition_error,
)


def _v1(slug, project_id, suffix=""):
    return f"/api/v1/workspaces/{slug}/projects/{project_id}/{suffix}"


def _web(slug, project_id, suffix=""):
    return f"/api/workspaces/{slug}/projects/{project_id}/{suffix}"


def _create_issue(project, workspace, state, user, name, assignee_ids=()):
    issue = Issue.objects.create(
        name=name, project=project, workspace=workspace, state=state, created_by=user, updated_by=user
    )
    for assignee_id in assignee_ids:
        IssueAssignee.objects.create(issue=issue, assignee_id=assignee_id, project=project, workspace=workspace)
    return issue


@pytest.fixture
def project(db, workspace, create_user):
    project = Project.objects.create(
        name="State Rules", identifier="SRL", workspace=workspace, created_by=create_user, updated_by=create_user
    )
    ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=20, is_active=True)
    return project


@pytest.fixture
def states(db, workspace, project):
    def create(name, group, sequence, default=False):
        return State.objects.create(
            name=name, group=group, sequence=sequence, default=default, project=project, workspace=workspace
        )

    return SimpleNamespace(
        backlog=create("Backlog", "backlog", 15000, default=True),
        todo=create("Todo", "unstarted", 25000),
        in_progress=create("In Progress", "started", 35000),
        awaiting=create("Awaiting Human", "started", 42500),
        done=create("Done", "completed", 45000),
        cancelled=create("Cancelled", "cancelled", 55000),
        review=create("Review", "started", 40000),
    )


@pytest.fixture
def human_api(api_token):
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=api_token.token)
    return client


@pytest.fixture
def human_web(create_user):
    client = APIClient()
    client.force_authenticate(user=create_user)
    return client


@pytest.fixture
def bot(workspace, human_web):
    response = human_web.post(
        f"/api/workspaces/{workspace.slug}/ai-bot-members/", {"display_name": "Rules Bot"}, format="json"
    )
    assert response.status_code == status.HTTP_201_CREATED, response.data
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=response.data["api_token"]["token"])
    return SimpleNamespace(id=str(response.data["workspace_member"]["member"]["id"]), client=client)


def _patch_state(client, workspace, project, issue, state):
    return client.patch(
        _v1(workspace.slug, project.id, f"work-items/{issue.id}/"), {"state": str(state.id)}, format="json"
    )


def _state_of(issue):
    issue.refresh_from_db()
    return issue.state_id


@pytest.mark.unit
class TestStateTable:
    def test_table_matches_the_orchestrator_client(self):
        assert {key: set(value) for key, value in STATE_TRANSITIONS.items()} == {
            "backlog": {"todo", "awaiting human"},
            "todo": {"in progress"},
            "in progress": {"awaiting human", "done"},
            "awaiting human": {"todo"},
            "done": set(),
        }

    def test_human_ticket_table_hands_over_and_never_takes_back(self):
        # AC3: Todo -> Awaiting Human exists only in the table of unassigned [Human] items.
        assert {key: set(value) for key, value in HUMAN_TICKET_STATE_TRANSITIONS.items()} == {
            "backlog": {"todo", "awaiting human"},
            "todo": {"awaiting human"},
            "in progress": {"awaiting human"},
            "awaiting human": set(),
            "done": set(),
        }
        assert "awaiting human" not in STATE_TRANSITIONS["todo"]

    def test_human_ticket_transitions(self):
        todo = SimpleNamespace(id=1, name="Todo", group="unstarted")
        awaiting = SimpleNamespace(id=2, name="Awaiting Human", group="started")
        in_progress = SimpleNamespace(id=3, name="In Progress", group="started")

        # AC3: only the [Human] table has Todo -> Awaiting Human, and only for bots.
        assert transition_error(todo, awaiting, for_bot=True) is not None
        assert transition_error(todo, awaiting, for_bot=True, human_ticket=True) is None
        assert transition_error(todo, awaiting, for_bot=False, human_ticket=True) is not None
        # AC2: nothing leads out of Awaiting Human, and a [Human] item is never started.
        error = transition_error(awaiting, todo, for_bot=True, human_ticket=True)
        assert error.status_code == 400
        assert error.detail["allowed_states"] == []
        assert "[Human]" in error.message and "a person closes it" in error.message
        assert transition_error(todo, in_progress, for_bot=True, human_ticket=True) is not None

    def test_human_ticket_names_never_match_a_closing_state(self):
        # AC4: the names of the table ignore states of the completed and cancelled groups.
        backlog = SimpleNamespace(id=1, name="Backlog", group="backlog")
        for group in ("completed", "cancelled"):
            closing = SimpleNamespace(id=2, name="Awaiting Human", group=group)
            assert transition_error(backlog, closing, for_bot=True, human_ticket=True) is not None
        started = SimpleNamespace(id=3, name="Awaiting Human", group="started")
        assert transition_error(backlog, started, for_bot=True, human_ticket=True) is None

    def test_state_names_compare_case_insensitively(self):
        assert state_key("  Awaiting   HUMAN ") == "awaiting human"

    def test_error_names_both_states_and_the_allowed_targets(self):
        todo = SimpleNamespace(id=1, name="Todo", group="unstarted")
        done = SimpleNamespace(id=2, name="Done", group="completed")

        error = transition_error(todo, done, for_bot=True)

        assert error.status_code == 400
        assert "Todo" in error.message and "Done" in error.message and "In Progress" in error.message
        assert error.detail["allowed_states"] == ["In Progress"]
        assert error.detail["current_state"] == "Todo"
        assert error.detail["requested_state"] == "Done"

    def test_human_exceptions(self):
        done = SimpleNamespace(id=1, name="Done", group="completed")
        cancelled = SimpleNamespace(id=2, name="Cancelled", group="cancelled")
        custom = SimpleNamespace(id=3, name="Review", group="started")
        todo = SimpleNamespace(id=4, name="Todo", group="unstarted")

        assert transition_error(done, cancelled, for_bot=False) is None
        assert transition_error(done, custom, for_bot=False) is None
        assert transition_error(custom, done, for_bot=False) is None
        assert transition_error(done, todo, for_bot=False) is not None
        assert transition_error(done, cancelled, for_bot=True) is not None
        assert transition_error(todo, custom, for_bot=True) is not None


@pytest.mark.contract
@pytest.mark.django_db
class TestBotTransitions:
    @pytest.mark.parametrize(
        "current,target",
        [
            ("backlog", "todo"),
            ("backlog", "awaiting"),
            ("todo", "in_progress"),
            ("in_progress", "awaiting"),
            ("in_progress", "done"),
            ("awaiting", "todo"),
            ("todo", "todo"),
            ("done", "done"),
        ],
    )
    def test_legal_transition(self, workspace, project, states, create_user, bot, current, target):
        issue = _create_issue(project, workspace, getattr(states, current), create_user, "Bot task", [bot.id])

        response = _patch_state(bot.client, workspace, project, issue, getattr(states, target))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert _state_of(issue) == getattr(states, target).id

    @pytest.mark.parametrize(
        "current,target,allowed",
        [
            ("backlog", "in_progress", ["Todo", "Awaiting Human"]),
            ("backlog", "done", ["Todo", "Awaiting Human"]),
            ("todo", "done", ["In Progress"]),
            ("todo", "awaiting", ["In Progress"]),
            ("todo", "backlog", ["In Progress"]),
            ("in_progress", "todo", ["Awaiting Human", "Done"]),
            ("awaiting", "in_progress", ["Todo"]),
            ("awaiting", "done", ["Todo"]),
            ("done", "todo", []),
            ("in_progress", "cancelled", ["Awaiting Human", "Done"]),
            ("in_progress", "review", ["Awaiting Human", "Done"]),
            ("review", "done", []),
        ],
    )
    def test_illegal_transition_is_400(self, workspace, project, states, create_user, bot, current, target, allowed):
        issue = _create_issue(project, workspace, getattr(states, current), create_user, "Bot task", [bot.id])

        response = _patch_state(bot.client, workspace, project, issue, getattr(states, target))

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["current_state"] == getattr(states, current).name
        assert response.data["requested_state"] == getattr(states, target).name
        assert response.data["allowed_states"] == allowed
        assert getattr(states, current).name in response.data["error"]
        assert getattr(states, target).name in response.data["error"]
        assert _state_of(issue) == getattr(states, current).id

    def test_state_names_match_case_insensitively(self, workspace, project, states, create_user, bot):
        State.objects.filter(pk=states.todo.pk).update(name="TODO")
        State.objects.filter(pk=states.in_progress.pk).update(name="in progress")
        issue = _create_issue(project, workspace, states.todo, create_user, "Bot task", [bot.id])

        assert _patch_state(bot.client, workspace, project, issue, states.in_progress).status_code == 200
        assert _patch_state(bot.client, workspace, project, issue, states.todo).status_code == 400

    def test_todo_to_awaiting_human_only_for_unassigned_human_items(self, workspace, project, states, create_user, bot):
        # AC3: the bot's own items in Todo; only the unassigned one named [Human] goes to Awaiting Human.
        def own(name, assignee_ids=()):
            issue = _create_issue(project, workspace, states.todo, None, name, assignee_ids)
            Issue.objects.filter(pk=issue.pk).update(created_by_id=bot.id)
            return issue

        ticket = own("[Human] permission to push to git remote and deploy to vps")
        assert is_unassigned_human_ticket(ticket) is True
        response = _patch_state(bot.client, workspace, project, ticket, states.awaiting)
        assert response.status_code == status.HTTP_200_OK, response.data
        assert _state_of(ticket) == states.awaiting.id

        for issue in (own("Planned task"), own("[Human] assigned to the bot", [bot.id]), own("Bot task", [bot.id])):
            assert is_unassigned_human_ticket(issue) is False
            response = _patch_state(bot.client, workspace, project, issue, states.awaiting)
            assert response.status_code == status.HTTP_400_BAD_REQUEST, (issue.name, response.data)
            assert response.data["allowed_states"] == ["In Progress"]
            assert _state_of(issue) == states.todo.id

    def test_bot_never_moves_an_unassigned_human_item_out_of_awaiting_human(
        self, workspace, project, states, create_user, bot
    ):
        # AC2: Bot PATCH {state: Todo} on its unassigned [Human] ticket in Awaiting Human -> 400.
        ticket = _create_issue(
            project, workspace, states.awaiting, None, "[Human] permission to push to git remote and deploy to vps"
        )
        Issue.objects.filter(pk=ticket.pk).update(created_by_id=bot.id)

        response = _patch_state(bot.client, workspace, project, ticket, states.todo)

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["current_state"] == "Awaiting Human"
        assert response.data["requested_state"] == "Todo"
        assert response.data["allowed_states"] == []
        assert _state_of(ticket) == states.awaiting.id

        # The bot's own work item (assigned to it) still resumes from Awaiting Human.
        task = _create_issue(project, workspace, states.awaiting, create_user, "Bot task", [bot.id])
        assert _patch_state(bot.client, workspace, project, task, states.todo).status_code == 200

    def test_bot_patch_without_state_is_not_checked(self, workspace, project, states, create_user, bot):
        issue = _create_issue(project, workspace, states.review, create_user, "Bot task", [bot.id])

        response = bot.client.patch(
            _v1(workspace.slug, project.id, f"work-items/{issue.id}/"), {"name": "Renamed"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK, response.data


@pytest.mark.contract
@pytest.mark.django_db
class TestBotCreatesWorkItems:
    @pytest.mark.parametrize("initial", ["backlog", "todo", "awaiting"])
    def test_allowed_initial_states(self, workspace, project, states, bot, initial):
        response = bot.client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "New", "state": str(getattr(states, initial).id)},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert Issue.objects.get(pk=response.data["id"]).state_id == getattr(states, initial).id

    @pytest.mark.parametrize("initial", ["in_progress", "done", "cancelled", "review"])
    def test_other_initial_states_are_400(self, workspace, project, states, bot, initial):
        response = bot.client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "New", "state": str(getattr(states, initial).id)},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["allowed_states"] == ["Backlog", "Todo", "Awaiting Human"]
        assert response.data["requested_state"] == getattr(states, initial).name
        assert not Issue.objects.filter(project=project).exists()

    def test_default_state_is_used_when_none_is_given(self, workspace, project, states, bot):
        response = bot.client.post(_v1(workspace.slug, project.id, "work-items/"), {"name": "New"}, format="json")

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert Issue.objects.get(pk=response.data["id"]).state_id == states.backlog.id

    def test_default_state_outside_the_initial_states_is_400(self, workspace, project, states, bot):
        State.objects.filter(pk=states.backlog.pk).update(default=False)
        State.objects.filter(pk=states.review.pk).update(default=True)

        response = bot.client.post(_v1(workspace.slug, project.id, "work-items/"), {"name": "New"}, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["requested_state"] == "Review"

    def test_humans_create_work_items_in_any_state(self, workspace, project, states, human_api):
        response = human_api.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "New", "state": str(states.done.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data


@pytest.mark.contract
@pytest.mark.django_db
class TestBotCreatesHumanWorkItems:
    """A ``[Human]`` work item a bot creates starts in Backlog or Awaiting Human (none given: Awaiting Human)."""

    NAME = "[Human] permission to push to git remote and deploy to vps"

    def _post(self, bot, workspace, project, **body):
        return bot.client.post(
            _v1(workspace.slug, project.id, "work-items/"), {"name": self.NAME, "assignees": [], **body}, format="json"
        )

    def test_initial_states_of_a_human_ticket(self):
        assert HUMAN_TICKET_INITIAL_STATES == {"backlog", "awaiting human"}

    @pytest.mark.parametrize("initial", ["backlog", "awaiting"])
    def test_backlog_and_awaiting_human_are_accepted(self, workspace, project, states, bot, initial):
        response = self._post(bot, workspace, project, state=str(getattr(states, initial).id))

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert Issue.objects.get(pk=response.data["id"]).state_id == getattr(states, initial).id

    def test_no_state_means_awaiting_human_not_the_project_default(self, workspace, project, states, bot):
        # The project default is Backlog; a [Human] ticket still waits for the person.
        for body in ({}, {"state": None}, {"state": ""}):
            response = self._post(bot, workspace, project, **body)

            assert response.status_code == status.HTTP_201_CREATED, (body, response.data)
            issue = Issue.objects.get(pk=response.data["id"])
            assert issue.state_id == states.awaiting.id, body
            assert not IssueAssignee.objects.filter(issue=issue).exists()

    @pytest.mark.parametrize("initial", ["todo", "in_progress", "done", "cancelled", "review"])
    def test_other_initial_states_are_400(self, workspace, project, states, bot, initial):
        response = self._post(bot, workspace, project, state=str(getattr(states, initial).id))

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["allowed_states"] == ["Backlog", "Awaiting Human"]
        assert response.data["requested_state"] == getattr(states, initial).name
        assert "[Human]" in response.data["error"]
        assert not Issue.objects.filter(project=project).exists()

    def test_a_closing_state_named_awaiting_human_is_400(self, workspace, project, states, bot):
        State.objects.filter(pk=states.awaiting.pk).update(name="Waiting")
        forged = State.objects.create(
            name="Awaiting Human", group="completed", sequence=70000, project=project, workspace=workspace
        )

        response = self._post(bot, workspace, project, state=str(forged.id))

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert not Issue.objects.filter(project=project).exists()
        # ... and it is not picked as the default either: the project default (Backlog) applies.
        response = self._post(bot, workspace, project)
        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert Issue.objects.get(pk=response.data["id"]).state_id == states.backlog.id

    def test_project_without_awaiting_human_uses_its_default_state(self, workspace, project, states, bot):
        State.objects.filter(pk=states.awaiting.pk).delete()

        response = self._post(bot, workspace, project)
        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert Issue.objects.get(pk=response.data["id"]).state_id == states.backlog.id

        # A default outside Backlog and Awaiting Human is refused, Todo included.
        State.objects.filter(pk=states.backlog.pk).update(default=False)
        State.objects.filter(pk=states.todo.pk).update(default=True)
        response = self._post(bot, workspace, project)
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["requested_state"] == "Todo"

    def test_other_bot_items_still_start_in_todo(self, workspace, project, states, bot):
        response = bot.client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "Rogue [Human] note", "state": str(states.todo.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data

    def test_humans_create_human_tickets_in_any_state(self, workspace, project, states, create_user, human_api):
        response = human_api.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": self.NAME, "state": str(states.todo.id), "assignees": [str(create_user.id)]},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert Issue.objects.get(pk=response.data["id"]).state_id == states.todo.id


@pytest.mark.contract
@pytest.mark.django_db
class TestHumanTransitions:
    def test_human_on_bot_item_illegal_move_is_400(self, workspace, project, states, create_user, bot, human_api):
        issue = _create_issue(project, workspace, states.todo, create_user, "Bot task", [bot.id, str(create_user.id)])

        response = _patch_state(human_api, workspace, project, issue, states.done)

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["allowed_states"] == ["In Progress"]
        assert "cancel" in response.data["error"]
        assert _state_of(issue) == states.todo.id

    def test_human_on_bot_item_legal_move(self, workspace, project, states, create_user, bot, human_api):
        issue = _create_issue(project, workspace, states.awaiting, create_user, "Bot task", [bot.id])

        response = _patch_state(human_api, workspace, project, issue, states.todo)

        assert response.status_code == status.HTTP_200_OK, response.data
        assert _state_of(issue) == states.todo.id

    @pytest.mark.parametrize("current", ["backlog", "todo", "in_progress", "awaiting", "done"])
    def test_human_cancels_a_bot_item_from_any_state(
        self, workspace, project, states, create_user, bot, human_api, current
    ):
        issue = _create_issue(project, workspace, getattr(states, current), create_user, "Bot task", [bot.id])

        response = _patch_state(human_api, workspace, project, issue, states.cancelled)

        assert response.status_code == status.HTTP_200_OK, response.data
        assert _state_of(issue) == states.cancelled.id

    def test_human_uses_custom_states_on_a_bot_item(self, workspace, project, states, create_user, bot, human_api):
        issue = _create_issue(project, workspace, states.todo, create_user, "Bot task", [bot.id])

        assert _patch_state(human_api, workspace, project, issue, states.review).status_code == 200
        assert _patch_state(human_api, workspace, project, issue, states.done).status_code == 200
        # Out of Cancelled (not one of the five names) is free too.
        assert _patch_state(human_api, workspace, project, issue, states.cancelled).status_code == 200
        assert _patch_state(human_api, workspace, project, issue, states.backlog).status_code == 200

    def test_human_on_item_without_bot_is_unrestricted(self, workspace, project, states, create_user, human_api):
        issue = _create_issue(project, workspace, states.backlog, create_user, "Human task", [str(create_user.id)])

        for target in (states.done, states.todo, states.awaiting, states.in_progress, states.backlog):
            response = _patch_state(human_api, workspace, project, issue, target)
            assert response.status_code == status.HTTP_200_OK, (target.name, response.data)
            assert _state_of(issue) == target.id

    @pytest.mark.parametrize("target", ["done", "cancelled"])
    def test_human_closes_an_unassigned_human_ticket_from_awaiting_human(
        self, workspace, project, states, bot, human_api, human_web, target
    ):
        # A bot-created, unassigned [Human] ticket waits in Awaiting Human; a person closes it directly.
        for client, suffix in ((human_api, "api"), (human_web, "web")):
            ticket = _create_issue(project, workspace, states.awaiting, None, f"[Human] approve release ({suffix})")
            Issue.objects.filter(pk=ticket.pk).update(created_by_id=bot.id)
            if client is human_api:
                response = _patch_state(client, workspace, project, ticket, getattr(states, target))
                assert response.status_code == status.HTTP_200_OK, response.data
            else:
                response = client.patch(
                    _web(workspace.slug, project.id, f"issues/{ticket.id}/"),
                    {"state_id": str(getattr(states, target).id)},
                    format="json",
                )
                assert response.status_code == status.HTTP_204_NO_CONTENT, response.data
            assert _state_of(ticket) == getattr(states, target).id

    def test_assigning_a_bot_in_the_same_request_applies_the_rules(
        self, workspace, project, states, create_user, bot, human_api
    ):
        issue = _create_issue(project, workspace, states.backlog, create_user, "Human task")

        response = human_api.patch(
            _v1(workspace.slug, project.id, f"work-items/{issue.id}/"),
            {"state": str(states.in_progress.id), "assignees": [bot.id]},
            format="json",
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert _state_of(issue) == states.backlog.id
        assert not IssueAssignee.objects.filter(issue=issue).exists()


@pytest.mark.contract
@pytest.mark.django_db
class TestWebAppTransitions:
    """The web app (session auth) writes states through the same serializer: board drags, peek view, bulk edits."""

    def _patch(self, client, workspace, project, issue, body):
        return client.patch(_web(workspace.slug, project.id, f"issues/{issue.id}/"), body, format="json")

    def test_web_patch_on_bot_item_follows_the_table(self, workspace, project, states, create_user, bot, human_web):
        issue = _create_issue(project, workspace, states.backlog, create_user, "Bot task", [bot.id])

        refused = self._patch(human_web, workspace, project, issue, {"state_id": str(states.in_progress.id)})
        assert refused.status_code == status.HTTP_400_BAD_REQUEST, refused.data
        assert refused.data["allowed_states"] == ["Todo", "Awaiting Human"]
        assert _state_of(issue) == states.backlog.id

        allowed = self._patch(human_web, workspace, project, issue, {"state_id": str(states.todo.id)})
        assert allowed.status_code == status.HTTP_204_NO_CONTENT, allowed.data
        assert _state_of(issue) == states.todo.id

        cancelled = self._patch(human_web, workspace, project, issue, {"state_id": str(states.cancelled.id)})
        assert cancelled.status_code == status.HTTP_204_NO_CONTENT, cancelled.data

    def test_web_patch_on_item_without_bot_is_unrestricted(self, workspace, project, states, create_user, human_web):
        issue = _create_issue(project, workspace, states.backlog, create_user, "Human task")

        response = self._patch(human_web, workspace, project, issue, {"state_id": str(states.done.id)})

        assert response.status_code == status.HTTP_204_NO_CONTENT, response.data
        assert _state_of(issue) == states.done.id
