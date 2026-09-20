// oxlint-disable-next-line import/no-unassigned-import -- browser fixture uses the production stylesheet.
import "../../styles/globals.css";
import React from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { SWRConfig } from "swr";
import { ProjectDeliveryPage } from "../../core/components/project-delivery/root";
import { WorkflowV2Service } from "../../core/services/workflow-v2.service";
import { HumanRequestService } from "../../core/services/human-request.service";
import type {
  TProgressSnapshot,
  TWorkflowCommand,
  TWorkflowSnapshot,
  TWorkflowRecord,
} from "../../core/services/workflow-v2.service";

const work: TWorkflowRecord[] = [
  {
    id: "work-1",
    issue_id: "work-1",
    name: "Code change",
    state: "Done",
    next_action: "none",
    execution_kind: "code",
    estimated_cost_usd: "4",
    estimated_active_minutes: "20",
  },
  { id: "work-2", issue_id: "work-2", name: "Human prerequisite", state: "Done", execution_kind: "human" },
];
const candidate = {
  id: "candidate-1",
  name: "Exact release",
  version: 2,
  state: "Qualified",
  eligible: true,
  manifest: { repositories: { local: "a".repeat(40) } },
  manifest_digest: "manifest-abc",
  qualification_digest: "qualified-abc",
  guards: ["Publication requires a human decision"],
};
const decision = {
  id: "decision-1",
  version: 1,
  decision: "pending",
  subject_type: "candidate",
  subject_id: candidate.id,
  action: "publish",
  payload: { manifest_digest: candidate.manifest_digest },
  payload_digest: "payload-123",
  allowed_actions: ["resolve"],
};
const progress: TProgressSnapshot = {
  scope: { id: "scope-1", revision: 1, name: "Local scope", items: work, excluded_ids: ["parent-1"] },
  scopes: [
    { id: "scope-1", name: "Local scope", revision: 1 },
    { id: "scope-2", name: "Empty scope", revision: 2 },
  ],
  metrics: {
    completed_work: {
      numerator: 2,
      denominator: 2,
      numerator_ids: ["work-1", "work-2"],
      denominator_ids: ["work-1", "work-2"],
    },
    qualified_code: { numerator: 1, denominator: 1, numerator_ids: ["work-1"], denominator_ids: ["work-1"] },
    verified_delivery: {
      numerator: null,
      denominator: 1,
      numerator_ids: [],
      denominator_ids: ["work-1"],
      status: "unverified",
    },
  },
  attention: [decision],
  candidates: [candidate],
  deployments: [],
  environments: [{ id: "env-1", name: "local" }],
  environment: { name: "local", status: "Unverified", last_verified_identity: "old-artifact" },
  history: [],
  provenance: { as_of: new Date().toISOString(), source_sequence: 10, stale: false },
};
const workflow: TWorkflowSnapshot = {
  configuration: { workflow_version: 2 },
  settings: { id: "config" },
  candidates: [candidate],
  deployments: [],
  decisions: [decision],
  work,
};
const requests = {
  first: {
    question: "Which reviewed approach can proceed?",
    status: "open",
    answer: "",
    allowed_actions: ["resolve_human"],
  },
  second: {
    question: "Is the external prerequisite ready?",
    status: "open",
    answer: "",
    allowed_actions: ["resolve_human", "withdraw_human"],
  },
};
const waitingWork: TWorkflowRecord = {
  id: "waiting-work",
  issue_id: "waiting-work",
  name: "Review continuation",
  version: 1,
  state: "Awaiting Human",
  next_action: "review",
  execution_kind: "code",
  allowed_actions: ["resolve_human", "withdraw_human"],
  continuation: { requests },
};
const commands: TWorkflowCommand[] = [];
WorkflowV2Service.prototype.progress = async (_workspace, _project, filters) => {
  if (filters.scope_id === "scope-2")
    return {
      ...progress,
      scope: { ...progress.scope, id: "scope-2", revision: 2, items: [] },
      metrics: Object.fromEntries(
        Object.keys(progress.metrics).map((key) => [
          key,
          { numerator: 0, denominator: 0, numerator_ids: [], denominator_ids: [] },
        ])
      ) as unknown as TProgressSnapshot["metrics"],
    };
  return structuredClone(progress);
};
WorkflowV2Service.prototype.snapshot = async () => structuredClone(workflow);
WorkflowV2Service.prototype.command = async (_workspace, _project, command) => {
  commands.push(command);
  if (command.subject_type === "work") {
    const request = requests[String(command.payload.request_key) as keyof typeof requests];
    if (command.expected_version !== waitingWork.version) throw { error: "Work changed", code: "stale_version" };
    request.answer = String(command.payload.answer ?? "");
    request.status =
      command.action === "withdraw_human"
        ? "withdrawn"
        : command.payload.resolution === "actionable"
          ? "answered"
          : "open";
    waitingWork.version = Number(waitingWork.version) + 1;
    waitingWork.state = Object.values(requests).some((item) => item.status === "open") ? "Awaiting Human" : "Todo";
    return {
      command_id: command.command_id,
      subject_type: "work",
      subject_id: waitingWork.id,
      version: Number(waitingWork.version),
      state: String(waitingWork.state),
      event_sequence: 12,
      result: structuredClone(waitingWork),
    };
  }
  if (command.expected_version !== decision.version) throw { error: "Decision changed", code: "stale_version" };
  decision.decision = String(command.payload.decision);
  decision.version += 1;
  decision.allowed_actions = [];
  return {
    command_id: command.command_id,
    subject_type: "decision",
    subject_id: decision.id,
    version: decision.version,
    state: decision.decision,
    event_sequence: 11,
    result: structuredClone(decision),
  };
};
HumanRequestService.prototype.listOpen = async () => [];
Object.assign(window, {
  deliveryFixture: {
    commands,
    showWorkQuestions: () => {
      progress.provenance.stale = false;
      work.push(waitingWork);
      progress.scope.revision = 2;
      progress.metrics.completed_work.denominator = 3;
      progress.metrics.completed_work.denominator_ids.push(waitingWork.id);
      for (const key of ["qualified_code", "verified_delivery"] as const) {
        progress.metrics[key].denominator = 2;
        progress.metrics[key].denominator_ids.push(waitingWork.id);
      }
    },
    waitingWork,
    setStale: () => {
      progress.provenance.stale = true;
    },
    rejectVersion: () => {
      decision.version += 1;
    },
  },
});
const root = document.getElementById("root")!;
createRoot(root).render(
  <BrowserRouter>
    <SWRConfig value={{ dedupingInterval: 0 }}>
      <ProjectDeliveryPage workspaceSlug="local" projectId="project" view="progress" />
    </SWRConfig>
  </BrowserRouter>
);
