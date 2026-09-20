"""Dry-run source identities, transactional cutover, and unknown usage ingestion."""

# ruff: noqa: F811
from uuid import uuid4
import pytest
from plane.db.models import Issue, WorkContinuation, WorkflowConfiguration, WorkflowUsage
from plane.tests.workflow_fixtures import workflow, perform, make_work  # noqa: F401
from plane.workflow.service import Refusal, digest
from plane.workflow.projection import progress
from plane.tests.unit.test_workflow_projection import approved_scope

pytestmark = pytest.mark.django_db


def source_payload(f, issue):
    enrollment = {"issue_id": str(issue.id), "repository": "local/repo", "execution_kind": "code", "scope_revision": 1}
    records = [
        {
            "id": str(issue.id),
            "project_id": str(issue.project_id),
            "state_id": str(issue.state_id),
            "updated_at": issue.updated_at.isoformat(),
        }
    ]
    payload = {
        "mapping": [],
        "enrollments": [enrollment],
        "source_records": records,
        "source_records_digest": digest(records),
        "activate": True,
        "legacy_scheduler_disabled": True,
        "state_mapping": {name: str(state.id) for name, state in f.states.items()},
    }
    payload["source_digest"] = digest({"mapping": payload["mapping"], "enrollments": payload["enrollments"]})
    return payload


def test_migration_atomically_disables_legacy_and_preserves_unverified_history(workflow):
    perform(workflow, "configuration", "configure", payload={"enabled": False})
    issue = Issue.objects.create(
        project=workflow.project, workspace=workflow.workspace, name="Historical done", state=workflow.states["Done"]
    )
    payload = source_payload(workflow, issue)
    command = str(uuid4())
    result = perform(workflow, "configuration", "migrate", payload=payload, command_id=command)
    config = WorkflowConfiguration.objects.get(project=workflow.project)
    assert config.enabled and config.legacy_scheduler_disabled
    assert config.migration["approval_transfer"] is False
    work = WorkContinuation.objects.get(issue=issue)
    assert work.state == "Done" and work.evidence["unverified"] is True
    assert result["result"]["imported_work_ids"] == [str(issue.id)]
    # Replaying the exact envelope uses the original expected version.
    second = perform(
        workflow,
        "configuration",
        "migrate",
        payload=payload,
        command_id=command,
        expected_version=result["version"] - 1,
    )
    assert second == result
    assert WorkContinuation.objects.filter(issue=issue).count() == 1


def test_changed_source_and_inflight_migration_leave_configuration_disabled(workflow):
    perform(workflow, "configuration", "configure", payload={"enabled": False})
    issue = Issue.objects.create(
        project=workflow.project, workspace=workflow.workspace, name="Before", state=workflow.states["Todo"]
    )
    payload = source_payload(workflow, issue)
    issue.name = "Changed"
    issue.save()
    with pytest.raises(Refusal) as caught:
        perform(workflow, "configuration", "migrate", payload=payload)
    assert caught.value.code == "source_changed"
    payload = source_payload(workflow, issue)
    Issue.objects.create(
        project=workflow.project, workspace=workflow.workspace, name="Unresolved", state=workflow.states["In Progress"]
    )
    with pytest.raises(Refusal) as caught:
        perform(workflow, "configuration", "migrate", payload=payload)
    assert caught.value.code == "inflight_migration_exception"
    assert not WorkflowConfiguration.objects.get(project=workflow.project).enabled
    assert not WorkContinuation.objects.exists()


def test_migration_rejects_unmapped_state_and_rolls_back_activation(workflow):
    from plane.db.models import State

    perform(workflow, "configuration", "configure", payload={"enabled": False})
    custom = State.objects.create(
        project=workflow.project, workspace=workflow.workspace, name="Custom", group="backlog"
    )
    issue = Issue.objects.create(project=workflow.project, workspace=workflow.workspace, name="Ambiguous", state=custom)
    with pytest.raises(Refusal) as caught:
        perform(workflow, "configuration", "migrate", payload=source_payload(workflow, issue))
    assert caught.value.code == "migration_exception"
    assert not WorkflowConfiguration.objects.get(project=workflow.project).enabled


def test_unknown_usage_never_prices_missing_tokens_as_zero(workflow):
    work = make_work(workflow)
    scope = approved_scope(workflow, [work])
    for tokens, known in (({}, True), ({"input_tokens": 1, "output_tokens": 1}, False)):
        perform(
            workflow,
            "usage",
            "ingest",
            payload={
                "run_id": str(uuid4()),
                "role": "developer",
                "subject_type": "work",
                "subject_id": str(work.issue_id),
                "model": "gpt-4o",
                "active_seconds": 20,
                "tokens": tokens,
                "usage_known": known,
            },
        )
    assert all(row.cost_usd is None for row in WorkflowUsage.objects.filter(project=workflow.project))
    snapshot = progress(workflow.project.id, workflow.user, {"scope_id": str(scope.id)})
    assert snapshot["usage"]["unknown_cost_runs"] == 2
    assert snapshot["usage"]["active_seconds"] == 40


def test_projection_shared_release_overhead_charged_once(workflow):
    from decimal import Decimal

    work = make_work(workflow)
    scope = approved_scope(workflow, [work])
    for subject_type, subject_id, amount in [("work", work.issue_id, "3"), ("scope", scope.id, "2")]:
        WorkflowUsage.objects.create(
            project=workflow.project,
            run_id=uuid4(),
            role="system_tester",
            subject_type=subject_type,
            subject_id=subject_id,
            model="known",
            cost_usd=Decimal(amount),
            active_seconds=60,
        )
    snapshot = progress(workflow.project.id, workflow.user, {"scope_id": str(scope.id)})
    assert Decimal(snapshot["usage"]["known_cost_usd"]) == 5
    assert Decimal(snapshot["usage"]["release_overhead_known_cost_usd"]) == 2
    assert snapshot["usage"]["run_count"] == 2
