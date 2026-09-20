"""Version 2 workflow records. External effects are intents until observed receipts exist."""

import uuid
from django.db import models


class WorkflowRecord(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE)
    version = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class WorkflowConfiguration(WorkflowRecord):
    enabled = models.BooleanField(default=False)
    release_enabled = models.BooleanField(default=False)
    deployment_enabled = models.BooleanField(default=False)
    production_enabled = models.BooleanField(default=False)
    state_mapping = models.JSONField(default=dict)
    migration = models.JSONField(default=dict)
    legacy_scheduler_disabled = models.BooleanField(default=False)
    workflow_version = models.PositiveIntegerField(default=2)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project"], name="workflow_one_configuration")]


class WorkflowCapability(WorkflowRecord):
    principal = models.ForeignKey("db.User", on_delete=models.CASCADE)
    actions = models.JSONField(default=list)
    subject_type = models.CharField(max_length=32)
    subject_id = models.UUIDField(null=True)
    run_id = models.UUIDField(null=True)
    resource = models.CharField(max_length=255, blank=True)
    expires_at = models.DateTimeField()
    revoked = models.BooleanField(default=False)


class WorkContinuation(WorkflowRecord):
    issue = models.OneToOneField("db.Issue", on_delete=models.CASCADE, related_name="workflow_v2")
    state = models.CharField(max_length=32, default="Backlog")
    execution_kind = models.CharField(max_length=16, default="code")
    repository = models.CharField(max_length=255)
    scope_revision = models.PositiveIntegerField(default=1)
    next_action = models.CharField(max_length=40, default="develop")
    followup_of = models.ForeignKey("db.Issue", on_delete=models.PROTECT, null=True, related_name="workflow_followups")
    proposal_key = models.CharField(max_length=255, blank=True)

    active = models.BooleanField(default=False)
    dependencies = models.JSONField(default=list)
    evidence = models.JSONField(default=dict)
    continuation = models.JSONField(default=dict)
    estimated_cost_usd = models.DecimalField(max_digits=14, decimal_places=6, null=True)
    estimated_active_minutes = models.PositiveIntegerField(null=True)
    cost_ceiling = models.DecimalField(max_digits=14, decimal_places=6, null=True)
    minute_ceiling = models.PositiveIntegerField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "followup_of", "proposal_key"],
                condition=models.Q(followup_of__isnull=False),
                name="workflow_followup_proposal",
            )
        ]


class ReleaseScope(WorkflowRecord):
    name = models.CharField(max_length=255)
    revision = models.PositiveIntegerField(default=1)
    cycle = models.ForeignKey("db.Cycle", on_delete=models.SET_NULL, null=True)
    active = models.BooleanField(default=False)
    approved = models.BooleanField(default=False)
    definition = models.JSONField(default=dict)


class ReleaseCandidate(WorkflowRecord):
    scope = models.ForeignKey(ReleaseScope, on_delete=models.PROTECT, related_name="candidates")
    scope_revision = models.PositiveIntegerField()
    state = models.CharField(max_length=32, default="Draft")
    manifest = models.JSONField(default=dict)
    manifest_digest = models.CharField(max_length=64, blank=True)
    qualification = models.JSONField(default=dict)
    qualification_digest = models.CharField(max_length=64, blank=True)
    eligible = models.BooleanField(default=True)
    superseded_by = models.ForeignKey("self", on_delete=models.PROTECT, null=True)
    guards = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["scope", "scope_revision"], name="workflow_candidate_scope_revision")
        ]


class WorkflowEnvironment(WorkflowRecord):
    name = models.CharField(max_length=255)
    production = models.BooleanField(default=False)
    identity = models.JSONField(default=dict)
    last_verified_identity = models.JSONField(default=dict)
    health = models.CharField(max_length=32, default="unknown")
    hold = models.JSONField(default=dict)
    observed_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "name"], name="workflow_environment_name")]


class Deployment(WorkflowRecord):
    candidate = models.ForeignKey(ReleaseCandidate, on_delete=models.PROTECT, related_name="deployments")
    environment = models.ForeignKey(WorkflowEnvironment, on_delete=models.PROTECT, related_name="deployments")
    state = models.CharField(max_length=32, default="Pending")
    plan = models.JSONField(default=dict)
    plan_digest = models.CharField(max_length=64, blank=True)
    prior_identity = models.JSONField(default=dict)
    diagnosis = models.JSONField(default=dict)
    discarded = models.BooleanField(default=False)
    attempt_number = models.PositiveIntegerField(default=0)
    delivery_request_id = models.CharField(max_length=255, default="initial")
    incident_id = models.UUIDField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["candidate", "environment", "delivery_request_id"], name="workflow_initial_delivery"
            )
        ]


class DeploymentAttempt(WorkflowRecord):
    deployment = models.ForeignKey(Deployment, on_delete=models.PROTECT, related_name="attempts")
    number = models.PositiveIntegerField()
    fence = models.PositiveBigIntegerField()
    decision_id = models.UUIDField()
    status = models.CharField(max_length=32, default="running")
    evidence = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["deployment", "number"], name="workflow_attempt_number")]


class WorkflowDecision(WorkflowRecord):
    subject_type = models.CharField(max_length=32)
    subject_id = models.UUIDField()
    action = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    payload_digest = models.CharField(max_length=64)
    decision = models.CharField(max_length=16, default="pending")
    answer = models.TextField(blank=True)
    decided_by = models.ForeignKey("db.User", on_delete=models.PROTECT, null=True)
    not_before = models.DateTimeField(null=True)
    expires_at = models.DateTimeField(null=True)
    revoked = models.BooleanField(default=False)


class WorkflowLease(WorkflowRecord):
    resource = models.CharField(max_length=255)
    holder = models.ForeignKey("db.User", on_delete=models.PROTECT)
    run_id = models.UUIDField()
    fence = models.PositiveBigIntegerField(default=1)
    expires_at = models.DateTimeField()
    released = models.BooleanField(default=False)
    reconciled = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "resource"], name="workflow_resource_lease")]


class WorkflowCheckRun(WorkflowRecord):
    candidate = models.ForeignKey(ReleaseCandidate, on_delete=models.PROTECT, related_name="checks")
    run_id = models.UUIDField(unique=True)
    definition_digest = models.CharField(max_length=64)
    manifest_digest = models.CharField(max_length=64)
    artifact_digest = models.CharField(max_length=255)
    outcome = models.CharField(max_length=32)
    evidence = models.JSONField(default=dict)


class WorkflowCommand(models.Model):
    id = models.UUIDField(primary_key=True)
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE)
    actor = models.ForeignKey("db.User", on_delete=models.PROTECT)
    digest = models.CharField(max_length=64)
    response = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class WorkflowEvent(models.Model):
    sequence = models.BigAutoField(primary_key=True)
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE)
    command = models.ForeignKey(WorkflowCommand, on_delete=models.PROTECT)
    subject_type = models.CharField(max_length=32)
    subject_id = models.UUIDField()
    version = models.PositiveIntegerField()
    action = models.CharField(max_length=64)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class WorkflowOperation(WorkflowRecord):
    subject_type = models.CharField(max_length=32)
    subject_id = models.UUIDField()
    kind = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    payload_digest = models.CharField(max_length=64)
    decision = models.ForeignKey(WorkflowDecision, on_delete=models.PROTECT, null=True)
    status = models.CharField(max_length=32, default="pending")
    fence = models.PositiveBigIntegerField(null=True)
    lease = models.ForeignKey(WorkflowLease, on_delete=models.PROTECT, null=True)
    receipt = models.JSONField(default=dict)
    attempts = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "subject_type", "subject_id", "kind", "payload_digest"],
                name="workflow_unique_effect_intent",
            )
        ]


class WorkflowUsage(WorkflowRecord):
    run_id = models.UUIDField()
    role = models.CharField(max_length=32)
    subject_type = models.CharField(max_length=32)
    subject_id = models.UUIDField()
    model = models.CharField(max_length=255)
    effort = models.CharField(max_length=32, blank=True)
    active_seconds = models.PositiveIntegerField(null=True)
    cost_usd = models.DecimalField(max_digits=14, decimal_places=6, null=True)
    price_version = models.CharField(max_length=64, blank=True)
    tokens = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["project", "run_id"], name="workflow_usage_run")]
