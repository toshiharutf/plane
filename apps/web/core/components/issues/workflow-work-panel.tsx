import { useIssueDetail } from "@/hooks/store/use-issue-detail";
import { ManualWorkVerification, WorkQuestionForm, workQuestions } from "@/components/project-delivery/work-questions";
import { useState } from "react";
import useSWR from "swr";
import { Link } from "react-router";
import { WorkflowV2Service } from "@/services/workflow-v2.service";
import type { TWorkflowCommand, TWorkflowRecord } from "@/services/workflow-v2.service";
import { Evidence } from "@/components/project-delivery/evidence";
import { amountLabel, canAct, commandError, recordVersion, valueLabel } from "@/components/project-delivery/utils";

const service = new WorkflowV2Service();
export function WorkRoutingBadge({
  work,
}: {
  work?: { workflow_v2?: { next_action?: string; state?: string; execution_kind?: string } };
}) {
  const route = work?.workflow_v2;
  if (!route?.next_action) return null;
  return (
    <span
      className="rounded border border-subtle px-1.5 py-0.5 text-11 text-secondary"
      title={`Next executor: ${route.next_action}. ${route.state ?? ""}`}
    >
      Next: {route.next_action.replaceAll("_", " ")}
    </span>
  );
}

export function WorkflowWorkPanel({
  workspaceSlug,
  projectId,
  issueId,
}: {
  workspaceSlug: string;
  projectId: string;
  issueId: string;
}) {
  const { data, mutate, error } = useSWR(
    ["workflow-v2", workspaceSlug, projectId],
    () => service.snapshot(workspaceSlug, projectId),
    { refreshInterval: 30_000 }
  );
  const { fetchIssue } = useIssueDetail();
  const [failure, setFailure] = useState("");
  const [busy, setBusy] = useState(false);
  const work = (data?.work as TWorkflowRecord[] | undefined)?.find((item) => item.issue_id === issueId);
  if (!work) return null;
  const version = recordVersion(work);
  const submitQuestion = async (command: Omit<TWorkflowCommand, "command_id">) => {
    if (busy || error) return false;
    setBusy(true);
    setFailure("");
    try {
      await service.command(workspaceSlug, projectId, { ...command, command_id: crypto.randomUUID() });
      await Promise.all([mutate(), fetchIssue(workspaceSlug, projectId, issueId)]);
      return true;
    } catch (reason) {
      setFailure(commandError(reason));
      await mutate();
      return false;
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      className="space-y-3 rounded-lg border border-subtle bg-layer-1 p-3"
      aria-label="Work execution and evidence"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-13 font-medium">
          {valueLabel(work.state)} · Next: {valueLabel(work.next_action)}
        </span>
        <span className="text-11 text-tertiary">
          {valueLabel(work.execution_kind)} · scope revision {valueLabel(work.scope_revision)}
        </span>
      </div>
      <p className="text-11 text-secondary">
        An answer preserves the intended continuation. Only accepted integration or manual verification completes work.
      </p>
      {workQuestions(work)
        .filter(([, question]) => question.status === "open")
        .map(([key, question]) => (
          <WorkQuestionForm
            key={key}
            work={work}
            requestKey={key}
            question={question}
            stale={Boolean(error)}
            busy={busy}
            submit={submitQuestion}
          />
        ))}
      <ManualWorkVerification work={work} stale={Boolean(error)} busy={busy} submit={submitQuestion} />
      <details>
        <summary className="cursor-pointer text-13">Routing, blockers and evidence</summary>
        <Evidence
          value={Object.fromEntries(
            Object.entries(work).filter(([key]) =>
              [
                "repository",
                "dependencies",
                "continuation",
                "evidence",
                "active",
                "guards",
                "run",
                "lease",
                "scope_revision",
                "budget_admission",
              ].includes(key)
            )
          )}
        />
      </details>
      <dl className="grid grid-cols-2 gap-2 text-11">
        <div>
          <dt>Estimated cost</dt>
          <dd>{amountLabel(work.estimated_cost_usd, "USD")}</dd>
        </div>
        <div>
          <dt>Estimated active time</dt>
          <dd>{amountLabel(work.estimated_active_minutes, "min")}</dd>
        </div>
        <div>
          <dt>Cost ceiling</dt>
          <dd>{amountLabel(work.cost_ceiling, "USD")}</dd>
        </div>
        <div>
          <dt>Time ceiling</dt>
          <dd>{amountLabel(work.minute_ceiling, "min")}</dd>
        </div>
        <div>
          <dt>Actual cost</dt>
          <dd>{amountLabel(work.actual_cost_usd, "USD")}</dd>
        </div>
        <div>
          <dt>Active / human wait</dt>
          <dd>
            {amountLabel(work.actual_active_minutes, "min")} / {amountLabel(work.human_wait_minutes, "min")}
          </dd>
        </div>
      </dl>
      {canAct(work, "estimate", Boolean(error)) && version !== undefined && (
        <details>
          <summary className="cursor-pointer text-13">Edit cost and active time estimates</summary>
          <form
            className="mt-3 grid grid-cols-2 gap-3"
            key={`${work.id}:${version}`}
            onSubmit={async (event) => {
              event.preventDefault();
              if (busy) return;
              const values = new FormData(event.currentTarget);
              const payload = Object.fromEntries(
                ["estimated_cost_usd", "estimated_active_minutes", "cost_ceiling", "minute_ceiling"].map((key) => [
                  key,
                  values.get(key) === "" ? null : Number(values.get(key)),
                ])
              );
              setBusy(true);
              setFailure("");
              try {
                await service.command(workspaceSlug, projectId, {
                  command_id: crypto.randomUUID(),
                  action: "estimate",
                  subject_type: "work",
                  subject_id: issueId,
                  expected_version: version,
                  payload,
                });
                await mutate();
              } catch (reason) {
                setFailure(commandError(reason));
                await mutate();
              } finally {
                setBusy(false);
              }
            }}
          >
            {[
              ["estimated_cost_usd", "Estimated USD"],
              ["estimated_active_minutes", "Estimated active minutes"],
              ["cost_ceiling", "Cost ceiling USD"],
              ["minute_ceiling", "Time ceiling minutes"],
            ].map(([key, label]) => (
              <label className="text-11" key={key}>
                {label}
                <input
                  name={key}
                  type="number"
                  min="0"
                  step={key === "minute_ceiling" ? "1" : "0.01"}
                  defaultValue={work[key] == null ? "" : String(work[key])}
                  className="mt-1 w-full rounded border border-subtle bg-surface-1 p-2"
                />
              </label>
            ))}
            <button disabled={busy} className="rounded border border-subtle p-2 text-13">
              {busy ? "Saving…" : "Save estimates"}
            </button>
          </form>
        </details>
      )}
      {failure && (
        <p role="alert" className="text-13">
          {failure}
        </p>
      )}
      <Link className="text-11 text-accent-primary underline" to={`/${workspaceSlug}/projects/${projectId}/progress`}>
        View release and delivery evidence
      </Link>
    </section>
  );
}
