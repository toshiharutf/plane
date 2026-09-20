import { useState } from "react";
import type { TWorkflowCommand, TWorkflowRecord } from "@/services/workflow-v2.service";
import { Evidence } from "./evidence";
import { canAct, localDateTime, recordVersion, valueLabel } from "./utils";

type Props = {
  record: TWorkflowRecord;
  stale: boolean;
  busy: boolean;
  submit: (command: Omit<TWorkflowCommand, "command_id">) => Promise<boolean>;
};
export function DecisionReview({ record, stale, busy, submit }: Props) {
  const [decision, setDecision] = useState("clarify");
  const [answer, setAnswer] = useState("");
  const [reviewed, setReviewed] = useState(false);
  const [expires, setExpires] = useState(localDateTime(record.expires_at));
  const [notBefore, setNotBefore] = useState(localDateTime(record.not_before));
  const version = recordVersion(record);
  const actionable = canAct(record, "resolve", stale) && version !== undefined && Boolean(record.payload_digest);
  const expiry = expires ? new Date(expires) : null;
  const validExpiry =
    expiry &&
    Number.isFinite(expiry.getTime()) &&
    expiry.getTime() > Date.now() &&
    (!notBefore || (Number.isFinite(Date.parse(notBefore)) && Date.parse(notBefore) < expiry.getTime()));
  return (
    <div className="space-y-4">
      <p className="font-medium">
        {valueLabel(record.action)} · {valueLabel(record.subject_type)}
      </p>
      <p className="text-13 text-secondary">
        This decision applies only to the exact operation below. Publication and deployment require separate decisions.
      </p>
      <Evidence
        value={Object.fromEntries(
          Object.entries(record).filter(([key]) =>
            [
              "subject_id",
              "action",
              "payload",
              "payload_digest",
              "constraints",
              "not_before",
              "expires_at",
              "decision",
              "answer",
              "revoked",
              "continuation",
            ].includes(key)
          )
        )}
      />
      {actionable ? (
        <form
          className="space-y-3 border-t border-subtle pt-4"
          onSubmit={async (event) => {
            event.preventDefault();
            if (version === undefined || busy || !reviewed || !answer.trim() || (decision === "allow" && !validExpiry))
              return;
            await submit({
              action: "resolve",
              subject_type: "decision",
              subject_id: record.id,
              expected_version: version,
              payload: {
                decision,
                answer,
                answer_kind: "reason",
                payload_digest: record.payload_digest,
                expires_at: validExpiry ? expiry.toISOString() : null,
                not_before:
                  notBefore && Number.isFinite(Date.parse(notBefore)) ? new Date(notBefore).toISOString() : null,
              },
            });
          }}
        >
          <label className="block text-13">
            Decision
            <select
              className="mt-1 w-full rounded border border-subtle bg-surface-1 p-2"
              value={decision}
              onChange={(event) => setDecision(event.target.value)}
            >
              <option value="clarify">Request clarification</option>
              <option value="deny">Deny</option>
              <option value="allow">Authorize this exact operation</option>
            </select>
          </label>
          <label className="block text-13">
            Reason or question (execution conditions must use the time fields)
            <textarea
              className="mt-1 min-h-24 w-full rounded border border-subtle bg-surface-1 p-2"
              value={answer}
              onChange={(event) => setAnswer(event.target.value)}
              required
            />
          </label>
          {decision === "allow" && (
            <label className="block text-13">
              Not before (local time, optional)
              <input
                type="datetime-local"
                value={notBefore}
                onChange={(event) => setNotBefore(event.target.value)}
                className="mt-1 w-full rounded border border-subtle bg-surface-1 p-2"
              />
            </label>
          )}
          {decision === "allow" && (
            <label className="block text-13">
              Authorization expires (local time)
              <input
                className="mt-1 w-full rounded border border-subtle bg-surface-1 p-2"
                type="datetime-local"
                value={expires}
                onChange={(event) => setExpires(event.target.value)}
                required
              />
              <span className="text-11 text-tertiary">
                Choose an explicit expiry. Server guards recheck identity, conditions and freshness.
              </span>
            </label>
          )}
          <label className="flex items-start gap-2 text-13">
            <input type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} />I
            reviewed the subject, exact payload and constraints.
          </label>
          <button
            type="submit"
            className="rounded bg-accent-primary px-4 py-2 text-white disabled:opacity-40"
            disabled={busy || !reviewed || !answer.trim() || (decision === "allow" && !validExpiry)}
          >
            {busy ? "Saving…" : "Record decision"}
          </button>
        </form>
      ) : (
        <p className="rounded border border-subtle p-3 text-13">
          {stale
            ? "This snapshot is stale. Refresh before deciding."
            : "This decision is closed, expired, or your account cannot resolve it."}
        </p>
      )}
    </div>
  );
}

export function DeploymentRecovery({
  record,
  decisions,
  stale,
  busy,
  submit,
}: Props & { decisions: TWorkflowRecord[] }) {
  const [evidence, setEvidence] = useState("");
  const [reconciled, setReconciled] = useState(false);
  const [decisionId, setDecisionId] = useState("");
  const version = recordVersion(record);
  if (!canAct(record, "retry", stale) || version === undefined || record.discarded) return null;
  const choices = decisions.filter(
    (decision) =>
      decision.subject_id === record.id &&
      decision.subject_type === "deployment" &&
      decision.action === "start" &&
      decision.decision === "allow" &&
      !decision.revoked &&
      (!decision.expires_at || new Date(String(decision.expires_at)).getTime() > Date.now())
  );
  return (
    <form
      className="mt-4 space-y-3 border-t border-subtle pt-4"
      onSubmit={async (event) => {
        event.preventDefault();
        if (!reconciled || !evidence.trim() || !decisionId) return;
        await submit({
          action: "retry",
          subject_type: "deployment",
          subject_id: record.id,
          expected_version: version,
          payload: {
            repair_evidence: { receipt_digest: evidence.trim(), verified: reconciled },
            reconciled,
            decision_id: decisionId,
          },
        });
      }}
    >
      <h3 className="font-medium">Retry repaired deployment</h3>
      <p className="text-13 text-secondary">
        Returns to Pending for a new attempt after the server verifies repair and authority.
      </p>
      <label className="block text-13">
        Verified repair receipt digest
        <textarea
          required
          value={evidence}
          onChange={(event) => setEvidence(event.target.value)}
          className="mt-1 min-h-20 w-full rounded border border-subtle bg-surface-1 p-2"
        />
      </label>
      <label className="block text-13">
        Deployment authorization
        <select
          required
          value={decisionId}
          onChange={(event) => setDecisionId(event.target.value)}
          className="mt-1 w-full rounded border border-subtle bg-surface-1 p-2"
        >
          <option value="">Select valid decision</option>
          {choices.map((decision) => (
            <option key={decision.id} value={decision.id}>
              {decision.id} · {valueLabel(decision.action)}
            </option>
          ))}
        </select>
      </label>
      <label className="flex gap-2 text-13">
        <input type="checkbox" checked={reconciled} onChange={(event) => setReconciled(event.target.checked)} />
        The prior healthy identity was verified, and previous effects and environment ownership were reconciled.
      </label>
      <button
        className="rounded border border-subtle px-3 py-2 disabled:opacity-40"
        disabled={busy || !reconciled || !evidence.trim() || !decisionId}
      >
        Request guarded retry
      </button>
    </form>
  );
}
