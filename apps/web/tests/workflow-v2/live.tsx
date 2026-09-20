// Real production services and components against an authenticated local backend.
// Global CSS is intentionally loaded for this standalone browser fixture.
// eslint-disable-next-line import/no-unassigned-import
import "../../styles/globals.css";
import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { ProjectDeliveryPage } from "../../core/components/project-delivery/root";
const query = new URLSearchParams(window.location.search);
type LocalStatus = {
  stage: string;
  developers: number;
  reviewers: number;
  awaiting_human_count: number;
  at: number;
  states: Record<string, string>;
};
function LocalMonitor() {
  const [status, setStatus] = useState<LocalStatus | null>(null);
  useEffect(() => {
    const refresh = () =>
      fetch("/__test_status")
        .then((r) => r.json())
        .then(setStatus)
        .catch(() => {});
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, []);
  if (!status) return null;
  return (
    <aside style={{ padding: 20, borderBottom: "1px solid #999", background: "#f5f7fa", color: "#18212f" }}>
      <strong>Local orchestrator monitor</strong>
      <p>
        {status.stage} · Developers: {status.developers}/4 · Reviewers: {status.reviewers} · Awaiting Human:{" "}
        {status.awaiting_human_count}
      </p>
      <p>Observed: {new Date(status.at * 1000).toLocaleTimeString()}</p>
      <ul>
        {Object.entries(status.states).map(([name, state]) => (
          <li key={name}>
            {name}: <strong>{String(state)}</strong>
          </li>
        ))}
      </ul>
      <nav style={{ display: "flex", gap: 20, marginTop: 12 }}>
        {["progress", "releases", "decisions"].map((view) => (
          <a
            key={view}
            href={`/live.html?workspace=${query.get("workspace")}&project=${query.get("project")}&view=${view}`}
          >
            {view}
          </a>
        ))}
      </nav>
    </aside>
  );
}
createRoot(document.getElementById("root")!).render(
  <BrowserRouter>
    <LocalMonitor />
    <ProjectDeliveryPage
      workspaceSlug={query.get("workspace")!}
      projectId={query.get("project")!}
      view={
        query.get("view") === "decisions" ? "decisions" : query.get("view") === "releases" ? "releases" : "progress"
      }
    />
  </BrowserRouter>
);
