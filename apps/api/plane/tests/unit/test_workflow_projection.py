"""Count provenance, independent questions and event-time coverage regressions."""

# ruff: noqa: F811
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone
from plane.db.models import Cycle, Issue, ReleaseScope, WorkflowEvent, User, ProjectMember
from plane.tests.workflow_fixtures import workflow, perform, make_work  # noqa: F401
from plane.tests.unit.test_workflow_release import ready_deployment, ARTIFACT, CONFIG
from plane.workflow.projection import progress, records
from plane.workflow.service import digest

pytestmark = pytest.mark.django_db


def approved_scope(f, work, cycle=None, revision=1):
    definition = {"work": [{"id": str(item.issue_id), "revision": revision} for item in work], "targets": []}
    result = perform(
        f,
        "scope",
        "create",
        payload={"name": "Baseline", "definition": definition, "cycle_id": str(cycle.id) if cycle else None},
    )
    scope = ReleaseScope.objects.get(pk=result["subject_id"])
    perform(f, "scope", "approve", scope, {"definition_digest": digest(definition)})
    return scope


def test_attention_deduplicates_request_identity_and_wait_is_separate(workflow):
    work = make_work(workflow)
    scope = approved_scope(workflow, [work])
    for key in ("a", "b"):
        perform(workflow, "work", "ask_human", work, {"request_key": key, "question": key})
    first = (
        WorkflowEvent.objects.filter(subject_type="work", subject_id=work.id, action="ask_human")
        .order_by("sequence")
        .first()
    )
    WorkflowEvent.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(minutes=10))
    snapshot = progress(workflow.project.id, workflow.user, {"scope_id": str(scope.id)})
    assert snapshot["metrics"]["needs_attention"]["count"] == 2
    assert {r["request_key"] for r in snapshot["attention"]} == {"a", "b"}
    assert 9.9 < snapshot["work"][0]["human_wait_minutes"] < 10.1
    assert snapshot["work"][0]["actual_active_minutes"] is None
    user = User.objects.create(email=f"other-{uuid4()}@test.invalid", username=str(uuid4()))
    ProjectMember.objects.create(project=workflow.project, workspace=workflow.workspace, member=user, role=15)
    question = records(workflow.project.id, user)["work"][0]["continuation"]["requests"]["a"]
    assert question["allowed_actions"] == ["resolve_human"]


def test_cycle_union_deduplicates_work_and_excludes_summary_parents(workflow):
    cycle = Cycle.objects.create(
        project=workflow.project, workspace=workflow.workspace, name="Cycle", owned_by=workflow.user
    )
    one, two, parent = make_work(workflow), make_work(workflow), make_work(workflow)
    Issue.objects.create(
        project=workflow.project,
        workspace=workflow.workspace,
        name="Child",
        parent=parent.issue,
        state=workflow.states["Backlog"],
    )
    approved_scope(workflow, [one, parent], cycle)
    approved_scope(workflow, [one, two], cycle)
    snapshot = progress(workflow.project.id, workflow.user, {"cycle_id": str(cycle.id)})
    assert snapshot["metrics"]["completed_work"]["denominator"] == 2
    assert len(snapshot["scope_revisions"]) == 2
    assert snapshot["scope"]["id"] is None
    assert str(parent.issue_id) in snapshot["scope"]["excluded_ids"]
    assert snapshot["history"][-1]["denominator"] == 2


def test_obsolete_scope_evidence_does_not_count_completion(workflow):
    work = make_work(workflow)
    work.state, work.evidence = "Done", {"integration": {"integration_sha": "a" * 40}}
    work.save()
    scope = approved_scope(workflow, [work], revision=2)
    snapshot = progress(workflow.project.id, workflow.user, {"scope_id": str(scope.id)})
    assert snapshot["metrics"]["completed_work"]["denominator"] == 1
    assert snapshot["metrics"]["completed_work"]["numerator"] == 0
    assert snapshot["history"][-1]["completed_work"] == 0


def test_history_holds_and_revocation_show_unknown_delivery(workflow):
    deployment, env, candidate, owned, _, operation = ready_deployment(workflow)
    fence = {"lease_id": owned["id"], "fence": owned["fence"]}
    perform(workflow, "operation", "claim", operation["id"], fence)
    perform(
        workflow,
        "operation",
        "receipt",
        operation["id"],
        {
            **fence,
            "receipt": {
                "observed_at": timezone.now().isoformat(),
                "receipt_digest": "a" * 64,
                "artifact_digest": ARTIFACT,
                "config_digest": CONFIG,
            },
        },
    )
    perform(workflow, "deployment", "installed", deployment, {**fence, "operation_id": operation["id"]})
    perform(
        workflow,
        "deployment",
        "healthy",
        deployment,
        {
            **fence,
            "evidence": {
                "artifact_digest": ARTIFACT,
                "config_digest": CONFIG,
                "checks": {"api": True},
                "observation_seconds": 5,
                "receipt_digest": "a" * 64,
            },
        },
    )
    selected = {"scope_id": str(candidate.scope_id), "target": str(env.id)}
    before = progress(workflow.project.id, workflow.user, selected)
    assert before["history"][-1]["verified_delivery"] == 1
    perform(workflow, "environment", "hold", env, {"reason": "Ownership requires reconciliation"})
    held = progress(workflow.project.id, workflow.user, selected)
    assert held["history"][-1]["verified_delivery"] is None
    assert held["metrics"]["verified_delivery"]["numerator"] is None
    bug = make_work(workflow)
    perform(workflow, "candidate", "revoke", candidate, {"reason": "Regression", "defect_work_id": str(bug.issue_id)})
    revoked = progress(workflow.project.id, workflow.user, selected)
    assert revoked["history"][-1]["verified_delivery"] is None
    assert revoked["history"][-1]["qualified_code"] == 0


def test_pending_amendment_preserves_previous_approved_baseline(workflow):
    one, two = make_work(workflow), make_work(workflow)
    scope = approved_scope(workflow, [one])
    perform(
        workflow,
        "scope",
        "amend",
        scope,
        {
            "definition": {
                "work": [{"id": str(one.issue_id), "revision": 1}, {"id": str(two.issue_id), "revision": 1}],
                "targets": [],
            }
        },
    )
    pending = progress(workflow.project.id, workflow.user, {"scope_id": str(scope.id)})
    assert pending["scope"]["pending_revision"] == 2
    assert pending["scope"]["revision"] == 1
    assert pending["metrics"]["completed_work"]["denominator"] == 1
    assert pending["history"][-1]["denominator"] == 1
    scope.refresh_from_db()
    perform(workflow, "scope", "approve", scope, {"definition_digest": digest(scope.definition)})
    approved = progress(workflow.project.id, workflow.user, {"scope_id": str(scope.id)})
    assert approved["scope"]["revision"] == 2
    assert approved["metrics"]["completed_work"]["denominator"] == 2
    assert approved["history"][-1]["baseline_changed"] is True
