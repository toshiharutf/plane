"""Idempotent linked scope proposals; creation never approves execution or spend."""

from decimal import Decimal
from html import escape
from plane.db.models import Issue, IssueRelation, WorkContinuation, ReleaseCandidate, Deployment
from .service import require, evidence, scoped, state, lease, digest, serial, work_verified
from .budget import quantity


def require_blocking_followups_complete(project, source):
    pending = []
    for proposal in source.evidence.get("followups", []):
        if not proposal["blocking"]:
            continue
        target = WorkContinuation.objects.filter(project=project, issue_id=proposal["issue_id"]).first()
        if not target or not work_verified(target):
            pending.append(proposal["issue_id"])
    require(
        not pending,
        "blocking_followup_open",
        "Required follow-up work must be admitted and verified before completion.",
        work_ids=pending,
    )


def propose(project, actor, source, payload, config):
    state(source, {"In Progress", "In Review"})
    key = payload.get("proposal_key")
    require(
        isinstance(key, str) and 0 < len(key) <= 255,
        "invalid_proposal_key",
        "A bounded stable proposal key is required.",
    )
    require(
        type(payload.get("blocking")) is bool, "invalid_followup", "Declare whether the follow-up blocks acceptance."
    )
    evidence(payload, "name", "acceptance", "alternatives", "reproduction", "expected_behavior", "run_id")
    require(
        isinstance(payload["name"], str)
        and len(payload["name"]) <= 255
        and all(
            isinstance(payload[field], list)
            and payload[field]
            and all(isinstance(value, str) and value.strip() for value in payload[field])
            for field in ("acceptance", "alternatives")
        )
        and isinstance(payload["reproduction"], str)
        and isinstance(payload["expected_behavior"], str),
        "invalid_followup",
        "Provide a name, criteria, alternatives, reproduction and expected behavior.",
    )
    original = quantity(payload.get("original_minutes"))
    unexpected = quantity(payload.get("unexpected_minutes"))
    baseline = source.estimated_active_minutes if source.estimated_active_minutes is not None else source.minute_ceiling
    require(
        original is not None
        and original > 0
        and unexpected is not None
        and unexpected > 0
        and baseline is not None
        and original == Decimal(baseline),
        "followup_estimate_mismatch",
        "Proposal must use the recorded estimated original effort.",
    )
    owned = lease(
        project,
        actor,
        payload,
        f"repository:{source.repository}" if source.state == "In Review" else f"work:{source.issue_id}",
    )
    require(str(owned.run_id) == payload["run_id"], "run_mismatch", "Follow-up must belong to the assigned fenced run.")
    if payload.get("candidate_id"):
        scoped(ReleaseCandidate, project, payload["candidate_id"])
    if payload.get("deployment_id"):
        deployment = scoped(Deployment, project, payload["deployment_id"])
        require(
            not payload.get("candidate_id") or str(deployment.candidate_id) == payload["candidate_id"],
            "followup_subject_mismatch",
            "Linked deployment and candidate must match.",
        )
    normalized = {key: value for key, value in payload.items() if key not in {"lease_id", "fence"}}
    proposal_digest = digest(normalized)
    existing = next(
        (proposal for proposal in source.evidence.get("followups", []) if proposal["proposal_key"] == key), None
    )
    if existing:
        require(
            existing["payload_digest"] == proposal_digest,
            "followup_conflict",
            "The stable proposal key already binds different inputs.",
        )
        target = WorkContinuation.objects.get(project=project, issue_id=existing["issue_id"])
        return source, {
            "work": serial(source),
            "followup": serial(target),
            "threshold_required": unexpected > original * Decimal("0.25"),
        }
    if payload.get("defect_work_id"):
        require(payload["defect_work_id"] != str(source.issue_id), "invalid_followup", "Work cannot follow up itself.")
        issue = scoped(Issue, project, payload["defect_work_id"])
        target = WorkContinuation.objects.filter(project=project, issue=issue).first()
        require(target, "followup_not_enrolled", "Existing corrective work must have an explicit workflow enrollment.")
    else:
        body = (
            "<h2>Origin</h2><p>"
            + escape(str(source.issue_id))
            + "</p><h2>Reproduction</h2><p>"
            + escape(payload["reproduction"])
            + "</p>"
        )
        body += "<h2>Expected behavior</h2><p>" + escape(payload["expected_behavior"]) + "</p><h2>Acceptance</h2><ul>"
        body += (
            "".join("<li>" + escape(item) + "</li>" for item in payload["acceptance"])
            + "</ul><h2>Alternatives</h2><ul>"
        )
        body += "".join("<li>" + escape(item) + "</li>" for item in payload["alternatives"]) + "</ul>"
        issue = Issue.objects.create(
            project=project,
            workspace_id=project.workspace_id,
            name=payload["name"],
            state_id=config.state_mapping["Backlog"],
            description_html=body,
        )
        target = WorkContinuation.objects.create(
            project=project,
            issue=issue,
            state="Backlog",
            repository=source.repository,
            execution_kind="code",
            next_action="develop",
            active=False,
            followup_of=source.issue,
            proposal_key=key,
            version=1,
            estimated_active_minutes=int(unexpected.to_integral_value(rounding="ROUND_CEILING")),
            evidence={
                "origin": {
                    "source_work_id": str(source.issue_id),
                    "run_id": payload["run_id"],
                    "candidate_id": payload.get("candidate_id"),
                    "deployment_id": payload.get("deployment_id"),
                    "proposal_key": key,
                    "payload_digest": proposal_digest,
                }
            },
        )
    IssueRelation.objects.get_or_create(
        project=project,
        workspace_id=project.workspace_id,
        issue=source.issue,
        related_issue=issue,
        defaults={"relation_type": "blocked_by" if payload["blocking"] else "relates_to"},
    )
    source.evidence = {
        **source.evidence,
        "followups": [
            *source.evidence.get("followups", []),
            {
                "proposal_key": key,
                "issue_id": str(issue.id),
                "blocking": payload["blocking"],
                "payload_digest": proposal_digest,
                "threshold_required": unexpected > original * Decimal("0.25"),
            },
        ],
    }
    return source, {
        "work": serial(source),
        "followup": serial(target),
        "threshold_required": unexpected > original * Decimal("0.25"),
    }
