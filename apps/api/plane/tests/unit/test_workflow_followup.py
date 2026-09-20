"""Linked unexpected work remains a scoped proposal until separately admitted."""

# ruff: noqa: F811
import pytest
from django.utils import timezone
from datetime import timedelta
from plane.db.models import WorkContinuation, IssueRelation, WorkflowCapability
from plane.tests.workflow_fixtures import workflow, perform, make_work, acquire, fenced  # noqa: F401
from plane.workflow.service import Refusal

pytestmark = pytest.mark.django_db


def running(f):
    work = make_work(f)
    perform(f, "work", "estimate", work, {"minute_ceiling": 100})
    perform(f, "work", "activate", work)
    owned = acquire(f, f"work:{work.issue_id}")
    perform(f, "work", "claim", work, {**fenced(owned), "next_action": "develop"})
    return work, owned


def proposal(owned, **changes):
    return {
        **fenced(owned),
        "run_id": owned["run_id"],
        "proposal_key": "unexpected-schema",
        "original_minutes": 100,
        "unexpected_minutes": 26,
        "blocking": True,
        "name": "Fix unexpected schema mismatch",
        "acceptance": ["Regression test passes"],
        "alternatives": ["Expand-compatible migration", "Defer feature"],
        "reproduction": "Current schema rejects the fixture",
        "expected_behavior": "Existing data remains readable",
        **changes,
    }


def test_followup_is_linked_inactive_deduplicated_without_budget_authority(workflow):
    work, owned = running(workflow)
    result = perform(workflow, "work", "propose_followup", work, proposal(owned))
    assert result["state"] == "In Progress"
    target = result["result"]["followup"]
    assert target["state"] == "Backlog" and target["active"] is False and target["minute_ceiling"] is None
    assert target["followup_of_id"] == str(work.issue_id) and target["issue_snapshot"]["parent_id"] is None
    assert result["result"]["threshold_required"] is True
    repeated = perform(workflow, "work", "propose_followup", work, proposal(owned))
    assert repeated["result"]["followup"]["issue_id"] == target["issue_id"]
    assert WorkContinuation.objects.filter(followup_of=work.issue).count() == 1
    assert IssueRelation.objects.get(issue=work.issue).relation_type == "blocked_by"
    assert not workflow.project.workflowlease_set.get(pk=owned["id"]).released


def test_blocking_followup_prevents_submission_even_without_human_question(workflow):
    work, owned = running(workflow)
    perform(workflow, "work", "propose_followup", work, proposal(owned))
    with pytest.raises(Refusal) as caught:
        perform(
            workflow,
            "work",
            "submit",
            work,
            {
                **fenced(owned),
                "evidence": {"head_sha": "a" * 40, "base_sha": "b" * 40, "checks": True, "post_session_checks": True},
            },
        )
    assert caught.value.code == "blocking_followup_open"
    work.refresh_from_db()
    assert work.state == "In Progress"


def test_optional_exact_threshold_followup_does_not_block_submission(workflow):
    work, owned = running(workflow)
    result = perform(workflow, "work", "propose_followup", work, proposal(owned, unexpected_minutes=25, blocking=False))
    assert result["result"]["threshold_required"] is False
    response = perform(
        workflow,
        "work",
        "submit",
        work,
        {
            **fenced(owned),
            "evidence": {"head_sha": "a" * 40, "base_sha": "b" * 40, "checks": True, "post_session_checks": True},
        },
    )
    assert response["state"] == "In Review"


def test_conflicting_proposal_and_malformed_estimate_rejected(workflow):
    work, owned = running(workflow)
    perform(workflow, "work", "propose_followup", work, proposal(owned))
    for payload, code in [
        (proposal(owned, name="Changed"), "followup_conflict"),
        (proposal(owned, original_minutes=101), "followup_estimate_mismatch"),
        (proposal(owned, acceptance=[]), "evidence_required"),
    ]:
        with pytest.raises(Refusal) as caught:
            perform(workflow, "work", "propose_followup", work, payload)
        assert caught.value.code == code
    assert WorkContinuation.objects.filter(followup_of=work.issue).count() == 1


def test_ungranted_principal_cannot_propose_linked_work(workflow):
    work, owned = running(workflow)
    with pytest.raises(Refusal) as caught:
        perform(workflow, "work", "propose_followup", work, proposal(owned), actor=workflow.bot)
    assert caught.value.code == "capability_denied"
    WorkflowCapability.objects.create(
        project=workflow.project,
        principal=workflow.bot,
        subject_type="work",
        subject_id=work.issue_id,
        actions=["propose_followup"],
        expires_at=timezone.now() + timedelta(hours=1),
    )
    with pytest.raises(Refusal) as caught:
        perform(workflow, "work", "propose_followup", work, proposal(owned), actor=workflow.bot)
    assert caught.value.code == "stale_fence"


def test_threshold_uses_original_estimate_separately_from_authorized_ceiling(workflow):
    work = make_work(workflow)
    perform(workflow, "work", "estimate", work, {"estimated_active_minutes": 40, "minute_ceiling": 100})
    perform(workflow, "work", "activate", work)
    owned = acquire(workflow, f"work:{work.issue_id}")
    perform(workflow, "work", "claim", work, {**fenced(owned), "next_action": "develop"})
    result = perform(
        workflow, "work", "propose_followup", work, proposal(owned, original_minutes=40, unexpected_minutes=11)
    )
    assert result["result"]["threshold_required"] is True
