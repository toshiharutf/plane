"""Local command-level release/deployment drills, with no external effects."""

# ruff: noqa: F811
from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone
from plane.db.models import (
    Deployment,
    ReleaseScope,
    ReleaseCandidate,
    WorkflowEnvironment,
    WorkflowDecision,
    WorkflowCheckRun,
    WorkflowOperation,
    WorkflowLease,
)
from plane.tests.workflow_fixtures import workflow, perform, make_work, acquire, fenced  # noqa: F401
from plane.workflow.service import Refusal, digest

pytestmark = pytest.mark.django_db
ARTIFACT = "sha256:" + "c" * 64
CONFIG = "sha256:" + "d" * 64
CHECK = "e" * 64


def approve(f, decision):
    perform(
        f,
        "decision",
        "resolve",
        decision["id"],
        {
            "payload_digest": decision["payload_digest"],
            "decision": "allow",
            "answer": "yes",
            "expires_at": (timezone.now() + timedelta(hours=1)).isoformat(),
        },
    )
    return decision["id"]


def integrated_work(f):
    work = make_work(f)
    perform(f, "work", "activate", work)
    owned = acquire(f, f"work:{work.issue_id}")
    perform(f, "work", "claim", work, {**fenced(owned), "next_action": "develop"})
    perform(
        f,
        "work",
        "submit",
        work,
        {
            **fenced(owned),
            "evidence": {
                "head_sha": "a" * 40,
                "base_sha": "b" * 40,
                "checks": True,
                "post_session_checks": True,
            },
        },
    )
    review = acquire(f, "repository:local/repo")
    perform(
        f,
        "work",
        "integrate",
        work,
        {
            **fenced(review),
            "review_accepted": True,
            "evidence": {
                "head_sha": "a" * 40,
                "integration_sha": "a" * 40,
                "expected_base_sha": "b" * 40,
                "observed_ref": "develop",
                "receipt_digest": "f" * 64,
                "checks": True,
                "base_unchanged": True,
                "head_is_ancestor": True,
            },
        },
    )
    return work, review


def frozen_candidate(f, target=True, second_repository=False):
    work, review = integrated_work(f)
    environment = None
    if target:
        response = perform(f, "environment", "create", payload={"name": "local-rehearsal"})
        environment = WorkflowEnvironment.objects.get(pk=response["subject_id"])
        perform(
            f,
            "environment",
            "observe",
            environment,
            {
                "health": "healthy",
                "identity": {"artifact_digest": "sha256:" + "1" * 64, "config_digest": "sha256:" + "2" * 64},
                "evidence": {"receipt_digest": "3" * 64, "checks": True},
            },
        )
    definition = {
        "work": [{"id": str(work.issue_id), "revision": 1}],
        "compatibility_verified": True,
        "targets": [str(environment.id)] if environment else [],
        "manifest": {
            "repositories": [
                {
                    "repository": "local/repo",
                    "base_sha": "b" * 40,
                    "source_sha": "a" * 40,
                    "integration_receipt": "f" * 64,
                }
            ],
            "build": "recipe:v1",
            "dependencies": "lock:v1",
            "config": CONFIG,
            "required_checks": [CHECK],
            "compatibility": {"workflow_versions": [2]},
            "delta_work_ids": [str(work.issue_id)],
        },
    }
    if second_repository:
        definition["manifest"]["repositories"].append(
            {
                "repository": "local/second",
                "base_sha": "b" * 40,
                "source_sha": "a" * 40,
                "integration_receipt": "f" * 64,
            }
        )
    scope_result = perform(f, "scope", "create", payload={"name": "Pilot", "definition": definition})
    scope = ReleaseScope.objects.get(pk=scope_result["subject_id"])
    perform(f, "scope", "approve", scope, {"definition_digest": digest(definition)})
    perform(f, "scope", "activate", scope)
    candidate = ReleaseCandidate.objects.get(scope=scope)
    assert candidate.state == "Frozen"
    return candidate, environment, review


def qualify(f, candidate):
    test_lease = acquire(f, f"candidate:{candidate.id}")
    perform(
        f, "candidate", "start_checks", candidate, {**fenced(test_lease), "manifest_digest": candidate.manifest_digest}
    )
    check_result(f, candidate, test_lease)
    perform(f, "candidate", "qualify", candidate, {"artifact_digest": ARTIFACT})
    candidate.refresh_from_db()
    return test_lease


def check_result(f, candidate, owned, outcome="passed", **overrides):
    return perform(
        f,
        "candidate",
        "record_check",
        candidate,
        {
            **fenced(owned),
            "run_id": str(uuid4()),
            "definition_digest": CHECK,
            "manifest_digest": candidate.manifest_digest,
            "artifact_digest": ARTIFACT,
            "outcome": outcome,
            "evidence": {"receipt_digest": "f" * 64},
            **overrides,
        },
    )


def publish(f, candidate, owned):
    owned = acquire(f, "repository:local/repo")
    plan = {
        "effects": [
            {
                "repository": "local/repo",
                "expected_old": "b" * 40,
                "expected_new": "a" * 40,
                "ref": "refs/heads/develop",
                "remote_identity": "local-bare-pilot",
            }
        ]
    }
    request = perform(f, "candidate", "request_publication", candidate, {"plan": plan})
    approval = approve(f, request["result"]["decision"])
    queued = perform(f, "candidate", "publish", candidate, {"plan": plan, "decision_id": approval})
    op = queued["result"]["operations"][0]
    perform(f, "operation", "claim", op["id"], fenced(owned))
    perform(f, "operation", "unknown", op["id"], fenced(owned))
    with pytest.raises(Refusal) as caught:
        perform(f, "operation", "claim", op["id"], fenced(owned))
    assert caught.value.code == "operation_requires_reconciliation"
    perform(
        f,
        "operation",
        "receipt",
        op["id"],
        {
            **fenced(owned),
            "receipt": {
                "observed_at": timezone.now().isoformat(),
                "receipt_digest": "f" * 64,
                "observed_ref": plan["effects"][0]["ref"],
                "observed_sha": "a" * 40,
                "remote_identity": "local-bare-pilot",
            },
        },
    )
    result = perform(f, "candidate", "publish", candidate, {"plan": plan, "decision_id": approval})
    assert result["state"] == "Published"
    candidate.refresh_from_db()


def ready_deployment(f):
    candidate, environment, repo_lease = frozen_candidate(f)
    qualify(f, candidate)
    publish(f, candidate, repo_lease)
    deployment = Deployment.objects.get(candidate=candidate)
    environment.refresh_from_db()
    plan = {
        "artifact_digest": ARTIFACT,
        "config_digest": CONFIG,
        "preflight": {"passed": True},
        "recovery": {"safe": True, "prior_identity": environment.identity},
        "required_checks": ["api"],
        "observation_seconds": 5,
        "allow_retry": True,
    }
    perform(f, "deployment", "plan", deployment, {"plan": plan})
    decision = perform(f, "deployment", "request_approval", deployment)["result"]["decision"]
    approval = approve(f, decision)
    owned = acquire(f, f"environment:{environment.id}")
    perform(f, "configuration", "configure", payload={"deployment_enabled": True})
    started = perform(f, "deployment", "start", deployment, {**fenced(owned), "decision_id": approval})
    return deployment, environment, candidate, owned, approval, started["result"]["operation"]


def test_scope_activation_replay_and_frozen_inputs(workflow):
    candidate, _, _ = frozen_candidate(workflow, target=False)
    manifest = candidate.manifest
    perform(workflow, "scope", "reconcile", candidate.scope)
    assert ReleaseCandidate.objects.filter(scope=candidate.scope).count() == 1
    definition = {**candidate.scope.definition, "manifest": {**manifest, "config": "different"}}
    perform(workflow, "scope", "amend", candidate.scope, {"definition": definition})
    candidate.refresh_from_db()
    assert candidate.manifest == manifest
    assert candidate.state == "Frozen"


def test_interrupted_check_retries_same_candidate_product_failure_rejects(workflow):
    candidate, _, _ = frozen_candidate(workflow)
    owned = acquire(workflow, f"candidate:{candidate.id}")
    perform(
        workflow,
        "candidate",
        "start_checks",
        candidate,
        {**fenced(owned), "manifest_digest": candidate.manifest_digest},
    )
    check_result(workflow, candidate, owned, "incomplete")
    candidate.refresh_from_db()
    assert candidate.state == "Validating"
    with pytest.raises(Refusal) as caught:
        perform(workflow, "candidate", "qualify", candidate, {"artifact_digest": ARTIFACT})
    assert caught.value.code == "checks_incomplete"
    bug = make_work(workflow, name="Reproduction")
    check_result(
        workflow,
        candidate,
        owned,
        "product_failed",
        evidence={
            "receipt_digest": "f" * 64,
            "defect_work_id": str(bug.issue_id),
            "reproduction": "Assertion mismatch",
        },
    )
    candidate.refresh_from_db()
    assert candidate.state == "Rejected"
    assert WorkflowCheckRun.objects.filter(candidate=candidate).count() == 2
    assert not Deployment.objects.filter(candidate=candidate).exists()


def test_check_changed_artifact_cannot_qualify(workflow):
    candidate, _, _ = frozen_candidate(workflow)
    owned = acquire(workflow, f"candidate:{candidate.id}")
    perform(
        workflow,
        "candidate",
        "start_checks",
        candidate,
        {**fenced(owned), "manifest_digest": candidate.manifest_digest},
    )
    check_result(workflow, candidate, owned)
    with pytest.raises(Refusal) as caught:
        perform(workflow, "candidate", "qualify", candidate, {"artifact_digest": "sha256:" + "0" * 64})
    assert caught.value.code == "checks_incomplete"


def test_publication_only_creates_no_deployment(workflow):
    candidate, _, owned = frozen_candidate(workflow, target=False)
    qualify(workflow, candidate)
    publish(workflow, candidate, owned)
    assert not Deployment.objects.filter(candidate=candidate).exists()


def test_local_install_verified_health_and_distinct_deploy_authority(workflow):
    deployment, env, candidate, owned, approval, operation = ready_deployment(workflow)
    perform(workflow, "operation", "claim", operation["id"], fenced(owned))
    perform(
        workflow,
        "operation",
        "receipt",
        operation["id"],
        {
            **fenced(owned),
            "receipt": {
                "observed_at": timezone.now().isoformat(),
                "receipt_digest": "f" * 64,
                "artifact_digest": ARTIFACT,
                "config_digest": CONFIG,
            },
        },
    )
    perform(workflow, "deployment", "installed", deployment, {**fenced(owned), "operation_id": operation["id"]})
    result = perform(
        workflow,
        "deployment",
        "healthy",
        deployment,
        {
            **fenced(owned),
            "evidence": {
                "artifact_digest": ARTIFACT,
                "config_digest": CONFIG,
                "checks": {"api": True},
                "observation_seconds": 5,
                "receipt_digest": "f" * 64,
            },
        },
    )
    assert result["state"] == "Healthy"
    env.refresh_from_db()
    assert env.identity["candidate_id"] == str(candidate.id)
    assert env.health == "healthy"
    assert WorkflowDecision.objects.filter(project=workflow.project, decision="allow").count() == 2
    final_plan = {
        "effects": [
            {
                "repository": "local/repo",
                "expected_old": "0" * 40,
                "expected_new": "a" * 40,
                "ref": "refs/tags/beta_c0.local",
                "remote_identity": "local-bare-pilot",
            }
        ]
    }
    final_request = perform(workflow, "deployment", "request_finalization", deployment, {"plan": final_plan})
    final_approval = approve(workflow, final_request["result"]["decision"])
    finalized = perform(
        workflow, "deployment", "finalize", deployment, {"plan": final_plan, "decision_id": final_approval}
    )
    assert finalized["state"] == "Healthy" and finalized["result"]["finalization_status"] == "pending"
    repo_lease = WorkflowLease.objects.get(project=workflow.project, resource="repository:local/repo")
    final_fence = {"lease_id": str(repo_lease.id), "fence": repo_lease.fence}
    final_op = finalized["result"]["operations"][0]
    perform(workflow, "operation", "claim", final_op["id"], final_fence)
    perform(workflow, "operation", "unknown", final_op["id"], final_fence)
    deployment.refresh_from_db()
    assert deployment.state == "Healthy"
    perform(
        workflow,
        "operation",
        "receipt",
        final_op["id"],
        {
            **final_fence,
            "receipt": {
                "observed_at": timezone.now().isoformat(),
                "receipt_digest": "f" * 64,
                "observed_ref": final_plan["effects"][0]["ref"],
                "observed_sha": "a" * 40,
                "remote_identity": "local-bare-pilot",
            },
        },
    )
    finalized = perform(
        workflow, "deployment", "finalize", deployment, {"plan": final_plan, "decision_id": final_approval}
    )
    assert finalized["result"]["finalization_status"] == "completed"
    assert finalized["state"] == "Healthy"


def test_non_code_repair_new_attempt_same_identity(workflow):
    deployment, env, candidate, owned, approval, operation = ready_deployment(workflow)
    perform(workflow, "operation", "claim", operation["id"], fenced(owned))
    perform(
        workflow,
        "operation",
        "receipt",
        operation["id"],
        {
            **fenced(owned),
            "receipt": {
                "observed_at": timezone.now().isoformat(),
                "receipt_digest": "f" * 64,
                "outcome": "no_effect",
                "no_mutation_verified": True,
                "identity": deployment.environment.identity,
            },
        },
    )
    perform(
        workflow,
        "deployment",
        "diagnose",
        deployment,
        {
            **fenced(owned),
            "cause": "non_code",
            "evidence": {"registry_status": "offline"},
            "repair_plan": "Restore local registry",
        },
    )
    result = perform(
        workflow,
        "deployment",
        "retry",
        deployment,
        {
            "reconciled": True,
            "repair_evidence": {"verified": True, "receipt_digest": "f" * 64},
            "decision_id": approval,
        },
    )
    assert result["state"] == "Pending"
    started = perform(workflow, "deployment", "start", deployment, {**fenced(owned), "decision_id": approval})
    assert started["result"]["attempt"]["number"] == 2
    assert Deployment.objects.get(pk=deployment.pk).candidate_id == candidate.id


def test_unknown_diagnosis_holds_then_verified_code_rollback_discards(workflow):
    deployment, env, candidate, owned, approval, operation = ready_deployment(workflow)
    result = perform(
        workflow,
        "deployment",
        "diagnose",
        deployment,
        {**fenced(owned), "cause": "unknown", "evidence": {"symptom": "Unavailable"}},
    )
    assert result["state"] == "Deploying"
    with pytest.raises(Refusal):
        perform(workflow, "deployment", "retry", deployment, {"reconciled": True})
    bug = make_work(workflow, name="Regression bug")
    perform(
        workflow,
        "deployment",
        "diagnose",
        deployment,
        {
            **fenced(owned),
            "cause": "code",
            "evidence": {"assertion": "Failed"},
            "defect_work_id": str(bug.issue_id),
            "reproduction": "API fails",
        },
    )
    restore = WorkflowOperation.objects.get(subject_id=deployment.id, kind="restore")
    with pytest.raises(Refusal):
        perform(workflow, "deployment", "restored", deployment, {**fenced(owned), "operation_id": str(restore.id)})
    perform(workflow, "operation", "claim", restore, fenced(owned))
    deployment.refresh_from_db()
    perform(
        workflow,
        "operation",
        "receipt",
        restore,
        {
            **fenced(owned),
            "receipt": {
                "observed_at": timezone.now().isoformat(),
                "receipt_digest": "f" * 64,
                "identity": deployment.prior_identity,
                "healthy": True,
                "schema_verified": True,
            },
        },
    )
    result = perform(workflow, "deployment", "restored", deployment, {**fenced(owned), "operation_id": str(restore.id)})
    assert result["state"] == "Rolled Back"
    deployment.refresh_from_db()
    candidate.refresh_from_db()
    env.refresh_from_db()
    assert deployment.discarded and not candidate.eligible
    assert env.identity == deployment.prior_identity and env.health == "healthy"
    with pytest.raises(Refusal):
        perform(workflow, "deployment", "retry", deployment, {"reconciled": True})


def test_partial_multi_repository_publication_retains_receipts_and_waits(workflow):
    candidate, _, _ = frozen_candidate(workflow, second_repository=True)
    qualify(workflow, candidate)
    effects = [
        {
            "repository": repo,
            "expected_old": "b" * 40,
            "expected_new": "a" * 40,
            "ref": "refs/heads/develop",
            "remote_identity": repo + "/bare",
        }
        for repo in ("local/repo", "local/second")
    ]
    request = perform(workflow, "candidate", "request_publication", candidate, {"plan": {"effects": effects}})
    approval = approve(workflow, request["result"]["decision"])
    result = perform(
        workflow, "candidate", "publish", candidate, {"plan": {"effects": effects}, "decision_id": approval}
    )
    operations = result["result"]["operations"]
    owned = acquire(workflow, "repository:local/repo")
    perform(workflow, "operation", "claim", operations[0]["id"], fenced(owned))
    perform(
        workflow,
        "operation",
        "receipt",
        operations[0]["id"],
        {
            **fenced(owned),
            "receipt": {
                "observed_at": timezone.now().isoformat(),
                "receipt_digest": "f" * 64,
                "observed_ref": effects[0]["ref"],
                "observed_sha": "a" * 40,
                "remote_identity": effects[0]["remote_identity"],
            },
        },
    )
    result = perform(
        workflow, "candidate", "publish", candidate, {"plan": {"effects": effects}, "decision_id": approval}
    )
    assert result["state"] == "Qualified"
    assert result["result"]["operations"][0]["status"] == "observed"
    assert result["result"]["operations"][1]["status"] == "pending"
    assert not Deployment.objects.filter(candidate=candidate).exists()


def test_revoked_and_expired_publication_authority_prevent_effects(workflow):
    candidate, _, _ = frozen_candidate(workflow)
    qualify(workflow, candidate)
    plan = {
        "effects": [
            {
                "repository": "local/repo",
                "expected_old": "b" * 40,
                "expected_new": "a" * 40,
                "ref": "refs/heads/develop",
                "remote_identity": "local-bare",
            }
        ]
    }
    request = perform(workflow, "candidate", "request_publication", candidate, {"plan": plan})
    approval = approve(workflow, request["result"]["decision"])
    WorkflowDecision.objects.filter(pk=approval).update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(Refusal) as caught:
        perform(workflow, "candidate", "publish", candidate, {"plan": plan, "decision_id": approval})
    assert caught.value.code == "approval_required"
    WorkflowDecision.objects.filter(pk=approval).update(expires_at=timezone.now() + timedelta(hours=1), revoked=True)
    with pytest.raises(Refusal) as caught:
        perform(workflow, "candidate", "publish", candidate, {"plan": plan, "decision_id": approval})
    assert caught.value.code == "approval_required"
    assert not WorkflowOperation.objects.filter(subject_id=candidate.id, kind__startswith="publish:").exists()
