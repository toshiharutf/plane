import type { TWorkflowRecord } from "@/services/workflow-v2.service";
import { amountLabel, valueLabel } from "./utils";
import { Evidence } from "./evidence";

export function ScopeBudgets({ scopes }: { scopes: TWorkflowRecord[] }) {
  return (
    <div className="space-y-3">
      <p className="text-13 text-secondary">
        Budgets and reserves belong to approved scope revisions. Shared work is counted once in work totals; scope
        envelopes are not added together.
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-13">
          <thead>
            <tr>
              {[
                "Scope / revision",
                "Control",
                "Approved USD",
                "Active minutes",
                "Reserve USD",
                "Reserve minutes",
                "Forecast",
              ].map((label) => (
                <th className="p-2 whitespace-nowrap" key={label}>
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {scopes.map((scope) => {
              const definition = scope.definition as
                | {
                    budget?: Record<string, unknown>;
                    cycle_budget?: Record<string, unknown>;
                    budget_mode?: string;
                    forecast?: unknown;
                  }
                | undefined;
              const budget = definition?.budget ?? definition?.cycle_budget;
              return (
                <tr key={scope.id} className="border-t border-subtle">
                  <th className="font-normal p-2">
                    {scope.name ?? scope.id} / {valueLabel(scope.revision)}
                  </th>
                  <td className="p-2">{valueLabel(definition?.budget_mode ?? budget?.policy ?? budget?.mode)}</td>
                  <td className="p-2">{amountLabel(budget?.cost_ceiling_usd ?? budget?.usd, "USD")}</td>
                  <td className="p-2">{amountLabel(budget?.active_minute_ceiling ?? budget?.minutes, "min")}</td>
                  <td className="p-2">{amountLabel(budget?.contingency_usd ?? budget?.reserve_usd, "USD")}</td>
                  <td className="p-2">{amountLabel(budget?.contingency_minutes ?? budget?.reserve_minutes, "min")}</td>
                  <td className="p-2">
                    {Boolean(scope.budget_admission) && (
                      <details>
                        <summary className="cursor-pointer">Admission, commitments and cumulative usage</summary>
                        <Evidence value={scope.budget_admission} />
                      </details>
                    )}
                    {definition?.forecast ? (
                      <details>
                        <summary className="cursor-pointer">Recorded forecast and source</summary>
                        <Evidence value={definition.forecast} />
                      </details>
                    ) : (
                      "Unknown — no forecast recorded"
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
