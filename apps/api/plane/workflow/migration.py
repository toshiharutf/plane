"""Audited local import and scheduler cutover; legacy labels never grant authority."""

from django.utils import timezone
from plane.db.models import (
    Issue,
    WorkContinuation,
    ReleaseCandidate,
    Deployment,
    WorkflowDecision,
    WorkflowCheckRun,
)
from .service import require, digest, scoped, handle_configuration, handle_work, serial


def apply_migration(project, actor, row, payload):
    mapping, enrollments = payload.get("mapping"), payload.get("enrollments")
    require(
        isinstance(mapping, list) and isinstance(enrollments, list),
        "invalid_migration",
        "Explicit mapping and enrollment lists are required.",
    )
    require(
        payload.get("source_digest") == digest({"mapping": mapping, "enrollments": enrollments}),
        "migration_digest_mismatch",
        "The reviewed mapping digest differs from the proposed import.",
    )
    require(
        payload.get("activate") is True and payload.get("legacy_scheduler_disabled") is True,
        "scheduler_cutover_required",
        "Activation requires explicit legacy scheduler shutdown.",
    )
    records = payload.get("source_records")
    require(
        isinstance(records, list) and payload.get("source_records_digest") == digest(records),
        "source_snapshot_required",
        "Supply the digested reviewed source snapshot.",
    )
    expected_ids = {str(item.get("source_id")) for item in mapping} | {
        str(item.get("issue_id")) for item in enrollments
    }
    require(
        {str(item.get("id")) for item in records} == expected_ids and len(records) == len(expected_ids),
        "source_snapshot_incomplete",
        "Every source must occur once in the reviewed snapshot.",
    )
    for source in records:
        issue = scoped(Issue, project, source.get("id"))
        require(
            str(issue.project_id) == str(source.get("project_id"))
            and str(issue.state_id) == str(source.get("state_id"))
            and source.get("updated_at") == issue.updated_at.isoformat(),
            "source_changed",
            "A source changed after migration preview.",
            source_id=str(issue.id),
        )
    require(
        not Issue.objects.filter(project=project, state__group="started").exists(),
        "inflight_migration_exception",
        "Quiesce and reconcile every in-flight work item before cutover.",
    )
    models = {
        "work": Issue,
        "candidate": ReleaseCandidate,
        "deployment": Deployment,
        "decision": WorkflowDecision,
        "check": WorkflowCheckRun,
    }
    previous = dict(row.migration)
    old_mapping = {entry["source_id"]: entry for entry in previous.get("mapping", [])}
    for entry in mapping:
        require(
            entry.get("kind") in models and entry.get("evidence_digest"),
            "invalid_migration_mapping",
            "Each source requires a typed existing target and preserved evidence digest.",
        )
        scoped(models[entry["kind"]], project, entry.get("target_id"))
        source_id = str(entry["source_id"])
        require(
            source_id not in old_mapping or old_mapping[source_id] == entry,
            "migration_mapping_conflict",
            "An existing source mapping cannot silently change its historical target.",
        )
        old_mapping[source_id] = entry
    handle_configuration(
        project,
        actor,
        "configure",
        row,
        {
            "state_mapping": payload.get("state_mapping", row.state_mapping),
            "enabled": True,
        },
        row,
    )
    # Configuration must exist for the command-only state synchronizer. The whole
    # command transaction rolls this temporary save back if any source fails.
    row.save()
    imported = []
    for enrollment in enrollments:
        existing = WorkContinuation.objects.filter(project=project, issue_id=enrollment.get("issue_id")).first()
        if existing:
            require(
                existing.repository == enrollment.get("repository", "")
                and existing.execution_kind == enrollment.get("execution_kind", "code")
                and existing.scope_revision == enrollment.get("scope_revision", 1),
                "migration_enrollment_conflict",
                "Existing enrollment differs from the reviewed import.",
            )
            imported.append(str(existing.issue_id))
            continue
        work, _ = handle_work(project, actor, "enroll", None, enrollment, row)
        if work.state == "Done":
            work.evidence = {"unverified": True, "migration_source": "legacy_status_only"}
        work.version = 1
        work.save()
        imported.append(str(work.issue_id))
    batches = list(previous.get("batches", []))
    if not any(batch["source_digest"] == payload["source_digest"] for batch in batches):
        batches.append(
            {
                "source_digest": payload["source_digest"],
                "source_records_digest": payload["source_records_digest"],
                "imported_by": str(actor.id),
                "imported_at": timezone.now().isoformat(),
                "work_ids": imported,
            }
        )
    row.migration = {"mapping": list(old_mapping.values()), "batches": batches, "approval_transfer": False}
    row.legacy_scheduler_disabled = True
    return row, {
        "configuration": serial(row),
        "imported_work_ids": imported,
        "mapping": list(old_mapping.values()),
        "approval_transfer": False,
    }
