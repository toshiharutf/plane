import { ScopeBudgets } from "./scope-budget";
import { WorkQuestionInbox } from "./work-questions";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import useSWR from "swr";
import { WorkflowV2Service } from "@/services/workflow-v2.service";
import type { TProgressMeasure, TWorkflowCommand, TWorkflowRecord } from "@/services/workflow-v2.service";
import { HumanRequestService } from "@/services/human-request.service";
import { DecisionReview, DeploymentRecovery } from "./decision";
import { Evidence, EvidenceDrawer, WorkLinks } from "./evidence";
import { CostSummary, ProgressCards, ProgressHistory } from "./progress";
import { commandError, metricIds, valueLabel } from "./utils";

const service = new WorkflowV2Service();
const humanService = new HumanRequestService();
type Selection =
  | { kind: "candidate" | "deployment" | "decision"; id: string }
  | { kind: "metric"; title: string; metric: TProgressMeasure }
  | { kind: "definitions" };
const panel = "rounded-lg border border-subtle bg-surface-1 p-5";

export function ProjectDeliveryPage({
  workspaceSlug,
  projectId,
  view,
}: {
  workspaceSlug: string;
  projectId: string;
  view: "progress" | "releases" | "decisions";
}) {
  const [search, setSearch] = useSearchParams();
  const [selection, setSelection] = useState<Selection | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const filters = {
    scope_id: search.get("scope_id") || undefined,
    scope_revision: search.get("scope_revision") || undefined,
    cycle_id: search.get("cycle_id") || undefined,
    target: search.get("target") || undefined,
  };
  const {
    data: snapshot,
    error: progressError,
    mutate: refreshProgress,
    isValidating,
  } = useSWR(
    ["project-progress", workspaceSlug, projectId, filters],
    () => service.progress(workspaceSlug, projectId, filters),
    { refreshInterval: 30_000, keepPreviousData: false }
  );
  const {
    data: workflow,
    error: workflowError,
    mutate: refreshWorkflow,
  } = useSWR(["workflow-v2", workspaceSlug, projectId], () => service.snapshot(workspaceSlug, projectId), {
    refreshInterval: 30_000,
  });
  const { data: questions } = useSWR(
    view === "decisions" ? ["delivery-human-requests", workspaceSlug, projectId] : null,
    () => humanService.listOpen(workspaceSlug)
  );
  // Keep effect controls closed when the projection is missing, stale or failed. Commands still recheck all guards.
  const stale = !snapshot || snapshot.provenance.stale || Boolean(progressError || workflowError);
  useEffect(() => {
    setSelection(null);
    setFailure("");
  }, [workspaceSlug, projectId, filters.scope_id, filters.scope_revision, filters.cycle_id, filters.target]);
  const candidates = workflow?.candidates ?? snapshot?.candidates ?? [];
  const deployments = workflow?.deployments ?? snapshot?.deployments ?? [];
  const decisions = workflow?.decisions ?? [];
  const work = snapshot?.scope.items ?? [];
  const selected =
    selection && "id" in selection
      ? (selection.kind === "candidate" ? candidates : selection.kind === "deployment" ? deployments : decisions).find(
          (record) => record.id === selection.id
        )
      : undefined;
  const refresh = async () => {
    await Promise.all([refreshProgress(), refreshWorkflow()]);
  };
  const submit = async (command: Omit<TWorkflowCommand, "command_id">): Promise<boolean> => {
    if (stale || busy) return false;
    setBusy(true);
    setFailure("");
    try {
      await service.command(workspaceSlug, projectId, { ...command, command_id: crypto.randomUUID() });
      await refresh();
      return true;
    } catch (error) {
      setFailure(commandError(error));
      await refresh();
      return false;
    } finally {
      setBusy(false);
    }
  };
  const changeFilter = (key: string, value: string) => {
    const next = new URLSearchParams(search);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key === "scope_id") next.delete("scope_revision");
    setSearch(next);
  };
  const open = (kind: "candidate" | "deployment" | "decision", record: TWorkflowRecord) => {
    setFailure("");
    setSelection({ kind, id: record.id });
  };
  const renderRecords = (records: TWorkflowRecord[], kind: "candidate" | "deployment" | "decision") =>
    records.length ? (
      <ul className="space-y-2">
        {records.map((record) => (
          <li key={record.id}>
            <button
              type="button"
              className="w-full rounded border border-subtle p-3 text-left hover:bg-layer-1"
              onClick={() => open(kind, record)}
            >
              <span className="font-medium">{record.name ?? record.id}</span>
              <span className="ml-2 text-13 text-secondary">
                {valueLabel(record.phase ?? record.state ?? record.decision)}
              </span>
              {record.eligible === false && <span className="ml-2 text-13"> · Ineligible / revoked</span>}
              {Boolean(record.discarded) && <span className="ml-2 text-13"> · Discarded</span>}
              <div className="mt-1 text-11 text-tertiary">
                {record.action
                  ? valueLabel(record.action)
                  : valueLabel(record.next_action ?? record.guards ?? "Inspect guards and evidence")}
              </div>
            </button>
          </li>
        ))}
      </ul>
    ) : (
      <p className="text-13 text-tertiary">None recorded in this view.</p>
    );
  return (
    <main className="h-full overflow-y-auto bg-layer-1 p-4 md:p-6" aria-label="Project delivery">
      <div className="mx-auto max-w-7xl space-y-5">
        <header className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-24 font-semibold">
              {view === "progress" ? "Project progress" : view === "releases" ? "Releases" : "Human decisions"}
            </h1>
            <p className="mt-1 text-13 text-secondary">Implementation, qualification and verified delivery</p>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={busy || isValidating}
            className="rounded border border-subtle bg-surface-1 px-3 py-2 text-13"
          >
            {isValidating ? "Updating…" : "Refresh"}
          </button>
        </header>
        <nav className="flex flex-wrap gap-4 text-13" aria-label="Project delivery sections">
          {["progress", "releases", "decisions"].map((tab) => (
            <Link
              key={tab}
              to={`/${workspaceSlug}/projects/${projectId}/${tab}${search.size ? `?${search}` : ""}`}
              aria-current={view === tab ? "page" : undefined}
              className={view === tab ? "font-semibold text-accent-primary underline" : "text-secondary"}
            >
              {tab === "progress" ? "Project progress" : tab === "releases" ? "Releases" : "Decisions"}
            </Link>
          ))}
          <Link to={`/${workspaceSlug}/projects/${projectId}/issues`} className="text-secondary">
            Work items
          </Link>
        </nav>
        {(progressError || workflowError) && (
          <div role="alert" className={panel}>
            {commandError(progressError ?? workflowError)}{" "}
            <button type="button" onClick={() => void refresh()} className="underline">
              Retry loading
            </button>
          </div>
        )}
        {!snapshot && !progressError && <p role="status">Loading project evidence…</p>}
        {snapshot && (
          <>
            <section className={`${panel} flex flex-wrap items-end gap-4`} aria-label="Scope and target filters">
              <label className="text-13">
                Approved scope
                <select
                  aria-label="Approved scope"
                  value={filters.scope_id ?? ""}
                  onChange={(event) => changeFilter("scope_id", event.target.value)}
                  className="mt-1 block max-w-full rounded border border-subtle bg-surface-1 p-2"
                >
                  <option value="">Current approved scope</option>
                  {snapshot.scopes.map((scope) => (
                    <option key={scope.id} value={scope.id}>
                      {scope.name ?? scope.id} · revision {valueLabel(scope.revision)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-13">
                Delivery target
                <select
                  aria-label="Delivery target"
                  value={filters.target ?? ""}
                  onChange={(event) => changeFilter("target", event.target.value)}
                  className="mt-1 block rounded border border-subtle bg-surface-1 p-2"
                >
                  <option value="">Planned target</option>
                  {snapshot.environments.map((environment) => (
                    <option key={environment.id} value={String(environment.name ?? environment.id)}>
                      {environment.name ?? environment.id}
                    </option>
                  ))}
                </select>
              </label>
              {snapshot.cycles && (
                <label className="text-13">
                  Cycle
                  <select
                    aria-label="Cycle"
                    value={filters.cycle_id ?? ""}
                    onChange={(event) => changeFilter("cycle_id", event.target.value)}
                    className="mt-1 block rounded border border-subtle bg-surface-1 p-2"
                  >
                    <option value="">All selected work</option>
                    {snapshot.cycles.map((cycle) => (
                      <option key={cycle.id} value={cycle.id}>
                        {cycle.name ?? cycle.id}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <div className="text-11 text-tertiary">
                <p>
                  Scope revision {snapshot.scope.revision ?? "—"} · {snapshot.scope.items.length} items ·{" "}
                  {snapshot.scope.excluded_ids.length} excluded
                </p>
                <p>
                  Snapshot {new Date(snapshot.provenance.as_of).toLocaleString()} · event{" "}
                  {snapshot.provenance.source_sequence}
                </p>
                <button type="button" onClick={() => setSelection({ kind: "definitions" })} className="mt-1 underline">
                  How counts work
                </button>
              </div>
            </section>
            {Boolean(snapshot.scope.scope_revisions) && (
              <details className={panel}>
                <summary className="cursor-pointer text-13">Approved scope revisions in this cycle</summary>
                <Evidence value={snapshot.scope.scope_revisions} />
              </details>
            )}
            {Boolean(snapshot.unplanned_work_ids?.length) && (
              <p className="text-13 text-secondary">
                {snapshot.unplanned_work_ids?.length} work items await scope admission. They do not change this approved
                baseline.
              </p>
            )}
            {Boolean(snapshot.incomplete_evidence?.length) && (
              <details className={panel}>
                <summary className="cursor-pointer text-13">Incomplete scope evidence</summary>
                <Evidence value={snapshot.incomplete_evidence} />
              </details>
            )}
            {stale && (
              <p role="status" className="border-amber-500 rounded border p-3 text-13">
                Stale or unavailable evidence. Effect decisions are disabled until current state is refreshed.
              </p>
            )}
            {view === "progress" && (
              <>
                <ProgressCards
                  snapshot={snapshot}
                  inspect={(title, metric) => setSelection({ kind: "metric", title, metric })}
                  attention={() => document.getElementById("project-attention")?.focus()}
                />
                <section className={panel}>
                  <h2 className="mb-3 text-16 font-semibold">Delivery path</h2>
                  <p className="mb-4 text-13 text-secondary">
                    {snapshot.scope.name ?? "Approved scope"} → verified work → frozen candidate and checks →
                    publication → separately authorized deployment
                  </p>
                  <div className="grid gap-5 lg:grid-cols-2">
                    <div>
                      <h3 className="mb-2 font-medium">Release candidates</h3>
                      {renderRecords(snapshot.candidates, "candidate")}
                      {!snapshot.candidates.length && (
                        <p className="mt-2 text-13">
                          A Draft is created when an approved release scope is activated. Readiness requires accepted
                          work evidence.
                        </p>
                      )}
                    </div>
                    <div>
                      <h3 className="mb-2 font-medium">Deployments</h3>
                      {renderRecords(snapshot.deployments, "deployment")}
                      {!snapshot.deployments.length && (
                        <p className="mt-2 text-13">
                          Pending deployments follow verified publication for an explicitly planned target. Publication
                          approval does not authorize installation.
                        </p>
                      )}
                    </div>
                  </div>
                </section>
                {snapshot.modules && (
                  <section className={panel}>
                    <h2 className="mb-3 text-16 font-semibold">Scope progress by module</h2>
                    <p className="mb-3 text-13 text-secondary">Project totals count shared work once.</p>
                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-13">
                        <thead>
                          <tr>
                            <th>Module</th>
                            <th>Completed work</th>
                            <th>Qualified code</th>
                            <th>Verified delivery</th>
                          </tr>
                        </thead>
                        <tbody>
                          {snapshot.modules.map((module) => (
                            <tr key={module.id} className="border-t border-subtle">
                              <th className="font-normal py-3">{module.name ?? module.id}</th>
                              {["completed_work", "qualified_code", "verified_delivery"].map((key) => {
                                const metric = (module.metrics as Record<string, TProgressMeasure> | undefined)?.[key];
                                return (
                                  <td key={key}>
                                    {metric ? (
                                      <button
                                        type="button"
                                        className="text-accent-primary underline"
                                        onClick={() =>
                                          setSelection({
                                            kind: "metric",
                                            title: `${module.name ?? module.id} · ${key.replaceAll("_", " ")}`,
                                            metric,
                                          })
                                        }
                                      >
                                        {metric.denominator
                                          ? `${metric.numerator ?? "Unverified"} / ${metric.denominator}`
                                          : "No applicable work"}
                                      </button>
                                    ) : (
                                      "Unknown"
                                    )}
                                  </td>
                                );
                              })}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </section>
                )}
                <section className={panel}>
                  <h2 className="mb-3 text-16 font-semibold">Progress over time</h2>
                  <ProgressHistory history={snapshot.history} />
                </section>
                <section className={panel}>
                  <h2 className="mb-3 text-16 font-semibold">Cost and active time</h2>
                  <CostSummary
                    work={snapshot.scope.items}
                    usage={((workflow?.usage as TWorkflowRecord[] | undefined) ?? []).filter(
                      (usage) =>
                        usage.subject_type === "work" &&
                        snapshot.scope.items.some((item) => item.id === usage.subject_id)
                    )}
                  />
                  <ScopeBudgets
                    scopes={(snapshot.selected_scopes ?? snapshot.scopes).filter((scope) =>
                      snapshot.scope.id
                        ? scope.id === snapshot.scope.id
                        : filters.cycle_id
                          ? scope.cycle_id === filters.cycle_id
                          : false
                    )}
                  />
                  {snapshot.costs && <Evidence value={snapshot.costs} />}
                </section>
              </>
            )}
            {view === "releases" && (
              <div className="grid gap-5 lg:grid-cols-2">
                <section className={panel}>
                  <h2 className="mb-3 text-16 font-semibold">Release candidates</h2>
                  {renderRecords(candidates, "candidate")}
                </section>
                <section className={panel}>
                  <h2 className="mb-3 text-16 font-semibold">Deployments and attempts</h2>
                  {renderRecords(deployments, "deployment")}
                </section>
              </div>
            )}
            {(view === "progress" || view === "decisions") && (
              <section id="project-attention" tabIndex={-1} className={panel}>
                <h2 className="mb-3 text-16 font-semibold">Attention and decisions</h2>
                {renderRecords(
                  (view === "progress"
                    ? snapshot.attention.filter((gate) => gate.kind === "decision" || gate.subject_type)
                    : decisions
                  ).filter((decision) => decision.decision === "pending" || decision.decision === "clarify"),
                  "decision"
                )}
                <WorkQuestionInbox
                  work={view === "progress" ? work : ((workflow?.work as TWorkflowRecord[] | undefined) ?? [])}
                  workspaceSlug={workspaceSlug}
                  projectId={projectId}
                  stale={stale}
                  busy={busy}
                  submit={submit}
                />
                {failure && !selection && (
                  <p role="alert" className="mt-3 text-13">
                    {failure}
                  </p>
                )}
                <details className="mt-3">
                  <summary className="cursor-pointer text-13">Scope guards and operational holds</summary>
                  <Evidence value={snapshot.attention} />
                </details>
                {view === "decisions" && (
                  <>
                    <h3 className="mt-5 mb-3 font-medium">Work questions</h3>
                    <ul className="space-y-3">
                      {questions
                        ?.filter((question) => question.project_id === projectId)
                        .map((question) => (
                          <li key={question.id} className="rounded border border-subtle p-3">
                            <Link
                              className="text-accent-primary underline"
                              to={`/${workspaceSlug}/projects/${projectId}/issues/${question.issue_id}`}
                            >
                              {question.issue_name}
                            </Link>
                            <p className="mt-1 text-13 whitespace-pre-wrap">{question.question}</p>
                            <p className="mt-1 text-11 text-tertiary">
                              Answer on the work item to preserve its intended continuation.
                            </p>
                          </li>
                        ))}
                    </ul>
                    <details className="mt-4">
                      <summary className="cursor-pointer text-13">Decision history</summary>
                      {renderRecords(decisions, "decision")}
                    </details>
                  </>
                )}
              </section>
            )}
            <section className={panel}>
              <h2 className="mb-3 text-16 font-semibold">Environment identity</h2>
              <p className="mb-3 text-13">
                {snapshot.environment.name ?? "Selected target"} · {snapshot.environment.status}
              </p>
              <Evidence value={snapshot.environment} />
            </section>
            {view === "progress" && (
              <section className={panel}>
                <h2 className="mb-3 text-16 font-semibold">Activity and cycle evidence</h2>
                <Evidence value={snapshot.activity ?? workflow?.activity ?? []} />
              </section>
            )}
          </>
        )}
      </div>
      {selection && (
        <EvidenceDrawer
          title={
            selection.kind === "metric"
              ? selection.title
              : selection.kind === "definitions"
                ? "Progress definitions"
                : `${selection.kind} details`
          }
          onClose={() => {
            if (!busy) setSelection(null);
          }}
        >
          {failure && (
            <p role="alert" className="border-red-500 mb-4 rounded border p-3 text-13 whitespace-pre-wrap">
              {failure}
            </p>
          )}
          {selection.kind === "definitions" ? (
            <div className="space-y-4 text-13">
              <p>
                Completed work counts distinct executable leaf work with accepted integration or manual evidence.
                Administrative Done without evidence is unverified.
              </p>
              <p>
                Qualified code excludes manual work and requires eligible matching candidate evidence at the selected
                scope revision.
              </p>
              <p>
                Verified delivery uses the currently observed environment identity and artifact provenance. Historical
                Healthy does not prove the current installation; rollback can reduce coverage.
              </p>
              <p>
                Cancelled work and grouping parents are excluded. Shared module and candidate membership never increases
                the project denominator. Empty scope means no applicable work. Missing evidence and cost remain unknown.
              </p>
              <p>
                Historical snapshots keep their original scope baseline. An amended scope changes the baseline visibly.
              </p>
            </div>
          ) : selection.kind === "metric" ? (
            <WorkLinks
              ids={metricIds(selection.metric)}
              verifiedIds={selection.metric.numerator_ids}
              records={work}
              workspace={workspaceSlug}
              project={projectId}
            />
          ) : selected ? (
            <div className="space-y-4">
              {selection.kind === "decision" ? (
                <DecisionReview
                  key={`${selected.id}:${selected.version}`}
                  record={selected}
                  stale={stale}
                  busy={busy}
                  submit={submit}
                />
              ) : (
                <>
                  <p className="font-medium">
                    {valueLabel(selected.phase ?? selected.state)}
                    {selected.eligible === false ? " · Ineligible / revoked" : ""}
                    {selected.discarded ? " · Discarded" : ""}
                  </p>
                  {selection.kind === "deployment" &&
                    selected.state === "Healthy" &&
                    (() => {
                      const effects = ((workflow?.operations as TWorkflowRecord[] | undefined) ?? []).filter(
                        (operation) =>
                          operation.subject_id === selected.id && String(operation.kind).startsWith("finalize:")
                      );
                      const approval = decisions.find(
                        (decision) => decision.subject_id === selected.id && decision.action === "finalize"
                      );
                      return (
                        <section className="rounded border border-subtle p-3">
                          <h3 className="font-medium">Publication finalization</h3>
                          <p className="mt-1 text-13">
                            {effects.length
                              ? effects.every((effect) => effect.status === "observed")
                                ? "Completed — all finalization receipts observed."
                                : effects.some((effect) => ["failed", "unknown"].includes(String(effect.status)))
                                  ? "Finalization needs attention. Deployment remains Healthy; reconcile the recorded publication effects."
                                  : "Finalization pending. Deployment remains Healthy; do not redeploy to retry publication."
                              : approval
                                ? "Waiting for the separate finalization decision or executor."
                                : "No finalization operation requested."}
                          </p>
                        </section>
                      );
                    })()}
                  <Evidence value={selected} />
                  {selection.kind === "deployment" && (
                    <DeploymentRecovery
                      key={`${selected.id}:${selected.version}`}
                      record={selected}
                      decisions={decisions}
                      stale={stale}
                      busy={busy}
                      submit={submit}
                    />
                  )}
                  <h3 className="font-medium">Checks, attempts and operation receipts</h3>
                  <Evidence
                    value={[
                      ...((workflow?.checks as TWorkflowRecord[] | undefined) ?? []).filter(
                        (check) => check.candidate_id === selected.id
                      ),
                      ...((workflow?.attempts as TWorkflowRecord[] | undefined) ?? []).filter(
                        (attempt) => attempt.deployment_id === selected.id
                      ),
                      ...((workflow?.operations as TWorkflowRecord[] | undefined) ?? []).filter(
                        (operation) => operation.subject_id === selected.id
                      ),
                      ...((workflow?.leases as TWorkflowRecord[] | undefined) ?? []).filter(
                        (lease) => lease.subject_id === selected.id
                      ),
                    ]}
                  />
                  {Array.isArray(selected.corrective_work_ids) && (
                    <>
                      <h3 className="font-medium">Corrective work</h3>
                      <WorkLinks
                        ids={selected.corrective_work_ids as string[]}
                        records={work}
                        workspace={workspaceSlug}
                        project={projectId}
                      />
                    </>
                  )}
                  {typeof (selected.diagnosis as { work_id?: string } | undefined)?.work_id === "string" && (
                    <>
                      <h3 className="font-medium">Corrective work</h3>
                      <WorkLinks
                        ids={[(selected.diagnosis as { work_id: string }).work_id]}
                        records={work}
                        workspace={workspaceSlug}
                        project={projectId}
                      />
                    </>
                  )}
                  <h3 className="font-medium">Related decisions</h3>
                  {renderRecords(
                    decisions.filter((decision) => decision.subject_id === selected.id),
                    "decision"
                  )}
                  {selection.kind === "candidate" && (
                    <>
                      <h3 className="font-medium">Deployments</h3>
                      {renderRecords(
                        deployments.filter((deployment) => deployment.candidate_id === selected.id),
                        "deployment"
                      )}
                    </>
                  )}
                </>
              )}
            </div>
          ) : (
            <p>This record is no longer available in your current view. Refresh the snapshot.</p>
          )}
        </EvidenceDrawer>
      )}
    </main>
  );
}
