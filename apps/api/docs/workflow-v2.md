# Guarded work, releases and deployments

Workflow v2 is opt-in per project. A configuration row defaults to disabled work,
release and deployment execution, with production execution independently disabled.
The implementation performs no Git, container, SSH or network effects in an HTTP
request. It records a durable intent; a separately credentialed executor observes
and receipts the exact approved effect.

Both authenticated API surfaces call `plane.workflow.service.execute`:

- Session: `/api/workspaces/{slug}/projects/{project}/workflow-v2/`
- Token: `/api/v1/workspaces/{slug}/projects/{project}/workflow-v2/`
- POST `commands/` beneath either path submits a command.
- GET `progress/` beneath the project path returns a consistent scoped projection.

The read response includes configuration, capabilities, work, scopes, candidates,
checks, deployments, attempts, decisions, environments, leases, operations and usage.
It advertises workflow version 2 and the SHA-256 digest of `contract.json`. Clients
must refuse unsupported versions; legacy completion is not a v2 fallback.

## Commands and authority

```json
{
  "command_id": "a UUID generated once and retained across retries",
  "workflow_version": 2,
  "subject_type": "work",
  "subject_id": "the Issue UUID",
  "expected_version": 4,
  "action": "submit",
  "payload": {
    "lease_id": "the lease UUID",
    "fence": 1,
    "evidence": {
      "base_sha": "full immutable Git SHA",
      "head_sha": "full immutable Git SHA",
      "checks": true,
      "post_session_checks": true
    }
  }
}
```

Work command subjects use the existing Issue ID; other subjects use their own UUID.
Creation commands omit `subject_id` and expect version zero. Configuration commands
may omit the subject while expecting the existing configuration version. Every
accepted command, primary and secondary event, and resulting intent commits in one
transaction under a project lock. A byte-equivalent canonical command replay returns
the persisted response; reusing its ID for another payload fails. Canonical JSON uses
sorted keys, compact separators, UTF-8 and rejects NaN.

A session-provided role label grants no authority. Bots require an unexpired,
unrevoked action capability, project/resource scope and optional subject/run binding.
Only project administrators grant capabilities or configure activation. Human
membership is required for questions, manual verification, plan approval and effect
authorization. Give AI sessions only their narrow contributor capabilities; keep
integration and external-effect credentials in the deterministic controller.

`lease.acquire` takes a stable resource (`work:UUID`, `repository:IDENTITY`,
`candidate:UUID`, `environment:UUID`), a run UUID and a bounded TTL. Reacquisition
increments the fence only after the previous owner explicitly stopped and reconciled.
Expiry never proves that an external process stopped. Expired owners can report an
observed result under their unchanged fence, but cannot start another effect.

## Activation and compatibility

Run `manage.py workflow_migration_report --project UUID` for a read-only inventory.
Map all seven WorkItem phases to distinct project state IDs in `configuration.configure`
and explicitly enable each rollout feature. Display names can change without changing
the mapping. Unmapped states and historical completions without integration evidence
remain exceptions. The report does not infer permission or evidence from a title.

Once enabled, legacy model, queryset, app/API, bulk and raw SQL state writes are
blocked by an early structured refusal and a PostgreSQL trigger. Initial Backlog/Todo
creation remains available. Command writes use a transaction-local database flag.
Only trusted server code may set that flag. Legacy projects retain their previous
behavior, including the `all_objects` hard-delete manager semantics.

## Evidence and handoffs

Work enrolls an existing Issue, then activates and claims under a lease. A successful
submission reaches In Review. Only independent accepted integration at the exact
revision reaches Done. Human work has its own manual verification action. Questions
retain independent request keys, answers, next action and continuation; denying a
request cannot queue execution. A review question resumes review, and inactive work
cannot dispatch after an answer.

Approved scope activation creates one Draft per scope revision. Readiness checks
accepted work, approved complete source delta, checks, compatibility and holds. Ready
scopes freeze their exact manifest and queue checks. Infrastructure interruption creates
an incomplete CheckRun; product failure rejects the candidate and links corrective work.
Qualification seals passing checks for the same manifest and final artifact identity.

Publication requires a separate exact human decision and observed receipts for every
repository effect. Partial publication stays Qualified. Published candidates create
Pending deployments only for their frozen planned targets. Deployment preflight binds
artifact, config, prior identity, safe recovery and verification policy. Installation
requires another decision, environment lease and enabled execution policy.

Non-code recovery reconciles the previous attempt and verifies repair before retrying
the same deployment with a new attempt. Code defects revoke eligibility and require
verified restoration before Rolled Back/discarded. Unknown causes and unsafe rollback
retain holds and their current phase. A healthy deployment's later main/tag publication
uses `request_finalization`/`finalize`; pending or unknown finalization never rewrites
its historical Healthy result. Intentional redeployment requires a new delivery request.

## Projection and usage

Progress filters are `scope_id`, `scope_revision`, `cycle_id`, and `target` (environment
UUID or name). Completed work, eligible qualified code and currently verified delivery
have separate denominators and contributing IDs. Manual work does not count as code.
Unknown runtime identity returns an unverified numerator and last verified coverage.
Empty denominators mean no applicable work. Event-time history carries its baseline
revision and change annotations. Server-side guards recheck state for every action.

Run usage is immutable and project/run unique. Replay the same command after a lost
response. Stored pricing and duration distinguish unknown values from zero; retries
retain prior consumption. Existing usage APIs also have database-backed delivery
identity deduplication and persisted cost categories.

## Local verification

The pytest configuration defaults to `--nomigrations`; explicitly pass `--migrations`
for the workflow suite so the PostgreSQL trigger is exercised. Use an isolated local
PostgreSQL/Redis database and `DEBUG=0` in the environment:

```
python -m pytest --migrations --reuse-db \
  plane/tests/unit/test_workflow_v2.py \
  plane/tests/unit/test_workflow_release.py \
  plane/tests/unit/test_workflow_hardening.py
```

These drills cover lost-response reconciliation, stale fences, concurrent claims,
review continuations, null/bulk/raw state bypass, partial publication, revoked/expired
approvals, immutable checks, operational retries, verified code rollback and
post-health finalization. Real local Git/artifact drills live in the companion project
manager worktree. No production adapter is implicitly enabled by this suite.

## Budget admission and migration cutover

A scope may declare `definition.budget` and `definition.cycle_budget` using
`policy` (`cost`, `time`, or `both`), `cost_ceiling_usd`, `active_minute_ceiling`,
`contingency_usd` and `contingency_minutes`. Scope approval, activation and work
claims validate committed distinct leaf ceilings, retained usage and shared release
overhead under the same project lock. Unknown usage blocks the relevant dimension.
Forecasts do not grant capacity. Missing budget policy is explicitly reported as
`legacy_task_ceiling_only`. The projection exposes the same `budget_admission`
contexts and refusal reasons, including conflicting cycle ceilings.

The administrator-only `configuration.migrate` command takes reviewed `mapping`,
`enrollments`, `source_digest` (canonical digest of those two lists), and a minimal
`source_records` snapshot containing Issue `id`, `project_id`, `state_id` and
`updated_at`. `source_records_digest` binds that snapshot; the server compares each
source with current rows before applying it. Use Python ISO timestamps with an
explicit UTC offset. Set `legacy_scheduler_disabled: true`, `activate: true`, and
provide the seven-state mapping. Unmapped or in-flight sources block the whole
transaction. Successful cutover preserves append-only source mappings and records
legacy scheduler shutdown together with activation. Historical Done imports remain
unverified; no old decision becomes new publication/deployment authority.

Progress retains the last approved baseline while an amendment awaits approval.
Selecting a cycle unions its approved scopes, deduplicates memberships and returns
all baseline revisions. Human wait is measured from accepted event intervals,
independently from active run duration. Work and shared release usage are charged
once and reported separately as well as in project totals.
