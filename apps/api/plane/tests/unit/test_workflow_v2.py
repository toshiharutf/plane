# ruff: noqa: F811
from datetime import timedelta
from uuid import uuid4
import pytest
from django.db import connection, transaction, DatabaseError
from django.utils import timezone
from rest_framework.test import APIClient
from plane.db.models import (
    Issue,
    WorkflowCommand,
    WorkflowEvent,
    WorkflowLease,
    WorkflowDecision,
    ReleaseScope,
    ReleaseCandidate,
    WorkflowEnvironment,
)
from plane.tests.workflow_fixtures import workflow, perform, make_work, acquire, fenced  # noqa: F401
from plane.workflow.service import Refusal, digest
from plane.workflow.projection import progress
from plane.workflow.guards import WorkflowWriteRequired

pytestmark = pytest.mark.django_db


def test_work_claim_submit_requires_integration_and_durable_replay(workflow):
    row = make_work(workflow)
    perform(workflow, "work", "activate", row)
    owned = acquire(workflow, f"work:{row.issue_id}")
    perform(workflow, "work", "claim", row, {"next_action": "develop", **fenced(owned)})
    command_id = str(uuid4())
    submitted = perform(
        workflow,
        "work",
        "submit",
        row,
        {
            **fenced(owned),
            "evidence": {"head_sha": "a" * 40, "base_sha": "b" * 40, "checks": True, "post_session_checks": True},
        },
        command_id=command_id,
    )
    assert submitted["state"] == "In Review"
    assert WorkflowCommand.objects.get(pk=command_id).response == submitted
    assert WorkflowEvent.objects.filter(command_id=command_id).exists()
    row.refresh_from_db()
    assert not row.evidence.get("integration")
    with pytest.raises(Refusal):
        perform(
            workflow, "work", "manual_verify", row, {"evidence": {"acceptance": "done", "receipt_digest": "a" * 64}}
        )


def test_human_review_continuation_and_inactive_answer(workflow):
    row = make_work(workflow)
    perform(workflow, "work", "ask_human", row, {"question": "Clarify acceptance?", "request_key": "q1"})
    perform(workflow, "work", "resolve_human", row, {"answer": "Use criterion A", "resolution": "actionable"})
    owned = acquire(workflow, f"work:{row.issue_id}")
    with pytest.raises(Refusal) as caught:
        perform(workflow, "work", "claim", row, {"next_action": "develop", **fenced(owned)})
    assert caught.value.code == "inactive_scope"
    row.refresh_from_db()
    row.active, row.state, row.next_action = True, "In Review", "review"
    row.evidence = {
        "submission": {"head_sha": "a" * 40, "base_sha": "b" * 40, "checks": True, "post_session_checks": True}
    }
    row.save()
    perform(
        workflow,
        "lease",
        "release",
        owned["id"],
        {"fence": owned["fence"], "executor_stopped": True, "effects_reconciled": True},
    )
    perform(
        workflow,
        "work",
        "ask_human",
        row,
        {"question": "Review blocker", "request_key": "q2", "executor_stopped": True},
    )
    perform(workflow, "work", "resolve_human", row, {"answer": "yes", "resolution": "actionable"})
    with pytest.raises(Refusal) as caught:
        perform(workflow, "work", "claim", row, {"next_action": "develop"})
    assert caught.value.code == "continuation_mismatch"
    review = acquire(workflow, "repository:local/repo")
    result = perform(workflow, "work", "claim", row, {"next_action": "review", **fenced(review)})
    assert result["state"] == "In Review"


def test_denial_preserves_answer_without_dispatch(workflow):
    row = make_work(workflow)
    perform(workflow, "work", "ask_human", row, {"question": "Proceed?", "request_key": "q"})
    response = perform(workflow, "work", "resolve_human", row, {"answer": "No", "resolution": "deny"})
    assert response["state"] == "Awaiting Human"
    row.refresh_from_db()
    assert row.continuation["answer"] == "No"


def test_bot_cannot_spoof_capability_or_human_decision(workflow):
    row = make_work(workflow)
    with pytest.raises(Refusal) as caught:
        perform(workflow, "work", "activate", row, {"role": "controller"}, actor=workflow.bot)
    assert caught.value.code == "capability_denied"
    with pytest.raises(Refusal) as caught:
        perform(workflow, "capability", "grant", payload={"role": "admin"}, actor=workflow.bot)
    assert caught.value.code == "human_required"


def test_legacy_null_custom_bulk_and_raw_writes_blocked(workflow):
    row = make_work(workflow)
    for value in (None, workflow.states["Done"]):
        with pytest.raises(WorkflowWriteRequired):
            Issue.objects.filter(pk=row.issue_id).update(state=value)
    issue = row.issue
    issue.state = workflow.states["Done"]
    with pytest.raises(WorkflowWriteRequired):
        issue.save()
    with pytest.raises(DatabaseError), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("UPDATE issues SET state_id = NULL WHERE id = %s", [row.issue_id])


def test_lease_expiry_requires_reconciliation(workflow):
    owned = acquire(workflow, "environment:isolated")
    WorkflowLease.objects.filter(pk=owned["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(Refusal) as caught:
        acquire(workflow, "environment:isolated")
    assert caught.value.code == "lease_held"
    perform(
        workflow,
        "lease",
        "release",
        owned["id"],
        {"fence": owned["fence"], "executor_stopped": True, "effects_reconciled": True},
    )
    next_lease = acquire(workflow, "environment:isolated")
    assert next_lease["fence"] == owned["fence"] + 1
    with pytest.raises(Refusal) as caught:
        perform(workflow, "lease", "renew", owned["id"], {"fence": owned["fence"]})
    assert caught.value.code == "stale_fence"


def test_stale_version_and_command_id_conflict(workflow):
    row = make_work(workflow)
    command_id = str(uuid4())
    perform(workflow, "work", "activate", row, command_id=command_id)
    with pytest.raises(Refusal) as caught:
        perform(workflow, "work", "activate", row, command_id=command_id)
    assert caught.value.code == "idempotency_conflict"
    with pytest.raises(Refusal) as caught:
        perform(workflow, "work", "activate", row, expected_version=0)
    assert caught.value.code == "version_conflict"


def test_decision_exact_payload_expiry_and_human_identity(workflow):
    decision = WorkflowDecision.objects.create(
        project=workflow.project,
        subject_type="candidate",
        subject_id=uuid4(),
        action="publish",
        payload={"revision": "a" * 40},
        payload_digest=digest({"revision": "a" * 40}),
    )
    with pytest.raises(Refusal) as caught:
        perform(
            workflow, "decision", "resolve", decision, {"decision": "allow", "answer": "yes", "payload_digest": "wrong"}
        )
    assert caught.value.code == "approval_payload_changed"
    with pytest.raises(Refusal) as caught:
        perform(workflow, "decision", "resolve", decision, {"decision": "allow"}, actor=workflow.bot)
    assert caught.value.code == "human_required"
    perform(
        workflow,
        "decision",
        "resolve",
        decision,
        {
            "decision": "allow",
            "answer": "yes",
            "payload_digest": decision.payload_digest,
            "expires_at": (timezone.now() + timedelta(hours=1)).isoformat(),
        },
    )
    decision.refresh_from_db()
    assert decision.decided_by == workflow.user


def test_progress_deduplicates_manual_parents_unknown_and_revocation(workflow):
    code = make_work(workflow)
    manual = make_work(workflow, "human")
    scope = ReleaseScope.objects.create(
        project=workflow.project,
        name="Baseline",
        approved=True,
        definition={
            "work": [{"id": str(code.issue_id), "revision": 1}, {"id": str(manual.issue_id), "revision": 1}],
            "targets": [],
        },
    )
    code.state, code.evidence = "Done", {"integration": {"integration_sha": "a" * 40}}
    code.save()
    manual.state, manual.evidence = "Done", {"manual": {"acceptance": "checked"}}
    manual.save()
    candidate = ReleaseCandidate.objects.create(
        project=workflow.project,
        scope=scope,
        scope_revision=1,
        state="Published",
        manifest={"work": [{"id": str(code.issue_id), "revision": 1, "kind": "code", "evidence": code.evidence}]},
        qualification={"artifact_digest": "sha256:" + "a" * 64},
    )
    env = WorkflowEnvironment.objects.create(
        project=workflow.project,
        name="Local",
        health="unknown",
        identity={"candidate_id": str(candidate.id), "artifact_digest": candidate.qualification["artifact_digest"]},
    )
    snapshot = progress(workflow.project.id, workflow.user, {"scope_id": str(scope.id), "target": str(env.id)})
    assert snapshot["metrics"]["completed_work"]["numerator"] == 2
    assert snapshot["metrics"]["qualified_code"]["denominator"] == 1
    assert snapshot["metrics"]["verified_delivery"]["numerator"] is None
    env.health = "healthy"
    env.save()
    snapshot = progress(workflow.project.id, workflow.user, {"scope_id": str(scope.id), "target": str(env.id)})
    assert snapshot["metrics"]["verified_delivery"]["numerator"] == 1
    candidate.eligible = False
    candidate.save()
    snapshot = progress(workflow.project.id, workflow.user, {"scope_id": str(scope.id), "target": str(env.id)})
    assert snapshot["metrics"]["qualified_code"]["numerator"] == 0
    assert snapshot["metrics"]["verified_delivery"]["numerator"] is None


def test_empty_scope_does_not_claim_one_hundred_percent(workflow):
    snapshot = progress(workflow.project.id, workflow.user, {})
    assert snapshot["metrics"]["completed_work"]["status"] == "no_applicable_work"


def test_both_api_surfaces_share_projection_and_forbidden_membership(workflow):
    client = APIClient()
    client.force_authenticate(user=workflow.user)
    base = f"workspaces/{workflow.workspace.slug}/projects/{workflow.project.id}/"
    for prefix in ("/api/", "/api/v1/"):
        response = client.get(prefix + base + "workflow-v2/")
        assert response.status_code == 200, response.data
        assert response.data["workflow_versions"] == [2]
        response = client.get(prefix + base + "progress/")
        assert response.status_code == 200, response.data


def test_unsupported_approval_conditions_refused(workflow):
    approval = WorkflowDecision.objects.create(
        project=workflow.project,
        subject_type="candidate",
        subject_id=uuid4(),
        action="publish",
        payload={"revision": "a" * 40},
        payload_digest=digest({"revision": "a" * 40}),
    )
    with pytest.raises(Refusal) as caught:
        perform(
            workflow,
            "decision",
            "resolve",
            approval,
            {
                "decision": "allow",
                "answer": "yes",
                "payload_digest": approval.payload_digest,
                "conditions": {"after_review": True},
                "expires_at": (timezone.now() + timedelta(hours=1)).isoformat(),
            },
        )
    assert caught.value.code == "unsupported_conditions"
    approval.refresh_from_db()
    assert approval.decision == "pending"


@pytest.mark.django_db(transaction=True)
def test_concurrent_controllers_one_resource_owner(workflow):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from django.db import close_old_connections

    barrier = Barrier(2)

    def claim():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return acquire(workflow, "repository:concurrent")
        except Refusal as refusal:
            return refusal.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: claim(), range(2)))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert "lease_held" in results


def test_project_permission_denial_redacts_projection(workflow):
    from plane.db.models import User

    outsider = User.objects.create(email=f"outsider-{uuid4()}@test.invalid", username=str(uuid4()))
    client = APIClient()
    client.force_authenticate(user=outsider)
    response = client.get(f"/api/workspaces/{workflow.workspace.slug}/projects/{workflow.project.id}/progress/")
    assert response.status_code == 403
    assert "metrics" not in response.data


def test_issue_serializers_return_continuation_and_migration_inventory(workflow):
    import io
    import json
    from django.core.management import call_command
    from plane.api.serializers.issue import IssueSerializer as PublicIssueSerializer
    from plane.app.serializers.issue import IssueListDetailSerializer

    row = make_work(workflow)
    assert PublicIssueSerializer(row.issue).data["workflow_v2"]["next_action"] == "develop"
    issue = row.issue
    issue.cycle_id = None
    issue.sub_issues_count = issue.attachment_count = issue.link_count = 0
    assert IssueListDetailSerializer(issue).data["workflow_v2"]["execution_kind"] == "code"
    output = io.StringIO()
    call_command("workflow_migration_report", project=str(workflow.project.id), stdout=output)
    report = json.loads(output.getvalue())
    assert report["dry_run"] is True
    assert report["records"][0]["enrolled"] is True
    assert report["records"][0]["mapped_phase"] == "Backlog"
