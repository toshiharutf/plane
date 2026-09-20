# ruff: noqa: F401, F811  # pytest fixtures are intentionally re-exported
"""Adversarial continuation, identity, authority, and history regressions."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from plane.db.models import (
    Deployment,
    ReleaseCandidate,
    ReleaseScope,
    WorkflowCapability,
    WorkflowDecision,
    WorkflowEnvironment,
    WorkflowEvent,
    WorkflowLease,
    WorkflowOperation,
)
from plane.tests.workflow_fixtures import workflow, perform, make_work, acquire, fenced
from plane.workflow.service import Refusal, digest

pytestmark = pytest.mark.django_db


def test_questions_are_independent_and_denial_cannot_be_actionable(workflow):
    work = make_work(workflow)
    for key in ("first", "second"):
        perform(workflow, "work", "ask_human", work, {"request_key": key, "question": "Which approach?"})
    response = perform(
        workflow, "work", "resolve_human", work, {"request_key": "first", "answer": "Use A", "resolution": "actionable"}
    )
    assert response["state"] == "Awaiting Human"
    response = perform(
        workflow, "work", "resolve_human", work, {"request_key": "second", "answer": "No", "resolution": "actionable"}
    )
    assert response["state"] == "Awaiting Human"
    response = perform(
        workflow,
        "work",
        "resolve_human",
        work,
        {"request_key": "second", "answer": "Use B", "resolution": "actionable"},
    )
    assert response["state"] == "Todo"


def test_question_withdrawal_requires_requester_and_obsolete_evidence(workflow):
    work = make_work(workflow)
    perform(workflow, "work", "ask_human", work, {"request_key": "q", "question": "Which approach?"})
    with pytest.raises(Refusal):
        perform(workflow, "work", "withdraw_human", work, {"request_key": "q"})
    response = perform(
        workflow,
        "work",
        "withdraw_human",
        work,
        {
            "request_key": "q",
            "evidence": {"obsolete_reason": "Scope removed this dependency", "receipt_digest": "a" * 64},
        },
    )
    assert response["state"] == "Todo"


def test_condition_in_free_text_does_not_grant_immediate_authority(workflow):
    decision = WorkflowDecision.objects.create(
        project=workflow.project,
        subject_type="candidate",
        subject_id=uuid4(),
        action="publish",
        payload={"revision": "a" * 40},
        payload_digest=digest({"revision": "a" * 40}),
    )
    result = perform(
        workflow,
        "decision",
        "resolve",
        decision,
        {
            "payload_digest": decision.payload_digest,
            "decision": "allow",
            "answer": "yes, after 22:00",
            "expires_at": (timezone.now() + timedelta(hours=1)).isoformat(),
        },
    )
    assert result["state"] == "clarify"
    decision.refresh_from_db()
    assert decision.expires_at is None


def test_run_capability_cannot_submit_for_another_lease(workflow):
    work = make_work(workflow)
    run = uuid4()
    WorkflowCapability.objects.create(
        project=workflow.project,
        principal=workflow.bot,
        subject_type="work",
        subject_id=work.issue_id,
        run_id=run,
        actions=["activate"],
        expires_at=timezone.now() + timedelta(hours=1),
    )
    with pytest.raises(Refusal) as caught:
        perform(workflow, "work", "activate", work, {"run_id": str(uuid4())}, actor=workflow.bot)
    assert caught.value.code == "capability_denied"


def test_version_and_input_digest_are_explicit(workflow):
    work = make_work(workflow)
    with pytest.raises(Refusal) as caught:
        perform(workflow, "work", "activate", work, workflow_version=None)
    assert caught.value.code == "unsupported_workflow"
    with pytest.raises(Refusal) as caught:
        perform(workflow, "work", "activate", work, input_digest="wrong")
    assert caught.value.code == "input_mismatch"


def test_request_changes_preserves_review_phase_until_developer_dispatch(workflow):
    work = make_work(workflow)
    work.state, work.active, work.next_action = "In Review", True, "review"
    work.save()
    review = acquire(workflow, "repository:local/repo")
    result = perform(workflow, "work", "request_changes", work, {**fenced(review), "reason": "Add regression coverage"})
    assert result["state"] == "In Review"
    work.refresh_from_db()
    assert work.next_action == "develop"
    owned = acquire(workflow, f"work:{work.issue_id}")
    result = perform(workflow, "work", "claim", work, {**fenced(owned), "next_action": "develop"})
    assert result["state"] == "In Progress"


def test_secondary_environment_mutation_has_durable_history(workflow):
    env = WorkflowEnvironment.objects.create(project=workflow.project, name="Local")
    scope = ReleaseScope.objects.create(project=workflow.project, name="Scope")
    candidate = ReleaseCandidate.objects.create(
        project=workflow.project,
        scope=scope,
        scope_revision=1,
        state="Published",
        manifest={},
        qualification={"artifact_digest": "a"},
    )
    env.identity = {"candidate_id": str(candidate.id)}
    env.health = "healthy"
    env.save()
    bug = make_work(workflow)
    result = perform(
        workflow,
        "candidate",
        "revoke",
        candidate,
        {"reason": "Regression observed", "defect_work_id": str(bug.issue_id)},
    )
    event = WorkflowEvent.objects.get(command_id=result["command_id"], subject_type="environment", subject_id=env.id)
    assert event.before["health"] == "healthy"
    assert event.after["health"] == "attention"


def test_redeployment_requires_new_explicit_delivery_request(workflow):
    env = WorkflowEnvironment.objects.create(project=workflow.project, name="Local")
    scope = ReleaseScope.objects.create(project=workflow.project, name="Scope")
    candidate = ReleaseCandidate.objects.create(
        project=workflow.project, scope=scope, scope_revision=1, state="Published", manifest={}
    )
    previous = Deployment.objects.create(
        project=workflow.project, candidate=candidate, environment=env, state="Healthy"
    )
    result = perform(
        workflow,
        "deployment",
        "redeploy",
        previous,
        {"delivery_request_id": "operator-request-2", "reason": "Reinstall after environment replacement"},
    )
    new = Deployment.objects.get(pk=result["subject_id"])
    assert new.id != previous.id and new.state == "Pending"
    assert new.candidate_id == previous.candidate_id
    assert new.incident_id == previous.id
    perform(
        workflow,
        "deployment",
        "redeploy",
        previous,
        {"delivery_request_id": "operator-request-2", "reason": "Reinstall after environment replacement"},
    )
    assert Deployment.objects.filter(candidate=candidate).count() == 2


def test_expired_owner_can_reconcile_receipt_but_cannot_start_effect(workflow):
    env = WorkflowEnvironment.objects.create(project=workflow.project, name="Local")
    scope = ReleaseScope.objects.create(project=workflow.project, name="Scope")
    candidate = ReleaseCandidate.objects.create(
        project=workflow.project, scope=scope, scope_revision=1, state="Published"
    )
    deployment = Deployment.objects.create(
        project=workflow.project,
        candidate=candidate,
        environment=env,
        prior_identity={"artifact_digest": "prior"},
        state="Deploying",
    )
    owned = acquire(workflow, f"environment:{env.id}")
    op = WorkflowOperation.objects.create(
        project=workflow.project,
        subject_type="deployment",
        subject_id=deployment.id,
        kind="install",
        payload={"environment_id": str(env.id)},
        payload_digest="a" * 64,
        status="unknown",
        lease_id=owned["id"],
        fence=owned["fence"],
    )
    WorkflowLease.objects.filter(pk=owned["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
    result = perform(
        workflow,
        "operation",
        "receipt",
        op,
        {
            **fenced(owned),
            "receipt": {
                "observed_at": timezone.now().isoformat(),
                "receipt_digest": "b" * 64,
                "outcome": "no_effect",
                "no_mutation_verified": True,
                "identity": deployment.prior_identity,
            },
        },
    )
    assert result["result"]["status"] == "not_applied"
