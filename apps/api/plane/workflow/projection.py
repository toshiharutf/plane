"""Permission-scoped live projections from a single project-serialized snapshot."""

import copy
import json
from pathlib import Path
from django.db import transaction
from django.utils import timezone
from plane.db.models import (
    Project,
    Issue,
    ModuleIssue,
    WorkContinuation,
    ReleaseScope,
    ReleaseCandidate,
    Deployment,
    WorkflowConfiguration,
    WorkflowEnvironment,
    WorkflowDecision,
    WorkflowLease,
    WorkflowOperation,
    WorkflowCapability,
    WorkflowUsage,
    WorkflowEvent,
    WorkflowCheckRun,
    DeploymentAttempt,
)
from .service import serial, membership, authorize, Refusal, ACTIONS, work_verified, digest


def allowed(project, actor, kind, row):
    actions = []
    for action in ACTIONS[kind]:
        try:
            authorize(
                project,
                actor,
                kind,
                action,
                row.issue_id if kind == "work" else row.id,
                getattr(row, "repository", "") or (str(row.environment_id) if kind == "deployment" else ""),
            )
            actions.append(action)
        except Refusal:
            pass
    return actions


def record(project, actor, kind, row):
    data = serial(row)
    data["allowed_actions"] = allowed(project, actor, kind, row)
    if kind == "work":
        data["name"] = row.issue.name
        data["human_wait_minutes"] = human_wait_minutes(row)
        from .budget import admission

        data["budget_admission"] = admission(project, issue_id=row.issue_id)

        for question in data.get("continuation", {}).get("requests", {}).values():
            question["allowed_actions"] = []
            if question.get("status") == "open":
                if "resolve_human" in data["allowed_actions"]:
                    question["allowed_actions"].append("resolve_human")
                if "withdraw_human" in data["allowed_actions"] and question.get("requester_id") == str(actor.id):
                    question["allowed_actions"].append("withdraw_human")
        usage = list(WorkflowUsage.objects.filter(project=project, subject_type="work", subject_id=row.issue_id))
        data["actual_cost_usd"] = (
            str(sum(u.cost_usd for u in usage)) if usage and all(u.cost_usd is not None for u in usage) else None
        )
        data["actual_active_minutes"] = (
            sum(u.active_seconds for u in usage) / 60
            if usage and all(u.active_seconds is not None for u in usage)
            else None
        )
    if kind == "scope":
        data["definition_digest"] = digest(row.definition)
        from .budget import admission

        data["budget_admission"] = admission(project, scope=row)

        if getattr(row, "pending_revision", None):
            data["pending_revision"] = row.pending_revision
            data["allowed_actions"] = []
    if kind == "decision":
        data["status"] = row.decision
        data["constraints"] = {"not_before": data["not_before"], "expires_at": data["expires_at"]}
    return data


def human_wait_minutes(row):
    """Accumulate only accepted Awaiting Human intervals, independently of run usage."""
    waiting_since = None
    seconds = 0.0
    found = False
    for event in WorkflowEvent.objects.filter(project=row.project, subject_type="work", subject_id=row.id).order_by(
        "sequence"
    ):
        found = True
        waiting = event.after.get("state") == "Awaiting Human"
        if waiting and waiting_since is None:
            waiting_since = event.created_at
        elif not waiting and waiting_since is not None:
            seconds += max(0, (event.created_at - waiting_since).total_seconds())
            waiting_since = None
    if waiting_since is not None:
        seconds += max(0, (timezone.now() - waiting_since).total_seconds())
    return seconds / 60 if found else None


@transaction.atomic
def records(project_id, actor):
    project = Project.objects.select_for_update().get(pk=project_id)
    membership(project, actor)
    contract = json.loads(Path(__file__).with_name("contract.json").read_text())
    result = {
        "workflow_versions": [2],
        "workflow_version": 2,
        "contract_digest": digest(contract),
        "capability_schema_version": 1,
    }
    kinds = {
        "work": WorkContinuation,
        "scopes": ReleaseScope,
        "candidates": ReleaseCandidate,
        "deployments": Deployment,
        "environments": WorkflowEnvironment,
        "decisions": WorkflowDecision,
        "leases": WorkflowLease,
        "operations": WorkflowOperation,
        "capabilities": WorkflowCapability,
        "usage": WorkflowUsage,
    }
    singular = {
        "scopes": "scope",
        "candidates": "candidate",
        "deployments": "deployment",
        "environments": "environment",
        "decisions": "decision",
        "leases": "lease",
        "operations": "operation",
        "capabilities": "capability",
    }
    for key, model in kinds.items():
        query = model.objects.filter(project=project).order_by("created_at")
        if key == "capabilities" and actor.is_bot:
            query = query.filter(principal=actor)
        result[key] = [record(project, actor, singular.get(key, key), row) for row in query]
    config = WorkflowConfiguration.objects.filter(project=project).first()
    result["configuration"] = (
        record(project, actor, "configuration", config)
        if config
        else {
            "enabled": False,
            "release_enabled": False,
            "deployment_enabled": False,
            "production_enabled": False,
            "workflow_version": 2,
            "version": 0,
        }
    )
    result["checks"] = [serial(r) for r in WorkflowCheckRun.objects.filter(project=project)]
    result["attempts"] = [serial(r) for r in DeploymentAttempt.objects.filter(project=project)]
    result["provenance"] = provenance(project)
    return result


def provenance(project):
    last = WorkflowEvent.objects.filter(project=project).order_by("-sequence").first()
    return {"as_of": timezone.now().isoformat(), "source_sequence": last.sequence if last else 0, "stale": False}


def metric(numerator, denominator, status="verified"):
    return {
        "numerator": len(numerator) if status != "unverified" else None,
        "denominator": len(denominator),
        "numerator_ids": sorted(numerator),
        "denominator_ids": sorted(denominator),
        "status": "no_applicable_work" if not denominator else status,
    }


def covered(candidate, members):
    return {
        item["id"]
        for item in candidate.manifest.get("work", [])
        if item["id"] in members
        and item.get("revision") == members[item["id"]].scope_revision
        and item.get("kind") == "code"
        and item.get("evidence", {}).get("integration")
    }


@transaction.atomic
def progress(project_id, actor, filters):
    project = Project.objects.select_for_update().get(pk=project_id)
    membership(project, actor)
    all_scopes = []
    for current in ReleaseScope.objects.filter(project=project).order_by("-created_at"):
        if current.approved:
            all_scopes.append(current)
            continue
        previous = (
            WorkflowEvent.objects.filter(
                project=project, subject_type="scope", subject_id=current.id, after__approved=True
            )
            .order_by("-sequence")
            .first()
        )
        if previous:
            baseline = copy.copy(current)
            baseline.pending_revision = current.revision
            baseline.revision = previous.after["revision"]
            baseline.definition = previous.after["definition"]
            baseline.approved = True
            all_scopes.append(baseline)
    scopes = [
        item
        for item in all_scopes
        if (not filters.get("cycle_id") or str(item.cycle_id) == filters["cycle_id"])
        and (not filters.get("scope_id") or str(item.id) == filters["scope_id"])
        and (not filters.get("scope_revision") or item.revision == int(filters["scope_revision"]))
    ]
    scope = scopes[0] if scopes else None
    selected_scopes = (
        list(scopes) if filters.get("cycle_id") and not filters.get("scope_id") else ([scope] if scope else [])
    )
    scope_ids = {item.id for item in selected_scopes}
    approved_members = {}
    leaf_ids, frozen_hierarchy = set(), set()
    for selected_scope in selected_scopes:
        for item in selected_scope.definition.get("work", []):
            approved_members[item["id"]] = max(approved_members.get(item["id"], 0), item["revision"])
        leaf_ids.update(
            selected_scope.definition.get(
                "executable_leaf_ids", [item["id"] for item in selected_scope.definition.get("work", [])]
            )
        )
        if "executable_leaf_ids" in selected_scope.definition:
            frozen_hierarchy.update(item["id"] for item in selected_scope.definition.get("work", []))
    members, excluded, incomplete, obsolete = {}, set(), [], set()
    for issue_id, revision in approved_members.items():
        row = WorkContinuation.objects.filter(project=project, issue_id=issue_id).select_related("issue").first()
        if (
            issue_id not in leaf_ids
            or (issue_id not in frozen_hierarchy and Issue.objects.filter(parent_id=issue_id).exists())
            or row
            and row.state == "Cancelled"
        ):
            excluded.add(issue_id)
            continue
        members[issue_id] = row
        if issue_id in frozen_hierarchy and Issue.objects.filter(parent_id=issue_id).exists():
            obsolete.add(issue_id)
            incomplete.append({"work_id": issue_id, "reason": "hierarchy_changed_requires_scope_amendment"})
        if not row or row.scope_revision != revision:
            obsolete.add(issue_id)
            incomplete.append({"work_id": issue_id, "reason": "missing_or_obsolete_scope_evidence"})
    selected = {key: row for key, row in members.items() if row is not None}
    eligible_work = {key: row for key, row in selected.items() if key not in obsolete}
    denominator = set(members)
    completed = {key for key, row in eligible_work.items() if work_verified(row)}
    code = {key for key, row in selected.items() if row.execution_kind == "code"}
    code |= {key for key, row in members.items() if row is None}
    target = filters.get("target")
    environments = list(WorkflowEnvironment.objects.filter(project=project))
    env = (
        next((e for e in environments if str(e.id) == target or e.name == target), None)
        if target
        else (next((e for e in environments if e.production), environments[0] if environments else None))
    )
    applicable = set()
    for selected_scope in selected_scopes:
        mapping = selected_scope.definition.get("delivery_targets")
        applicable.update(
            mapping.get(str(env.id) if env else "publication", [])
            if mapping
            else [item["id"] for item in selected_scope.definition.get("work", [])]
        )
    code &= applicable
    candidates = list(ReleaseCandidate.objects.filter(project=project).select_related("scope"))
    qualified = set()
    for candidate in candidates:
        if candidate.eligible and candidate.state in {"Qualified", "Published"}:
            qualified |= covered(candidate, eligible_work)
    qualified &= code
    delivered, last_verified = set(), set()
    status = "unverified"
    if env:
        for candidate in candidates:
            if str(candidate.id) == env.last_verified_identity.get("candidate_id"):
                last_verified |= covered(candidate, eligible_work)
            if (
                str(candidate.id) == env.identity.get("candidate_id")
                and candidate.eligible
                and env.health == "healthy"
                and not env.hold
            ):
                if env.identity.get("artifact_digest") == candidate.qualification.get("artifact_digest"):
                    delivered |= covered(candidate, eligible_work)
                    status = "verified"
        if env.health == "healthy" and not env.hold and not env.identity.get("candidate_id"):
            status = "verified"
    elif selected_scopes and all(not item.definition.get("targets") for item in selected_scopes):
        status = "verified"
        for candidate in candidates:
            if candidate.state == "Published" and candidate.eligible:
                delivered |= covered(candidate, eligible_work)
    delivered &= code
    attention = []
    scoped_ids = {str(c.id) for c in candidates if c.scope_id in scope_ids}
    deliveries = list(Deployment.objects.filter(project=project))
    scoped_ids |= {str(d.id) for d in deliveries if str(d.candidate_id) in scoped_ids}
    for d in WorkflowDecision.objects.filter(project=project, decision__in=["pending", "clarify"], revoked=False):
        if str(d.subject_id) in scoped_ids:
            attention.append({**record(project, actor, "decision", d), "kind": "decision"})
    for key, row in selected.items():
        if row.state == "Awaiting Human":
            requests = record(project, actor, "work", row)["continuation"].get("requests", {})
            for request_key, question in requests.items():
                if question.get("status") == "open":
                    attention.append(
                        {
                            "id": f"{key}:{request_key}",
                            "request_key": request_key,
                            "kind": "question",
                            "work_id": key,
                            "next_action": row.next_action,
                            **question,
                        }
                    )
    for e in environments:
        if e.hold:
            attention.append({"id": str(e.id), "kind": "environment_hold", "environment_id": str(e.id), **e.hold})
    modules = []
    module_ids = (
        ModuleIssue.objects.filter(project=project, issue_id__in=denominator)
        .values_list("module_id", flat=True)
        .distinct()
    )
    for module_id in module_ids:
        rows = ModuleIssue.objects.filter(module_id=module_id, issue_id__in=denominator).select_related("module")
        ids = {str(r.issue_id) for r in rows}
        modules.append(
            {
                "id": str(module_id),
                "name": rows[0].module.name,
                "metrics": {
                    "completed_work": metric(completed & ids, denominator & ids),
                    "qualified_code": metric(qualified & ids, code & ids),
                    "verified_delivery": metric(delivered & ids, code & ids, status),
                },
            }
        )
    selected_candidate_ids = {candidate.id for candidate in candidates if candidate.scope_id in scope_ids}
    selected_deployment_ids = {
        delivery.id for delivery in deliveries if delivery.candidate_id in selected_candidate_ids
    }
    usage = [
        row
        for row in WorkflowUsage.objects.filter(project=project)
        if (
            row.subject_type == "work"
            and str(row.subject_id) in denominator
            or row.subject_type == "scope"
            and row.subject_id in scope_ids
            or row.subject_type == "candidate"
            and row.subject_id in selected_candidate_ids
            or row.subject_type == "deployment"
            and row.subject_id in selected_deployment_ids
        )
    ]

    metrics = {
        "completed_work": metric(completed, denominator),
        "qualified_code": metric(qualified, code),
        "verified_delivery": {
            **metric(delivered, code, status),
            "last_verified": len(last_verified & code),
            "last_verified_ids": sorted(last_verified & code),
        },
        "needs_attention": {"count": len({a["id"] for a in attention})},
    }
    prov = provenance(project)
    result = {
        "scope": {
            **record(project, actor, "scope", scope),
            "items": sorted(denominator),
            "excluded_ids": sorted(excluded),
        }
        if scope
        else None,
        "scopes": [record(project, actor, "scope", s) for s in all_scopes],
        "metrics": metrics,
        "attention": attention,
        "incomplete_evidence": incomplete,
        "modules": modules,
        "work": [record(project, actor, "work", r) for r in selected.values()],
        "candidates": [record(project, actor, "candidate", c) for c in candidates if c.scope_id in scope_ids],
        "deployments": [
            record(project, actor, "deployment", d) for d in deliveries if str(d.candidate_id) in scoped_ids
        ],
        "environments": [serial(e) for e in environments],
        "environment": {**serial(env), "status": env.health, "current_identity": env.identity} if env else None,
        "usage": {
            "known_cost_usd": str(sum(u.cost_usd for u in usage if u.cost_usd is not None)),
            "unknown_cost_runs": sum(u.cost_usd is None for u in usage),
            "active_seconds": sum(u.active_seconds for u in usage if u.active_seconds is not None),
            "unknown_duration_runs": sum(u.active_seconds is None for u in usage),
        },
        "provenance": prov,
        "history": historical(project, selected_scopes, env),
        "unplanned_work_ids": list(
            map(
                str,
                WorkContinuation.objects.filter(project=project)
                .exclude(issue_id__in=denominator | excluded)
                .values_list("issue_id", flat=True),
            )
        ),
    }
    result["usage"]["work_known_cost_usd"] = str(
        sum(row.cost_usd for row in usage if row.subject_type == "work" and row.cost_usd is not None)
    )
    result["usage"]["release_overhead_known_cost_usd"] = str(
        sum(row.cost_usd for row in usage if row.subject_type != "work" and row.cost_usd is not None)
    )
    result["usage"]["actual_cost_usd"] = (
        str(sum(row.cost_usd for row in usage)) if usage and all(row.cost_usd is not None for row in usage) else None
    )
    result["usage"]["run_count"] = len(usage)
    result["selected_scopes"] = [record(project, actor, "scope", item) for item in selected_scopes]
    result["scope_revisions"] = [{"scope_id": str(item.id), "revision": item.revision} for item in selected_scopes]
    if len(selected_scopes) > 1:
        result["scope"] = {
            "id": None,
            "revision": None,
            "name": "Selected cycle",
            "items": sorted(denominator),
            "excluded_ids": sorted(excluded),
            "scope_revisions": result["scope_revisions"],
        }
    from plane.db.models import Cycle

    result["cycles"] = [{"id": str(c.id), "name": c.name} for c in Cycle.objects.filter(project=project)]
    result["checks"] = [serial(c) for c in WorkflowCheckRun.objects.filter(project=project)]
    result["attempts"] = [serial(a) for a in DeploymentAttempt.objects.filter(project=project)]
    result["operations"] = [
        record(project, actor, "operation", o) for o in WorkflowOperation.objects.filter(project=project)
    ]
    result["activity"] = [
        {
            "id": e.sequence,
            "action": e.action,
            "subject_type": e.subject_type,
            "subject_id": str(e.subject_id),
            "created_at": e.created_at.isoformat(),
        }
        for e in WorkflowEvent.objects.filter(project=project).order_by("-sequence")[:30]
    ]
    return result


def historical(project, scopes, env):
    """Replay accepted event-time evidence without carrying obsolete coverage forward."""
    if not scopes:
        return []
    if not isinstance(scopes, list):
        scopes = [scopes]
    selected_ids = {str(scope.id) for scope in scopes}
    work, candidates, environment, baselines = {}, {}, {}, {}
    result = []
    for event in WorkflowEvent.objects.filter(project=project).order_by("sequence"):
        after = event.after
        kind, subject_id = event.subject_type, str(event.subject_id)
        relevant = False
        baseline_changed = False
        if kind == "scope" and subject_id in selected_ids:
            # Pending amendments cannot silently replace the last approved baseline.
            if after.get("approved"):
                previous = baselines.get(subject_id)
                baselines[subject_id] = after
                baseline_changed = not previous or previous.get("revision") != after.get("revision")
            relevant = True
        elif kind == "work":
            work[after["issue_id"]] = after
            relevant = True
        elif kind == "candidate":
            candidates[subject_id] = after
            relevant = True
        elif kind == "environment" and env and subject_id == str(env.id):
            environment = after
            relevant = True
        if not baselines or not relevant:
            continue
        baseline, leaf_ids, frozen_hierarchy = {}, set(), set()
        for scope_data in baselines.values():
            definition = scope_data.get("definition", {})
            for member in definition.get("work", []):
                baseline[member["id"]] = max(baseline.get(member["id"], 0), member["revision"])
            leaf_ids.update(definition.get("executable_leaf_ids", [m["id"] for m in definition.get("work", [])]))
            if "executable_leaf_ids" in definition:
                frozen_hierarchy.update(member["id"] for member in definition.get("work", []))
        ids = {
            key
            for key in baseline
            if key in leaf_ids
            and work.get(key, {}).get("state") != "Cancelled"
            and (key in frozen_hierarchy or not work.get(key, {}).get("issue_snapshot", {}).get("is_summary", False))
        }
        if kind == "work" and after.get("issue_id") not in baseline:
            continue
        if kind == "candidate" and not any(m["id"] in baseline for m in after.get("manifest", {}).get("work", [])):
            continue
        code = {key for key in ids if work.get(key, {}).get("execution_kind", "code") == "code"}
        applicable = set()
        for scope_data in baselines.values():
            definition = scope_data.get("definition", {})
            targets = definition.get("delivery_targets")
            applicable.update(
                targets.get(str(env.id) if env else "publication", [])
                if targets
                else [member["id"] for member in definition.get("work", [])]
            )
        code &= applicable
        completed = {
            key
            for key in ids
            if work.get(key, {}).get("state") == "Done"
            and work[key].get("scope_revision") == baseline[key]
            and not work[key].get("evidence", {}).get("unverified")
            and bool(
                work[key].get("evidence", {}).get("integration")
                if work[key].get("execution_kind") == "code"
                else work[key].get("evidence", {}).get("manual")
            )
        }
        qualified, delivered = set(), set()
        verified = False
        for key, candidate in candidates.items():
            covered_ids = {
                m["id"]
                for m in candidate.get("manifest", {}).get("work", [])
                if m["id"] in code
                and m.get("revision") == baseline[m["id"]]
                and m.get("kind") == "code"
                and m.get("evidence", {}).get("integration")
            }
            if candidate.get("eligible") and candidate.get("state") in {"Qualified", "Published"}:
                qualified |= covered_ids
                identity = environment.get("identity", {})
                if (
                    environment.get("health") == "healthy"
                    and not environment.get("hold")
                    and identity.get("candidate_id") == key
                    and identity.get("artifact_digest") == candidate.get("qualification", {}).get("artifact_digest")
                ):
                    delivered |= covered_ids
                    verified = True
                if (
                    env is None
                    and candidate.get("state") == "Published"
                    and all(not scope_data.get("definition", {}).get("targets") for scope_data in baselines.values())
                ):
                    delivered |= covered_ids
                    verified = True
        if (
            environment.get("health") == "healthy"
            and not environment.get("hold")
            and not environment.get("identity", {}).get("candidate_id")
        ):
            verified = True
        revisions = [{"scope_id": key, "revision": value.get("revision")} for key, value in baselines.items()]
        result.append(
            {
                "as_of": event.created_at.isoformat(),
                "source_sequence": event.sequence,
                "scope_revision": revisions[0]["revision"] if len(revisions) == 1 else None,
                "scope_revisions": revisions,
                "baseline_changed": baseline_changed,
                "completed_work": len(completed),
                "qualified_code": len(qualified),
                "verified_delivery": len(delivered) if verified else None,
                "denominator": len(ids),
                "code_denominator": len(code),
            }
        )
    return result
