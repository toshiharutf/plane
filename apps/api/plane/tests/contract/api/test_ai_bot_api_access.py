# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Public API (``/api/v1``) access rules for workspace AI_AGENT bots.

A bot authenticated with its API key can:

- read every work item, comment, relation and activity in the workspace,
- update only work items it is assigned to,
- hand unassigned ``[Human]`` work items it created to a person: Backlog -> Todo or Awaiting Human
  and Todo -> Awaiting Human (nothing else on them, and never out of Awaiting Human),
- create sub work items under work items it is assigned to,
- create unassigned top-level work items that ask a human for an action,
- comment on any work item and edit only its own comments,
- add links to work items it is assigned to and edit or delete only its own links,
- create relations between any work items,
- read public pages, create pages, and update only pages it owns,
- create top-level work items that are unassigned or assigned only to itself
  (a ``[Human]`` ticket must stay unassigned),
- update the planning fields of work items it created, never to a Done state,
  never assigning anyone but itself,
- read, create and update cycles and modules and add work items to them, but
  never delete or archive them,
- read and create labels, but never update or delete them,
- create the project's estimate and its points when none exists, and make an
  estimate it created the project's estimate.

Every state a bot sets also follows the work item state machine
(``plane.utils.work_item_state_rules``, see ``test_work_item_state_rules.py``).
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import (
    AWAITING_HUMAN_STATE_NAME,
    Cycle,
    CycleIssue,
    Estimate,
    EstimatePoint,
    Issue,
    IssueAssignee,
    IssueComment,
    IssueLink,
    IssueRelation,
    Label,
    Module,
    ModuleIssue,
    Page,
    Project,
    ProjectMember,
    ProjectPage,
    State,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.db.models.api import APIToken


def _v1(slug, project_id, suffix=""):
    return f"/api/v1/workspaces/{slug}/projects/{project_id}/{suffix}"


def _stub_tasks(monkeypatch):
    for module in ("issue", "cycle", "module", "project"):
        monkeypatch.setattr(f"plane.api.views.{module}.model_activity.delay", lambda **kwargs: None)
    for module in ("issue", "cycle", "module"):
        monkeypatch.setattr(f"plane.api.views.{module}.issue_activity.delay", lambda **kwargs: None)
    monkeypatch.setattr("plane.api.views.issue.crawl_work_item_link_title.delay", lambda *args, **kwargs: None)
    monkeypatch.setattr("plane.api.views.page.page_transaction.delay", lambda **kwargs: None)


def _create_bot(session_client, workspace, display_name):
    response = session_client.post(
        f"/api/workspaces/{workspace.slug}/ai-bot-members/",
        {"display_name": display_name},
        format="json",
    )
    assert response.status_code == status.HTTP_201_CREATED, response.data
    bot_id = str(response.data["workspace_member"]["member"]["id"])
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=response.data["api_token"]["token"])
    return bot_id, client


def _create_issue(project, workspace, state, user, name, assignee_ids=()):
    issue = Issue.objects.create(
        name=name,
        project=project,
        workspace=workspace,
        state=state,
        created_by=user,
        updated_by=user,
    )
    for assignee_id in assignee_ids:
        IssueAssignee.objects.create(issue=issue, assignee_id=assignee_id, project=project, workspace=workspace)
    return issue


def _create_issue_by(creator_id, project, workspace, state, name, assignee_ids=()):
    """Work item whose ``created_by`` is ``creator_id`` (``save`` resets it to the request user, none in tests)."""
    issue = _create_issue(project, workspace, state, None, name, assignee_ids)
    Issue.objects.filter(pk=issue.pk).update(created_by_id=creator_id)
    return issue


def _create_page(project, workspace, owner, name, access=Page.PUBLIC_ACCESS, description_html="<p>doc</p>"):
    page = Page.objects.create(
        name=name,
        workspace=workspace,
        owned_by=owner,
        access=access,
        description_html=description_html,
        description_binary=b"binary",
        description_json={"type": "doc"},
        created_by=owner,
        updated_by=owner,
    )
    ProjectPage.objects.create(project=project, page=page, workspace=workspace)
    return page


@pytest.fixture
def project(db, workspace, create_user):
    project = Project.objects.create(
        name="AI Access Project",
        identifier="AIX",
        workspace=workspace,
        created_by=create_user,
        updated_by=create_user,
    )
    ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=20, is_active=True)
    return project


@pytest.fixture
def state(db, workspace, project):
    return State.objects.create(name="Todo", group="unstarted", project=project, workspace=workspace)


@pytest.fixture
def started_state(db, workspace, project):
    return State.objects.create(name="In Progress", group="started", project=project, workspace=workspace)


@pytest.fixture
def awaiting_state(db, workspace, project):
    """The state a ``[Human]`` ticket waits in; a bot creates such a ticket only here or in Backlog."""
    return State.objects.create(name=AWAITING_HUMAN_STATE_NAME, group="started", project=project, workspace=workspace)


@pytest.fixture
def bot(session_client, workspace):
    """(bot user id, API-key authenticated client) for a workspace AI_AGENT bot."""
    return _create_bot(session_client, workspace, "Executor AI")


@pytest.fixture
def human_client(api_client, api_token):
    """API-key authenticated client for the human project admin."""
    api_client.credentials(HTTP_X_API_KEY=api_token.token)
    return api_client


@pytest.mark.contract
@pytest.mark.django_db
class TestAIBotWorkItemAccess:
    def test_bot_reads_all_work_items_including_human_only_ones(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        own = _create_issue(project, workspace, state, create_user, "Bot task", [bot_id])
        human = _create_issue(project, workspace, state, create_user, "Human task", [str(create_user.id)])

        response = bot_client.get(_v1(workspace.slug, project.id, "work-items/"))

        assert response.status_code == status.HTTP_200_OK
        ids = {str(item["id"]) for item in response.data["results"]}
        assert {str(own.id), str(human.id)} <= ids

        detail = bot_client.get(_v1(workspace.slug, project.id, f"work-items/{human.id}/"))
        assert detail.status_code == status.HTTP_200_OK

    def test_bot_updates_any_field_of_assigned_work_item(
        self, workspace, project, state, started_state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        issue = _create_issue(project, workspace, state, create_user, "Bot task", [bot_id])

        response = bot_client.patch(
            _v1(workspace.slug, project.id, f"work-items/{issue.id}/"),
            {"name": "Bot task (refined)", "priority": "high", "state": str(started_state.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        issue.refresh_from_db()
        assert issue.name == "Bot task (refined)"
        assert issue.priority == "high"
        assert issue.state_id == started_state.id

    def test_bot_updates_work_item_shared_with_a_human(self, workspace, project, state, create_user, bot, monkeypatch):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        issue = _create_issue(project, workspace, state, create_user, "Pair task", [bot_id, str(create_user.id)])

        response = bot_client.patch(
            _v1(workspace.slug, project.id, f"work-items/{issue.id}/"),
            {"description_html": "<p>Progress note</p>"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data

    def test_bot_cannot_update_unassigned_work_item(self, workspace, project, state, create_user, bot, monkeypatch):
        _stub_tasks(monkeypatch)
        _bot_id, bot_client = bot
        human = _create_issue(project, workspace, state, create_user, "Human task", [str(create_user.id)])
        unassigned = _create_issue(project, workspace, state, create_user, "Nobody's task")

        for issue in (human, unassigned):
            response = bot_client.patch(
                _v1(workspace.slug, project.id, f"work-items/{issue.id}/"),
                {"name": "Hijacked"},
                format="json",
            )
            assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_bot_hands_its_own_unassigned_human_item_to_awaiting_human_from_backlog_and_todo(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        # AC1: Backlog -> Awaiting Human, Backlog -> Todo and Todo -> Awaiting Human are accepted.
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        backlog = State.objects.create(name="Backlog", group="backlog", project=project, workspace=workspace)
        awaiting = State.objects.create(
            name=AWAITING_HUMAN_STATE_NAME, group="started", project=project, workspace=workspace
        )
        url = lambda issue: _v1(workspace.slug, project.id, f"work-items/{issue.id}/")  # noqa: E731

        gate = _create_issue_by(bot_id, project, workspace, backlog, "[Human] Approve skill change: x (T27)")
        response = bot_client.patch(url(gate), {"state": str(awaiting.id)}, format="json")
        assert response.status_code == status.HTTP_200_OK, response.data
        gate.refresh_from_db()
        assert gate.state_id == awaiting.id

        ticket = _create_issue_by(
            bot_id, project, workspace, backlog, "[Human] permission to push to git remote and deploy to vps"
        )
        for target in (state, awaiting):
            response = bot_client.patch(url(ticket), {"state": str(target.id)}, format="json")
            assert response.status_code == status.HTTP_200_OK, (target.name, response.data)
            ticket.refresh_from_db()
            assert ticket.state_id == target.id

        # AC2: the same ticket, now with the person, does not come back to Todo.
        response = bot_client.patch(url(ticket), {"state": str(state.id)}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        ticket.refresh_from_db()
        assert ticket.state_id == awaiting.id

    def test_bot_cannot_take_its_human_item_back_out_of_awaiting_human(
        self, workspace, project, state, started_state, create_user, bot, monkeypatch
    ):
        # AC2: once handed to a person, no bot move leads out of Awaiting Human.
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        backlog = State.objects.create(name="Backlog", group="backlog", project=project, workspace=workspace)
        done = State.objects.create(name="Done", group="completed", project=project, workspace=workspace)
        cancelled = State.objects.create(name="Cancelled", group="cancelled", project=project, workspace=workspace)
        awaiting = State.objects.create(
            name=AWAITING_HUMAN_STATE_NAME, group="started", project=project, workspace=workspace
        )
        ticket = _create_issue_by(
            bot_id, project, workspace, awaiting, "[Human] permission to push to git remote and deploy to vps"
        )
        url = _v1(workspace.slug, project.id, f"work-items/{ticket.id}/")

        for target in (state, backlog):
            response = bot_client.patch(url, {"state": str(target.id)}, format="json")
            assert response.status_code == status.HTTP_400_BAD_REQUEST, (target.name, response.data)
            assert response.data["current_state"] == AWAITING_HUMAN_STATE_NAME
            assert response.data["allowed_states"] == []
        for target in (started_state, done, cancelled):
            response = bot_client.patch(url, {"state": str(target.id)}, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, target.name
        ticket.refresh_from_db()
        assert ticket.state_id == awaiting.id

    def test_bot_gate_move_refuses_other_states_fields_assignees_and_creators(
        self, workspace, project, state, started_state, create_user, bot, monkeypatch
    ):
        # AC2: only a bare {state} to a gate state, on the bot's own unassigned item; never In Progress,
        # the completed group or the cancelled group, from any state.
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        backlog = State.objects.create(name="Backlog", group="backlog", project=project, workspace=workspace)
        done = State.objects.create(name="Done", group="completed", project=project, workspace=workspace)
        cancelled = State.objects.create(name="Cancelled", group="cancelled", project=project, workspace=workspace)
        review = State.objects.create(name="Review", group="started", project=project, workspace=workspace)
        awaiting = State.objects.create(
            name=AWAITING_HUMAN_STATE_NAME, group="started", project=project, workspace=workspace
        )
        other_project = Project.objects.create(
            name="Other Project", identifier="OTH", workspace=workspace, created_by=create_user, updated_by=create_user
        )
        other_awaiting = State.objects.create(
            name=AWAITING_HUMAN_STATE_NAME, group="started", project=other_project, workspace=workspace
        )
        url = lambda issue: _v1(workspace.slug, project.id, f"work-items/{issue.id}/")  # noqa: E731

        for current in (backlog, state):
            ticket = _create_issue_by(bot_id, project, workspace, current, f"[Human] Approve ({current.name})")
            for body in (
                {"state": str(done.id)},
                {"state": str(cancelled.id)},
                {"state": str(started_state.id)},
                {"state": str(review.id)},
                {"state": str(other_awaiting.id)},
                {"state": str(awaiting.id), "name": "x"},
                {"state": str(awaiting.id), "assignees": [bot_id]},
            ):
                response = bot_client.patch(url(ticket), body, format="json")
                assert response.status_code == status.HTTP_403_FORBIDDEN, (current.name, body)
            ticket.refresh_from_db()
            assert ticket.state_id == current.id
            assert ticket.name == f"[Human] Approve ({current.name})"

        own_ticket = _create_issue_by(bot_id, project, workspace, state, "[Human] approve")
        assigned = _create_issue_by(bot_id, project, workspace, state, "[Human] assigned", [str(create_user.id)])
        human_created = _create_issue_by(create_user.id, project, workspace, state, "[Human] by a person")
        for issue in (assigned, human_created):
            response = bot_client.patch(url(issue), {"state": str(awaiting.id)}, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, issue.name
            issue.refresh_from_db()
            assert issue.state_id == state.id
        # The same move on the bot's own unassigned item is accepted.
        response = bot_client.patch(url(own_ticket), {"state": str(awaiting.id)}, format="json")
        assert response.status_code == status.HTTP_200_OK, response.data

    def test_bot_gate_move_matches_awaiting_human_by_exact_name_in_any_case(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        # AC1 and AC2: the Awaiting Human state is found by its name (any case, as for human requests);
        # another started state with a similar name is still refused.
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        lower_awaiting = State.objects.create(
            name=AWAITING_HUMAN_STATE_NAME.lower(), group="started", project=project, workspace=workspace
        )
        look_alike = State.objects.create(
            name=f"{AWAITING_HUMAN_STATE_NAME} review", group="started", project=project, workspace=workspace
        )
        ticket = _create_issue_by(bot_id, project, workspace, state, "[Human] Approve skill change: x (T27)")
        url = _v1(workspace.slug, project.id, f"work-items/{ticket.id}/")

        response = bot_client.patch(url, {"state": str(look_alike.id)}, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        response = bot_client.patch(url, {"state": str(lower_awaiting.id)}, format="json")
        assert response.status_code == status.HTTP_200_OK, response.data
        ticket.refresh_from_db()
        assert ticket.state_id == lower_awaiting.id

    @pytest.mark.parametrize("group", ["completed", "cancelled"])
    def test_bot_gate_move_ignores_an_awaiting_human_name_on_a_closing_state(
        self, workspace, project, state, create_user, bot, monkeypatch, group
    ):
        # AC4: a state named Awaiting Human that closes the item (completed or cancelled group) is no gate state.
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        closing = State.objects.create(
            name=AWAITING_HUMAN_STATE_NAME, group=group, project=project, workspace=workspace
        )
        ticket = _create_issue_by(bot_id, project, workspace, state, "[Human] Approve skill change: x (T27)")

        response = bot_client.patch(
            _v1(workspace.slug, project.id, f"work-items/{ticket.id}/"), {"state": str(closing.id)}, format="json"
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN, response.data
        ticket.refresh_from_db()
        assert ticket.state_id == state.id

    def test_bot_reopen_right_is_limited_to_gate_states_on_its_own_unassigned_work_items(
        self, workspace, project, state, started_state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        done = State.objects.create(name="Done", group="completed", project=project, workspace=workspace)
        backlog = State.objects.create(name="Backlog", group="backlog", project=project, workspace=workspace)
        url = lambda issue: _v1(workspace.slug, project.id, f"work-items/{issue.id}/")  # noqa: E731

        ticket = _create_issue_by(bot_id, project, workspace, done, "[Human] approve")
        # Other target states or other fields are refused.
        for body in (
            {"state": str(started_state.id)},
            {"state": str(state.id), "name": "Approved by the bot"},
            {"name": "Approved by the bot"},
        ):
            response = bot_client.patch(url(ticket), body, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, body
        # A gate state passes the permission, but Done is final in the state machine.
        for target in (backlog, state):
            response = bot_client.patch(url(ticket), {"state": str(target.id)}, format="json")
            assert response.status_code == status.HTTP_400_BAD_REQUEST, (target.name, response.data)
        ticket.refresh_from_db()
        assert ticket.state_id == done.id

        # Unassigned items created by a human, and bot-created items assigned to a human, are refused.
        human_created = _create_issue_by(create_user.id, project, workspace, done, "Human's unassigned item")
        assigned = _create_issue_by(bot_id, project, workspace, done, "[Human] assigned", [str(create_user.id)])
        for issue in (human_created, assigned):
            response = bot_client.patch(url(issue), {"state": str(state.id)}, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_bot_reopen_refuses_states_of_other_projects_and_malformed_state_ids(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        done = State.objects.create(name="Done", group="completed", project=project, workspace=workspace)
        other_project = Project.objects.create(
            name="Other Project", identifier="OTH", workspace=workspace, created_by=create_user, updated_by=create_user
        )
        other_todo = State.objects.create(name="Todo", group="unstarted", project=other_project, workspace=workspace)
        ticket = _create_issue_by(bot_id, project, workspace, done, "[Human] approve")
        url = _v1(workspace.slug, project.id, f"work-items/{ticket.id}/")

        for state_id in (str(other_todo.id), str(uuid4()), "not-a-uuid", "", None):
            response = bot_client.patch(url, {"state": state_id}, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, state_id
        ticket.refresh_from_db()
        assert ticket.state_id == done.id

    def test_bot_hands_over_its_ticket_once_the_human_assignee_was_removed(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        awaiting = State.objects.create(name="Awaiting Human", group="started", project=project, workspace=workspace)
        ticket = _create_issue_by(bot_id, project, workspace, state, "[Human] approve", [str(create_user.id)])
        url = _v1(workspace.slug, project.id, f"work-items/{ticket.id}/")

        response = bot_client.patch(url, {"state": str(awaiting.id)}, format="json")
        assert response.status_code == status.HTTP_403_FORBIDDEN

        # Unassigning soft deletes the assignee row; the ticket counts as unassigned again.
        IssueAssignee.objects.filter(issue=ticket).delete()
        assert IssueAssignee.all_objects.filter(issue=ticket, deleted_at__isnull=False).exists()
        response = bot_client.patch(url, {"state": str(awaiting.id)}, format="json")
        assert response.status_code == status.HTTP_200_OK, response.data
        ticket.refresh_from_db()
        assert ticket.state_id == awaiting.id

    def test_bot_cannot_move_its_assigned_work_item_to_a_custom_state(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        # The Error state is gone; a leftover custom state is outside the state machine for bots.
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        error = State.objects.create(name="Error", group="started", project=project, workspace=workspace)
        task = _create_issue(project, workspace, state, create_user, "Bot task", [bot_id])
        url = _v1(workspace.slug, project.id, f"work-items/{task.id}/")

        response = bot_client.patch(url, {"state": str(error.id)}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert response.data["allowed_states"] == ["In Progress"]
        task.refresh_from_db()
        assert task.state_id == state.id

    def test_bot_cannot_upsert_or_delete_work_items(self, workspace, project, state, create_user, bot, monkeypatch):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        issue = _create_issue(project, workspace, state, create_user, "Bot task", [bot_id])

        delete_response = bot_client.delete(_v1(workspace.slug, project.id, f"work-items/{issue.id}/"))
        put_response = bot_client.put(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "Upsert", "external_id": "x-1", "external_source": "bot"},
            format="json",
        )

        assert delete_response.status_code == status.HTTP_403_FORBIDDEN
        assert put_response.status_code == status.HTTP_403_FORBIDDEN
        assert Issue.objects.filter(pk=issue.id).exists()

    def test_bot_creates_sub_work_item_under_assigned_work_item(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        parent = _create_issue(project, workspace, state, create_user, "Bot epic", [bot_id])

        response = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "Step 1", "parent": str(parent.id), "state": str(state.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        child = Issue.objects.get(pk=response.data["id"])
        assert child.parent_id == parent.id
        assert child.created_by_id == bot_id or str(child.created_by_id) == bot_id
        assert set(IssueAssignee.objects.filter(issue=child).values_list("assignee_id", flat=True)) == {
            child.assignees.first().id
        }
        assert str(child.assignees.first().id) == bot_id

        # The bot can keep working on the sub work item it just created.
        update = bot_client.patch(
            _v1(workspace.slug, project.id, f"work-items/{child.id}/"),
            {"name": "Step 1 (done)"},
            format="json",
        )
        assert update.status_code == status.HTTP_200_OK

    def test_bot_sub_work_item_keeps_explicit_assignees(
        self, session_client, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        other_bot_id, _other_client = _create_bot(session_client, workspace, "Helper AI")
        parent = _create_issue(project, workspace, state, create_user, "Bot epic", [bot_id])

        response = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "Delegated step", "parent": str(parent.id), "assignees": [other_bot_id]},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assigned = IssueAssignee.objects.filter(issue_id=response.data["id"]).values_list("assignee_id", flat=True)
        assignee_ids = {str(a) for a in assigned}
        assert assignee_ids == {other_bot_id}

    def test_bot_cannot_create_foreign_sub_work_items_or_top_level_work_for_others(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        _bot_id, bot_client = bot
        human = _create_issue(project, workspace, state, create_user, "Human epic", [str(create_user.id)])

        top_level = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "Rogue", "assignees": [str(create_user.id)]},
            format="json",
        )
        foreign_child = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "Rogue child", "parent": str(human.id)},
            format="json",
        )
        bad_parent = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "Rogue child", "parent": "not-a-uuid"},
            format="json",
        )

        shared = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "Rogue shared", "assignees": [_bot_id, str(create_user.id)]},
            format="json",
        )

        assert top_level.status_code == status.HTTP_403_FORBIDDEN
        assert shared.status_code == status.HTTP_403_FORBIDDEN
        assert foreign_child.status_code == status.HTTP_403_FORBIDDEN
        assert bad_parent.status_code == status.HTTP_403_FORBIDDEN
        assert Issue.objects.filter(name__startswith="Rogue").count() == 0

    def test_bot_creates_unassigned_ticket_for_humans_that_blocks_its_work_item(
        self, workspace, project, state, awaiting_state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        blocked = _create_issue(project, workspace, state, create_user, "Bot task", [bot_id])

        response = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "[Human] choose where the script lives", "assignees": [], "state": str(awaiting_state.id)},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        ticket = Issue.objects.get(pk=response.data["id"])
        assert ticket.parent_id is None
        assert not IssueAssignee.objects.filter(issue=ticket).exists()

        # The bot cannot work on the human ticket it created ...
        update = bot_client.patch(
            _v1(workspace.slug, project.id, f"work-items/{ticket.id}/"), {"name": "Taken over"}, format="json"
        )
        assert update.status_code == status.HTTP_403_FORBIDDEN

        # ... but it can mark its own work item as blocked by it.
        relation = bot_client.post(
            _v1(workspace.slug, project.id, f"work-items/{blocked.id}/relations/"),
            {"relation_type": "blocked_by", "issues": [str(ticket.id)]},
            format="json",
        )
        assert relation.status_code == status.HTTP_201_CREATED, relation.data
        assert IssueRelation.objects.filter(issue=blocked, related_issue=ticket, relation_type="blocked_by").exists()

    def test_bot_creates_release_item_assigned_to_itself_with_sub_work_items(
        self, session_client, workspace, project, state, awaiting_state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot

        implicit = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "[Release] - develop", "state": str(state.id)},
            format="json",
        )
        explicit = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "[Release] - main", "assignees": [bot_id]},
            format="json",
        )

        assert implicit.status_code == status.HTTP_201_CREATED, implicit.data
        assert explicit.status_code == status.HTTP_201_CREATED, explicit.data
        release = Issue.objects.get(pk=implicit.data["id"])
        assert release.parent_id is None
        assignees = IssueAssignee.objects.filter(issue=release).values_list("assignee_id", flat=True)
        assert {str(assignee) for assignee in assignees} == {bot_id}

        # The bot works on its release item: state changes and sub work items.
        update = bot_client.patch(
            _v1(workspace.slug, project.id, f"work-items/{release.id}/"),
            {"name": "[Release] - develop"},
            format="json",
        )
        child = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "Local testing", "parent": str(release.id)},
            format="json",
        )
        human_child = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {
                "name": "[Human] permission to push to git remote and deploy to vps",
                "parent": str(release.id),
                "assignees": [],
            },
            format="json",
        )
        assert update.status_code == status.HTTP_200_OK
        assert child.status_code == status.HTTP_201_CREATED, child.data
        assert human_child.status_code == status.HTTP_201_CREATED, human_child.data
        assert not IssueAssignee.objects.filter(issue_id=human_child.data["id"]).exists()
        # No state given: the approval waits for the person in Awaiting Human.
        assert str(human_child.data["state"]) == str(awaiting_state.id)
        # The approval sub work item stays out of the bot's reach.
        approve = bot_client.patch(
            _v1(workspace.slug, project.id, f"work-items/{human_child.data['id']}/"),
            {"name": "approved"},
            format="json",
        )
        assert approve.status_code == status.HTTP_403_FORBIDDEN

    def test_bot_cannot_create_release_items_for_others(
        self, session_client, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        other_bot_id, _other_client = _create_bot(session_client, workspace, "Helper AI")
        cases = [
            {"name": "[Release] - develop", "assignees": [str(create_user.id)]},
            {"name": "[Release] - develop", "assignees": [other_bot_id]},
            {"name": "[Release] - develop", "assignees": [bot_id, str(create_user.id)]},
        ]
        for body in cases:
            response = bot_client.post(_v1(workspace.slug, project.id, "work-items/"), body, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, body

        assert Issue.objects.filter(name__icontains="release").count() == 0

    def test_release_name_does_not_widen_sub_work_item_or_assignee_rules(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        human = _create_issue(project, workspace, state, create_user, "Human epic", [str(create_user.id)])
        cases = [
            # A release name gives no access to a parent the bot is not assigned to.
            {"name": "[Release] - develop", "parent": str(human.id)},
            {"name": "[Release] - develop", "parent": str(human.id), "assignees": [bot_id]},
            {"name": "[Release] - develop", "parent": "not-a-uuid"},
            {"name": "[Release] - develop", "parent": ""},
            # ``assignees`` must be a list holding the bot alone.
            {"name": "[Release] - develop", "assignees": bot_id},
            {"name": "[Release] - develop", "assignees": None},
            {"name": "[Release] - develop", "assignees": [bot_id, bot_id]},
        ]
        for body in cases:
            response = bot_client.post(_v1(workspace.slug, project.id, "work-items/"), body, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, body

        assert Issue.objects.filter(name__icontains="release").count() == 0

    def test_bot_creates_release_item_from_form_data_and_unassigned_release_named_ticket(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot

        implicit = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"), {"name": "[Release] - develop"}, format="multipart"
        )
        explicit = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "[Release] - main", "assignees": [bot_id]},
            format="multipart",
        )
        foreign = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "[Release] - hotfix", "assignees": [bot_id, str(create_user.id)]},
            format="multipart",
        )
        # An explicit empty list stays the unassigned ticket for humans, whatever its name.
        unassigned = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "[Release] - staging", "assignees": []},
            format="json",
        )

        assert implicit.status_code == status.HTTP_201_CREATED, implicit.data
        assert explicit.status_code == status.HTTP_201_CREATED, explicit.data
        assert foreign.status_code == status.HTTP_403_FORBIDDEN
        assert unassigned.status_code == status.HTTP_201_CREATED, unassigned.data
        for response in (implicit, explicit):
            assignees = IssueAssignee.objects.filter(issue_id=response.data["id"]).values_list("assignee_id", flat=True)
            assert {str(assignee) for assignee in assignees} == {bot_id}
        assert not IssueAssignee.objects.filter(issue_id=unassigned.data["id"]).exists()
        assert not Issue.objects.filter(name="[Release] - hotfix").exists()

    def test_human_creates_release_named_work_item_for_anyone(
        self, workspace, project, state, create_user, human_client, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, _bot_client = bot

        response = human_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "[Release] - develop", "assignees": [str(create_user.id), bot_id]},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assignees = IssueAssignee.objects.filter(issue_id=response.data["id"]).values_list("assignee_id", flat=True)
        assert {str(assignee) for assignee in assignees} == {str(create_user.id), bot_id}


@pytest.mark.contract
@pytest.mark.django_db
class TestAIBotCommentAndRelationAccess:
    def test_bot_comments_on_human_work_item_and_edits_only_its_own(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        human = _create_issue(project, workspace, state, create_user, "Human task", [str(create_user.id)])
        human_comment = IssueComment.objects.create(
            issue=human, project=project, workspace=workspace, actor=create_user, comment_html="<p>Human note</p>"
        )
        comments_url = _v1(workspace.slug, project.id, f"work-items/{human.id}/comments/")

        created = bot_client.post(comments_url, {"comment_html": "<p>Bot note</p>"}, format="json")
        assert created.status_code == status.HTTP_201_CREATED, created.data
        bot_comment = IssueComment.objects.get(pk=created.data["id"])
        assert str(bot_comment.actor_id) == bot_id

        listed = bot_client.get(comments_url)
        assert listed.status_code == status.HTTP_200_OK
        assert {str(c["id"]) for c in listed.data["results"]} == {str(human_comment.id), str(bot_comment.id)}

        own_update = bot_client.patch(
            f"{comments_url}{bot_comment.id}/", {"comment_html": "<p>Bot note v2</p>"}, format="json"
        )
        assert own_update.status_code == status.HTTP_200_OK, own_update.data

        foreign_update = bot_client.patch(
            f"{comments_url}{human_comment.id}/", {"comment_html": "<p>Tampered</p>"}, format="json"
        )
        foreign_delete = bot_client.delete(f"{comments_url}{human_comment.id}/")
        assert foreign_update.status_code == status.HTTP_403_FORBIDDEN
        assert foreign_delete.status_code == status.HTTP_403_FORBIDDEN
        human_comment.refresh_from_db()
        assert human_comment.comment_html == "<p>Human note</p>"

    def test_bot_reads_activity_of_any_work_item(self, workspace, project, state, create_user, bot, monkeypatch):
        _stub_tasks(monkeypatch)
        _bot_id, bot_client = bot
        human = _create_issue(project, workspace, state, create_user, "Human task", [str(create_user.id)])

        response = bot_client.get(_v1(workspace.slug, project.id, f"work-items/{human.id}/activities/"))

        assert response.status_code == status.HTTP_200_OK

    def test_bot_creates_relations_between_human_work_items(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        _bot_id, bot_client = bot
        first = _create_issue(project, workspace, state, create_user, "First", [str(create_user.id)])
        second = _create_issue(project, workspace, state, create_user, "Second")
        relations_url = _v1(workspace.slug, project.id, f"work-items/{first.id}/relations/")

        created = bot_client.post(
            relations_url, {"relation_type": "blocked_by", "issues": [str(second.id)]}, format="json"
        )
        assert created.status_code == status.HTTP_201_CREATED, created.data
        assert IssueRelation.objects.filter(issue=first, related_issue=second, relation_type="blocked_by").exists()

        listed = bot_client.get(relations_url)
        assert listed.status_code == status.HTTP_200_OK
        assert [r["issue_id"] for r in listed.data["blocked_by"]] == [str(second.id)]


@pytest.mark.contract
@pytest.mark.django_db
class TestAIBotPageAccess:
    def test_bot_reads_public_pages_but_not_private_ones(self, workspace, project, create_user, bot, monkeypatch):
        _stub_tasks(monkeypatch)
        _bot_id, bot_client = bot
        public = _create_page(project, workspace, create_user, "Team wiki")
        private = _create_page(project, workspace, create_user, "Private notes", access=Page.PRIVATE_ACCESS)

        listed = bot_client.get(_v1(workspace.slug, project.id, "pages/"))
        assert listed.status_code == status.HTTP_200_OK, listed.data
        assert {str(p["id"]) for p in listed.data["results"]} == {str(public.id)}
        assert "description_html" not in listed.data["results"][0]

        detail = bot_client.get(_v1(workspace.slug, project.id, f"pages/{public.id}/"))
        assert detail.status_code == status.HTTP_200_OK
        assert detail.data["description_html"] == "<p>doc</p>"

        hidden = bot_client.get(_v1(workspace.slug, project.id, f"pages/{private.id}/"))
        assert hidden.status_code == status.HTTP_404_NOT_FOUND

    def test_bot_creates_pages_and_updates_only_its_own(self, workspace, project, create_user, bot, monkeypatch):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        human_page = _create_page(project, workspace, create_user, "Team wiki")

        created = bot_client.post(
            _v1(workspace.slug, project.id, "pages/"),
            {"name": "Bot design notes", "description_html": "<p>Plan</p>"},
            format="json",
        )
        assert created.status_code == status.HTTP_201_CREATED, created.data
        page = Page.objects.get(pk=created.data["id"])
        assert str(page.owned_by_id) == bot_id
        assert ProjectPage.objects.filter(page=page, project=project).exists()
        assert page.description_html == "<p>Plan</p>"

        # Give the page an editor binary, then update through the API: the binary must be cleared
        # so the collaborative editor rebuilds it from the new HTML.
        page.description_binary = b"stale"
        page.description_json = {"type": "doc"}
        page.save(update_fields=["description_binary", "description_json"])
        own_update = bot_client.patch(
            _v1(workspace.slug, project.id, f"pages/{page.id}/"),
            {"name": "Bot design notes v2", "description_html": "<p>Plan v2</p>"},
            format="json",
        )
        assert own_update.status_code == status.HTTP_200_OK, own_update.data
        page.refresh_from_db()
        assert page.name == "Bot design notes v2"
        assert page.description_html == "<p>Plan v2</p>"
        assert page.description_binary is None
        assert page.description_json == {}

        foreign_update = bot_client.patch(
            _v1(workspace.slug, project.id, f"pages/{human_page.id}/"),
            {"description_html": "<p>Tampered</p>"},
            format="json",
        )
        assert foreign_update.status_code == status.HTTP_403_FORBIDDEN
        human_page.refresh_from_db()
        assert human_page.description_html == "<p>doc</p>"

    def test_bot_cannot_edit_locked_or_archived_own_pages(self, workspace, project, create_user, bot, monkeypatch):
        _stub_tasks(monkeypatch)
        _bot_id, bot_client = bot
        created = bot_client.post(_v1(workspace.slug, project.id, "pages/"), {"name": "Notes"}, format="json")
        page = Page.objects.get(pk=created.data["id"])
        page.is_locked = True
        page.save(update_fields=["is_locked"])

        response = bot_client.patch(
            _v1(workspace.slug, project.id, f"pages/{page.id}/"), {"name": "Renamed"}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_human_api_key_uses_page_endpoints(self, workspace, project, create_user, human_client, bot, monkeypatch):
        _stub_tasks(monkeypatch)
        _bot_id, bot_client = bot
        bot_page = bot_client.post(_v1(workspace.slug, project.id, "pages/"), {"name": "Bot page"}, format="json")
        assert bot_page.status_code == status.HTTP_201_CREATED

        created = human_client.post(
            _v1(workspace.slug, project.id, "pages/"),
            {"name": "Human page", "description_html": "<p>Hello</p>", "access": Page.PRIVATE_ACCESS},
            format="json",
        )
        assert created.status_code == status.HTTP_201_CREATED, created.data

        listed = human_client.get(_v1(workspace.slug, project.id, "pages/"))
        assert listed.status_code == status.HTTP_200_OK
        assert {str(p["id"]) for p in listed.data["results"]} == {str(bot_page.data["id"]), str(created.data["id"])}

        # Project admins may edit the bot's public page; the bot may not see the human's private page.
        edited = human_client.patch(
            _v1(workspace.slug, project.id, f"pages/{bot_page.data['id']}/"),
            {"name": "Bot page (reviewed)"},
            format="json",
        )
        assert edited.status_code == status.HTTP_200_OK, edited.data
        hidden = bot_client.get(_v1(workspace.slug, project.id, f"pages/{created.data['id']}/"))
        assert hidden.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.contract
@pytest.mark.django_db
class TestAIBotLinkAccess:
    def test_bot_creates_link_on_assigned_work_item_and_reads_all_links(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        own = _create_issue(project, workspace, state, create_user, "Bot task", [bot_id])
        human = _create_issue(project, workspace, state, create_user, "Human task", [str(create_user.id)])
        human_link = IssueLink.objects.create(
            issue=human, project=project, workspace=workspace, url="https://example.com/spec", created_by=create_user
        )

        created = bot_client.post(
            _v1(workspace.slug, project.id, f"work-items/{own.id}/links/"),
            {"url": "https://example.com/pr/1", "title": "PR"},
            format="json",
        )
        assert created.status_code == status.HTTP_201_CREATED, created.data
        link = IssueLink.objects.get(pk=created.data["id"])
        assert str(link.created_by_id) == bot_id
        assert link.issue_id == own.id

        listed = bot_client.get(_v1(workspace.slug, project.id, f"work-items/{human.id}/links/"))
        assert listed.status_code == status.HTTP_200_OK
        assert {str(l["id"]) for l in listed.data["results"]} == {str(human_link.id)}

        detail = bot_client.get(_v1(workspace.slug, project.id, f"work-items/{human.id}/links/{human_link.id}/"))
        assert detail.status_code == status.HTTP_200_OK

    def test_bot_cannot_create_link_on_unassigned_work_item(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        _bot_id, bot_client = bot
        human = _create_issue(project, workspace, state, create_user, "Human task", [str(create_user.id)])
        unassigned = _create_issue(project, workspace, state, create_user, "Nobody's task")

        for issue in (human, unassigned):
            response = bot_client.post(
                _v1(workspace.slug, project.id, f"work-items/{issue.id}/links/"),
                {"url": "https://example.com/rogue"},
                format="json",
            )
            assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not IssueLink.objects.filter(url="https://example.com/rogue").exists()

    def test_bot_edits_and_deletes_only_its_own_links(self, workspace, project, state, create_user, bot, monkeypatch):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        issue = _create_issue(project, workspace, state, create_user, "Pair task", [bot_id, str(create_user.id)])
        links_url = _v1(workspace.slug, project.id, f"work-items/{issue.id}/links/")
        human_link = IssueLink.objects.create(
            issue=issue, project=project, workspace=workspace, url="https://example.com/human", created_by=create_user
        )
        created = bot_client.post(links_url, {"url": "https://example.com/bot"}, format="json")
        assert created.status_code == status.HTTP_201_CREATED, created.data
        bot_link_id = created.data["id"]

        own_update = bot_client.patch(f"{links_url}{bot_link_id}/", {"title": "Bot link v2"}, format="json")
        assert own_update.status_code == status.HTTP_200_OK, own_update.data
        assert IssueLink.objects.get(pk=bot_link_id).title == "Bot link v2"

        foreign_update = bot_client.patch(f"{links_url}{human_link.id}/", {"title": "Tampered"}, format="json")
        foreign_delete = bot_client.delete(f"{links_url}{human_link.id}/")
        assert foreign_update.status_code == status.HTTP_403_FORBIDDEN
        assert foreign_delete.status_code == status.HTTP_403_FORBIDDEN
        human_link.refresh_from_db()
        assert human_link.title is None

        own_delete = bot_client.delete(f"{links_url}{bot_link_id}/")
        assert own_delete.status_code == status.HTTP_204_NO_CONTENT
        assert not IssueLink.objects.filter(pk=bot_link_id).exists()

    def test_bot_cannot_edit_own_link_through_another_work_item(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        own = _create_issue(project, workspace, state, create_user, "Bot task", [bot_id])
        other = _create_issue(project, workspace, state, create_user, "Other task", [bot_id])
        created = bot_client.post(
            _v1(workspace.slug, project.id, f"work-items/{own.id}/links/"),
            {"url": "https://example.com/bot"},
            format="json",
        )
        assert created.status_code == status.HTTP_201_CREATED, created.data

        response = bot_client.patch(
            _v1(workspace.slug, project.id, f"work-items/{other.id}/links/{created.data['id']}/"),
            {"title": "Moved"},
            format="json",
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN


def _set_created_by(instance, user_id):
    """``save`` resets ``created_by`` to the request user (none in tests); set it afterwards."""
    type(instance).objects.filter(pk=instance.pk).update(created_by_id=user_id)
    instance.refresh_from_db()
    return instance


def _create_cycle(project, workspace, owner, name, start_days=1, end_days=7):
    return Cycle.objects.create(
        name=name,
        start_date=timezone.now() + timedelta(days=start_days),
        end_date=timezone.now() + timedelta(days=end_days),
        project=project,
        workspace=workspace,
        owned_by=owner,
    )


@pytest.mark.contract
@pytest.mark.django_db
class TestAIBotPlannedWorkItemAccess:
    def test_bot_creates_top_level_work_items_unassigned_or_for_itself(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        url = _v1(workspace.slug, project.id, "work-items/")

        implicit = bot_client.post(url, {"name": "Planned A"}, format="json")
        explicit = bot_client.post(url, {"name": "Planned B", "assignees": [bot_id]}, format="json")
        unassigned = bot_client.post(url, {"name": "Planned C", "assignees": []}, format="json")

        for response in (implicit, explicit, unassigned):
            assert response.status_code == status.HTTP_201_CREATED, response.data
        assignees = lambda response: {  # noqa: E731
            str(a)
            for a in IssueAssignee.objects.filter(issue_id=response.data["id"]).values_list("assignee_id", flat=True)
        }
        assert assignees(implicit) == {bot_id}
        assert assignees(explicit) == {bot_id}
        assert assignees(unassigned) == set()
        assert Issue.objects.get(pk=implicit.data["id"]).created_by_id is not None
        assert str(Issue.objects.get(pk=implicit.data["id"]).created_by_id) == bot_id

    def test_bot_cannot_create_human_tickets_assigned_to_itself(
        self, workspace, project, state, awaiting_state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        url = _v1(workspace.slug, project.id, "work-items/")

        for body in (
            {"name": "[Human] approve release"},
            {"name": "[Human] approve release", "assignees": [bot_id]},
            {"name": " [human] approve release", "assignees": [bot_id]},
            {"name": "Planned", "assignees": [str(create_user.id)]},
            {"name": "Planned", "assignees": bot_id},
        ):
            response = bot_client.post(url, body, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, body

        assert not Issue.objects.filter(project=project).exists()
        ticket = bot_client.post(url, {"name": "[Human] approve release", "assignees": []}, format="json")
        assert ticket.status_code == status.HTTP_201_CREATED, ticket.data

    def test_bot_updates_planning_fields_of_its_own_work_item(
        self, workspace, project, state, started_state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        cancelled = State.objects.create(name="Cancelled", group="cancelled", project=project, workspace=workspace)
        label = Label.objects.create(name="lane:fork", project=project, workspace=workspace)
        estimate = Estimate.objects.create(name="Points", type="points", project=project, workspace=workspace)
        point = EstimatePoint.objects.create(estimate=estimate, key=1, value="3", project=project, workspace=workspace)
        item = _create_issue_by(bot_id, project, workspace, state, "Planned item")
        url = _v1(workspace.slug, project.id, f"work-items/{item.id}/")

        response = bot_client.patch(
            url,
            {
                "name": "Planned item (reshaped)",
                "description_html": "<p>Contract</p>",
                "priority": "high",
                "labels": [str(label.id)],
                "estimate_point": str(point.id),
                "start_date": "2026-09-21",
                "target_date": "2026-09-27",
                "state": str(started_state.id),
            },
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK, response.data
        item.refresh_from_db()
        assert item.name == "Planned item (reshaped)"
        assert item.priority == "high"
        assert item.estimate_point_id == point.id
        assert list(item.labels.values_list("id", flat=True)) == [label.id]

        # Assign it to itself and give it back.
        for body in ({"assignees": [bot_id]}, {"assignees": []}):
            response = bot_client.patch(url, body, format="json")
            assert response.status_code == status.HTTP_200_OK, (body, response.data)
        item.refresh_from_db()
        assert item.state_id == started_state.id
        assert not IssueAssignee.objects.filter(issue=item).exists()

        # Cancelling is outside the state machine: only a person may cancel a work item.
        response = bot_client.patch(url, {"state": str(cancelled.id)}, format="json")
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        item.refresh_from_db()
        assert item.state_id == started_state.id

    def test_bot_cannot_set_a_completed_state_or_assign_others_on_its_own_work_item(
        self, session_client, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        other_bot_id, _other_client = _create_bot(session_client, workspace, "Helper AI")
        done = State.objects.create(name="Done", group="completed", project=project, workspace=workspace)
        other_project = Project.objects.create(
            name="Other Project", identifier="OTH", workspace=workspace, created_by=create_user, updated_by=create_user
        )
        other_state = State.objects.create(name="Todo", group="unstarted", project=other_project, workspace=workspace)
        item = _create_issue_by(bot_id, project, workspace, state, "Planned item")
        url = _v1(workspace.slug, project.id, f"work-items/{item.id}/")

        for body in (
            {"state": str(done.id)},
            {"state": str(done.id), "name": "Finished"},
            {"state": str(other_state.id)},
            {"state": None},
            {"assignees": [str(create_user.id)]},
            {"assignees": [bot_id, str(create_user.id)]},
            {"assignees": [other_bot_id]},
            {"assignees": [bot_id, bot_id]},
            {"name": "[Human] approve release"},
            {"external_source": "plane-planner", "external_id": "x"},
            {"sort_order": 1},
            {},
        ):
            response = bot_client.patch(url, body, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, body
        item.refresh_from_db()
        assert item.state_id == state.id
        assert item.name == "Planned item"
        assert not IssueAssignee.objects.filter(issue=item).exists()

    def test_bot_cannot_update_work_items_it_did_not_create_or_that_a_human_holds(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        human_item = _create_issue_by(create_user.id, project, workspace, state, "Human's item")
        # ``external_source`` can be set by any client; it gives no ownership.
        Issue.objects.filter(pk=human_item.pk).update(external_source="plane-planner", external_id="plan/T1")
        held = _create_issue_by(bot_id, project, workspace, state, "Planned, taken by a human", [str(create_user.id)])
        human_ticket = _create_issue_by(bot_id, project, workspace, state, "[Human] choose a host")

        for issue in (human_item, held, human_ticket):
            for body in ({"name": "Hijacked"}, {"description_html": "<p>x</p>"}, {"assignees": []}):
                response = bot_client.patch(
                    _v1(workspace.slug, project.id, f"work-items/{issue.id}/"), body, format="json"
                )
                assert response.status_code == status.HTTP_403_FORBIDDEN, (issue.name, body)
        assert IssueAssignee.objects.filter(issue=held, assignee=create_user).exists()

    def test_bot_moves_its_own_work_item_under_a_parent_it_owns(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        item = _create_issue_by(bot_id, project, workspace, state, "Planned item")
        own_parent = _create_issue_by(bot_id, project, workspace, state, "Planned epic")
        assigned_parent = _create_issue(project, workspace, state, create_user, "Human epic for the bot", [bot_id])
        url = _v1(workspace.slug, project.id, f"work-items/{item.id}/")

        for parent in (own_parent, assigned_parent):
            response = bot_client.patch(url, {"parent": str(parent.id)}, format="json")
            assert response.status_code == status.HTTP_200_OK, response.data
            item.refresh_from_db()
            assert item.parent_id == parent.id

        response = bot_client.patch(url, {"parent": None}, format="json")
        assert response.status_code == status.HTTP_200_OK, response.data
        item.refresh_from_db()
        assert item.parent_id is None

    def test_bot_cannot_move_its_work_item_under_a_foreign_parent(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        other_project = Project.objects.create(
            name="Other Project", identifier="OTH", workspace=workspace, created_by=create_user, updated_by=create_user
        )
        other_state = State.objects.create(name="Todo", group="unstarted", project=other_project, workspace=workspace)
        item = _create_issue_by(bot_id, project, workspace, state, "Planned item")
        human_parent = _create_issue_by(create_user.id, project, workspace, state, "Human epic")
        foreign = _create_issue_by(bot_id, other_project, workspace, other_state, "Bot item elsewhere")
        url = _v1(workspace.slug, project.id, f"work-items/{item.id}/")

        for parent in (str(human_parent.id), str(foreign.id), str(item.id), str(uuid4()), "not-a-uuid", ""):
            response = bot_client.patch(url, {"parent": parent}, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, parent
        item.refresh_from_db()
        assert item.parent_id is None

    def test_bot_creates_sub_items_under_its_own_unassigned_backlog_parent(
        self, workspace, project, create_user, bot, monkeypatch
    ):
        """AC1: the planner writes sub items under its own unassigned future-cycle item."""
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        backlog = State.objects.create(name="Backlog", group="backlog", project=project, workspace=workspace)
        parent = _create_issue_by(bot_id, project, workspace, backlog, "Add the state machine")
        self_parent = _create_issue_by(bot_id, project, workspace, backlog, "Self assigned epic", [bot_id])
        url = _v1(workspace.slug, project.id, "work-items/")

        for owner, body in (
            (parent, {"name": "[1] Add the state", "assignees": []}),
            (parent, {"name": "[2] Wire the state", "assignees": [bot_id]}),
            (parent, {"name": "[3] Default assignee"}),
            (self_parent, {"name": "[1] Under a self-assigned epic", "assignees": []}),
        ):
            response = bot_client.post(url, {**body, "parent": str(owner.id), "state": str(backlog.id)}, format="json")
            assert response.status_code == status.HTTP_201_CREATED, (body, response.data)
            child = Issue.objects.get(pk=response.data["id"])
            assert child.parent_id == owner.id
            assignees = {str(a) for a in child.assignees.values_list("id", flat=True)}
            assert assignees <= {bot_id}

        child = Issue.objects.get(name="[1] Add the state")
        assert not child.assignees.exists()

    def test_bot_sub_items_under_its_own_parent_follow_the_top_level_rules(
        self, workspace, project, state, awaiting_state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        parent = _create_issue_by(bot_id, project, workspace, state, "Planned epic")
        url = _v1(workspace.slug, project.id, "work-items/")

        for body in (
            {"name": "Rogue for a human", "assignees": [str(create_user.id)]},
            {"name": "Rogue shared", "assignees": [bot_id, str(create_user.id)]},
            {"name": "[Human] Rogue gate", "assignees": [bot_id]},
        ):
            response = bot_client.post(url, {**body, "parent": str(parent.id)}, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, body
        assert not Issue.objects.filter(parent=parent).exists()

        gate = bot_client.post(
            url, {"name": "[Human] Approve it", "parent": str(parent.id), "assignees": []}, format="json"
        )
        assert gate.status_code == status.HTTP_201_CREATED, gate.data

    def test_bot_cannot_create_sub_items_under_parents_it_neither_created_nor_holds(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        """AC2: a parent created by someone else and not assigned to the bot stays refused (403)."""
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        human_unassigned = _create_issue_by(create_user.id, project, workspace, state, "Human epic")
        human_held = _create_issue_by(create_user.id, project, workspace, state, "Human held", [str(create_user.id)])
        url = _v1(workspace.slug, project.id, "work-items/")

        for parent in (human_unassigned, human_held):
            for body in ({"name": "Rogue child", "assignees": []}, {"name": "Rogue child", "assignees": [bot_id]}):
                response = bot_client.post(url, {**body, "parent": str(parent.id)}, format="json")
                assert response.status_code == status.HTTP_403_FORBIDDEN, (parent.name, body)
        assert not Issue.objects.filter(name="Rogue child").exists()

    def test_create_and_patch_parent_rules_accept_the_same_parents(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        """AC3: ``POST`` with ``parent`` and ``PATCH`` of ``parent`` accept and refuse the same parents."""
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        human_id = str(create_user.id)
        other_project = Project.objects.create(
            name="Other Project", identifier="OTH", workspace=workspace, created_by=create_user, updated_by=create_user
        )
        other_state = State.objects.create(name="Todo", group="unstarted", project=other_project, workspace=workspace)
        parents = {
            "own unassigned": (_create_issue_by(bot_id, project, workspace, state, "Own epic"), True),
            "own self-assigned": (_create_issue_by(bot_id, project, workspace, state, "Own held", [bot_id]), True),
            "human assigned to bot": (
                _create_issue_by(human_id, project, workspace, state, "Human for bot", [bot_id]),
                True,
            ),
            "shared with a human": (
                _create_issue_by(human_id, project, workspace, state, "Shared", [bot_id, human_id]),
                True,
            ),
            "own held by a human": (
                _create_issue_by(bot_id, project, workspace, state, "Own for human", [human_id]),
                False,
            ),
            "own [Human] ticket": (_create_issue_by(bot_id, project, workspace, state, "[Human] Decide"), False),
            "human unassigned": (_create_issue_by(human_id, project, workspace, state, "Human epic"), False),
            "own in another project": (
                _create_issue_by(bot_id, other_project, workspace, other_state, "Elsewhere"),
                False,
            ),
        }
        item = _create_issue_by(bot_id, project, workspace, state, "Planned item")
        item_url = _v1(workspace.slug, project.id, f"work-items/{item.id}/")

        for label, (parent, allowed) in parents.items():
            created = bot_client.post(
                _v1(workspace.slug, project.id, "work-items/"),
                {"name": f"Child of {label}", "parent": str(parent.id), "assignees": []},
                format="json",
            )
            moved = bot_client.patch(item_url, {"parent": str(parent.id)}, format="json")
            expected = status.HTTP_201_CREATED if allowed else status.HTTP_403_FORBIDDEN
            assert created.status_code == expected, (label, created.data)
            assert moved.status_code == (status.HTTP_200_OK if allowed else status.HTTP_403_FORBIDDEN), label
            item.refresh_from_db()
            if allowed:
                assert item.parent_id == parent.id
            else:
                assert item.parent_id != parent.id

    def test_human_still_completes_and_assigns_bot_created_work_items(
        self, workspace, project, state, create_user, human_client, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, _bot_client = bot
        done = State.objects.create(name="Done", group="completed", project=project, workspace=workspace)
        item = _create_issue_by(bot_id, project, workspace, state, "Planned item")

        response = human_client.patch(
            _v1(workspace.slug, project.id, f"work-items/{item.id}/"),
            {"state": str(done.id), "assignees": [str(create_user.id)]},
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK, response.data
        item.refresh_from_db()
        assert item.state_id == done.id


@pytest.mark.contract
@pytest.mark.django_db
class TestAIBotHumanItemsStayHumanOnly:
    """``[Human]`` items stay human-only: a bot never assigns, renames or closes one, whoever is assigned to it."""

    PERMISSION = "[Human] permission to push to git remote and deploy to vps"

    @pytest.fixture
    def closing_states(self, workspace, project):
        done = State.objects.create(name="Done", group="completed", project=project, workspace=workspace)
        cancelled = State.objects.create(name="Cancelled", group="cancelled", project=project, workspace=workspace)
        return done, cancelled

    def test_bot_posts_the_permission_sub_item_unassigned_and_cannot_close_it(
        self, workspace, project, state, started_state, awaiting_state, closing_states, create_user, bot, monkeypatch
    ):
        # AC1 example: POST the sub item with assignees [] -> 201 in Awaiting Human; then PATCH {state: Done} -> 403.
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        done, cancelled = closing_states
        release = _create_issue_by(bot_id, project, workspace, started_state, "[Release] - develop", [bot_id])

        response = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": self.PERMISSION, "parent": str(release.id), "assignees": []},
            format="json",
        )

        assert response.status_code == status.HTTP_201_CREATED, response.data
        ticket = Issue.objects.get(pk=response.data["id"])
        assert ticket.parent_id == release.id
        assert ticket.state_id == awaiting_state.id
        assert not IssueAssignee.objects.filter(issue=ticket).exists()

        url = _v1(workspace.slug, project.id, f"work-items/{ticket.id}/")
        for target in (done, cancelled):
            response = bot_client.patch(url, {"state": str(target.id)}, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, target.name
        ticket.refresh_from_db()
        assert ticket.state_id == awaiting_state.id

    def test_bot_creates_human_items_only_with_an_empty_assignee_list(
        self, workspace, project, state, awaiting_state, create_user, bot, monkeypatch
    ):
        # AC1: top-level, under a parent the bot holds, and under a parent it planned: only ``assignees: []``.
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        held = _create_issue(project, workspace, state, create_user, "Bot task", [bot_id])
        planned = _create_issue_by(bot_id, project, workspace, state, "Planned epic")
        url = _v1(workspace.slug, project.id, "work-items/")

        for parent in (None, held, planned):
            where = {"parent": str(parent.id)} if parent is not None else {}
            for assignees in (
                {},  # would be auto-assigned to the bot
                {"assignees": [bot_id]},
                {"assignees": [str(create_user.id)]},
                {"assignees": [bot_id, str(create_user.id)]},
                {"assignees": None},
                {"assignees": bot_id},
            ):
                for name in ("[Human] Approve it", "  [human] approve it"):
                    response = bot_client.post(url, {"name": name, **where, **assignees}, format="json")
                    assert response.status_code == status.HTTP_403_FORBIDDEN, (where, assignees, name)
        assert not Issue.objects.filter(name__icontains="approve it").exists()

        for parent in (None, held, planned):
            where = {"parent": str(parent.id)} if parent is not None else {}
            response = bot_client.post(url, {"name": "[Human] Approve it", **where, "assignees": []}, format="json")
            assert response.status_code == status.HTTP_201_CREATED, (where, response.data)
            assert not IssueAssignee.objects.filter(issue_id=response.data["id"]).exists()
            assert str(response.data["state"]) == str(awaiting_state.id)

        # Form data follows the same rule.
        response = bot_client.post(url, {"name": "[Human] Approve it", "parent": str(held.id)}, format="multipart")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        # Sub items that are not [Human] tickets are still assigned to the bot.
        response = bot_client.post(url, {"name": "Local testing", "parent": str(held.id)}, format="json")
        assert response.status_code == status.HTTP_201_CREATED, response.data
        assignees = IssueAssignee.objects.filter(issue_id=response.data["id"]).values_list("assignee_id", flat=True)
        assert {str(assignee) for assignee in assignees} == {bot_id}

    def test_bot_cannot_rename_a_work_item_to_or_from_a_human_name(
        self, workspace, project, state, awaiting_state, create_user, bot, monkeypatch
    ):
        # AC2: 403 in both directions, even on work items the bot is assigned to.
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        url = lambda issue: _v1(workspace.slug, project.id, f"work-items/{issue.id}/")  # noqa: E731
        task = _create_issue(project, workspace, state, create_user, "Bot task", [bot_id])
        planned = _create_issue_by(bot_id, project, workspace, state, "Planned")
        held_gate = _create_issue(project, workspace, awaiting_state, create_user, "[Human] Approve release", [bot_id])
        own_gate = _create_issue_by(bot_id, project, workspace, awaiting_state, "[Human] Approve plan")

        for issue, names in (
            (task, ("[Human] Approve release of develop abc123", " [human] forged", "[HUMAN]")),
            (planned, ("[Human] Approve plan",)),
            (held_gate, ("Taken over", "[Human] Approve release of another commit", "")),
            (own_gate, ("Taken over", "[Human] Approve another plan")),
        ):
            original = issue.name
            for name in names:
                for body in ({"name": name}, {"name": name, "description_html": "<p>edited</p>"}):
                    response = bot_client.patch(url(issue), body, format="json")
                    assert response.status_code == status.HTTP_403_FORBIDDEN, (original, body)
            issue.refresh_from_db()
            assert issue.name == original
            assert issue.description_html != "<p>edited</p>"

        # Not a rename: the bot still renames its ordinary work, and may resend an unchanged name.
        response = bot_client.patch(url(task), {"name": "Bot task, refined"}, format="json")
        assert response.status_code == status.HTTP_200_OK, response.data
        response = bot_client.patch(url(held_gate), {"name": "[Human] Approve release"}, format="json")
        assert response.status_code == status.HTTP_200_OK, response.data

    def test_bot_cannot_assign_anyone_to_a_human_item_or_close_it(
        self, workspace, project, state, awaiting_state, closing_states, create_user, bot, monkeypatch
    ):
        # AC3: 403, also when a person assigned the bot to the [Human] item.
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        done, cancelled = closing_states
        closed_lookalike = State.objects.create(
            name="awaiting human ", group="completed", project=project, workspace=workspace
        )
        url = lambda issue: _v1(workspace.slug, project.id, f"work-items/{issue.id}/")  # noqa: E731
        held_gate = _create_issue(project, workspace, awaiting_state, create_user, "[Human] Approve release", [bot_id])
        own_gate = _create_issue_by(bot_id, project, workspace, awaiting_state, "[human] approve plan")
        foreign_gate = _create_issue(project, workspace, awaiting_state, create_user, "[Human] Decide")

        for gate in (held_gate, own_gate, foreign_gate):
            for body in (
                {"assignees": [bot_id]},
                {"assignees": [str(create_user.id)]},
                {"assignees": [bot_id, str(create_user.id)]},
                {"assignees": bot_id},
                {"state": str(done.id)},
                {"state": str(cancelled.id)},
                {"state": str(closed_lookalike.id)},
                {"state": str(done.id), "assignees": []},
                {"description_html": "<p>approved</p>", "state": str(done.id)},
            ):
                response = bot_client.patch(url(gate), body, format="json")
                assert response.status_code == status.HTTP_403_FORBIDDEN, (gate.name, body)
            gate.refresh_from_db()
            assert gate.state_id == awaiting_state.id
        assert {
            str(a) for a in IssueAssignee.objects.filter(issue=held_gate).values_list("assignee_id", flat=True)
        } == {bot_id}
        assert not IssueAssignee.objects.filter(issue__in=[own_gate, foreign_gate]).exists()

        # Form data follows the same rule.
        response = bot_client.patch(url(held_gate), {"state": str(done.id)}, format="multipart")
        assert response.status_code == status.HTTP_403_FORBIDDEN
        # The bot still closes its ordinary work: through In Review, since In Progress -> Done is no bot move.
        in_progress = State.objects.create(name="In Progress", group="started", project=project, workspace=workspace)
        in_review = State.objects.create(name="In Review", group="started", project=project, workspace=workspace)
        task = _create_issue(project, workspace, in_progress, create_user, "Bot task", [bot_id])
        for target in (in_review, done):
            response = bot_client.patch(url(task), {"state": str(target.id)}, format="json")
            assert response.status_code == status.HTTP_200_OK, response.data

    def test_humans_still_assign_rename_and_close_human_items(
        self, workspace, project, state, awaiting_state, closing_states, create_user, bot, human_client, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, _ = bot
        done, _cancelled = closing_states
        gate = _create_issue_by(bot_id, project, workspace, awaiting_state, self.PERMISSION)
        url = _v1(workspace.slug, project.id, f"work-items/{gate.id}/")

        for body in (
            {"assignees": [str(create_user.id)]},
            {"name": "[Human] permission to deploy"},
            {"state": str(done.id)},
        ):
            response = human_client.patch(url, body, format="json")
            assert response.status_code == status.HTTP_200_OK, (body, response.data)
        gate.refresh_from_db()
        assert gate.state_id == done.id

    def test_orchestrator_human_tickets_and_gate_moves_keep_working(
        self, workspace, project, state, awaiting_state, closing_states, create_user, bot, human_client, monkeypatch
    ):
        # AC4: the release approval sub item, the blocker ticket and the planner's gates (T35 moves).
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        done, _cancelled = closing_states
        backlog = State.objects.create(name="Backlog", group="backlog", project=project, workspace=workspace)
        items = _v1(workspace.slug, project.id, "work-items/")
        url = lambda issue_id: _v1(workspace.slug, project.id, f"work-items/{issue_id}/")  # noqa: E731

        release = bot_client.post(items, {"name": "[Release] - develop", "state": str(state.id)}, format="json")
        assert release.status_code == status.HTTP_201_CREATED, release.data
        approval = bot_client.post(
            items, {"name": self.PERMISSION, "parent": release.data["id"], "assignees": []}, format="json"
        )
        blocker = bot_client.post(
            items,
            {"name": "[Human] Approve release of develop abc123", "assignees": [], "state": str(awaiting_state.id)},
            format="json",
        )
        for ticket in (approval, blocker):
            assert ticket.status_code == status.HTTP_201_CREATED, ticket.data
            assert str(ticket.data["state"]) == str(awaiting_state.id)

        # The planner writes its gates in Backlog and hands them over when the cycle starts.
        for path in ((awaiting_state,), (state, awaiting_state)):
            gate = bot_client.post(
                items, {"name": "[Human] G5 gate", "assignees": [], "state": str(backlog.id)}, format="json"
            )
            assert gate.status_code == status.HTTP_201_CREATED, gate.data
            for target in path:
                response = bot_client.patch(url(gate.data["id"]), {"state": str(target.id)}, format="json")
                assert response.status_code == status.HTTP_200_OK, (target.name, response.data)
            assert Issue.objects.get(pk=gate.data["id"]).state_id == awaiting_state.id

        # The person answers by closing the ticket.
        response = human_client.patch(url(approval.data["id"]), {"state": str(done.id)}, format="json")
        assert response.status_code == status.HTTP_200_OK, response.data


@pytest.mark.contract
@pytest.mark.django_db
class TestAIBotCycleAccess:
    def test_bot_reads_creates_and_updates_cycles(self, workspace, project, create_user, bot, monkeypatch):
        _stub_tasks(monkeypatch)
        Project.objects.filter(pk=project.pk).update(cycle_view=True)
        _bot_id, bot_client = bot
        human_cycle = _create_cycle(project, workspace, create_user, "Human cycle")

        listing = bot_client.get(_v1(workspace.slug, project.id, "cycles/"))
        assert listing.status_code == status.HTTP_200_OK, listing.data
        assert str(human_cycle.id) in {str(cycle["id"]) for cycle in listing.data["results"]}

        created = bot_client.post(
            _v1(workspace.slug, project.id, "cycles/"),
            {
                "name": "C1",
                "start_date": "2026-10-05",
                "end_date": "2026-10-11",
                "external_source": "plane-planner",
                "external_id": "plan/C1",
            },
            format="json",
        )
        assert created.status_code == status.HTTP_201_CREATED, created.data

        for cycle_id in (created.data["id"], human_cycle.id):
            url = _v1(workspace.slug, project.id, f"cycles/{cycle_id}/")
            response = bot_client.patch(
                url,
                {
                    "name": "C1 - Bot writes",
                    "description": "Goal",
                    "start_date": "2026-10-06",
                    "end_date": "2026-10-12",
                },
                format="json",
            )
            assert response.status_code == status.HTTP_200_OK, response.data
            assert bot_client.get(url).status_code == status.HTTP_200_OK
        human_cycle.refresh_from_db()
        assert human_cycle.name == "C1 - Bot writes"

    def test_bot_cannot_delete_archive_or_change_other_cycle_fields(
        self, workspace, project, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        cycle = _create_cycle(project, workspace, create_user, "Human cycle")
        _set_created_by(cycle, bot_id)
        url = _v1(workspace.slug, project.id, f"cycles/{cycle.id}/")

        for body in ({"owned_by": bot_id}, {"name": "Renamed", "sort_order": 1}, {}):
            response = bot_client.patch(url, body, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, body
        assert bot_client.delete(url).status_code == status.HTTP_403_FORBIDDEN
        archive = bot_client.post(_v1(workspace.slug, project.id, f"cycles/{cycle.id}/archive/"))
        assert archive.status_code == status.HTTP_403_FORBIDDEN
        cycle.refresh_from_db()
        assert cycle.name == "Human cycle"
        assert cycle.archived_at is None
        assert Cycle.objects.filter(pk=cycle.id).exists()

    def test_bot_adds_work_items_to_a_cycle_and_transfers_open_ones(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        current = _create_cycle(project, workspace, create_user, "C1")
        following = _create_cycle(project, workspace, create_user, "C2", start_days=8, end_days=14)
        items = [_create_issue_by(bot_id, project, workspace, state, f"Planned {n}") for n in range(2)]

        added = bot_client.post(
            _v1(workspace.slug, project.id, f"cycles/{current.id}/cycle-issues/"),
            {"issues": [str(item.id) for item in items]},
            format="json",
        )
        assert added.status_code == status.HTTP_200_OK, added.data
        assert CycleIssue.objects.filter(cycle=current).count() == 2

        # Transfer only works once the cycle has ended.
        Cycle.objects.filter(pk=current.pk).update(
            start_date=timezone.now() - timedelta(days=8), end_date=timezone.now() - timedelta(days=1)
        )
        transfer = bot_client.post(
            _v1(workspace.slug, project.id, f"cycles/{current.id}/transfer-issues/"),
            {"new_cycle_id": str(following.id)},
            format="json",
        )
        assert transfer.status_code == status.HTTP_200_OK, transfer.data
        assert CycleIssue.objects.filter(cycle=following).count() == 2

        # Removing a work item from a cycle stays refused.
        remove = bot_client.delete(
            _v1(workspace.slug, project.id, f"cycles/{following.id}/cycle-issues/{items[0].id}/")
        )
        assert remove.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.contract
@pytest.mark.django_db
class TestAIBotModuleAccess:
    def test_bot_reads_creates_updates_modules_and_adds_work_items(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        Project.objects.filter(pk=project.pk).update(module_view=True)
        bot_id, bot_client = bot
        human_module = Module.objects.create(name="Human module", project=project, workspace=workspace)
        item = _create_issue_by(bot_id, project, workspace, state, "Planned item")

        listing = bot_client.get(_v1(workspace.slug, project.id, "modules/"))
        assert listing.status_code == status.HTTP_200_OK, listing.data
        assert str(human_module.id) in {str(module["id"]) for module in listing.data["results"]}

        created = bot_client.post(
            _v1(workspace.slug, project.id, "modules/"),
            {"name": "M1", "status": "planned", "external_source": "plane-planner", "external_id": "plan/M1"},
            format="json",
        )
        assert created.status_code == status.HTTP_201_CREATED, created.data

        url = _v1(workspace.slug, project.id, f"modules/{created.data['id']}/")
        response = bot_client.patch(
            url,
            {
                "name": "M1 - Bot writes",
                "description": "Serves R4",
                "status": "in-progress",
                "start_date": "2026-10-05",
                "target_date": "2026-10-25",
            },
            format="json",
        )
        assert response.status_code == status.HTTP_200_OK, response.data
        assert bot_client.get(url).status_code == status.HTTP_200_OK

        added = bot_client.post(
            _v1(workspace.slug, project.id, f"modules/{created.data['id']}/module-issues/"),
            {"issues": [str(item.id)]},
            format="json",
        )
        assert added.status_code == status.HTTP_200_OK, added.data
        assert ModuleIssue.objects.filter(module_id=created.data["id"], issue=item).exists()

    def test_bot_cannot_delete_archive_or_change_other_module_fields(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        module = _set_created_by(Module.objects.create(name="M1", project=project, workspace=workspace), bot_id)
        item = _create_issue_by(bot_id, project, workspace, state, "Planned item")
        ModuleIssue.objects.create(module=module, issue=item, project=project, workspace=workspace)
        url = _v1(workspace.slug, project.id, f"modules/{module.id}/")

        for body in ({"lead": str(create_user.id)}, {"name": "Renamed", "members": [str(create_user.id)]}, {}):
            response = bot_client.patch(url, body, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, body
        assert bot_client.delete(url).status_code == status.HTTP_403_FORBIDDEN
        archive = bot_client.post(_v1(workspace.slug, project.id, f"modules/{module.id}/archive/"))
        assert archive.status_code == status.HTTP_403_FORBIDDEN
        remove = bot_client.delete(_v1(workspace.slug, project.id, f"modules/{module.id}/module-issues/{item.id}/"))
        assert remove.status_code == status.HTTP_403_FORBIDDEN
        module.refresh_from_db()
        assert module.name == "M1"
        assert module.archived_at is None
        assert ModuleIssue.objects.filter(module=module, issue=item).exists()


@pytest.mark.contract
@pytest.mark.django_db
class TestAIBotLabelAccess:
    def test_bot_reads_and_creates_labels_but_cannot_change_them(
        self, workspace, project, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        human_label = Label.objects.create(name="bug", project=project, workspace=workspace)

        listing = bot_client.get(_v1(workspace.slug, project.id, "labels/"))
        assert listing.status_code == status.HTTP_200_OK, listing.data
        assert str(human_label.id) in {str(label["id"]) for label in listing.data["results"]}

        created = bot_client.post(
            _v1(workspace.slug, project.id, "labels/"), {"name": "lane:fork", "color": "#3a86ff"}, format="json"
        )
        assert created.status_code == status.HTTP_201_CREATED, created.data

        for label_id in (created.data["id"], human_label.id):
            url = _v1(workspace.slug, project.id, f"labels/{label_id}/")
            assert bot_client.get(url).status_code == status.HTTP_200_OK
            assert bot_client.patch(url, {"name": "renamed"}, format="json").status_code == status.HTTP_403_FORBIDDEN
            assert bot_client.delete(url).status_code == status.HTTP_403_FORBIDDEN
        assert Label.objects.filter(pk=human_label.id, name="bug").exists()
        assert Label.objects.filter(pk=created.data["id"], name="lane:fork").exists()

    def test_human_still_updates_and_deletes_labels(self, workspace, project, human_client, monkeypatch):
        _stub_tasks(monkeypatch)
        label = Label.objects.create(name="bug", project=project, workspace=workspace)
        url = _v1(workspace.slug, project.id, f"labels/{label.id}/")

        assert human_client.patch(url, {"name": "defect"}, format="json").status_code == status.HTTP_200_OK
        assert human_client.delete(url).status_code == status.HTTP_204_NO_CONTENT


@pytest.mark.contract
@pytest.mark.django_db
class TestAIBotEstimateAccess:
    def test_bot_creates_the_first_estimate_its_points_and_sets_it_on_the_project(
        self, workspace, project, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        _bot_id, bot_client = bot
        assert bot_client.get(_v1(workspace.slug, project.id, "estimates/")).status_code == status.HTTP_404_NOT_FOUND

        created = bot_client.post(
            _v1(workspace.slug, project.id, "estimates/"), {"name": "Points", "type": "points"}, format="json"
        )
        assert created.status_code == status.HTTP_201_CREATED, created.data
        estimate_id = created.data["id"]

        # The project keeps one estimate; a second POST answers 409 with the bot's estimate id.
        again = bot_client.post(
            _v1(workspace.slug, project.id, "estimates/"), {"name": "Points", "type": "points"}, format="json"
        )
        assert again.status_code == status.HTTP_409_CONFLICT
        assert str(again.data["id"]) == str(estimate_id)

        points_url = _v1(workspace.slug, project.id, f"estimates/{estimate_id}/estimate-points/")
        points = bot_client.post(
            points_url, {"estimate_points": [{"key": 1, "value": "1"}, {"key": 2, "value": "3"}]}, format="json"
        )
        assert points.status_code == status.HTTP_201_CREATED, points.data
        assert bot_client.get(points_url).status_code == status.HTTP_200_OK
        assert bot_client.get(_v1(workspace.slug, project.id, "estimates/")).status_code == status.HTTP_200_OK

        response = bot_client.patch(
            f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/", {"estimate": estimate_id}, format="json"
        )
        assert response.status_code == status.HTTP_200_OK, response.data
        project.refresh_from_db()
        assert str(project.estimate_id) == str(estimate_id)

        # Points and the estimate itself cannot be changed or deleted.
        point_id = points.data[0]["id"]
        point_url = _v1(workspace.slug, project.id, f"estimates/{estimate_id}/estimate-points/{point_id}/")
        assert bot_client.patch(point_url, {"value": "8"}, format="json").status_code == status.HTTP_403_FORBIDDEN
        assert bot_client.delete(point_url).status_code == status.HTTP_403_FORBIDDEN
        estimate_url = _v1(workspace.slug, project.id, "estimates/")
        assert bot_client.patch(estimate_url, {"name": "X"}, format="json").status_code == status.HTTP_403_FORBIDDEN
        assert bot_client.delete(estimate_url).status_code == status.HTTP_403_FORBIDDEN
        assert EstimatePoint.objects.filter(pk=point_id, value="1").exists()

    def test_bot_cannot_touch_an_estimate_created_by_a_human(self, workspace, project, create_user, bot, monkeypatch):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        human_estimate = _set_created_by(
            Estimate.objects.create(name="Hours", type="time", project=project, workspace=workspace), create_user.id
        )
        project_url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/"

        # A human estimate exists: no new estimate, no points on it.
        created = bot_client.post(
            _v1(workspace.slug, project.id, "estimates/"), {"name": "Points", "type": "points"}, format="json"
        )
        assert created.status_code == status.HTTP_403_FORBIDDEN
        points = bot_client.post(
            _v1(workspace.slug, project.id, f"estimates/{human_estimate.id}/estimate-points/"),
            {"estimate_points": [{"key": 1, "value": "1"}]},
            format="json",
        )
        assert points.status_code == status.HTTP_403_FORBIDDEN

        # The project uses the human estimate: the bot cannot replace it, not even with its own.
        Project.objects.filter(pk=project.pk).update(estimate=human_estimate)
        own = _set_created_by(
            Estimate.objects.create(name="Points", type="points", project=project, workspace=workspace), bot_id
        )
        for body in ({"estimate": str(own.id)}, {"estimate": None}):
            response = bot_client.patch(project_url, body, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, body
        project.refresh_from_db()
        assert project.estimate_id == human_estimate.id

    def test_bot_project_patch_is_limited_to_an_estimate_of_the_project(
        self, workspace, project, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        other_project = Project.objects.create(
            name="Other Project", identifier="OTH", workspace=workspace, created_by=create_user, updated_by=create_user
        )
        own = _set_created_by(
            Estimate.objects.create(name="Points", type="points", project=project, workspace=workspace), bot_id
        )
        elsewhere = _set_created_by(
            Estimate.objects.create(name="Points", type="points", project=other_project, workspace=workspace), bot_id
        )
        project_url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/"

        for body in (
            {"estimate": str(own.id), "name": "Renamed"},
            {"name": "Renamed"},
            {"estimate": str(elsewhere.id)},
            {"estimate": str(uuid4())},
            {"estimate": "not-a-uuid"},
            {"estimate": None},
            {},
        ):
            response = bot_client.patch(project_url, body, format="json")
            assert response.status_code == status.HTTP_403_FORBIDDEN, body
        assert bot_client.get(project_url).status_code == status.HTTP_403_FORBIDDEN
        assert bot_client.delete(project_url).status_code == status.HTTP_403_FORBIDDEN
        project.refresh_from_db()
        assert project.estimate_id is None
        assert project.name == "AI Access Project"

    def test_human_still_manages_estimates_and_the_project(self, workspace, project, human_client, bot, monkeypatch):
        _stub_tasks(monkeypatch)
        bot_id, _bot_client = bot
        bot_estimate = _set_created_by(
            Estimate.objects.create(name="Points", type="points", project=project, workspace=workspace), bot_id
        )
        project_url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/"

        response = human_client.patch(
            project_url, {"estimate": str(bot_estimate.id), "name": "Renamed by a human"}, format="json"
        )
        assert response.status_code == status.HTTP_200_OK, response.data
        points = human_client.post(
            _v1(workspace.slug, project.id, f"estimates/{bot_estimate.id}/estimate-points/"),
            {"estimate_points": [{"key": 1, "value": "5"}]},
            format="json",
        )
        assert points.status_code == status.HTTP_201_CREATED, points.data
        project.refresh_from_db()
        assert project.estimate_id == bot_estimate.id
        assert project.name == "Renamed by a human"


def _planning_requests(client, slug, project_id, cycle, module, estimate, item, skip=()):
    """(label, response) of every planning read and write the bot rules cover, except the labels in ``skip``."""
    project_url = f"/api/v1/workspaces/{slug}/projects/{project_id}/"
    issues = {"issues": [str(item.id)]}
    requests = [
        ("list cycles", "get", _v1(slug, project_id, "cycles/"), None),
        ("list modules", "get", _v1(slug, project_id, "modules/"), None),
        ("list labels", "get", _v1(slug, project_id, "labels/"), None),
        ("read estimate", "get", _v1(slug, project_id, "estimates/"), None),
        (
            "create cycle",
            "post",
            _v1(slug, project_id, "cycles/"),
            {"name": "C9", "start_date": "2026-10-05", "end_date": "2026-10-11"},
        ),
        ("create module", "post", _v1(slug, project_id, "modules/"), {"name": "M9"}),
        ("create label", "post", _v1(slug, project_id, "labels/"), {"name": "lane:x"}),
        ("create estimate", "post", _v1(slug, project_id, "estimates/"), {"name": "P", "type": "points"}),
        ("update cycle", "patch", _v1(slug, project_id, f"cycles/{cycle.id}/"), {"name": "X"}),
        ("update module", "patch", _v1(slug, project_id, f"modules/{module.id}/"), {"name": "X"}),
        ("add to cycle", "post", _v1(slug, project_id, f"cycles/{cycle.id}/cycle-issues/"), issues),
        ("add to module", "post", _v1(slug, project_id, f"modules/{module.id}/module-issues/"), issues),
        (
            "add estimate points",
            "post",
            _v1(slug, project_id, f"estimates/{estimate.id}/estimate-points/"),
            {"estimate_points": [{"key": 1, "value": "1"}]},
        ),
        ("set project estimate", "patch", project_url, {"estimate": str(estimate.id)}),
        ("create top-level item", "post", _v1(slug, project_id, "work-items/"), {"name": "Planned"}),
        ("update own item", "patch", _v1(slug, project_id, f"work-items/{item.id}/"), {"name": "Hijacked"}),
    ]
    return [
        (label, getattr(client, method)(url) if body is None else getattr(client, method)(url, body, format="json"))
        for label, method, url, body in requests
        if label not in skip
    ]


def _assert_planning_untouched(project, cycle, module, estimate, item):
    project.refresh_from_db()
    cycle.refresh_from_db()
    module.refresh_from_db()
    item.refresh_from_db()
    assert project.estimate_id is None
    assert cycle.name == "C1"
    assert module.name == "M1"
    assert item.name == "Planned item"
    assert Cycle.objects.filter(project=project).count() == 1
    assert Module.objects.filter(project=project).count() == 1
    assert Estimate.objects.filter(project=project).count() == 1
    assert not Label.objects.filter(project=project).exists()
    assert not EstimatePoint.objects.filter(estimate=estimate).exists()
    assert not CycleIssue.objects.filter(cycle=cycle).exists()
    assert not ModuleIssue.objects.filter(module=module).exists()
    assert Issue.objects.filter(project=project).count() == 1


@pytest.mark.contract
@pytest.mark.django_db
class TestAIBotPlanningAccessIsolation:
    """The planning rights belong to active AI_AGENT members of the workspace, and to nobody else."""

    @pytest.fixture
    def planning(self, workspace, project, state, create_user):
        """(cycle, module, estimate, work item) of the project; the estimate and the item get their creator later."""
        cycle = _create_cycle(project, workspace, create_user, "C1")
        module = Module.objects.create(name="M1", project=project, workspace=workspace)
        estimate = Estimate.objects.create(name="Points", type="points", project=project, workspace=workspace)
        item = _create_issue(project, workspace, state, None, "Planned item")
        return cycle, module, estimate, item

    def _owned_by(self, user_id, planning):
        _cycle, _module, estimate, item = planning
        _set_created_by(estimate, user_id)
        Issue.objects.filter(pk=item.pk).update(created_by_id=user_id)

    def test_active_bot_of_the_workspace_gets_every_planning_request(
        self, workspace, project, bot, planning, monkeypatch
    ):
        """Control for the refusals below: the same requests pass for the bot the rules are meant for."""
        _stub_tasks(monkeypatch)
        Project.objects.filter(pk=project.pk).update(cycle_view=True, module_view=True)
        bot_id, bot_client = bot
        self._owned_by(bot_id, planning)

        statuses = {
            label: response.status_code
            for label, response in _planning_requests(bot_client, workspace.slug, project.id, *planning)
        }
        # The fixture's estimate is the bot's own, so creating another one answers 409 with its id.
        assert statuses.pop("create estimate") == status.HTTP_409_CONFLICT
        assert len(statuses) == 15
        assert set(statuses.values()) <= {status.HTTP_200_OK, status.HTTP_201_CREATED}, statuses

    def test_bot_of_another_workspace_gets_nothing(
        self, session_client, workspace, project, create_user, planning, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        other_workspace = Workspace.objects.create(name="Other Workspace", owner=create_user, slug="other-workspace")
        WorkspaceMember.objects.create(workspace=other_workspace, member=create_user, role=20)
        foreign_id, foreign_client = _create_bot(session_client, other_workspace, "Foreign AI")
        self._owned_by(foreign_id, planning)

        for label, response in _planning_requests(foreign_client, workspace.slug, project.id, *planning):
            assert response.status_code == status.HTTP_403_FORBIDDEN, label
        _assert_planning_untouched(project, *planning)

    @pytest.mark.parametrize("membership", [{"is_active": False}, {"role": 5}])
    def test_bot_without_an_active_member_role_gets_nothing(
        self, workspace, project, bot, planning, membership, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        self._owned_by(bot_id, planning)
        WorkspaceMember.objects.filter(workspace=workspace, member_id=bot_id).update(**membership)

        for label, response in _planning_requests(bot_client, workspace.slug, project.id, *planning):
            assert response.status_code == status.HTTP_403_FORBIDDEN, label
        _assert_planning_untouched(project, *planning)

    def test_bot_that_is_not_an_ai_agent_gets_nothing(self, workspace, project, create_bot_user, planning, monkeypatch):
        _stub_tasks(monkeypatch)
        WorkspaceMember.objects.create(workspace=workspace, member=create_bot_user, role=15)
        token = APIToken.objects.create(user=create_bot_user, label="Plain bot", user_type=1)
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=token.token)
        self._owned_by(create_bot_user.id, planning)

        for label, response in _planning_requests(client, workspace.slug, project.id, *planning):
            assert response.status_code == status.HTTP_403_FORBIDDEN, label
        _assert_planning_untouched(project, *planning)

    def test_human_outside_the_project_gains_nothing_from_the_bot_rules(
        self, workspace, project, planning, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        outsider = User.objects.create(email="outsider@plane.so", username="outsider", first_name="Out")
        WorkspaceMember.objects.create(workspace=workspace, member=outsider, role=15)
        token = APIToken.objects.create(user=outsider, label="Outsider")
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=token.token)
        self._owned_by(outsider.id, planning)

        # Creating a label is left out: the unchanged ``ProjectMemberPermission`` decides a human's
        # ``POST`` by the workspace role alone, with or without the bot rules.
        requests = _planning_requests(client, workspace.slug, project.id, *planning, skip=("create label",))
        assert len(requests) == 15
        for label, response in requests:
            assert response.status_code == status.HTTP_403_FORBIDDEN, label
        _assert_planning_untouched(project, *planning)

    def test_project_guest_reads_but_does_not_write_planning_entities(self, workspace, project, planning, monkeypatch):
        _stub_tasks(monkeypatch)
        Project.objects.filter(pk=project.pk).update(cycle_view=True, module_view=True)
        guest = User.objects.create(email="guest@plane.so", username="guest", first_name="Guest")
        WorkspaceMember.objects.create(workspace=workspace, member=guest, role=5)
        ProjectMember.objects.create(workspace=workspace, project=project, member=guest, role=5, is_active=True)
        token = APIToken.objects.create(user=guest, label="Guest")
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=token.token)
        cycle, module, _estimate, _item = planning

        assert client.get(_v1(workspace.slug, project.id, "cycles/")).status_code == status.HTTP_200_OK
        assert client.get(_v1(workspace.slug, project.id, "modules/")).status_code == status.HTTP_200_OK
        for url, body in (
            (_v1(workspace.slug, project.id, "cycles/"), {"name": "C9"}),
            (_v1(workspace.slug, project.id, "modules/"), {"name": "M9"}),
        ):
            assert client.post(url, body, format="json").status_code == status.HTTP_403_FORBIDDEN, url
        for url in (
            _v1(workspace.slug, project.id, f"cycles/{cycle.id}/"),
            _v1(workspace.slug, project.id, f"modules/{module.id}/"),
        ):
            assert client.patch(url, {"name": "X"}, format="json").status_code == status.HTTP_403_FORBIDDEN, url

    def test_bot_sets_the_ai_model_of_its_own_work_item(self, workspace, project, state, bot, monkeypatch):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        item = _create_issue_by(bot_id, project, workspace, state, "Planned item")

        response = bot_client.patch(
            _v1(workspace.slug, project.id, f"work-items/{item.id}/"), {"ai_model": "opus-high"}, format="json"
        )
        assert response.status_code == status.HTTP_200_OK, response.data
        item.refresh_from_db()
        assert item.ai_model == "opus-high"
