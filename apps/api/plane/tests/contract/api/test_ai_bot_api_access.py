# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Public API (``/api/v1``) access rules for workspace AI_AGENT bots.

A bot authenticated with its API key can:

- read every work item, comment, relation and activity in the workspace,
- update only work items it is assigned to,
- create sub work items under work items it is assigned to,
- create unassigned top-level work items that ask a human for an action,
- comment on any work item and edit only its own comments,
- add links to work items it is assigned to and edit or delete only its own links,
- create relations between any work items,
- read public pages, create pages, and update only pages it owns.
"""

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import (
    Issue,
    IssueAssignee,
    IssueComment,
    IssueLink,
    IssueRelation,
    Page,
    Project,
    ProjectMember,
    ProjectPage,
    State,
)


def _v1(slug, project_id, suffix=""):
    return f"/api/v1/workspaces/{slug}/projects/{project_id}/{suffix}"


def _stub_tasks(monkeypatch):
    monkeypatch.setattr("plane.api.views.issue.issue_activity.delay", lambda **kwargs: None)
    monkeypatch.setattr("plane.api.views.issue.model_activity.delay", lambda **kwargs: None)
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

    def test_bot_cannot_create_top_level_or_foreign_sub_work_items(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        _bot_id, bot_client = bot
        human = _create_issue(project, workspace, state, create_user, "Human epic", [str(create_user.id)])

        top_level = bot_client.post(_v1(workspace.slug, project.id, "work-items/"), {"name": "Rogue"}, format="json")
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

        assigned_to_self = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "Rogue assigned", "assignees": [_bot_id]},
            format="json",
        )

        assert top_level.status_code == status.HTTP_403_FORBIDDEN
        assert assigned_to_self.status_code == status.HTTP_403_FORBIDDEN
        assert foreign_child.status_code == status.HTTP_403_FORBIDDEN
        assert bad_parent.status_code == status.HTTP_403_FORBIDDEN
        assert Issue.objects.filter(name__startswith="Rogue").count() == 0


    def test_bot_creates_unassigned_ticket_for_humans_that_blocks_its_work_item(
        self, workspace, project, state, create_user, bot, monkeypatch
    ):
        _stub_tasks(monkeypatch)
        bot_id, bot_client = bot
        blocked = _create_issue(project, workspace, state, create_user, "Bot task", [bot_id])

        response = bot_client.post(
            _v1(workspace.slug, project.id, "work-items/"),
            {"name": "Human: choose where the script lives", "assignees": [], "state": str(state.id)},
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
