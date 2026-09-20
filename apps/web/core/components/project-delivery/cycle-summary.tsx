import useSWR from "swr";
import { Link } from "react-router";
import { WorkflowV2Service } from "@/services/workflow-v2.service";
import { CostSummary } from "./progress";
import { ScopeBudgets } from "./scope-budget";
const service = new WorkflowV2Service();
export function CycleDeliverySummary({
  workspaceSlug,
  projectId,
  cycleId,
}: {
  workspaceSlug: string;
  projectId: string;
  cycleId: string;
}) {
  const { data, error } = useSWR(
    ["cycle-delivery", workspaceSlug, projectId, cycleId],
    () => service.progress(workspaceSlug, projectId, { cycle_id: cycleId }),
    { refreshInterval: 60_000 }
  );
  if (error || !data || (!data.scope.id && !data.scope.items.length && !data.scope.name)) return null;
  return (
    <details className="shrink-0 border-b border-subtle bg-surface-1 px-4 py-2">
      <summary className="cursor-pointer text-13">
        Cycle scope cost and active time · {data.scope.items.length} work items
      </summary>
      <div className="max-h-80 space-y-3 overflow-y-auto py-3">
        <CostSummary work={data.scope.items} usage={[]} />
        <h3 className="text-13 font-medium">Scope budget, forecast and reserve</h3>
        <ScopeBudgets scopes={data.selected_scopes ?? data.scopes.filter((scope) => scope.cycle_id === cycleId)} />
        <Link
          className="text-13 text-accent-primary underline"
          to={`/${workspaceSlug}/projects/${projectId}/progress?cycle_id=${cycleId}`}
        >
          Inspect implementation and delivery progress
        </Link>
      </div>
    </details>
  );
}
