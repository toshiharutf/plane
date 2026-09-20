"""Committed USD/active-minute admission, evaluated under the project command lock."""

from decimal import Decimal, InvalidOperation

from plane.db.models import (
    ReleaseScope,
    WorkContinuation,
    WorkflowUsage,
    WorkflowEvent,
    ReleaseCandidate,
    Deployment,
    Issue,
)


def approved_scopes(project, override=None):
    """A pending amendment does not erase the previous approved commitments."""
    rows = []
    for scope in ReleaseScope.objects.filter(project=project):
        if override and scope.id == override.id:
            rows.append(override)
        elif scope.approved:
            rows.append(scope)
        else:
            previous = (
                WorkflowEvent.objects.filter(
                    project=project, subject_type="scope", subject_id=scope.id, after__approved=True
                )
                .order_by("-sequence")
                .first()
            )
            if previous:
                scope.definition = previous.after["definition"]
                scope.revision = previous.after["revision"]
                rows.append(scope)
    if override and not any(scope.id == override.id for scope in rows):
        rows.append(override)
    return rows


def quantity(value, integer=False):
    try:
        if value is None or isinstance(value, bool):
            return None
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or integer and result != int(result):
            return None
        return result
    except (ValueError, InvalidOperation):
        return None


def evaluate(project, scopes, definition, label):
    policy = definition.get("policy", "cost") if isinstance(definition, dict) else None
    reasons = []
    if policy not in {"cost", "time", "both"}:
        return {"label": label, "guards": [{"code": "invalid_budget_policy", "label": label}]}
    work_ids = set()
    for scope in scopes:
        leaves = set(scope.definition.get("executable_leaf_ids", [m["id"] for m in scope.definition.get("work", [])]))
        for member in scope.definition.get("work", []):
            if member["id"] in leaves and (
                "executable_leaf_ids" in scope.definition or not Issue.objects.filter(parent_id=member["id"]).exists()
            ):
                work_ids.add(member["id"])
                if "executable_leaf_ids" in scope.definition and Issue.objects.filter(parent_id=member["id"]).exists():
                    reasons.append({"code": "hierarchy_changed_requires_scope_amendment", "work_id": member["id"]})
    work = {str(row.issue_id): row for row in WorkContinuation.objects.filter(project=project, issue_id__in=work_ids)}
    scope_ids = {scope.id for scope in scopes}
    candidate_ids = set(
        ReleaseCandidate.objects.filter(project=project, scope_id__in=scope_ids).values_list("id", flat=True)
    )
    deployment_ids = set(
        Deployment.objects.filter(project=project, candidate_id__in=candidate_ids).values_list("id", flat=True)
    )
    usage = [
        row
        for row in WorkflowUsage.objects.filter(project=project)
        if (
            row.subject_type == "work"
            and str(row.subject_id) in work_ids
            or row.subject_type == "scope"
            and row.subject_id in scope_ids
            or row.subject_type == "candidate"
            and row.subject_id in candidate_ids
            or row.subject_type == "deployment"
            and row.subject_id in deployment_ids
        )
    ]
    dimensions = {}
    for dimension, relevant, ceiling_key, reserve_key, allocation_key, actual_key in (
        ("cost", policy in {"cost", "both"}, "cost_ceiling_usd", "contingency_usd", "cost_ceiling", "cost_usd"),
        (
            "time",
            policy in {"time", "both"},
            "active_minute_ceiling",
            "contingency_minutes",
            "minute_ceiling",
            "active_seconds",
        ),
    ):
        if not relevant:
            continue
        ceiling = quantity(definition.get(ceiling_key), integer=dimension == "time")
        reserve = quantity(definition.get(reserve_key, 0), integer=dimension == "time")
        if ceiling is None or reserve is None:
            reasons.append({"code": "budget_ceiling_required", "label": label, "dimension": dimension})
            continue
        allocation, charged, unknown, overhead = Decimal(0), Decimal(0), [], Decimal(0)
        per_work = {}
        for row in usage:
            amount = getattr(row, actual_key)
            if amount is None:
                unknown.append(str(row.run_id))
                continue
            amount = Decimal(amount) / 60 if dimension == "time" else Decimal(amount)
            charged += amount
            if row.subject_type == "work":
                key = str(row.subject_id)
                per_work[key] = per_work.get(key, Decimal(0)) + amount
            else:
                overhead += amount
        for key in work_ids:
            allocated = quantity(getattr(work.get(key), allocation_key, None), integer=dimension == "time")
            if allocated is None:
                reasons.append(
                    {"code": "task_ceiling_required", "label": label, "dimension": dimension, "work_id": key}
                )
            else:
                allocation += max(allocated, per_work.get(key, Decimal(0)))
        committed = allocation + max(reserve, overhead)
        if committed > ceiling:
            reasons.append(
                {
                    "code": "committed_budget_exceeded",
                    "label": label,
                    "dimension": dimension,
                    "committed": str(committed),
                    "ceiling": str(ceiling),
                }
            )
        if unknown:
            reasons.append({"code": "usage_unknown", "label": label, "dimension": dimension, "run_ids": unknown})
        if charged >= ceiling:
            reasons.append(
                {
                    "code": "budget_exhausted",
                    "label": label,
                    "dimension": dimension,
                    "actual": str(charged),
                    "ceiling": str(ceiling),
                }
            )
        dimensions[dimension] = {
            "ceiling": str(ceiling),
            "committed": str(committed),
            "contingency": str(reserve),
            "actual": str(charged) if usage and not unknown else None,
            "unknown_runs": len(unknown),
            "units": "USD" if dimension == "cost" else "active_minutes",
        }
    return {"label": label, "policy": policy, "work_ids": sorted(work_ids), "dimensions": dimensions, "guards": reasons}


def admission(project, issue_id=None, scope=None):
    scopes = approved_scopes(project, override=scope)
    selected = [
        item
        for item in scopes
        if scope
        and item.id == scope.id
        or issue_id
        and any(member["id"] == str(issue_id) for member in item.definition.get("work", []))
    ]
    contexts = []
    cycles = set()
    for item in selected:
        if "budget" in item.definition:
            contexts.append(evaluate(project, [item], item.definition["budget"], f"scope:{item.id}:{item.revision}"))
        if item.cycle_id:
            cycles.add(item.cycle_id)
    for cycle_id in cycles:
        cycle_scopes = [item for item in scopes if item.cycle_id == cycle_id]
        definitions = [item.definition["cycle_budget"] for item in cycle_scopes if "cycle_budget" in item.definition]
        if not definitions:
            continue
        label = f"cycle:{cycle_id}"
        if any(definition != definitions[0] for definition in definitions[1:]):
            contexts.append({"label": label, "guards": [{"code": "cycle_budget_conflict", "label": label}]})
        else:
            contexts.append(evaluate(project, cycle_scopes, definitions[0], label))
    return {
        "contexts": contexts,
        "guards": [reason for context in contexts for reason in context["guards"]],
        "policy": "explicit" if contexts else "legacy_task_ceiling_only",
    }
