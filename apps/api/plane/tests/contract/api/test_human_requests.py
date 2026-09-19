# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Human requests: a bot asks a question or an approval on its work item, a human answers in text.

Covers the public API (``/api/v1``: ask, list, retrieve, answer) and the web API
(``/api/workspaces/<slug>/human-requests/``: workspace list and answer).
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from plane.db.models import (
    AWAITING_HUMAN_STATE_NAME,
    HumanRequest,
    Issue,
    IssueActivity,
    IssueAssignee,
    IssueComment,
    Project,
    ProjectMember,
    State,
    User,
    WorkspaceMember,
)
from plane.bgtasks.issue_activities_task import issue_activity
from plane.utils import human_request as human_request_module
from plane.utils.human_request import HumanRequestError, parse_answer


def _v1_url(slug, project_id, issue_id, suffix=""):
    return f"/api/v1/workspaces/{slug}/projects/{project_id}/work-items/{issue_id}/human-requests/{suffix}"


def _answer_url(slug, project_id, issue_id, request_id):
    return _v1_url(slug, project_id, issue_id, f"{request_id}/answer/")


def _web_url(slug, suffix=""):
    return f"/api/workspaces/{slug}/human-requests/{suffix}"


def _create_bot(web_client, workspace, display_name):
    response = web_client.post(
        f"/api/workspaces/{workspace.slug}/ai-bot-members/", {"display_name": display_name}, format="json"
    )
    assert response.status_code == status.HTTP_201_CREATED, response.data
    bot_id = str(response.data["workspace_member"]["member"]["id"])
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=response.data["api_token"]["token"])
    return bot_id, client


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
        name="Human Requests", identifier="YGG", workspace=workspace, created_by=create_user, updated_by=create_user
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
        todo=create("Todo", "unstarted", 25000, default=True),
        in_progress=create("In Progress", "started", 35000),
        awaiting=create(AWAITING_HUMAN_STATE_NAME, "started", 42500),
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
    bot_id, client = _create_bot(human_web, workspace, "Lucy")
    return SimpleNamespace(id=bot_id, client=client)


@pytest.fixture
def bot_item(workspace, project, states, create_user, bot):
    """YGG-5: In Progress and assigned to the bot."""
    return _create_issue(project, workspace, states.in_progress, create_user, "YGG-5", [bot.id])


def _ask(bot, workspace, project, issue, kind, question):
    return bot.client.post(
        _v1_url(workspace.slug, project.id, issue.id), {"kind": kind, "question": question}, format="json"
    )


def _open_request(bot, workspace, project, issue, kind="approval", question="Push abc123 and deploy?"):
    response = _ask(bot, workspace, project, issue, kind, question)
    assert response.status_code == status.HTTP_201_CREATED, response.data
    return response.data["id"]


@pytest.mark.contract
@pytest.mark.django_db
class TestAskHuman:
    def test_ac1_bot_asks_approval_and_item_awaits_human(self, workspace, project, states, bot, bot_item):
        response = _ask(bot, workspace, project, bot_item, "approval", "Push abc123 and deploy?")

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert response.data["kind"] == "approval"
        assert response.data["question"] == "Push abc123 and deploy?"
        assert response.data["is_open"] is True
        assert response.data["decision"] == ""
        assert str(response.data["requested_by"]) == bot.id
        assert str(response.data["state_before"]) == str(states.in_progress.id)
        bot_item.refresh_from_db()
        assert bot_item.state_id == states.awaiting.id

    def test_ac1_bot_cannot_ask_on_item_not_assigned_to_it(self, workspace, project, states, create_user, bot):
        other = _create_issue(project, workspace, states.in_progress, create_user, "Not mine")

        response = _ask(bot, workspace, project, other, "question", "Which DB for tests?")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not HumanRequest.objects.filter(issue=other).exists()
        other.refresh_from_db()
        assert other.state_id == states.in_progress.id

    @pytest.mark.parametrize(
        "payload",
        [
            {"kind": "poll", "question": "Which DB for tests?"},
            {"kind": "question", "question": "   "},
            {"question": "Which DB for tests?"},
        ],
    )
    def test_ac1_invalid_request_is_400(self, workspace, project, states, bot, bot_item, payload):
        response = bot.client.post(_v1_url(workspace.slug, project.id, bot_item.id), payload, format="json")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        bot_item.refresh_from_db()
        assert bot_item.state_id == states.in_progress.id

    def test_ac1_second_open_request_on_the_item_is_409(self, workspace, project, states, bot, bot_item):
        first = _open_request(bot, workspace, project, bot_item)

        response = _ask(bot, workspace, project, bot_item, "question", "Which DB for tests?")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert response.data["human_request"] == str(first)
        assert HumanRequest.objects.filter(issue=bot_item).count() == 1


@pytest.mark.contract
@pytest.mark.django_db
class TestAnswerHumanRequest:
    def test_ac2_question_answer_moves_item_to_todo_and_comments(
        self, workspace, project, states, create_user, bot, bot_item, human_api
    ):
        request_id = _open_request(bot, workspace, project, bot_item, "question", "Which DB for tests?")

        response = human_api.post(
            _answer_url(workspace.slug, project.id, bot_item.id, request_id), {"answer": "use sqlite"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["decision"] == "answered"
        assert response.data["answer"] == "use sqlite"
        assert response.data["is_open"] is False
        assert str(response.data["resolved_by"]) == str(create_user.id)
        assert response.data["resolved_at"] is not None
        # Awaiting Human -> Todo (not back to In Progress): the orchestrator picks it up again.
        assert str(response.data["state_before"]) == str(states.in_progress.id)
        bot_item.refresh_from_db()
        assert bot_item.state_id == states.todo.id
        comments = IssueComment.objects.filter(issue=bot_item)
        assert [comment.comment_stripped for comment in comments] == ["Answer: use sqlite"]
        assert comments[0].actor_id == create_user.id

        # The bot reads the answer back (the orchestrator resumes from it).
        detail = bot.client.get(_v1_url(workspace.slug, project.id, bot_item.id, f"{request_id}/"))
        assert detail.status_code == status.HTTP_200_OK
        assert detail.data["decision"] == "answered"
        assert detail.data["answer"] == "use sqlite"

    def test_ac2_activity_records_state_moves_and_answer_comment(
        self, workspace, project, states, bot, bot_item, human_api, monkeypatch, django_capture_on_commit_callbacks
    ):
        """The activity tasks are queued after commit; run them inline to check what they record."""
        queued = []
        monkeypatch.setattr(
            human_request_module, "issue_activity", SimpleNamespace(delay=lambda **kwargs: queued.append(kwargs))
        )
        with django_capture_on_commit_callbacks(execute=True):
            request_id = _open_request(bot, workspace, project, bot_item, "question", "Which DB for tests?")
        with django_capture_on_commit_callbacks(execute=True):
            human_api.post(
                _answer_url(workspace.slug, project.id, bot_item.id, request_id),
                {"answer": "use sqlite"},
                format="json",
            )

        assert [kwargs["type"] for kwargs in queued] == [
            "issue.activity.updated",
            "issue.activity.updated",
            "comment.activity.created",
        ]
        for kwargs in queued:
            issue_activity(**kwargs)
        activities = IssueActivity.objects.filter(issue=bot_item)
        states_moved = activities.filter(field="state").order_by("created_at")
        assert [(a.old_value, a.new_value) for a in states_moved] == [
            ("In Progress", AWAITING_HUMAN_STATE_NAME),
            (AWAITING_HUMAN_STATE_NAME, "Todo"),
        ]
        assert activities.filter(field="comment", verb="created").exists()

    def test_ac2_empty_answer_is_400(self, workspace, project, states, bot, bot_item, human_api):
        request_id = _open_request(bot, workspace, project, bot_item, "question", "Which DB for tests?")

        response = human_api.post(
            _answer_url(workspace.slug, project.id, bot_item.id, request_id), {"answer": "  "}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert HumanRequest.objects.get(pk=request_id).is_open

    def test_ac2_answer_body_that_is_not_an_object_is_400(
        self, workspace, project, states, bot, bot_item, human_api, human_web
    ):
        request_id = _open_request(bot, workspace, project, bot_item, "question", "Which DB for tests?")

        v1 = human_api.post(_answer_url(workspace.slug, project.id, bot_item.id, request_id), ["yes"], format="json")
        web = human_web.post(_web_url(workspace.slug, f"{request_id}/answer/"), ["yes"], format="json")

        assert v1.status_code == status.HTTP_400_BAD_REQUEST
        assert web.status_code == status.HTTP_400_BAD_REQUEST
        assert HumanRequest.objects.get(pk=request_id).is_open

    def test_ac3_approval_deny_keeps_the_note(self, workspace, project, states, bot, bot_item, human_api):
        request_id = _open_request(bot, workspace, project, bot_item)

        response = human_api.post(
            _answer_url(workspace.slug, project.id, bot_item.id, request_id),
            {"answer": "no - wait for Monday"},
            format="json",
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["decision"] == "deny"
        assert response.data["note"] == "wait for Monday"
        assert response.data["answer"] == "no - wait for Monday"
        bot_item.refresh_from_db()
        assert bot_item.state_id == states.todo.id
        assert IssueComment.objects.get(issue=bot_item).comment_stripped == "Answer: no - wait for Monday"

    def test_ac3_approval_maybe_is_400_and_stays_open(self, workspace, project, states, bot, bot_item, human_api):
        request_id = _open_request(bot, workspace, project, bot_item)

        response = human_api.post(
            _answer_url(workspace.slug, project.id, bot_item.id, request_id), {"answer": "maybe"}, format="json"
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert HumanRequest.objects.get(pk=request_id).is_open
        bot_item.refresh_from_db()
        assert bot_item.state_id == states.awaiting.id
        assert not IssueComment.objects.filter(issue=bot_item).exists()

    @pytest.mark.parametrize(
        "text, decision, note",
        [
            ("yes", "accept", ""),
            ("YES", "accept", ""),
            ("Accept, ship it", "accept", "ship it"),
            ("no - wait for Monday", "deny", "wait for Monday"),
            ("Deny: not today", "deny", "not today"),
            ("  no\nwrong branch", "deny", "wrong branch"),
        ],
    )
    def test_ac3_approval_answers(self, text, decision, note):
        assert parse_answer("approval", text) == (decision, note)

    @pytest.mark.parametrize("text", ["maybe", "yesterday", "nope", "acceptable", "ok, yes", "", None])
    def test_ac3_approval_answers_without_yes_or_no_are_400(self, text):
        with pytest.raises(HumanRequestError) as error:
            parse_answer("approval", text)
        assert error.value.status_code == 400

    def test_ac4_bot_token_on_answer_is_403(self, workspace, project, states, bot, bot_item):
        request_id = _open_request(bot, workspace, project, bot_item)

        response = bot.client.post(
            _answer_url(workspace.slug, project.id, bot_item.id, request_id), {"answer": "yes"}, format="json"
        )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert HumanRequest.objects.get(pk=request_id).is_open
        bot_item.refresh_from_db()
        assert bot_item.state_id == states.awaiting.id

    def test_ac4_bot_in_web_app_is_403_even_as_project_member(self, workspace, project, states, bot, bot_item):
        request_id = _open_request(bot, workspace, project, bot_item)
        ProjectMember.objects.create(workspace=workspace, project=project, member_id=bot.id, role=15, is_active=True)
        bot_web = APIClient()
        bot_web.force_authenticate(user=User.objects.get(pk=bot.id))

        response = bot_web.post(_web_url(workspace.slug, f"{request_id}/answer/"), {"answer": "yes"}, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert HumanRequest.objects.get(pk=request_id).is_open

    def test_ac5_second_answer_is_409(self, workspace, project, states, bot, bot_item, human_api):
        request_id = _open_request(bot, workspace, project, bot_item)
        url = _answer_url(workspace.slug, project.id, bot_item.id, request_id)
        assert human_api.post(url, {"answer": "yes"}, format="json").status_code == status.HTTP_200_OK

        response = human_api.post(url, {"answer": "no"}, format="json")

        assert response.status_code == status.HTTP_409_CONFLICT
        human_request = HumanRequest.objects.get(pk=request_id)
        assert (human_request.decision, human_request.answer) == ("accept", "yes")
        assert IssueComment.objects.filter(issue=bot_item).count() == 1

    def test_ac2_web_answer_by_project_member(self, workspace, project, states, bot, bot_item, human_web):
        request_id = _open_request(bot, workspace, project, bot_item)

        response = human_web.post(
            _web_url(workspace.slug, f"{request_id}/answer/"), {"answer": "Yes, go ahead"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["decision"] == "accept"
        assert response.data["note"] == "go ahead"
        bot_item.refresh_from_db()
        assert bot_item.state_id == states.todo.id
        again = human_web.post(_web_url(workspace.slug, f"{request_id}/answer/"), {"answer": "yes"}, format="json")
        assert again.status_code == status.HTTP_409_CONFLICT


@pytest.mark.contract
@pytest.mark.django_db
class TestListHumanRequests:
    def _request(self, issue, user, minutes_ago, decision=""):
        return HumanRequest.objects.create(
            issue=issue,
            project=issue.project,
            kind="question",
            question=f"{issue.name} {minutes_ago}",
            requested_by=user,
            requested_at=timezone.now() - timedelta(minutes=minutes_ago),
            decision=decision,
            answer="done" if decision else "",
        )

    def test_ac6_item_list_is_open_newest_first(self, workspace, project, states, create_user, bot, bot_item):
        answered = self._request(bot_item, create_user, 120, decision="answered")
        older_answered = self._request(bot_item, create_user, 240, decision="answered")
        open_request = self._request(bot_item, create_user, 60)
        url = _v1_url(workspace.slug, project.id, bot_item.id)

        def ids(query=""):
            response = bot.client.get(url + query)
            assert response.status_code == status.HTTP_200_OK, response.data
            return [str(row["id"]) for row in response.data["results"]]

        assert ids() == [str(open_request.id)]
        assert ids("?status=all") == [str(open_request.id), str(answered.id), str(older_answered.id)]
        assert ids("?status=answered") == [str(answered.id), str(older_answered.id)]
        assert bot.client.get(url + "?status=closed").status_code == status.HTTP_400_BAD_REQUEST

    def test_ac6_workspace_list_is_open_newest_first_in_my_projects(
        self, workspace, project, states, create_user, human_web
    ):
        first = _create_issue(project, workspace, states.awaiting, create_user, "First")
        second = _create_issue(project, workspace, states.awaiting, create_user, "Second")
        older = self._request(first, create_user, 30)
        newer = self._request(second, create_user, 5)
        self._request(first, create_user, 90, decision="answered")
        hidden_project = Project.objects.create(
            name="Hidden", identifier="HID", workspace=workspace, created_by=create_user, updated_by=create_user
        )
        hidden_state = State.objects.create(
            name="In Progress", group="started", project=hidden_project, workspace=workspace
        )
        self._request(_create_issue(hidden_project, workspace, hidden_state, create_user, "Hidden"), create_user, 1)

        response = human_web.get(_web_url(workspace.slug))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert [row["id"] for row in response.data] == [newer.id, older.id]
        assert response.data[0]["project_identifier"] == "YGG"
        assert response.data[0]["issue_sequence_id"] == second.sequence_id
        assert response.data[0]["issue_name"] == "Second"
        assert response.data[0]["is_open"] is True

        one_item = human_web.get(_web_url(workspace.slug), {"issue_id": str(first.id)})
        assert [row["id"] for row in one_item.data] == [older.id]
        assert human_web.get(_web_url(workspace.slug), {"issue_id": "nope"}).status_code == 400


@pytest.mark.contract
@pytest.mark.django_db
class TestHumanRequestEdgeCases:
    """Release checks for the paths around the examples: where the item goes back to, and who may answer."""

    def _web_user(self, workspace, email, workspace_role=15):
        user = User.objects.create(email=email, username=email.split("@")[0], first_name="Web", last_name="User")
        WorkspaceMember.objects.create(workspace=workspace, member=user, role=workspace_role)
        client = APIClient()
        client.force_authenticate(user=user)
        return user, client

    def test_answer_leaves_an_item_that_was_moved_elsewhere_in_place(
        self, workspace, project, states, bot, bot_item, human_api
    ):
        done = State.objects.create(
            name="Done", group="completed", sequence=60000, project=project, workspace=workspace
        )
        request_id = _open_request(bot, workspace, project, bot_item)
        Issue.objects.filter(pk=bot_item.pk).update(state=done)

        response = human_api.post(
            _answer_url(workspace.slug, project.id, bot_item.id, request_id), {"answer": "yes"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["decision"] == "accept"
        bot_item.refresh_from_db()
        assert bot_item.state_id == done.id
        assert IssueComment.objects.get(issue=bot_item).comment_stripped == "Answer: yes"

    def test_asking_from_backlog_and_the_answer_moves_the_item_to_todo(
        self, workspace, project, states, create_user, bot, human_api
    ):
        backlog = State.objects.create(
            name="Backlog", group="backlog", sequence=15000, project=project, workspace=workspace
        )
        issue = _create_issue(project, workspace, backlog, create_user, "Unclear", [bot.id])
        request_id = _open_request(bot, workspace, project, issue, "question", "Which DB for tests?")
        assert HumanRequest.objects.get(pk=request_id).state_before_id == backlog.id

        response = human_api.post(
            _answer_url(workspace.slug, project.id, issue.id, request_id), {"answer": "use sqlite"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        issue.refresh_from_db()
        assert issue.state_id == states.todo.id

    @pytest.mark.parametrize(
        "name,group", [("Todo", "unstarted"), ("Done", "completed"), ("Cancelled", "cancelled"), ("Review", "started")]
    )
    def test_asking_outside_backlog_in_progress_or_in_review_is_400(
        self, workspace, project, states, create_user, bot, name, group
    ):
        # Only Backlog, In Progress and In Review may move to Awaiting Human (the work item state machine).
        current = State.objects.filter(project=project, name=name).first() or State.objects.create(
            name=name, group=group, sequence=60000, project=project, workspace=workspace
        )
        issue = _create_issue(project, workspace, current, create_user, f"In {name}", [bot.id])

        response = _ask(bot, workspace, project, issue, "question", "Continue?")

        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.data
        assert name in response.data["error"]
        assert response.data["current_state"] == name
        assert response.data["requested_state"] == AWAITING_HUMAN_STATE_NAME
        assert response.data["allowed_from_states"] == ["Backlog", "In Progress", "In Review"]
        assert "in Backlog, In Progress or In Review, not in" in response.data["error"]
        assert not HumanRequest.objects.filter(issue=issue).exists()
        issue.refresh_from_db()
        assert issue.state_id == current.id

    def test_asking_on_an_item_already_awaiting_human_then_answer_moves_it_to_todo(
        self, workspace, project, states, create_user, bot, human_api
    ):
        issue = _create_issue(project, workspace, states.awaiting, create_user, "Waiting", [bot.id])

        response = _ask(bot, workspace, project, issue, "question", "Which DB for tests?")

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert response.data["state_before"] is None
        answer = human_api.post(
            _answer_url(workspace.slug, project.id, issue.id, response.data["id"]),
            {"answer": "use sqlite"},
            format="json",
        )
        assert answer.status_code == status.HTTP_200_OK, answer.data
        issue.refresh_from_db()
        assert issue.state_id == states.todo.id

    def test_answer_uses_the_default_unstarted_state_when_no_state_is_named_todo(
        self, workspace, project, states, bot, bot_item, human_api
    ):
        State.objects.filter(pk=states.todo.pk).update(name="Ready")
        later = State.objects.create(
            name="Queued", group="unstarted", sequence=20000, project=project, workspace=workspace
        )
        request_id = _open_request(bot, workspace, project, bot_item, "question", "Which DB for tests?")

        response = human_api.post(
            _answer_url(workspace.slug, project.id, bot_item.id, request_id), {"answer": "use sqlite"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        bot_item.refresh_from_db()
        # "Ready" is the project default; "Queued" comes first by sequence but is not the default.
        assert bot_item.state_id == states.todo.id
        assert later.id != states.todo.id

    def test_asking_in_a_project_without_awaiting_human_state_is_400(self, workspace, project, states, bot, bot_item):
        State.objects.filter(pk=states.awaiting.pk).update(name="Blocked")

        response = _ask(bot, workspace, project, bot_item, "approval", "Push abc123 and deploy?")

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert AWAITING_HUMAN_STATE_NAME in response.data["error"]
        assert not HumanRequest.objects.filter(issue=bot_item).exists()
        bot_item.refresh_from_db()
        assert bot_item.state_id == states.in_progress.id

    def test_awaiting_human_state_name_matches_case_insensitively(self, workspace, project, states, bot, bot_item):
        State.objects.filter(pk=states.awaiting.pk).update(name="awaiting human")

        response = _ask(bot, workspace, project, bot_item, "question", "Which DB for tests?")

        assert response.status_code == status.HTTP_201_CREATED, response.data
        bot_item.refresh_from_db()
        assert bot_item.state_id == states.awaiting.id

    def test_web_answer_needs_an_admin_or_member_role_in_the_project(self, workspace, project, states, bot, bot_item):
        request_id = _open_request(bot, workspace, project, bot_item)
        url = _web_url(workspace.slug, f"{request_id}/answer/")
        guest, guest_client = self._web_user(workspace, "guest@plane.so")
        ProjectMember.objects.create(workspace=workspace, project=project, member=guest, role=5, is_active=True)
        _outsider, outsider_client = self._web_user(workspace, "outsider@plane.so")

        # A project guest sees the request but cannot answer it; a workspace member outside the project sees nothing.
        assert [row["id"] for row in guest_client.get(_web_url(workspace.slug)).data] == [HumanRequest.objects.get().id]
        assert guest_client.post(url, {"answer": "yes"}, format="json").status_code == status.HTTP_403_FORBIDDEN
        assert outsider_client.get(_web_url(workspace.slug)).data == []
        assert outsider_client.post(url, {"answer": "yes"}, format="json").status_code == status.HTTP_404_NOT_FOUND
        assert HumanRequest.objects.get(pk=request_id).is_open

    def test_web_list_hides_requests_of_archived_or_deleted_items(
        self, workspace, project, states, create_user, bot, human_web
    ):
        archived = _create_issue(project, workspace, states.in_progress, create_user, "Archived", [bot.id])
        deleted = _create_issue(project, workspace, states.in_progress, create_user, "Deleted", [bot.id])
        live = _create_issue(project, workspace, states.in_progress, create_user, "Live", [bot.id])
        for issue in (archived, deleted, live):
            _open_request(bot, workspace, project, issue, "question", f"{issue.name}?")
        Issue.objects.filter(pk=archived.pk).update(archived_at=timezone.now().date())
        Issue.all_objects.filter(pk=deleted.pk).update(deleted_at=timezone.now())

        response = human_web.get(_web_url(workspace.slug))

        assert response.status_code == status.HTTP_200_OK, response.data
        assert [row["issue_name"] for row in response.data] == ["Live"]


@pytest.mark.contract
@pytest.mark.django_db
class TestHumanRequestInReview:
    """A blocked merge asks the owner on the item in review, and the answer resumes the review."""

    @pytest.fixture
    def in_review(self, workspace, project, states):
        return State.objects.create(
            name="In Review", group="started", sequence=38750, project=project, workspace=workspace
        )

    @pytest.fixture
    def review_item(self, workspace, project, in_review, create_user, bot):
        return _create_issue(project, workspace, in_review, create_user, "YGG-6", [bot.id])

    def test_ac3_request_on_an_in_review_item_and_the_answer_moves_it_back(
        self, workspace, project, states, in_review, review_item, bot, human_api
    ):
        response = _ask(bot, workspace, project, review_item, "question", "The merge conflicts in api.py: whose side?")

        assert response.status_code == status.HTTP_201_CREATED, response.data
        assert str(response.data["state_before"]) == str(in_review.id)
        review_item.refresh_from_db()
        assert review_item.state_id == states.awaiting.id

        answer = human_api.post(
            _answer_url(workspace.slug, project.id, review_item.id, response.data["id"]),
            {"answer": "keep develop"},
            format="json",
        )

        assert answer.status_code == status.HTTP_200_OK, answer.data
        assert answer.data["decision"] == "answered"
        review_item.refresh_from_db()
        assert review_item.state_id == in_review.id
        assert IssueComment.objects.get(issue=review_item).comment_stripped == "Answer: keep develop"

    @pytest.mark.parametrize("text,decision", [("yes", "accept"), ("no, fix the tests first", "deny")])
    def test_ac3_web_approval_answer_moves_the_item_back_to_in_review(
        self, workspace, project, states, in_review, review_item, bot, human_web, text, decision
    ):
        request_id = _open_request(bot, workspace, project, review_item)

        response = human_web.post(_web_url(workspace.slug, f"{request_id}/answer/"), {"answer": text}, format="json")

        assert response.status_code == status.HTTP_200_OK, response.data
        assert response.data["decision"] == decision
        review_item.refresh_from_db()
        assert review_item.state_id == in_review.id

    def test_ac3_in_review_state_name_matches_case_insensitively(
        self, workspace, project, states, in_review, review_item, bot, human_api
    ):
        State.objects.filter(pk=in_review.pk).update(name="in  REVIEW")
        request_id = _open_request(bot, workspace, project, review_item)

        response = human_api.post(
            _answer_url(workspace.slug, project.id, review_item.id, request_id), {"answer": "yes"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        review_item.refresh_from_db()
        assert review_item.state_id == in_review.id

    def test_ac3_activity_records_the_move_back_to_in_review(
        self, workspace, project, states, in_review, review_item, bot, human_api, monkeypatch
    ):
        calls = []
        monkeypatch.setattr(human_request_module, "_dispatch_activity", lambda **kwargs: calls.append(kwargs))
        request_id = _open_request(bot, workspace, project, review_item)

        response = human_api.post(
            _answer_url(workspace.slug, project.id, review_item.id, request_id), {"answer": "yes"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        moves = [call["requested_data"] for call in calls if call["type"] == "issue.activity.updated"]
        assert moves == [f'{{"state_id": "{states.awaiting.id}"}}', f'{{"state_id": "{in_review.id}"}}']

    @pytest.mark.parametrize("change", ["deleted", "renamed"])
    def test_ac3_answer_falls_back_to_todo_when_the_in_review_state_is_gone(
        self, workspace, project, states, in_review, review_item, bot, human_api, change
    ):
        request_id = _open_request(bot, workspace, project, review_item)
        if change == "deleted":
            State.all_state_objects.filter(pk=in_review.pk).update(deleted_at=timezone.now())
        else:
            State.objects.filter(pk=in_review.pk).update(name="Code Review")

        response = human_api.post(
            _answer_url(workspace.slug, project.id, review_item.id, request_id), {"answer": "yes"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        review_item.refresh_from_db()
        assert review_item.state_id == states.todo.id

    def test_ac3_answer_leaves_a_review_item_that_was_moved_elsewhere_in_place(
        self, workspace, project, states, in_review, review_item, bot, human_api
    ):
        request_id = _open_request(bot, workspace, project, review_item)
        Issue.objects.filter(pk=review_item.pk).update(state=states.in_progress)

        response = human_api.post(
            _answer_url(workspace.slug, project.id, review_item.id, request_id), {"answer": "yes"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        review_item.refresh_from_db()
        assert review_item.state_id == states.in_progress.id

    def test_ac3_requests_asked_outside_in_review_still_resume_in_todo(
        self, workspace, project, states, in_review, bot, bot_item, human_api
    ):
        # The project has an In Review state, but the item was asked in In Progress: Todo as before.
        request_id = _open_request(bot, workspace, project, bot_item)

        response = human_api.post(
            _answer_url(workspace.slug, project.id, bot_item.id, request_id), {"answer": "yes"}, format="json"
        )

        assert response.status_code == status.HTTP_200_OK, response.data
        bot_item.refresh_from_db()
        assert bot_item.state_id == states.todo.id
