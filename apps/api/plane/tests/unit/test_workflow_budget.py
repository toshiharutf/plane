"""Server admission never substitutes forecast for committed ceilings."""

# ruff: noqa: F811
from uuid import uuid4
from decimal import Decimal
import pytest
from plane.db.models import Cycle, ReleaseScope, WorkflowUsage
from plane.tests.workflow_fixtures import workflow, perform, make_work, acquire, fenced  # noqa: F401
from plane.workflow.service import Refusal, digest
from plane.workflow.projection import records

pytestmark = pytest.mark.django_db


def budget_scope(f, work, budget=None, cycle=None, cycle_budget=None):
    definition = {"work": [{"id": str(item.issue_id), "revision": 1} for item in work]}
    if budget is not None:
        definition["budget"] = budget
    if cycle_budget is not None:
        definition["cycle_budget"] = cycle_budget
    result = perform(
        f,
        "scope",
        "create",
        payload={"name": "Budgeted", "definition": definition, "cycle_id": str(cycle.id) if cycle else None},
    )
    return ReleaseScope.objects.get(pk=result["subject_id"])


def approve(f, scope):
    return perform(f, "scope", "approve", scope, {"definition_digest": digest(scope.definition)})


def estimate(f, work, cost, minutes=10):
    perform(
        f,
        "work",
        "estimate",
        work,
        {
            "cost_ceiling": str(cost),
            "minute_ceiling": minutes,
            "estimated_cost_usd": "0.01",
            "estimated_active_minutes": 1,
        },
    )


def test_small_forecast_cannot_admit_oversized_commitment(workflow):
    one, two = make_work(workflow), make_work(workflow)
    estimate(workflow, one, 50)
    estimate(workflow, two, 50)
    scope = budget_scope(workflow, [one, two], {"policy": "cost", "cost_ceiling_usd": "100", "contingency_usd": "10"})
    with pytest.raises(Refusal) as caught:
        approve(workflow, scope)
    assert caught.value.code == "budget_admission_blocked"
    assert caught.value.details["guards"][0]["code"] == "committed_budget_exceeded"
    scope.refresh_from_db()
    assert not scope.approved


def test_cycle_allocations_deduplicate_overlap_and_admit_distinct_work_once(workflow):
    cycle = Cycle.objects.create(
        project=workflow.project, workspace=workflow.workspace, name="Budget", owned_by=workflow.user
    )
    one, two = make_work(workflow), make_work(workflow)
    estimate(workflow, one, 50)
    estimate(workflow, two, 60)
    cycle_budget = {"policy": "both", "cost_ceiling_usd": "100", "active_minute_ceiling": 30}
    first = budget_scope(workflow, [one], cycle=cycle, cycle_budget=cycle_budget)
    approve(workflow, first)
    overlap = budget_scope(workflow, [one], cycle=cycle, cycle_budget=cycle_budget)
    approve(workflow, overlap)
    overflow = budget_scope(workflow, [two], cycle=cycle, cycle_budget=cycle_budget)
    with pytest.raises(Refusal) as caught:
        approve(workflow, overflow)
    assert any(g["code"] == "committed_budget_exceeded" for g in caught.value.details["guards"])


def test_unknown_prior_usage_blocks_claim_and_projection_explains(workflow):
    work = make_work(workflow)
    estimate(workflow, work, 90)
    scope = budget_scope(workflow, [work], {"policy": "cost", "cost_ceiling_usd": "100", "contingency_usd": "10"})
    approve(workflow, scope)
    perform(workflow, "work", "activate", work)
    WorkflowUsage.objects.create(
        project=workflow.project,
        run_id=uuid4(),
        role="developer",
        subject_type="work",
        subject_id=work.issue_id,
        model="unknown",
        cost_usd=None,
        active_seconds=30,
    )
    owned = acquire(workflow, f"work:{work.issue_id}")
    with pytest.raises(Refusal) as caught:
        perform(workflow, "work", "claim", work, {**fenced(owned), "next_action": "develop"})
    assert any(g["code"] == "usage_unknown" for g in caught.value.details["guards"])
    projected = records(workflow.project.id, workflow.user)["work"][0]
    assert any(g["code"] == "usage_unknown" for g in projected["budget_admission"]["guards"])
    assert projected["budget_admission"]["contexts"][0]["dimensions"]["cost"]["actual"] is None


def test_release_overhead_charged_once_and_retries_do_not_reset(workflow):
    work = make_work(workflow)
    estimate(workflow, work, 90)
    scope = budget_scope(workflow, [work], {"policy": "cost", "cost_ceiling_usd": "100", "contingency_usd": "10"})
    approve(workflow, scope)
    perform(workflow, "work", "activate", work)
    for spent in ("6", "5"):
        WorkflowUsage.objects.create(
            project=workflow.project,
            run_id=uuid4(),
            role="system_tester",
            subject_type="scope",
            subject_id=scope.id,
            model="known",
            cost_usd=Decimal(spent),
            active_seconds=30,
        )
    owned = acquire(workflow, f"work:{work.issue_id}")
    with pytest.raises(Refusal) as caught:
        perform(workflow, "work", "claim", work, {**fenced(owned), "next_action": "develop"})
    assert any(g.get("committed") == "101.000000" for g in caught.value.details["guards"])


def test_missing_task_ceiling_and_conflicting_cycle_budget_are_not_guessed(workflow):
    work = make_work(workflow)
    scope = budget_scope(workflow, [work], {"policy": "time", "active_minute_ceiling": 60})
    with pytest.raises(Refusal) as caught:
        approve(workflow, scope)
    assert any(g["code"] == "task_ceiling_required" for g in caught.value.details["guards"])


def test_frozen_leaf_becoming_parent_holds_readiness_and_retains_commitment(workflow):
    from plane.workflow.budget import evaluate
    from plane.workflow.service import readiness
    from plane.db.models import ReleaseCandidate

    work = make_work(workflow)
    estimate(workflow, work, 50)
    scope = budget_scope(workflow, [work], {"policy": "cost", "cost_ceiling_usd": "100"})
    approve(workflow, scope)
    scope.refresh_from_db()
    child = make_work(workflow)
    child.issue.parent = work.issue
    child.issue.save(update_fields=["parent"])
    result = evaluate(workflow.project, [scope], scope.definition["budget"], "scope")
    assert result["dimensions"]["cost"]["committed"] == "50.000000"
    assert any(g["code"] == "hierarchy_changed_requires_scope_amendment" for g in result["guards"])
    candidate = ReleaseCandidate(project=workflow.project, scope=scope, scope_revision=scope.revision)
    guards, _ = readiness(candidate)
    assert any(g["code"] == "hierarchy_changed_requires_scope_amendment" for g in guards)
