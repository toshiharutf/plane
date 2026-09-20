import { Line, LineChart, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from "recharts";
import type { TProgressMeasure, TProgressSnapshot, TWorkflowRecord } from "@/services/workflow-v2.service";
import { Evidence } from "./evidence";
import { amountLabel, measureLabel, valueLabel } from "./utils";

export const MEASURES = [
  { key: "completed_work", label: "Completed work", color: "#6366f1" },
  { key: "qualified_code", label: "Qualified code", color: "#ca8a04" },
  { key: "verified_delivery", label: "Verified delivery", color: "#059669" },
] as const;
export function ProgressCards({
  snapshot,
  inspect,
  attention,
}: {
  snapshot: TProgressSnapshot;
  inspect: (title: string, metric: TProgressMeasure) => void;
  attention: () => void;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {MEASURES.map(({ key, label }) => {
        const measure = snapshot.metrics[key];
        return (
          <button
            type="button"
            key={key}
            onClick={() => inspect(label, measure)}
            className="rounded-lg border border-subtle bg-surface-1 p-5 text-left hover:bg-layer-1"
          >
            <h2 className="text-13 text-secondary">{label}</h2>
            <p className="text-22 mt-2 font-semibold">{measureLabel(measure)}</p>
            <p className="mt-2 text-11 text-tertiary">
              {key === "completed_work" ? "Verified code and manual leaf work" : "Distinct applicable code work"}
            </p>
            {measure.reasons?.map((reason) => (
              <p key={reason} className="mt-1 text-11">
                {reason}
              </p>
            ))}
          </button>
        );
      })}
      <button
        type="button"
        onClick={attention}
        className="rounded-lg border border-subtle bg-surface-1 p-5 text-left hover:bg-layer-1"
      >
        <h2 className="text-13 text-secondary">Needs attention</h2>
        <p className="text-22 mt-2 font-semibold">
          {new Set(snapshot.attention.map((gate) => gate.id)).size} open gates
        </p>
        <p className="mt-2 text-11 text-tertiary">Human decisions, holds and operational blockers</p>
      </button>
    </div>
  );
}

export function ProgressHistory({ history }: { history: TWorkflowRecord[] }) {
  if (!history.length)
    return (
      <p className="text-13 text-tertiary">No historical snapshots recorded yet. Missing observations are unknown.</p>
    );
  const data: (TWorkflowRecord & { date: string })[] = history.map((entry) => ({
    ...entry,
    date: String(entry.as_of ?? entry.created_at ?? entry.id),
    ...Object.fromEntries(
      MEASURES.map(({ key }) => {
        const value = entry[key] ?? (entry.metrics as Record<string, unknown> | undefined)?.[key];
        return [key, typeof value === "number" ? value : ((value as TProgressMeasure | undefined)?.numerator ?? null)];
      })
    ),
  }));
  return (
    <div
      role="figure"
      aria-label="Completed, qualified and verified delivered item counts over time. Gaps mean unknown observations; scope changes mark new baselines."
    >
      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="date" tickFormatter={(date: string) => new Date(date).toLocaleDateString()} />
            <YAxis allowDecimals={false} />
            <Tooltip />
            <Legend />
            {MEASURES.map(({ key, label, color }) => (
              <Line key={key} name={label} type="stepAfter" dataKey={key} stroke={color} connectNulls={false} dot />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      <details className="mt-3 text-13">
        <summary className="cursor-pointer">Snapshot values and scope baselines</summary>
        <div className="overflow-x-auto">
          <table className="mt-2 w-full text-left">
            <thead>
              <tr>
                <th>Date</th>
                <th>Scope revision</th>
                {MEASURES.map(({ label }) => (
                  <th key={label}>{label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.map((row) => (
                <tr key={row.id ?? row.date}>
                  <td className="py-2">{row.date}</td>
                  <td>
                    {valueLabel(row.scope_revision ?? row.scope_revisions)}
                    {row.baseline_changed ? " · Baseline changed" : ""}
                  </td>
                  {MEASURES.map(({ key }) => (
                    <td key={key}>{valueLabel(row[key])}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

export function CostSummary({ work, usage }: { work: TWorkflowRecord[]; usage: TWorkflowRecord[] }) {
  const leaves = [
    ...new Map(
      work
        .filter((item) => !item.is_parent && !item.is_summary && item.state !== "cancelled")
        .map((item) => [item.id, item])
    ).values(),
  ];
  const sum = (key: string) =>
    leaves.length && leaves.every((item) => item[key] != null && Number.isFinite(Number(item[key])))
      ? leaves.reduce((total, item) => total + Number(item[key]), 0)
      : null;
  const totals = [
    ["Estimated cost", amountLabel(sum("estimated_cost_usd"), "USD")],
    ["Estimated active time", amountLabel(sum("estimated_active_minutes"), "min")],
    ["Cost ceiling", amountLabel(sum("cost_ceiling"), "USD")],
    ["Time ceiling", amountLabel(sum("minute_ceiling"), "min")],
    ["Actual cost", amountLabel(sum("actual_cost_usd"), "USD")],
    ["Actual active time", amountLabel(sum("actual_active_minutes"), "min")],
  ];
  return (
    <div className="space-y-4">
      <p className="text-13 text-secondary">
        {leaves.length} distinct executable work items. Forecasts, spending ceilings and actuals are separate. Missing
        values stay unknown; legacy points are unchanged.
      </p>
      <dl className="grid grid-cols-2 gap-3 md:grid-cols-3">
        {totals.map(([label, value]) => (
          <div key={label} className="rounded border border-subtle p-3">
            <dt className="text-11 text-tertiary">{label}</dt>
            <dd className="text-16 font-medium">{value}</dd>
          </div>
        ))}
      </dl>
      <details>
        <summary className="cursor-pointer text-13">Work estimates, reserves and actual usage</summary>
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-left text-13">
            <thead>
              <tr>
                {[
                  "Work",
                  "Estimated USD",
                  "Estimated minutes",
                  "Ceiling USD",
                  "Ceiling minutes",
                  "Actual USD",
                  "Active minutes",
                  "Human wait minutes",
                ].map((title) => (
                  <th className="p-2 whitespace-nowrap" key={title}>
                    {title}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {leaves.map((item) => (
                <tr key={item.id} className="border-t border-subtle">
                  <th className="font-normal p-2">{item.name ?? item.id}</th>
                  {[
                    "estimated_cost_usd",
                    "estimated_active_minutes",
                    "cost_ceiling",
                    "minute_ceiling",
                    "actual_cost_usd",
                    "actual_active_minutes",
                    "human_wait_minutes",
                  ].map((key) => (
                    <td key={key} className="p-2 whitespace-nowrap">
                      {amountLabel(item[key], key.includes("usd") || key === "cost_ceiling" ? "USD" : "min")}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
      {usage.length > 0 && (
        <details>
          <summary className="cursor-pointer text-13">Model usage, price identity and cost categories</summary>
          <Evidence value={usage} />
        </details>
      )}
    </div>
  );
}
