import { ScopeBudgets } from "../../core/components/project-delivery/scope-budget";
import {
  ManualWorkVerification,
  WorkQuestionForm,
  workQuestions,
} from "../../core/components/project-delivery/work-questions";
import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router";
import {
  measureLabel,
  canAct,
  amountLabel,
  metricIds,
  commandError,
  localDateTime,
} from "../../core/components/project-delivery/utils";
import { DecisionReview, DeploymentRecovery } from "../../core/components/project-delivery/decision";
import { WorkLinks } from "../../core/components/project-delivery/evidence";
import {
  getTabPreferences,
  getTabUrl,
  getValidatedDefaultTab,
} from "../../core/components/navigation/tab-navigation-utils";

const metric = { numerator: 2, denominator: 3, numerator_ids: ["a", "b"], denominator_ids: ["a", "b", "c", "a"] };
const decision = {
  id: "decision-1",
  version: 3,
  subject_type: "candidate",
  subject_id: "candidate-1",
  action: "publish",
  payload: { repository: "local", exact_sha: "a".repeat(40) },
  payload_digest: "digest-123",
  decision: "pending",
  allowed_actions: ["resolve"],
};
const submit = async () => true;
test("empty, missing and revoked observations never become successful percentages", () => {
  assert.equal(measureLabel({ ...metric, denominator: 0 }), "No applicable work");
  assert.equal(measureLabel({ ...metric, numerator: null }), "Unverified");
  assert.equal(measureLabel({ ...metric, status: "revoked" }), "Unverified");
  assert.equal(measureLabel(metric), "2 / 3 items");
  assert.deepEqual(metricIds(metric), ["a", "b", "c"]);
});
test("unknown cost remains unknown while measured zero remains zero", () => {
  assert.equal(amountLabel(null, "USD"), "Unknown");
  assert.equal(amountLabel("", "min"), "Unknown");
  assert.equal(amountLabel(0, "USD"), "$0.00");
  assert.equal(amountLabel("17.5", "min"), "17.5 min");
});
test("stale, unauthorized, expired and revoked decisions cannot act", () => {
  assert.equal(canAct(decision, "resolve", false), true);
  assert.equal(canAct(decision, "resolve", true), false);
  assert.equal(canAct({ ...decision, revoked: true }, "resolve", false), false);
  assert.equal(canAct({ ...decision, expires_at: "2020-01-01" }, "resolve", false), false);
  assert.equal(canAct({ ...decision, allowed_actions: [] }, "resolve", false), false);
});
test("approval review shows exact operation and starts with clarification, no assumed authority", () => {
  const html = renderToStaticMarkup(<DecisionReview record={decision} stale={false} busy={false} submit={submit} />);
  assert.match(html, /digest-123/);
  assert.match(html, /aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/);
  assert.match(html, /selected="">Request clarification/);
  assert.match(html, /disabled="">Record decision/);
  assert.match(html, /Publication and deployment require separate decisions/);
});
test("stale decision keeps evidence visible but removes submit form", () => {
  const html = renderToStaticMarkup(<DecisionReview record={decision} stale busy={false} submit={submit} />);
  assert.match(html, /digest-123/);
  assert.match(html, /snapshot is stale/);
  assert.doesNotMatch(html, /<form/);
});
test("discarded deployments never show a retry even if an obsolete action is projected", () => {
  const html = renderToStaticMarkup(
    <DeploymentRecovery
      record={{ id: "d", version: 1, state: "Rolled Back", discarded: true, allowed_actions: ["retry"] }}
      decisions={[]}
      stale={false}
      busy={false}
      submit={submit}
    />
  );
  assert.equal(html, "");
});
test("work inspection deduplicates membership and identifies missing evidence", () => {
  const html = renderToStaticMarkup(
    <MemoryRouter>
      <WorkLinks
        ids={metricIds(metric)}
        verifiedIds={metric.numerator_ids}
        records={[{ id: "a", name: "Work A" }]}
        workspace="local"
        project="project"
      />
    </MemoryRouter>
  );
  assert.equal((html.match(/Work A/g) ?? []).length, 1);
  assert.match(html, /Not included in verified numerator/);
});
test("structured server refusal remains actionable", () => {
  assert.match(
    commandError({ error: "Revision changed", code: "stale_version", details: { expected: 3, current: 4 } }),
    /Revision changed.*stale_version.*expected/s
  );
});
test("adding progress leaves saved defaults and hidden choices unchanged", () => {
  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: { getItem: () => JSON.stringify({ project: { defaultTab: "cycles", hiddenTabs: ["releases"] } }) },
  });
  assert.deepEqual(getTabPreferences("project"), { defaultTab: "cycles", hiddenTabs: ["releases"] });
  assert.equal(getValidatedDefaultTab("project", ["project_progress", "work_items", "cycles"]), "cycles");
  assert.equal(getTabUrl("local", "project", "project_progress"), "/local/projects/project/progress");
  assert.equal(localDateTime("invalid"), "");
});

test("only diagnosed operational recovery offers retry, and closed decisions cannot resolve", () => {
  const recovery = {
    id: "d",
    version: 1,
    state: "Recovery Required",
    diagnosis: { cause: "non_code" },
    allowed_actions: ["retry"],
  };
  assert.equal(canAct(recovery, "retry", false), true);
  assert.equal(canAct({ ...recovery, state: "Healthy" }, "retry", false), false);
  assert.equal(canAct({ ...recovery, diagnosis: { cause: "code" } }, "retry", false), false);
  assert.equal(canAct({ ...decision, decision: "allow" }, "resolve", false), false);
});

test("wire projection preserves IDs, unknown memberships and absent scopes", async () => {
  const { normalizeProgress } = await import("../../core/services/workflow-v2.service");
  const raw = {
    scope: { id: "scope", revision: 4, items: ["issue-a", "issue-b"], excluded_ids: [] },
    work: [{ id: "continuation-a", issue_id: "issue-a", name: "Known item" }],
    environment: null,
  };
  const projected = normalizeProgress(raw);
  assert.deepEqual(
    projected.scope.items.map((item) => item.id),
    ["issue-a", "issue-b"]
  );
  assert.equal(projected.scope.items[0].name, "Known item");
  assert.equal(projected.scope.items[1].name, undefined);
  assert.equal(projected.environment.current_identity, null);
  assert.deepEqual(normalizeProgress({ scope: null, environment: null }).scope.items, []);
});

test("typed work questions remain distinct, default to clarification, and restrict withdrawal", () => {
  const work = {
    id: "continuation",
    issue_id: "issue",
    version: 4,
    state: "Awaiting Human",
    next_action: "review",
    allowed_actions: ["resolve_human", "withdraw_human"],
    continuation: {
      requests: {
        a: { question: "Review path?", status: "open" },
        b: { question: "Obsolete?", status: "open", allowed_actions: ["withdraw_human"] },
      },
    },
  };
  assert.equal(workQuestions(work).length, 2);
  const first = renderToStaticMarkup(
    <WorkQuestionForm
      work={work}
      requestKey="a"
      question={work.continuation.requests.a}
      stale={false}
      busy={false}
      submit={submit}
    />
  );
  assert.match(first, /Intended continuation: review/);
  assert.match(first, /selected="">Keep waiting/);
  assert.doesNotMatch(first, /Withdraw my/);
  const second = renderToStaticMarkup(
    <WorkQuestionForm
      work={work}
      requestKey="b"
      question={work.continuation.requests.b}
      stale={false}
      busy={false}
      submit={submit}
    />
  );
  assert.match(second, /Withdraw my obsolete question/);
  assert.match(second, /Evidence receipt digest/);
});

test("manual completion is a distinct evidence action, never available for code work", () => {
  const work = {
    id: "manual",
    version: 1,
    state: "Todo",
    execution_kind: "human",
    active: true,
    next_action: "verify_manual_completion",
    allowed_actions: ["manual_verify"],
  };
  assert.match(
    renderToStaticMarkup(<ManualWorkVerification work={work} stale={false} busy={false} submit={submit} />),
    /Record manual verification/
  );
  assert.equal(
    renderToStaticMarkup(
      <ManualWorkVerification work={{ ...work, execution_kind: "code" }} stale={false} busy={false} submit={submit} />
    ),
    ""
  );
});

test("authoritative scope budget units and contingency render without legacy reinterpretation", () => {
  const html = renderToStaticMarkup(
    <ScopeBudgets
      scopes={[
        {
          id: "s",
          revision: 1,
          definition: {
            budget: {
              policy: "both",
              cost_ceiling_usd: "100",
              active_minute_ceiling: 120,
              contingency_usd: "10",
              contingency_minutes: 15,
            },
          },
          budget_admission: { guards: ["cycle_cost_limit"], contexts: [] },
        },
      ]}
    />
  );
  assert.match(html, /both/);
  assert.match(html, /\$100.00/);
  assert.match(html, /120.0 min/);
  assert.match(html, /\$10.00/);
  assert.match(html, /15.0 min/);
  assert.match(html, /cycle_cost_limit/);
});
