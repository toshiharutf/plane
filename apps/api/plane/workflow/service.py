"""Transactional CAS commands, scoped authorization, and durable effect intents.

The project row serializes commands and trigger evaluation. It also makes replay,
unique scope activation, and the returned event watermark one consistent snapshot.
No command in this module executes a shell, network operation, or deployment.
"""

import hashlib
import json
import re
import uuid
from contextvars import ContextVar
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.core.serializers.json import DjangoJSONEncoder

from plane.db.models import (
    Project,
    ProjectMember,
    WorkspaceMember,
    Issue,
    State,
    WorkflowConfiguration,
    WorkflowCapability,
    WorkContinuation,
    ReleaseScope,
    ReleaseCandidate,
    WorkflowEnvironment,
    Deployment,
    DeploymentAttempt,
    WorkflowDecision,
    WorkflowLease,
    WorkflowCheckRun,
    WorkflowCommand,
    WorkflowEvent,
    WorkflowOperation,
    WorkflowUsage,
)

command_write = ContextVar("workflow_command_write", default=False)
MODELS = dict(
    configuration=WorkflowConfiguration,
    capability=WorkflowCapability,
    work=WorkContinuation,
    scope=ReleaseScope,
    candidate=ReleaseCandidate,
    environment=WorkflowEnvironment,
    deployment=Deployment,
    decision=WorkflowDecision,
    lease=WorkflowLease,
    operation=WorkflowOperation,
    usage=WorkflowUsage,
)
WORK_STATES = {"Backlog", "Todo", "In Progress", "In Review", "Awaiting Human", "Done", "Cancelled"}
HUMAN_ACTIONS = {
    "configuration.configure",
    "configuration.migrate",
    "capability.grant",
    "capability.revoke",
    "decision.resolve",
    "decision.revoke",
    "scope.approve",
    "work.resolve_human",
    "work.manual_verify",
    "work.override",
    "work.cancel",
    "environment.create",
    "deployment.cancel",
    "deployment.redeploy",
}
ACTIONS = {
    "configuration": ["configure", "migrate"],
    "capability": ["grant", "revoke"],
    "work": [
        "enroll",
        "estimate",
        "propose_followup",
        "activate",
        "claim",
        "submit",
        "integrate",
        "request_changes",
        "ask_human",
        "resolve_human",
        "withdraw_human",
        "manual_verify",
        "cancel",
        "override",
    ],
    "scope": ["create", "approve", "activate", "amend", "reconcile"],
    "candidate": [
        "freeze",
        "start_checks",
        "record_check",
        "qualify",
        "request_publication",
        "publish",
        "supersede",
        "revoke",
    ],
    "deployment": [
        "plan",
        "request_approval",
        "request_finalization",
        "finalize",
        "start",
        "installed",
        "healthy",
        "diagnose",
        "retry",
        "restored",
        "abandon",
        "cancel",
        "redeploy",
    ],
    "environment": ["create", "observe", "hold", "clear_hold"],
    "decision": ["resolve", "revoke"],
    "lease": ["acquire", "renew", "release"],
    "operation": ["claim", "unknown", "receipt"],
    "usage": ["ingest"],
}


class Refusal(Exception):
    def __init__(self, code, message, status=409, **details):
        self.code, self.message, self.status, self.details = code, message, status, details
        super().__init__(message)


def require(condition, code, message, **details):
    if not condition:
        raise Refusal(code, message, **details)


def canonical(value):
    return json.dumps(
        value, cls=DjangoJSONEncoder, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def serial(record):
    data = {f.attname: getattr(record, f.attname) for f in record._meta.fields}
    if isinstance(record, WorkContinuation):
        data["issue_snapshot"] = {
            "name": record.issue.name,
            "parent_id": record.issue.parent_id,
            "is_summary": Issue.objects.filter(parent_id=record.issue_id).exists(),
        }
    return json.loads(canonical(data))


def date(value):
    result = parse_datetime(value) if isinstance(value, str) else value
    require(result is not None and timezone.is_aware(result), "invalid_time", "Supply an ISO timestamp with timezone.")
    return result


def membership(project, actor):
    require(actor.is_authenticated, "unauthenticated", "Authentication required.", status=403)
    if actor.is_bot:
        require(
            WorkspaceMember.objects.filter(workspace_id=project.workspace_id, member=actor, is_active=True).exists(),
            "forbidden",
            "An active workspace membership is required.",
            status=403,
        )
    else:
        require(
            ProjectMember.objects.filter(project=project, member=actor, is_active=True, role__gte=15).exists(),
            "forbidden",
            "An active project membership is required.",
            status=403,
        )


def authorize(project, actor, kind, action, subject_id=None, resource="", run_id=None):
    membership(project, actor)
    key = f"{kind}.{action}"
    if not actor.is_bot:
        if kind in {"configuration", "capability"}:
            require(
                ProjectMember.objects.filter(project=project, member=actor, is_active=True, role=20).exists(),
                "forbidden",
                "Project administrator required.",
                status=403,
            )
        return
    require(key not in HUMAN_ACTIONS, "human_required", "This decision requires an authenticated human.", status=403)
    grants = WorkflowCapability.objects.filter(
        project=project, principal=actor, subject_type=kind, revoked=False, expires_at__gt=timezone.now()
    )
    allowed = any(
        action in g.actions
        and (not g.subject_id or str(g.subject_id) == str(subject_id))
        and (not g.resource or g.resource == resource)
        and (not g.run_id or str(g.run_id) == str(run_id))
        for g in grants
    )
    require(allowed, "capability_denied", "No matching resource/action capability.", status=403, action=key)


def scoped(model, project, pk, lock=True):
    qs = model.objects.filter(project=project)
    if lock:
        qs = qs.select_for_update()
    try:
        return qs.get(pk=pk)
    except (model.DoesNotExist, ValueError, ValidationError):
        raise Refusal("not_found", "The scoped resource does not exist.", status=404)


def bump(record):
    record.version += 1
    record.save()
    return record


def event(command, kind, record, action, before=None):
    return WorkflowEvent.objects.create(
        project=record.project,
        command=command,
        subject_type=kind,
        subject_id=record.pk,
        version=record.version,
        action=action,
        before=before or {},
        after=serial(record),
    )


def state(record, expected):
    require(
        record.state in expected, "invalid_transition", "Action is not valid in the current phase.", state=record.state
    )


def evidence(value, *keys):
    require(
        isinstance(value, dict) and all(value.get(k) for k in keys),
        "evidence_required",
        "Matching observed evidence is required.",
        required=list(keys),
    )


def lease(project, actor, payload, resource, *, allow_expired=False):
    row = scoped(WorkflowLease, project, payload.get("lease_id"))
    require(
        row.resource == resource
        and row.holder_id == actor.id
        and not row.released
        and (allow_expired or row.expires_at > timezone.now())
        and row.fence == payload.get("fence"),
        "stale_fence",
        "A current owned lease and fencing token are required.",
        resource=resource,
    )
    return row


def decision(project, subject, action, payload, decision_id):
    row = scoped(WorkflowDecision, project, decision_id)
    now = timezone.now()
    require(
        row.subject_id == subject.id
        and row.action == action
        and row.decision == "allow"
        and not row.revoked
        and row.payload_digest == digest(payload)
        and row.expires_at is not None
        and row.expires_at > now
        and (row.not_before is None or row.not_before <= now),
        "approval_required",
        "A valid human approval for this exact operation is required.",
    )
    return row


def request_decision(project, kind, subject, action, payload):
    return WorkflowDecision.objects.get_or_create(
        project=project,
        subject_type=kind,
        subject_id=subject.id,
        action=action,
        payload_digest=digest(payload),
        decision="pending",
        defaults={"payload": payload},
    )[0]


def intent(project, kind, subject, action, payload, approval=None):
    return WorkflowOperation.objects.get_or_create(
        project=project,
        subject_type=kind,
        subject_id=subject.id,
        kind=action,
        payload_digest=digest(payload),
        defaults={"payload": payload, "decision": approval},
    )[0]


def sync_work(row):
    config = WorkflowConfiguration.objects.get(project=row.project)
    mapped = config.state_mapping.get(row.state)
    require(mapped, "state_mapping_required", "The workflow phase has no configured stable state ID.", state=row.state)
    token = command_write.set(True)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('plane.workflow_command', true)")
            previous = cursor.fetchone()[0] or ""
            cursor.execute("SELECT set_config('plane.workflow_command', 'on', true)")
            try:
                issue = Issue.objects.get(pk=row.issue_id)
                completed_at = (issue.completed_at or timezone.now()) if row.state == "Done" else None
                Issue.objects.filter(pk=row.issue_id).update(state_id=mapped, completed_at=completed_at)
            finally:
                cursor.execute("SELECT set_config('plane.workflow_command', %s, true)", [previous])
    finally:
        command_write.reset(token)


def work_verified(row):
    return (
        row.state == "Done"
        and bool(row.evidence.get("integration") if row.execution_kind == "code" else row.evidence.get("manual"))
        and not row.evidence.get("unverified")
    )


def readiness(candidate):
    definition = candidate.scope.definition
    reasons = []
    if candidate.scope_revision != candidate.scope.revision or not candidate.scope.approved:
        reasons.append({"code": "scope_revision_unapproved"})
    frozen_leaves = definition.get("executable_leaf_ids")
    members = (
        [member for member in definition.get("work", []) if member["id"] in frozen_leaves]
        if frozen_leaves is not None
        else [
            member for member in definition.get("work", []) if not Issue.objects.filter(parent_id=member["id"]).exists()
        ]
    )
    manifest_work = []
    for member in members:
        if frozen_leaves is not None and Issue.objects.filter(parent_id=member["id"]).exists():
            reasons.append({"code": "hierarchy_changed_requires_scope_amendment", "work_id": member["id"]})
        item = WorkContinuation.objects.filter(project=candidate.project, issue_id=member["id"]).first()
        if not item or not work_verified(item) or item.scope_revision != member["revision"]:
            reasons.append({"code": "work_evidence_missing", "work_id": member["id"]})
        else:
            manifest_work.append(
                {
                    "id": member["id"],
                    "revision": member["revision"],
                    "kind": item.execution_kind,
                    "repository": item.repository,
                    "evidence": item.evidence,
                }
            )
    if definition.get("holds"):
        reasons.append({"code": "scope_hold", "holds": definition["holds"]})
    manifest = definition.get("manifest", {})
    for key in ("repositories", "build", "dependencies", "config", "required_checks", "compatibility"):
        if not manifest.get(key):
            reasons.append({"code": "manifest_missing", "field": key})
    approved_ids = {x["id"] for x in members}
    if set(manifest.get("delta_work_ids", [])) != approved_ids:
        reasons.append({"code": "unapproved_source_delta"})
    for repo in manifest.get("repositories", []):
        if not all(repo.get(k) for k in ("repository", "base_sha", "source_sha", "integration_receipt")):
            reasons.append({"code": "source_identity_missing"})
        elif not all(re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", str(repo[k])) for k in ("base_sha", "source_sha")):
            reasons.append({"code": "invalid_source_revision"})
    if not definition.get("compatibility_verified"):
        reasons.append({"code": "compatibility_unverified"})
    return reasons, {
        **manifest,
        "work": manifest_work,
        "targets": definition.get("targets", []),
        "scope_id": str(candidate.scope_id),
        "scope_revision": candidate.scope_revision,
    }


def reconcile(project, command):
    config = WorkflowConfiguration.objects.filter(project=project).first()
    if not config or not config.release_enabled:
        return
    for scope in ReleaseScope.objects.filter(project=project, active=True, approved=True):
        candidate, created = ReleaseCandidate.objects.get_or_create(
            project=project, scope=scope, scope_revision=scope.revision
        )
        if created:
            event(command, "candidate", candidate, "scope_activated")
        if candidate.state == "Draft":
            reasons, manifest = readiness(candidate)
            before = serial(candidate)
            candidate.guards = reasons
            if not reasons:
                candidate.manifest = manifest
                candidate.manifest_digest = digest(manifest)
                candidate.state = "Frozen"
                intent(
                    project,
                    "candidate",
                    candidate,
                    "run_checks",
                    {"manifest_digest": candidate.manifest_digest, "checks": manifest["required_checks"]},
                )
            if serial(candidate) != before:
                bump(candidate)
                event(command, "candidate", candidate, "readiness_evaluated", before)
    for candidate in ReleaseCandidate.objects.filter(project=project, state="Published", eligible=True):
        for target in candidate.manifest.get("targets", []):
            environment = scoped(WorkflowEnvironment, project, target)
            row, created = Deployment.objects.get_or_create(
                project=project, candidate=candidate, environment=environment, delivery_request_id="initial"
            )
            if created:
                event(command, "deployment", row, "publication_verified")


@transaction.atomic
def execute(project_id, actor, envelope):
    project = Project.objects.select_for_update().get(pk=project_id)
    membership(project, actor)
    require(isinstance(envelope, dict), "invalid_command", "A command object is required.", status=400)
    require(
        type(envelope.get("workflow_version")) is int and envelope["workflow_version"] == 2,
        "unsupported_workflow",
        "Explicit workflow version 2 is required.",
    )
    try:
        command_id = uuid.UUID(str(envelope["command_id"]))
    except (KeyError, TypeError, ValueError):
        raise Refusal("invalid_command", "command_id must be a UUID.", status=400)
    command_digest = digest(envelope)
    prior = WorkflowCommand.objects.filter(pk=command_id).first()
    if prior:
        require(
            prior.project_id == project.id and prior.actor_id == actor.id and prior.digest == command_digest,
            "idempotency_conflict",
            "Command ID was already used with different inputs.",
        )
        return prior.response
    kind, action = envelope.get("subject_type"), envelope.get("action")
    require(
        kind in ACTIONS and action in ACTIONS.get(kind, []), "unknown_command", "Unknown typed command.", status=400
    )
    payload = envelope.get("payload", {})
    require(isinstance(payload, dict), "invalid_payload", "payload must be an object.", status=400)
    if "input_digest" in envelope:
        require(
            envelope["input_digest"] == digest(payload), "input_mismatch", "Command input digest differs from payload."
        )
    config, _ = WorkflowConfiguration.objects.get_or_create(project=project)
    require(
        config.enabled or kind in {"configuration", "capability"}, "workflow_disabled", "Workflow v2 is not activated."
    )
    require(
        kind not in {"scope", "candidate"} or config.release_enabled,
        "release_disabled",
        "Release execution is disabled.",
    )
    row = None
    subject_id = envelope.get("subject_id")
    if subject_id:
        if kind == "work":
            row = WorkContinuation.objects.select_for_update().filter(project=project, issue_id=subject_id).first()
            require(row, "not_found", "The work item is not enrolled.", status=404)
        else:
            row = scoped(MODELS[kind], project, subject_id)
    elif kind == "configuration":
        row = config
    expected = envelope.get("expected_version")
    require(
        type(expected) is int and expected == (row.version if row else 0),
        "version_conflict",
        "Expected version differs from the current record.",
        current_version=row.version if row else 0,
    )
    resource = getattr(row, "repository", "") or payload.get("resource", "")
    if kind == "deployment" and row:
        resource = str(row.environment_id)
    if kind == "operation" and row:
        resource = (
            str(scoped(Deployment, project, row.subject_id).environment_id)
            if row.subject_type == "deployment"
            else row.payload.get("repository", "")
        )
    owned = (
        WorkflowLease.objects.filter(project=project, pk=payload.get("lease_id")).first()
        if payload.get("lease_id")
        else None
    )
    authorize(project, actor, kind, action, subject_id, resource, owned.run_id if owned else payload.get("run_id"))
    before = serial(row) if row else {}
    secondary_models = {
        "work": WorkContinuation,
        "scope": ReleaseScope,
        "deployment": Deployment,
        "decision": WorkflowDecision,
        "operation": WorkflowOperation,
        "environment": WorkflowEnvironment,
        "candidate": ReleaseCandidate,
        "lease": WorkflowLease,
        "attempt": DeploymentAttempt,
        "check": WorkflowCheckRun,
    }
    secondary_before = {
        (kind, str(record.pk)): serial(record)
        for kind, model in secondary_models.items()
        for record in model.objects.filter(project=project)
    }
    command = WorkflowCommand.objects.create(id=command_id, project=project, actor=actor, digest=command_digest)
    handler = globals()[f"handle_{kind}"]
    row, result = handler(project, actor, action, row, payload, config)
    bump(row)
    event(command, kind, row, action, before)
    reconcile(project, command)
    # Secondary transitions (e.g. Healthy changing the current environment) are
    # authoritative history too, and must commit with the initiating command.
    for secondary_kind, model in secondary_models.items():
        for record in model.objects.filter(project=project):
            old = secondary_before.get((secondary_kind, str(record.pk)), {})
            if (
                old != serial(record)
                and not WorkflowEvent.objects.filter(
                    command=command, subject_type=secondary_kind, subject_id=record.pk, version=record.version
                ).exists()
            ):
                event(command, secondary_kind, record, "related_record_changed", old)
    response = {
        "command_id": str(command_id),
        "subject_type": kind,
        "subject_id": str(row.issue_id if kind == "work" else row.id),
        "version": row.version,
        "state": getattr(
            row, "state", row.decision if isinstance(row, WorkflowDecision) else getattr(row, "status", None)
        ),
        "event_sequence": WorkflowEvent.objects.filter(command=command)
        .order_by("-sequence")
        .values_list("sequence", flat=True)
        .first(),
        "result": result or serial(row),
    }
    command.response = response
    command.save(update_fields=["response"])
    return response


def handle_configuration(project, actor, action, row, p, config):
    if action == "migrate":
        from .migration import apply_migration

        return apply_migration(project, actor, row, p)
    mapping = p.get("state_mapping", row.state_mapping)
    if p.get("enabled", row.enabled):
        require(
            set(mapping) == WORK_STATES, "state_mapping_required", "Map every v2 phase to a stable project state ID."
        )
        require(
            len(set(mapping.values())) == len(WORK_STATES),
            "invalid_state_mapping",
            "Each phase requires a distinct state.",
        )
        require(
            State.objects.filter(project=project, pk__in=mapping.values()).count() == len(WORK_STATES),
            "invalid_state_mapping",
            "All mapped states must belong to the project.",
        )
    if row.enabled and p.get("enabled") is False:
        require(
            not WorkContinuation.objects.filter(project=project).exists(),
            "migration_required",
            "Enrolled records must be explicitly reconciled before disabling their guards.",
        )
    for key in ("enabled", "release_enabled", "deployment_enabled", "production_enabled"):
        if key in p:
            require(type(p[key]) is bool, "invalid_payload", "Activation flags must be booleans.", status=400)
            setattr(row, key, p[key])
    row.state_mapping = mapping
    return row, None


def handle_capability(project, actor, action, row, p, config):
    if action == "revoke":
        require(row, "not_found", "Capability is required.")
        row.revoked = True
    else:
        require(not row, "immutable_capability", "Create a new capability instead of broadening a grant.")
        require(
            p.get("subject_type") in ACTIONS
            and isinstance(p.get("actions"), list)
            and set(p["actions"]) <= set(ACTIONS[p["subject_type"]]),
            "invalid_capability",
            "Unknown capability actions.",
        )
        require(
            WorkspaceMember.objects.filter(
                workspace_id=project.workspace_id, member_id=p.get("principal_id"), is_active=True
            ).exists(),
            "invalid_principal",
            "Principal must belong to workspace.",
        )
        row = WorkflowCapability(
            project=project,
            principal_id=p["principal_id"],
            subject_type=p["subject_type"],
            subject_id=p.get("subject_id"),
            run_id=p.get("run_id"),
            actions=p["actions"],
            resource=p.get("resource", ""),
            expires_at=date(p["expires_at"]),
        )
    return row, None


def handle_work(project, actor, action, row, p, config):
    if action == "enroll":
        require(not row, "already_enrolled", "Work is already enrolled.")
        issue = scoped(Issue, project, p.get("issue_id"))
        require(
            not WorkContinuation.objects.filter(issue=issue).exists(), "already_enrolled", "Work is already enrolled."
        )
        current = next((name for name, pk in config.state_mapping.items() if str(pk) == str(issue.state_id)), None)
        require(current, "migration_exception", "The legacy state has no explicit v2 mapping.")
        require(
            p.get("execution_kind", "code") in {"code", "human"},
            "invalid_kind",
            "Execution kind must be code or human.",
        )
        row = WorkContinuation(
            project=project,
            issue=issue,
            state=current,
            repository=p.get("repository", ""),
            execution_kind=p.get("execution_kind", "code"),
            scope_revision=p.get("scope_revision", 1),
            dependencies=p.get("dependencies", []),
            cost_ceiling=p.get("cost_ceiling"),
            minute_ceiling=p.get("minute_ceiling"),
            next_action="verify_manual_completion" if p.get("execution_kind") == "human" else "develop",
        )
    else:
        require(row, "not_found", "Work must be enrolled.")
        if action == "propose_followup":
            from .followup import propose

            return propose(project, actor, row, p, config)
        if action in {"submit", "integrate", "manual_verify"}:
            from .followup import require_blocking_followups_complete

            require_blocking_followups_complete(project, row)
        if action == "estimate":
            for key in ("cost_ceiling", "minute_ceiling", "estimated_cost_usd", "estimated_active_minutes"):
                if key in p:
                    require(
                        p[key] is None or Decimal(str(p[key])) >= 0,
                        "invalid_estimate",
                        "Estimates and ceilings must be nonnegative or unknown.",
                    )
                    setattr(row, key, p[key])
        elif action == "activate":
            state(row, {"Backlog", "Todo"})
            row.active, row.state = True, "Todo"
        elif action == "claim":
            state(row, {"Todo", "In Review"})
            require(
                row.state != "In Review" or (row.next_action == "develop" and row.continuation.get("change_request")),
                "review_in_progress",
                "A review phase only dispatches development after an accepted change request.",
            )
            require(row.active, "inactive_scope", "Inactive work cannot dispatch.")
            from .budget import admission

            budget = admission(project, issue_id=row.issue_id)
            require(
                not budget["guards"],
                "budget_admission_blocked",
                "Committed scope/cycle budget does not admit this run.",
                guards=budget["guards"],
            )

            for pk in row.dependencies:
                dependency = WorkContinuation.objects.filter(project=project, issue_id=pk).first()
                require(
                    dependency and work_verified(dependency),
                    "dependency_blocked",
                    "A dependency lacks accepted evidence.",
                )
            require(
                p.get("next_action") == row.next_action,
                "continuation_mismatch",
                "Wrong executor for this continuation.",
            )
            usage = WorkflowUsage.objects.filter(project=project, subject_type="work", subject_id=row.issue_id)
            if row.cost_ceiling is not None:
                require(
                    all(u.cost_usd is not None for u in usage)
                    and sum((u.cost_usd for u in usage), Decimal(0)) < row.cost_ceiling,
                    "budget_exhausted",
                    "Cost ceiling is exhausted or prior cost is unknown.",
                )
            if row.minute_ceiling is not None:
                require(
                    all(u.active_seconds is not None for u in usage)
                    and sum(u.active_seconds for u in usage) < row.minute_ceiling * 60,
                    "budget_exhausted",
                    "Active time ceiling is exhausted or prior duration is unknown.",
                )
            resource = f"repository:{row.repository}" if row.next_action == "review" else f"work:{row.issue_id}"
            owned = lease(project, actor, p, resource)
            if row.next_action == "review":
                evidence(row.evidence.get("submission"), "head_sha", "checks", "post_session_checks")
            require(
                row.next_action != "verify_manual_completion" or not actor.is_bot,
                "human_required",
                "Manual work needs a human verifier.",
            )
            row.state = "In Review" if row.next_action == "review" else "In Progress"
            row.continuation = {
                **row.continuation,
                "run_id": str(owned.run_id),
                "lease_id": str(owned.id),
                "fence": owned.fence,
            }
        elif action == "submit":
            state(row, {"In Progress"})
            require(row.execution_kind == "code", "invalid_kind", "Code submissions require code work.")
            owned = lease(project, actor, p, f"work:{row.issue_id}")
            require(
                str(owned.run_id) == row.continuation.get("run_id"), "run_mismatch", "Result belongs to another run."
            )
            e = p.get("evidence")
            evidence(e, "head_sha", "base_sha", "checks", "post_session_checks")
            require(
                e["checks"] is True and e["post_session_checks"] is True, "checks_failed", "Required checks must pass."
            )
            require(
                all(re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", str(e[k])) for k in ("head_sha", "base_sha")),
                "invalid_revision",
                "Submission requires complete source revisions.",
            )
            row.evidence = {**row.evidence, "submission": e}
            row.state, row.next_action = "In Review", "review"
            owned.released, owned.reconciled = True, True
            bump(owned)
        elif action == "integrate":
            state(row, {"In Review"})
            owned = lease(project, actor, p, f"repository:{row.repository}")
            e = p.get("evidence")
            evidence(e, "head_sha", "integration_sha", "expected_base_sha", "observed_ref", "receipt_digest", "checks")
            require(
                e["head_sha"] == row.evidence.get("submission", {}).get("head_sha")
                and e["checks"] is True
                and e["observed_ref"] == "develop"
                and p.get("review_accepted") is True,
                "integration_mismatch",
                "Accepted review and exact observed develop integration are required.",
            )
            require(
                all(
                    re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", str(e[k]))
                    for k in ("head_sha", "integration_sha", "expected_base_sha")
                ),
                "invalid_revision",
                "Integration requires complete source revisions.",
            )
            require(
                e.get("base_unchanged") is True and e.get("head_is_ancestor") is True,
                "integration_unverified",
                "The controller must independently verify base CAS and ancestry.",
            )
            row.evidence = {**row.evidence, "integration": e}
            row.state = "Done"
            owned.released, owned.reconciled = True, True
            bump(owned)
        elif action == "request_changes":
            state(row, {"In Review"})
            owned = lease(project, actor, p, f"repository:{row.repository}")
            evidence(p, "reason")
            row.next_action = "develop"
            row.continuation = {"change_request": p["reason"]}
            owned.released, owned.reconciled = True, True
            bump(owned)
        elif action == "ask_human":
            state(row, WORK_STATES - {"Done", "Cancelled"})
            evidence(p, "question", "request_key")
            requests = dict(row.continuation.get("requests", {}))
            key = str(p["request_key"])
            require(
                key not in requests, "request_exists", "Question identity already exists; replay the original command."
            )
            next_action = "review" if row.state == "In Review" and row.next_action == "review" else row.next_action
            requests[key] = {"question": p["question"], "requester_id": str(actor.id), "status": "open"}
            prior_state = row.continuation.get("prior_state", row.state) if row.state == "Awaiting Human" else row.state
            row.continuation = {
                **row.continuation,
                "question": p["question"],
                "request_key": key,
                "prior_state": prior_state,
                "next_action": next_action,
                "answer": None,
                "requests": requests,
            }
            resources = [f"work:{row.issue_id}"]
            if prior_state == "In Review":
                resources.append(f"repository:{row.repository}")
            for owned in WorkflowLease.objects.filter(project=project, resource__in=resources, released=False):
                require(
                    p.get("executor_stopped") is True and owned.holder_id == actor.id,
                    "executor_unreconciled",
                    "The owning controller must confirm the executor stopped before blocking work.",
                )
                owned.released, owned.reconciled = True, True
                bump(owned)
            row.next_action, row.state = next_action, "Awaiting Human"
        elif action in {"resolve_human", "withdraw_human"}:
            state(row, {"Awaiting Human"})
            requests = dict(row.continuation.get("requests", {}))
            key = str(p.get("request_key", row.continuation.get("request_key", "")))
            require(
                key in requests and requests[key]["status"] == "open", "request_not_open", "Choose an open question."
            )
            question = dict(requests[key])
            if action == "withdraw_human":
                require(
                    question["requester_id"] == str(actor.id),
                    "requester_required",
                    "Only the requester may withdraw an obsolete question.",
                )
                evidence(p.get("evidence"), "obsolete_reason", "receipt_digest")
                question.update(status="withdrawn", evidence=p["evidence"])
            else:
                evidence(p, "answer")
                question.update(answer=p["answer"], answered_by=str(actor.id))
                actionable = p.get("resolution") == "actionable"
                denied = bool(re.match(r"^(no|deny|denied)\b", str(p["answer"]).strip(), re.IGNORECASE))
                if denied and not p.get("alternative"):
                    actionable = False
                if actionable:
                    question.update(status="answered", alternative=p.get("alternative"))
                else:
                    question["clarification_required"] = True
            requests[key] = question
            row.continuation = {
                **row.continuation,
                "requests": requests,
                "answer": question.get("answer"),
                "answered_by": question.get("answered_by"),
            }
            if all(request["status"] != "open" for request in requests.values()):
                row.state = "Todo"
                row.continuation.pop("clarification_required", None)
            else:
                row.continuation["clarification_required"] = True
        elif action == "manual_verify":
            state(row, {"Todo", "In Progress"})
            require(row.execution_kind == "human", "invalid_kind", "Manual verification cannot complete code work.")
            require(
                row.active and row.next_action == "verify_manual_completion",
                "inactive_scope",
                "Only activated manual work may be verified.",
            )
            evidence(p.get("evidence"), "acceptance", "receipt_digest")
            row.evidence, row.state = {"manual": {**p["evidence"], "verified_by": str(actor.id)}}, "Done"
        elif action in {"cancel", "override"}:
            evidence(p, "reason")
            require(p.get("executor_stopped") is True, "executor_unreconciled", "Reconcile active execution first.")
            if action == "cancel":
                state(row, WORK_STATES - {"Done", "Cancelled"})
                row.state = "Cancelled"
            else:
                require(p.get("state") in WORK_STATES, "invalid_state", "Unknown state.")
                row.state = p["state"]
                row.evidence = {**row.evidence, "unverified": True, "override_reason": p["reason"]}
                for candidate in ReleaseCandidate.objects.filter(project=project).exclude(state="Draft"):
                    if any(member["id"] == str(row.issue_id) for member in candidate.manifest.get("work", [])):
                        candidate.eligible = False
                        candidate.guards = [
                            *candidate.guards,
                            {"code": "work_evidence_overridden", "work_id": str(row.issue_id)},
                        ]
                        bump(candidate)
    sync_work(row)
    return row, None


def validate_scope(project, definition):
    require(
        isinstance(definition, dict) and isinstance(definition.get("work"), list),
        "invalid_scope",
        "Scope requires explicit work membership.",
    )
    ids = []
    for member in definition["work"]:
        require(
            isinstance(member, dict) and member.get("id") and type(member.get("revision")) is int,
            "invalid_scope",
            "Each member requires work ID and scope revision.",
        )
        scoped(Issue, project, member["id"])
        ids.append(member["id"])
    require(len(ids) == len(set(ids)), "invalid_scope", "Scope membership must be distinct.")
    for target in definition.get("targets", []):
        scoped(WorkflowEnvironment, project, target)


def handle_scope(project, actor, action, row, p, config):
    if action == "create":
        require(not row, "immutable_identity", "Create requires a new scope.")
        validate_scope(project, p.get("definition"))
        if p.get("cycle_id"):
            from plane.db.models import Cycle

            scoped(Cycle, project, p["cycle_id"])
        row = ReleaseScope(
            project=project, name=p.get("name", "Release scope"), cycle_id=p.get("cycle_id"), definition=p["definition"]
        )
    else:
        require(row, "not_found", "Scope is required.")
        if action == "approve":
            require(
                p.get("definition_digest") == digest(row.definition),
                "scope_changed",
                "Approve the displayed exact scope digest.",
            )
            row.approved = True
            row.definition = {
                **row.definition,
                "executable_leaf_ids": [
                    member["id"]
                    for member in row.definition.get("work", [])
                    if not Issue.objects.filter(parent_id=member["id"]).exists()
                ],
            }
            from .budget import admission

            budget = admission(project, scope=row)
            require(
                not budget["guards"],
                "budget_admission_blocked",
                "Committed scope/cycle ceilings exceed the approved budget.",
                guards=budget["guards"],
            )
        elif action == "activate":
            require(row.approved, "scope_unapproved", "Scope approval is required before activation.")
            from .budget import admission

            budget = admission(project, scope=row)
            require(
                not budget["guards"],
                "budget_admission_blocked",
                "Committed scope/cycle budget does not admit activation.",
                guards=budget["guards"],
            )

            row.active = True
        elif action == "amend":
            validate_scope(project, p.get("definition"))
            draft = row.candidates.filter(scope_revision=row.revision, state="Draft").first()
            row.definition, row.revision, row.approved = p["definition"], row.revision + 1, False
            if draft:
                draft.scope_revision = row.revision
                bump(draft)
        # reconcile is a durable nudge; the transaction evaluates all scopes.
    return row, None


def handle_candidate(project, actor, action, row, p, config):
    require(row, "not_found", "Candidate is required.")
    if action == "freeze":
        state(row, {"Draft"})
        reasons, manifest = readiness(row)
        require(not reasons, "not_ready", "Scope is not ready to freeze.", guards=reasons)
        row.manifest, row.manifest_digest, row.state = manifest, digest(manifest), "Frozen"
        intent(
            project,
            "candidate",
            row,
            "run_checks",
            {"manifest_digest": row.manifest_digest, "checks": manifest["required_checks"]},
        )
    elif action == "start_checks":
        state(row, {"Frozen", "Validating"})
        lease(project, actor, p, f"candidate:{row.id}")
        require(
            p.get("manifest_digest") == row.manifest_digest,
            "manifest_mismatch",
            "Check inputs must match the frozen manifest.",
        )
        row.state = "Validating"
    elif action == "record_check":
        state(row, {"Validating"})
        lease(project, actor, p, f"candidate:{row.id}")
        evidence(p, "run_id", "definition_digest", "manifest_digest", "artifact_digest", "outcome", "evidence")
        require(
            p["manifest_digest"] == row.manifest_digest and p["definition_digest"] in row.manifest["required_checks"],
            "check_mismatch",
            "Check identity does not match the frozen candidate.",
        )
        require(p["outcome"] in {"passed", "product_failed", "incomplete"}, "invalid_outcome", "Unknown check outcome.")
        require(
            not WorkflowCheckRun.objects.filter(run_id=p["run_id"]).exists(),
            "run_already_recorded",
            "A run result is immutable; retry creates a new run.",
        )
        WorkflowCheckRun.objects.create(
            project=project,
            candidate=row,
            **{
                k: p[k]
                for k in ("run_id", "definition_digest", "manifest_digest", "artifact_digest", "outcome", "evidence")
            },
        )
        if p["outcome"] == "product_failed":
            evidence(p["evidence"], "defect_work_id", "reproduction")
            scoped(Issue, project, p["evidence"]["defect_work_id"])
            row.state, row.eligible = "Rejected", False
    elif action == "qualify":
        state(row, {"Validating"})
        evidence(p, "artifact_digest")
        checks = list(
            row.checks.filter(
                outcome="passed", manifest_digest=row.manifest_digest, artifact_digest=p["artifact_digest"]
            )
        )
        require(
            set(row.manifest["required_checks"]) <= {c.definition_digest for c in checks},
            "checks_incomplete",
            "All required checks must pass for the same final artifact.",
        )
        row.qualification = {
            "manifest_digest": row.manifest_digest,
            "artifact_digest": p["artifact_digest"],
            "check_ids": sorted(str(c.id) for c in checks),
        }
        row.qualification_digest, row.state = digest(row.qualification), "Qualified"
    elif action == "request_publication":
        state(row, {"Qualified"})
        evidence(p.get("plan"), "effects")
        plan = p["plan"]
        repo_names = {r["repository"] for r in row.manifest["repositories"]}
        effects = plan["effects"]
        require(
            isinstance(effects, list)
            and len(effects) > 0
            and all(
                isinstance(e, dict)
                and e.get("repository") in repo_names
                and e.get("expected_old")
                and e.get("expected_new")
                and e.get("ref")
                and e.get("remote_identity")
                for e in effects
            ),
            "invalid_publication",
            "Publication requires exact repository, controlled remote, ref, old and new identities.",
        )
        require(
            {effect["repository"] for effect in effects} == repo_names,
            "publication_incomplete",
            "Every repository in the frozen manifest requires a publication effect.",
        )
        for effect in effects:
            source = next(r for r in row.manifest["repositories"] if r["repository"] == effect["repository"])
            require(
                effect["expected_new"] == source["source_sha"] and not effect.get("force"),
                "publication_mismatch",
                "Only the frozen source revision may be published without force.",
            )
        payload = {
            "candidate_id": str(row.id),
            "manifest_digest": row.manifest_digest,
            "qualification_digest": row.qualification_digest,
            "plan": plan,
        }
        approval = request_decision(project, "candidate", row, "publish", payload)
        return row, {"decision": serial(approval)}
    elif action == "publish":
        state(row, {"Qualified"})
        require(row.eligible, "candidate_revoked", "Candidate deployment eligibility has been revoked.")
        payload = {
            "candidate_id": str(row.id),
            "manifest_digest": row.manifest_digest,
            "qualification_digest": row.qualification_digest,
            "plan": p.get("plan"),
        }
        approval = decision(project, row, "publish", payload, p.get("decision_id"))
        operations = []
        for index, effect in enumerate(payload["plan"]["effects"]):
            operations.append(intent(project, "candidate", row, f"publish:{index}", effect, approval))
        if all(o.status == "observed" for o in operations):
            row.state = "Published"
        return row, {"operations": [serial(o) for o in operations]}
    elif action == "supersede":
        state(row, {"Draft", "Frozen", "Validating", "Qualified"})
        replacement = scoped(ReleaseCandidate, project, p.get("replacement_id"))
        evidence(p, "reason")
        require(
            replacement.id != row.id
            and not WorkflowOperation.objects.filter(
                project=project, subject_id=row.id, status__in=["running", "unknown", "pending"]
            )
            .exclude(kind="run_checks")
            .exists(),
            "effects_unreconciled",
            "Reconcile publication before replacing the candidate.",
        )
        row.superseded_by, row.state, row.eligible = replacement, "Superseded", False
    elif action == "revoke":
        evidence(p, "reason", "defect_work_id")
        scoped(Issue, project, p["defect_work_id"])
        row.eligible = False
        row.guards = [*row.guards, {"code": "revoked", "reason": p["reason"], "defect_work_id": p["defect_work_id"]}]
        for env in WorkflowEnvironment.objects.filter(project=project):
            if env.identity.get("candidate_id") == str(row.id):
                env.health, env.hold = "attention", {"reason": p["reason"]}
                bump(env)
    return row, None


def deployment_payload(row):
    return {
        "deployment_id": str(row.id),
        "candidate_id": str(row.candidate_id),
        "qualification_digest": row.candidate.qualification_digest,
        "artifact_digest": row.candidate.qualification.get("artifact_digest"),
        "environment_id": str(row.environment_id),
        "plan_digest": row.plan_digest,
        "plan": row.plan,
    }


def handle_deployment(project, actor, action, row, p, config):
    require(row, "not_found", "Deployment is required.")
    env, candidate = row.environment, row.candidate
    if action in {"request_finalization", "finalize"}:
        state(row, {"Healthy"})
        require(
            config.release_enabled and candidate.eligible,
            "candidate_ineligible",
            "Finalization requires enabled publication and an eligible candidate.",
        )
        plan = p.get("plan")
        evidence(plan, "effects")
        sources = {repo["repository"]: repo["source_sha"] for repo in candidate.manifest["repositories"]}
        require(
            isinstance(plan["effects"], list)
            and all(
                isinstance(effect, dict)
                and effect.get("repository") in sources
                and effect.get("expected_new") == sources[effect["repository"]]
                and effect.get("expected_old")
                and effect.get("remote_identity")
                and (effect.get("ref") == "refs/heads/main" or str(effect.get("ref", "")).startswith("refs/tags/"))
                and not effect.get("force")
                for effect in plan["effects"]
            ),
            "invalid_finalization",
            "Finalization may only fast-forward main or create a tag at the qualified revision.",
        )
        require(
            all(
                not effect["ref"].startswith("refs/tags/")
                or effect["expected_old"] == "0" * len(effect["expected_new"])
                for effect in plan["effects"]
            ),
            "tag_exists",
            "Finalization never moves an existing release tag.",
        )
        payload = {
            "deployment_id": str(row.id),
            "candidate_id": str(candidate.id),
            "qualification_digest": candidate.qualification_digest,
            "plan": plan,
        }
        if action == "request_finalization":
            approval = request_decision(project, "deployment", row, "finalize", payload)
            return row, {"decision": serial(approval)}
        approval = decision(project, row, "finalize", payload, p.get("decision_id"))
        operations = [
            intent(project, "deployment", row, f"finalize:{index}", effect, approval)
            for index, effect in enumerate(plan["effects"])
        ]
        return row, {
            "operations": [serial(op) for op in operations],
            "finalization_status": "completed" if all(op.status == "observed" for op in operations) else "pending",
        }
    if action == "redeploy":
        state(row, {"Healthy", "Failed", "Cancelled", "Rolled Back"})
        require(
            candidate.state == "Published" and candidate.eligible,
            "candidate_revoked",
            "Redeployment requires an eligible published candidate.",
        )
        evidence(p, "delivery_request_id", "reason")
        require(
            p["delivery_request_id"] != row.delivery_request_id and len(str(p["delivery_request_id"])) <= 255,
            "delivery_request_required",
            "An intentional redeployment requires a new request identity.",
        )
        replacement, _ = Deployment.objects.get_or_create(
            project=project,
            candidate=candidate,
            environment=env,
            delivery_request_id=p["delivery_request_id"],
            defaults={"incident_id": row.id},
        )
        return replacement, None
    if action == "plan":
        state(row, {"Pending"})
        plan = p.get("plan")
        evidence(
            plan, "artifact_digest", "config_digest", "recovery", "preflight", "required_checks", "observation_seconds"
        )
        require(
            plan["artifact_digest"] == candidate.qualification.get("artifact_digest")
            and plan["preflight"].get("passed") is True
            and plan["recovery"].get("safe") is True
            and plan["recovery"].get("prior_identity") == env.identity
            and env.health == "healthy",
            "unsafe_plan",
            "Exact artifact, successful preflight and a verified safe prior identity are required.",
        )
        frozen_config = candidate.manifest.get("config")
        config_digest = frozen_config.get("digest") if isinstance(frozen_config, dict) else frozen_config
        require(
            plan["config_digest"] == config_digest,
            "config_mismatch",
            "Deployment must use the qualified configuration.",
        )
        require(
            type(plan["observation_seconds"]) is int
            and plan["observation_seconds"] > 0
            and isinstance(plan["required_checks"], list)
            and all(isinstance(check, str) for check in plan["required_checks"]),
            "invalid_health_policy",
            "Required checks and a positive observation window must be explicit.",
        )
        row.plan, row.plan_digest, row.prior_identity = plan, digest(plan), env.identity
    elif action == "request_approval":
        state(row, {"Pending", "Recovery Required"})
        require(row.plan_digest, "plan_required", "Prepare the concrete plan first.")
        approval = request_decision(project, "deployment", row, "start", deployment_payload(row))
        return row, {"decision": serial(approval)}
    elif action == "start":
        state(row, {"Pending"})
        require(
            config.deployment_enabled and (not env.production or config.production_enabled),
            "deployment_disabled",
            "Execution is not activated for this environment.",
        )
        require(
            candidate.state == "Published" and candidate.eligible and not env.hold,
            "deployment_ineligible",
            "Published eligible candidate and unheld environment required.",
        )
        require(
            row.plan_digest and row.prior_identity == env.identity,
            "environment_changed",
            "Preflight no longer matches the environment.",
        )
        approval = decision(project, row, "start", deployment_payload(row), p.get("decision_id"))
        owned = lease(project, actor, p, f"environment:{env.id}")
        row.attempt_number += 1
        attempt = DeploymentAttempt.objects.create(
            project=project, deployment=row, number=row.attempt_number, fence=owned.fence, decision_id=approval.id
        )
        operation = intent(
            project, "deployment", row, "install", {**deployment_payload(row), "attempt_id": str(attempt.id)}, approval
        )
        operation.lease, operation.fence = owned, owned.fence
        bump(operation)
        row.state = "Deploying"
        env.health = "unknown"
        bump(env)
        return row, {"attempt": serial(attempt), "operation": serial(operation)}
    elif action == "installed":
        state(row, {"Deploying"})
        lease(project, actor, p, f"environment:{env.id}")
        operation = scoped(WorkflowOperation, project, p.get("operation_id"))
        require(
            operation.subject_id == row.id and operation.kind == "install" and operation.status == "observed",
            "installation_unverified",
            "An observed installation receipt is required.",
        )
        require(
            operation.receipt.get("artifact_digest") == row.plan["artifact_digest"]
            and operation.receipt.get("config_digest") == row.plan["config_digest"]
            and operation.payload.get("attempt_id") == str(row.attempts.get(number=row.attempt_number).id),
            "identity_mismatch",
            "Observed runtime artifact/config differs from the approved plan.",
        )
        row.state = "Verifying"
    elif action == "healthy":
        state(row, {"Verifying"})
        lease(project, actor, p, f"environment:{env.id}")
        e = p.get("evidence")
        evidence(e, "artifact_digest", "config_digest", "checks", "observation_seconds", "receipt_digest")
        require(
            candidate.eligible
            and e["artifact_digest"] == row.plan["artifact_digest"]
            and e["config_digest"] == row.plan["config_digest"]
            and set(row.plan["required_checks"]) <= set(e["checks"])
            and all(e["checks"].get(k) is True for k in row.plan["required_checks"])
            and e["observation_seconds"] >= row.plan["observation_seconds"],
            "health_unverified",
            "Required checks and observation window must pass at the exact identity.",
        )
        row.state = "Healthy"
        env.identity = {
            "candidate_id": str(candidate.id),
            "deployment_id": str(row.id),
            "artifact_digest": e["artifact_digest"],
            "config_digest": e["config_digest"],
        }
        env.last_verified_identity, env.health, env.observed_at, env.hold = env.identity, "healthy", timezone.now(), {}
        bump(env)
        row.attempts.filter(number=row.attempt_number).update(status="healthy", evidence=e)
    elif action == "diagnose":
        state(row, {"Deploying", "Verifying", "Recovery Required"})
        lease(project, actor, p, f"environment:{env.id}")
        evidence(p, "cause", "evidence")
        require(p["cause"] in {"unknown", "non_code", "code"}, "invalid_diagnosis", "Use unknown, non_code, or code.")
        row.diagnosis = p
        env.hold = {"deployment_id": str(row.id), "cause": p["cause"], "evidence": p["evidence"]}
        if p["cause"] == "non_code":
            evidence(p, "repair_plan")
            row.state = "Recovery Required"
        elif p["cause"] == "code":
            evidence(p, "defect_work_id", "reproduction")
            scoped(Issue, project, p["defect_work_id"])
            candidate.eligible = False
            bump(candidate)
            row.state = "Rolling Back"
            attempt = row.attempts.get(number=row.attempt_number)
            approval = scoped(WorkflowDecision, project, attempt.decision_id)
            require(
                approval.payload == deployment_payload(row) and row.plan["recovery"].get("safe") is True,
                "recovery_unauthorized",
                "Only the previously authorized bounded recovery may proceed.",
            )
            intent(
                project,
                "deployment",
                row,
                "restore",
                {
                    "deployment_id": str(row.id),
                    "attempt_id": str(attempt.id),
                    "prior_identity": row.prior_identity,
                    "recovery": row.plan["recovery"],
                },
                approval,
            )
        bump(env)
    elif action == "retry":
        state(row, {"Recovery Required"})
        require(
            row.diagnosis.get("cause") == "non_code" and candidate.eligible and p.get("reconciled") is True,
            "retry_unsafe",
            "Only reconciled non-code failures may retry the same deployment.",
        )
        evidence(p.get("repair_evidence"), "receipt_digest", "verified")
        require(
            p["repair_evidence"]["verified"] is True and env.identity == row.prior_identity,
            "repair_unverified",
            "Repair must restore and verify the unchanged prior identity.",
        )
        approval = decision(project, row, "start", deployment_payload(row), p.get("decision_id"))
        if not approval.payload["plan"].get("allow_retry"):
            prior_ids = set(row.attempts.values_list("decision_id", flat=True))
            require(
                approval.id not in prior_ids, "retry_approval_required", "This approval does not cover another attempt."
            )
        require(
            not WorkflowOperation.objects.filter(
                project=project, subject_id=row.id, status__in=["pending", "running", "unknown"]
            ).exists(),
            "effects_unreconciled",
            "Reconcile all previous effects first.",
        )
        row.state, env.hold = "Pending", {}
        bump(env)
        row.attempts.filter(number=row.attempt_number).update(status="repaired", evidence=p["repair_evidence"])
    elif action == "restored":
        state(row, {"Rolling Back"})
        lease(project, actor, p, f"environment:{env.id}")
        op = scoped(WorkflowOperation, project, p.get("operation_id"))
        require(
            op.subject_id == row.id
            and op.kind == "restore"
            and op.status == "observed"
            and op.receipt.get("identity") == row.prior_identity
            and op.receipt.get("healthy") is True
            and op.receipt.get("schema_verified") is True,
            "restore_unverified",
            "Verified artifact, health and schema restoration receipt required.",
        )
        row.state, row.discarded = "Rolled Back", True
        env.identity, env.last_verified_identity = row.prior_identity, row.prior_identity
        env.health, env.hold, env.observed_at = "healthy", {}, timezone.now()
        bump(env)
        row.attempts.filter(number=row.attempt_number).update(status="rolled_back", evidence=op.receipt)
    elif action in {"abandon", "cancel"}:
        state(row, {"Pending"} if action == "cancel" else {"Deploying", "Recovery Required"})
        evidence(p, "reason")
        require(
            p.get("no_mutation_verified") is True
            and p.get("reconciled") is True
            and not WorkflowOperation.objects.filter(
                project=project, subject_id=row.id, status__in=["pending", "running", "unknown"]
            ).exists(),
            "unsafe_abandonment",
            "Verify no mutation and reconcile every effect before abandonment.",
        )
        if action == "abandon":
            require(
                row.diagnosis.get("cause") == "non_code",
                "diagnosis_required",
                "Abandonment requires a non-code diagnosis.",
            )
        row.state = "Cancelled" if action == "cancel" else "Failed"
        if action == "abandon":
            require(
                env.health == "healthy" and env.identity == row.prior_identity,
                "environment_unverified",
                "Fresh observation of the prior healthy identity is required before abandoning an attempt.",
            )
            env.hold = {}
            bump(env)
    return row, None


def handle_environment(project, actor, action, row, p, config):
    if action == "create":
        require(not row, "immutable_identity", "Environment identity is immutable.")
        evidence(p, "name")
        row = WorkflowEnvironment(project=project, name=p["name"], production=p.get("production", False))
    else:
        require(row, "not_found", "Environment is required.")
        if action == "observe":
            evidence(p, "health", "evidence")
            require(p["health"] in {"unknown", "healthy", "attention"}, "invalid_health", "Unknown health state.")
            if p["health"] == "healthy":
                evidence(p.get("identity"), "artifact_digest", "config_digest")
                evidence(p["evidence"], "receipt_digest", "checks")
                require(p["evidence"]["checks"] is True, "health_unverified", "Observation checks must pass.")
                if p["identity"].get("candidate_id"):
                    candidate = scoped(ReleaseCandidate, project, p["identity"]["candidate_id"])
                    require(
                        candidate.eligible
                        and candidate.qualification.get("artifact_digest") == p["identity"]["artifact_digest"],
                        "candidate_revoked",
                        "Observed candidate is revoked or has another artifact.",
                    )
                row.last_verified_identity = p["identity"]
            row.identity, row.health, row.observed_at = p.get("identity", {}), p["health"], timezone.now()
        elif action == "hold":
            evidence(p, "reason")
            row.hold = {"reason": p["reason"]}
        elif action == "clear_hold":
            evidence(p.get("evidence"), "receipt_digest")
            require(
                p.get("ownership_reconciled") is True and row.health == "healthy",
                "hold_unresolved",
                "Reconcile ownership and verify health first.",
            )
            require(
                not Deployment.objects.filter(
                    environment=row, state__in=["Deploying", "Verifying", "Rolling Back"]
                ).exists(),
                "active_recovery",
                "An unresolved deployment owns this environment hold.",
            )
            row.hold = {}
    return row, None


def handle_decision(project, actor, action, row, p, config):
    require(row, "not_found", "Decision is required.")
    if action == "revoke":
        row.revoked = True
    else:
        require(
            not p.get("conditions"),
            "unsupported_conditions",
            "Use the verified not_before and expires_at timestamps; unsupported conditions require clarification.",
        )
        require(
            row.decision in {"pending", "clarify"} and not row.revoked,
            "decision_closed",
            "Closed decisions cannot be rewritten.",
        )
        require(
            p.get("payload_digest") == row.payload_digest,
            "approval_payload_changed",
            "Review the exact displayed operation.",
        )
        require(p.get("decision") in {"allow", "deny", "clarify"}, "invalid_decision", "Use allow, deny, or clarify.")
        evidence(p, "answer")
        row.decision, row.answer, row.decided_by = p["decision"], p["answer"], actor
        if row.decision == "allow":
            # Explicit decision UI sends a separately labelled audit reason.
            # Text-only approval must not silently discard conditional instructions.
            if p.get("answer_kind") != "reason" and str(p["answer"]).strip().lower().rstrip(".!") not in {
                "yes",
                "accept",
                "approve",
                "approved",
                "allow",
            }:
                row.decision = "clarify"
                return row, None
            require(
                not p.get("conditions") or set(p["conditions"]) <= {"not_before", "expires_at"},
                "unsupported_conditions",
                "Only structured time conditions are supported.",
            )
            row.expires_at = date(p.get("expires_at"))
            row.not_before = date(p["not_before"]) if p.get("not_before") else None
            require(
                row.expires_at > timezone.now() and (not row.not_before or row.not_before < row.expires_at),
                "invalid_time",
                "Approval must have a future bounded validity window.",
            )
    return row, None


def handle_lease(project, actor, action, row, p, config):
    if action == "acquire":
        evidence(p, "resource", "run_id")
        require(
            type(p.get("ttl_seconds", 300)) is int and 0 < p.get("ttl_seconds", 300) <= 3600,
            "invalid_ttl",
            "Lease TTL must be 1 to 3600 seconds.",
        )
        existing = WorkflowLease.objects.select_for_update().filter(project=project, resource=p["resource"]).first()
        if existing:
            require(
                existing.released and existing.reconciled,
                "lease_held",
                "Expiry does not fence an external executor. Reconcile and release its ownership first.",
            )
            require(not row or row.id == existing.id, "lease_mismatch", "Resource belongs to another lease.")
            row = existing
            row.fence += 1
            row.holder, row.run_id, row.released, row.reconciled = actor, p["run_id"], False, False
        else:
            row = WorkflowLease(project=project, resource=p["resource"], holder=actor, run_id=p["run_id"])
        row.expires_at = timezone.now() + timedelta(seconds=p.get("ttl_seconds", 300))
    else:
        require(
            row and row.holder_id == actor.id and row.fence == p.get("fence"),
            "stale_fence",
            "Only the current fenced holder may update its lease.",
        )
        if action == "renew":
            require(
                not row.released and row.expires_at > timezone.now(),
                "lease_expired",
                "Expired ownership requires reconciliation.",
            )
            require(
                type(p.get("ttl_seconds", 300)) is int and 0 < p.get("ttl_seconds", 300) <= 3600,
                "invalid_ttl",
                "Invalid TTL.",
            )
            row.expires_at = timezone.now() + timedelta(seconds=p.get("ttl_seconds", 300))
        else:
            require(
                p.get("executor_stopped") is True and p.get("effects_reconciled") is True,
                "executor_unreconciled",
                "Confirm shutdown and reconciliation before releasing ownership.",
            )
            row.released, row.reconciled = True, True
    return row, None


def handle_operation(project, actor, action, row, p, config):
    require(row, "not_found", "Operation is required.")
    require(row.kind != "run_checks", "not_external_effect", "Check scheduling uses candidate commands.")
    resource = (
        f"environment:{row.payload['environment_id']}"
        if row.kind == "install"
        else (
            f"environment:{scoped(Deployment, project, row.subject_id).environment_id}"
            if row.kind == "restore"
            else f"repository:{row.payload['repository']}"
        )
    )
    owned = lease(project, actor, p, resource, allow_expired=action != "claim")
    if action == "claim":
        require(
            row.status == "pending",
            "operation_requires_reconciliation",
            "Running/unknown operations must be observed before any retry.",
        )
        approval = row.decision
        require(
            approval
            and approval.decision == "allow"
            and not approval.revoked
            and approval.expires_at > timezone.now()
            and (not approval.not_before or approval.not_before <= timezone.now()),
            "approval_required",
            "Operation approval is no longer valid.",
        )
        if row.subject_type == "deployment":
            deployment = scoped(Deployment, project, row.subject_id)
            require(
                (row.kind.startswith("finalize:") and config.release_enabled and deployment.state == "Healthy")
                or (
                    row.kind in {"install", "restore"}
                    and config.deployment_enabled
                    and (not deployment.environment.production or config.production_enabled)
                ),
                "deployment_disabled",
                "Environment execution is disabled.",
            )
            require(
                row.kind == "restore" or deployment.candidate.eligible, "candidate_revoked", "Candidate was revoked."
            )
        else:
            candidate = scoped(ReleaseCandidate, project, row.subject_id)
            require(
                config.release_enabled and candidate.eligible and candidate.state == "Qualified",
                "candidate_ineligible",
                "Publication requires enabled execution and an eligible qualified candidate.",
            )
        row.status, row.lease, row.fence = "running", owned, owned.fence
        row.attempts += 1
    else:
        require(
            row.lease_id == owned.id and row.fence == owned.fence,
            "stale_fence",
            "Operation receipt must come from its fenced owner.",
        )
        require(
            row.status in {"running", "unknown"}, "invalid_operation_state", "Only active effects may report outcomes."
        )
        if action == "unknown":
            row.status = "unknown"
        else:
            evidence(p.get("receipt"), "observed_at", "receipt_digest")
            if p["receipt"].get("outcome") == "no_effect":
                require(
                    row.kind in {"install", "restore"} and p["receipt"].get("no_mutation_verified") is True,
                    "effect_unverified",
                    "A failed operation requires independent proof that it made no mutation.",
                )
                deployment = scoped(Deployment, project, row.subject_id)
                require(
                    p["receipt"].get("identity") == deployment.prior_identity,
                    "identity_mismatch",
                    "The prior environment identity must be observed unchanged.",
                )
                row.receipt, row.status = p["receipt"], "not_applied"
                return row, None
            if row.kind.startswith(("publish:", "finalize:")):
                require(
                    p["receipt"].get("observed_ref") == row.payload["ref"]
                    and p["receipt"].get("observed_sha") == row.payload["expected_new"]
                    and p["receipt"].get("remote_identity") == row.payload["remote_identity"],
                    "publication_unverified",
                    "Receipt must observe the approved remote ref and revision.",
                )
            row.receipt, row.status = p["receipt"], "observed"
    return row, None


def handle_usage(project, actor, action, row, p, config):
    from plane.utils.ai_pricing import compute_cost

    evidence(p, "run_id", "role", "subject_type", "subject_id", "model")
    require(
        p["role"] in {"project_planner", "developer", "pr_reviewer", "system_tester", "system_deployer"},
        "invalid_role",
        "Unknown functional role.",
    )
    require(
        p["subject_type"] in {"work", "candidate", "deployment", "scope"},
        "invalid_subject",
        "Usage requires a typed subject.",
    )
    if p["subject_type"] == "work":
        scoped(Issue, project, p["subject_id"])
    else:
        scoped(MODELS[p["subject_type"]], project, p["subject_id"])
    require(
        not WorkflowUsage.objects.filter(project=project, run_id=p["run_id"]).exists(),
        "usage_already_recorded",
        "Usage for this run is immutable. Replay the same command ID.",
    )
    tokens = p.get("tokens", {})
    fields = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens"}
    require(
        set(tokens) <= fields and all(type(v) is int and v >= 0 for v in tokens.values()),
        "invalid_tokens",
        "Token counts must be nonnegative integers.",
    )
    require(
        p.get("active_seconds") is None or type(p["active_seconds"]) is int and p["active_seconds"] >= 0,
        "invalid_duration",
        "Active duration must be nonnegative seconds or unknown.",
    )
    if p.get("usage_known") is False or not {"input_tokens", "output_tokens"} <= set(tokens):
        cost, price = None, ""
    else:
        cost, price = compute_cost(p["model"], **{f: tokens.get(f, 0) for f in fields})
    row = WorkflowUsage(
        project=project,
        run_id=p["run_id"],
        role=p["role"],
        subject_type=p["subject_type"],
        subject_id=p["subject_id"],
        model=p["model"],
        effort=p.get("effort", ""),
        active_seconds=p.get("active_seconds"),
        cost_usd=cost,
        price_version=price or "",
        tokens=tokens,
    )
    return row, None
