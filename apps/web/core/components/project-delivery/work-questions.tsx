import { useState } from "react";
import { Link } from "react-router";
import type { TWorkflowCommand, TWorkflowRecord } from "@/services/workflow-v2.service";
import { canAct, recordVersion, valueLabel } from "./utils";

export type TWorkQuestion = {
  question: string;
  status: string;
  answer?: string;
  requester_id?: string;
  clarification_required?: boolean;
  allowed_actions?: string[];
};
export function workQuestions(work: TWorkflowRecord): [string, TWorkQuestion][] {
  const continuation = work.continuation as { requests?: Record<string, TWorkQuestion> } | undefined;
  return Object.entries(continuation?.requests ?? {});
}

type Props = {
  work: TWorkflowRecord;
  requestKey: string;
  question: TWorkQuestion;
  stale: boolean;
  busy: boolean;
  submit: (command: Omit<TWorkflowCommand, "command_id">) => Promise<boolean>;
};
export function WorkQuestionForm({ work, requestKey, question, stale, busy, submit }: Props) {
  const [answer, setAnswer] = useState(question.answer ?? "");
  const [resolution, setResolution] = useState("clarify");
  const [alternative, setAlternative] = useState("");
  const [receipt, setReceipt] = useState("");
  const [obsoleteReason, setObsoleteReason] = useState("");
  const version = recordVersion(work);
  const open = question.status === "open" && work.state === "Awaiting Human";
  const canResolve = open && version !== undefined && canAct(work, "resolve_human", stale);
  const canWithdraw =
    open &&
    version !== undefined &&
    canAct(work, "withdraw_human", stale) &&
    question.allowed_actions?.includes("withdraw_human");
  return (
    <div className="space-y-3 rounded border border-subtle p-3" aria-label={`Work question ${requestKey}`}>
      <p className="text-13 whitespace-pre-wrap">{question.question}</p>
      <p className="text-11 text-tertiary">
        {requestKey} · {question.status} · Intended continuation: {valueLabel(work.next_action)}
      </p>
      {question.clarification_required && (
        <p className="text-13">
          An actionable answer is still required. Work remains waiting until every open question is resolved.
        </p>
      )}
      {question.answer && <p className="text-13 whitespace-pre-wrap">Recorded answer: {question.answer}</p>}
      {canResolve && (
        <form
          className="space-y-3"
          onSubmit={async (event) => {
            event.preventDefault();
            if (!answer.trim() || busy) return;
            await submit({
              action: "resolve_human",
              subject_type: "work",
              subject_id: String(work.issue_id ?? work.id),
              expected_version: version,
              payload: { request_key: requestKey, answer, resolution, ...(alternative.trim() ? { alternative } : {}) },
            });
          }}
        >
          <label className="block text-13">
            Answer
            <textarea
              className="mt-1 min-h-20 w-full rounded border border-subtle bg-surface-1 p-2"
              value={answer}
              onChange={(event) => setAnswer(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") event.stopPropagation();
              }}
              required
            />
          </label>
          <label className="block text-13">
            Continuation
            <select
              className="mt-1 w-full rounded border border-subtle bg-surface-1 p-2"
              value={resolution}
              onChange={(event) => setResolution(event.target.value)}
            >
              <option value="clarify">Keep waiting / request clarification</option>
              <option value="actionable">Answer allows the intended executor to continue</option>
            </select>
          </label>
          {resolution === "actionable" && (
            <label className="block text-13">
              Alternative if the requested path is denied (optional)
              <textarea
                value={alternative}
                onChange={(event) => setAlternative(event.target.value)}
                className="mt-1 min-h-16 w-full rounded border border-subtle bg-surface-1 p-2"
              />
            </label>
          )}
          <button
            type="submit"
            disabled={busy || !answer.trim()}
            className="rounded border border-subtle px-3 py-2 text-13 disabled:opacity-40"
          >
            {busy ? "Saving…" : "Send answer"}
          </button>
        </form>
      )}
      {canWithdraw && (
        <details>
          <summary className="cursor-pointer text-13">Withdraw my obsolete question</summary>
          <form
            className="mt-3 space-y-2"
            onSubmit={async (event) => {
              event.preventDefault();
              if (busy || !receipt.trim() || !obsoleteReason.trim()) return;
              await submit({
                action: "withdraw_human",
                subject_type: "work",
                subject_id: String(work.issue_id ?? work.id),
                expected_version: version,
                payload: {
                  request_key: requestKey,
                  evidence: { obsolete_reason: obsoleteReason, receipt_digest: receipt },
                },
              });
            }}
          >
            <label className="block text-13">
              Why this question is obsolete
              <textarea
                value={obsoleteReason}
                required
                onChange={(event) => setObsoleteReason(event.target.value)}
                className="mt-1 w-full rounded border border-subtle bg-surface-1 p-2"
              />
            </label>
            <label className="block text-13">
              Evidence receipt digest
              <input
                value={receipt}
                required
                onChange={(event) => setReceipt(event.target.value)}
                className="mt-1 w-full rounded border border-subtle bg-surface-1 p-2"
              />
            </label>
            <button
              type="submit"
              disabled={busy || !receipt.trim() || !obsoleteReason.trim()}
              className="rounded border border-subtle px-3 py-2 text-13 disabled:opacity-40"
            >
              Withdraw question with evidence
            </button>
          </form>
        </details>
      )}
    </div>
  );
}

export function WorkQuestionInbox({
  work,
  workspaceSlug,
  projectId,
  stale,
  busy,
  submit,
}: {
  work: TWorkflowRecord[];
  workspaceSlug: string;
  projectId: string;
  stale: boolean;
  busy: boolean;
  submit: Props["submit"];
}) {
  const waiting = work.filter((item) => workQuestions(item).some(([, question]) => question.status === "open"));
  return (
    <div className="space-y-4">
      {waiting.map((item) => (
        <section key={item.id} className="space-y-2">
          <Link
            className="text-13 text-accent-primary underline"
            to={`/${workspaceSlug}/projects/${projectId}/issues/${String(item.issue_id ?? item.id)}`}
          >
            {item.name ?? String(item.issue_id ?? item.id)}
          </Link>
          {workQuestions(item)
            .filter(([, question]) => question.status === "open")
            .map(([key, question]) => (
              <WorkQuestionForm
                key={key}
                work={item}
                requestKey={key}
                question={question}
                stale={stale}
                busy={busy}
                submit={submit}
              />
            ))}
        </section>
      ))}
    </div>
  );
}

export function ManualWorkVerification({
  work,
  stale,
  busy,
  submit,
}: Pick<Props, "work" | "stale" | "busy" | "submit">) {
  const [acceptance, setAcceptance] = useState("");
  const [receipt, setReceipt] = useState("");
  const [verified, setVerified] = useState(false);
  const version = recordVersion(work);
  if (
    work.execution_kind !== "human" ||
    !work.active ||
    work.next_action !== "verify_manual_completion" ||
    !["Todo", "In Progress"].includes(String(work.state)) ||
    version === undefined ||
    !canAct(work, "manual_verify", stale)
  )
    return null;
  return (
    <details>
      <summary className="cursor-pointer text-13">Verify manual completion</summary>
      <form
        className="mt-3 space-y-3"
        onSubmit={async (event) => {
          event.preventDefault();
          if (busy || !verified || !acceptance.trim() || !receipt.trim()) return;
          await submit({
            action: "manual_verify",
            subject_type: "work",
            subject_id: String(work.issue_id ?? work.id),
            expected_version: version,
            payload: { evidence: { acceptance, receipt_digest: receipt } },
          });
        }}
      >
        <p className="text-13 text-secondary">
          Record evidence that this human-only prerequisite satisfies its acceptance criteria.
        </p>
        <label className="block text-13">
          Acceptance evidence
          <textarea
            value={acceptance}
            required
            onChange={(event) => setAcceptance(event.target.value)}
            className="mt-1 min-h-20 w-full rounded border border-subtle bg-surface-1 p-2"
          />
        </label>
        <label className="block text-13">
          Evidence receipt digest
          <input
            value={receipt}
            required
            onChange={(event) => setReceipt(event.target.value)}
            className="mt-1 w-full rounded border border-subtle bg-surface-1 p-2"
          />
        </label>
        <label className="flex gap-2 text-13">
          <input type="checkbox" checked={verified} onChange={(event) => setVerified(event.target.checked)} />I verified
          this prerequisite against its acceptance criteria.
        </label>
        <button
          type="submit"
          disabled={busy || !verified || !acceptance.trim() || !receipt.trim()}
          className="rounded border border-subtle px-3 py-2 text-13 disabled:opacity-40"
        >
          Record manual verification
        </button>
      </form>
    </details>
  );
}
