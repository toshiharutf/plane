/** Copyright (c) 2026 Plane contributors. SPDX-License-Identifier: AGPL-3.0-only */
import { API_BASE_URL } from "@plane/constants";
import { APIService } from "@/services/api.service";

export type TWorkflowRecord = {
  id: string;
  name?: string;
  phase?: string;
  state_version?: number;
  version?: number;
  allowed_actions?: string[];
  [key: string]: unknown;
};
export type TProgressMeasure = {
  numerator: number | null;
  denominator: number;
  numerator_ids: string[];
  denominator_ids: string[];
  status?: string;
  reasons?: string[];
  last_verified?: number;
  last_verified_ids?: string[];
};
export type TProgressMetrics = Record<"completed_work" | "qualified_code" | "verified_delivery", TProgressMeasure>;
export type TProgressSnapshot = {
  scope: {
    id?: string;
    revision?: number;
    name?: string;
    items: TWorkflowRecord[];
    excluded_ids: string[];
    [key: string]: unknown;
  };
  metrics: TProgressMetrics;
  scopes: TWorkflowRecord[];
  selected_scopes?: TWorkflowRecord[];
  environments: TWorkflowRecord[];
  environment: {
    name?: string;
    status: string;
    current_identity?: unknown;
    last_verified_identity?: unknown;
    [key: string]: unknown;
  };
  candidates: TWorkflowRecord[];
  deployments: TWorkflowRecord[];
  attention: TWorkflowRecord[];
  history: TWorkflowRecord[];
  modules?: TWorkflowRecord[];
  activity?: TWorkflowRecord[];
  incomplete_evidence?: TWorkflowRecord[];
  unplanned_work_ids?: string[];
  cycles?: TWorkflowRecord[];
  costs?: TWorkflowRecord;
  provenance: { as_of: string; source_sequence: number; stale: boolean };
};
export type TWorkflowSnapshot = {
  configuration: Record<string, unknown>;
  settings?: TWorkflowRecord;
  candidates: TWorkflowRecord[];
  deployments: TWorkflowRecord[];
  decisions: TWorkflowRecord[];
  work_items?: TWorkflowRecord[];
  scopes?: TWorkflowRecord[];
  events?: TWorkflowRecord[];
  usage?: TWorkflowRecord[];
  [key: string]: unknown;
};
export type TWorkflowCommand = {
  command_id: string;
  action: string;
  subject_type: string;
  subject_id: string;
  expected_version: number;
  payload: Record<string, unknown>;
};
export type TWorkflowCommandResult = {
  command_id: string;
  subject_type: string;
  subject_id: string;
  version: number;
  state: string | null;
  event_sequence: number;
  result: Record<string, unknown>;
};
export type TProgressFilters = { scope_id?: string; scope_revision?: string; cycle_id?: string; target?: string };

export class WorkflowV2Service extends APIService {
  constructor() {
    super(API_BASE_URL);
  }
  private url(workspace: string, project: string, path: string) {
    return `/api/workspaces/${encodeURIComponent(workspace)}/projects/${encodeURIComponent(project)}/${path}/`;
  }
  async progress(workspace: string, project: string, filters: TProgressFilters): Promise<TProgressSnapshot> {
    try {
      const response = (await this.get(this.url(workspace, project, "progress"), { params: filters })).data;
      return normalizeProgress(response);
    } catch (error) {
      throw this.failure(error);
    }
  }
  async snapshot(workspace: string, project: string): Promise<TWorkflowSnapshot> {
    try {
      return (await this.get(this.url(workspace, project, "workflow-v2"))).data;
    } catch (error) {
      throw this.failure(error);
    }
  }
  async command(workspace: string, project: string, command: TWorkflowCommand): Promise<TWorkflowCommandResult> {
    try {
      return (
        await this.post(this.url(workspace, project, "workflow-v2/commands"), { ...command, workflow_version: 2 })
      ).data;
    } catch (error) {
      throw this.failure(error);
    }
  }
  private failure(error: unknown): unknown {
    return (error as { response?: { data?: unknown } })?.response?.data ?? error;
  }
}

/** Adapt the explicit wire projection without inventing evidence for missing records. */
export function normalizeProgress(raw: Record<string, unknown>): TProgressSnapshot {
  const scope = raw.scope as Record<string, unknown> | null;
  const members = (scope?.items ?? []) as (string | TWorkflowRecord)[];
  const work = (raw.work ?? []) as TWorkflowRecord[];
  const items = members.map((member) =>
    typeof member === "string"
      ? Object.assign(
          {},
          work.find((item) => item.issue_id === member || item.id === member),
          { id: member }
        )
      : member
  );
  const environment = raw.environment as TProgressSnapshot["environment"] | null;
  return {
    ...raw,
    scope: { ...scope, items, excluded_ids: (scope?.excluded_ids ?? []) as string[] },
    environment: environment ?? {
      name: "Publication only / no target configured",
      status: "No runtime observation",
      current_identity: null,
    },
    scopes: (raw.scopes ?? []) as TWorkflowRecord[],
    environments: (raw.environments ?? []) as TWorkflowRecord[],
    candidates: (raw.candidates ?? []) as TWorkflowRecord[],
    deployments: (raw.deployments ?? []) as TWorkflowRecord[],
    attention: (raw.attention ?? []) as TWorkflowRecord[],
    history: (raw.history ?? []) as TWorkflowRecord[],
  } as TProgressSnapshot;
}
