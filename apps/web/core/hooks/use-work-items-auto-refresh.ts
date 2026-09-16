/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useRef } from "react";
import { usePathname } from "next/navigation";
import useSWR, { useSWRConfig } from "swr";
// plane imports
import { EIssuesStoreType } from "@plane/types";
// hooks
import { useIssueDetail } from "@/hooks/store/use-issue-detail";
import { useIssues } from "@/hooks/store/use-issues";
// services
import { IssueService } from "@/services/issue";

const issueService = new IssueService();

export const WORK_ITEMS_SYNC_POLL_INTERVAL = 10_000;

/**
 * Polls the project's work item sync state and refreshes the open work item views when work items
 * were changed outside this client (API, AI bots, other users):
 * - the project work items list (list, kanban, calendar, spreadsheet, gantt)
 * - the work item peek overview
 * - the full work item detail page (browse)
 * Polling pauses while the tab is hidden and runs again as soon as it regains focus.
 */
export const useWorkItemsAutoRefresh = (workspaceSlug: string | undefined, projectId: string | undefined) => {
  const pathname = usePathname();
  const { mutate } = useSWRConfig();
  const {
    issues: { refreshIssues },
  } = useIssues(EIssuesStoreType.PROJECT);
  const {
    peekIssue,
    issue: { fetchIssue },
  } = useIssueDetail();
  // refs
  const lastSyncToken = useRef<string | undefined>(undefined);

  const isEnabled = !!workspaceSlug && !!projectId && !pathname?.includes("/settings/");

  const { data: syncState } = useSWR(
    isEnabled ? `WORK_ITEMS_SYNC_STATE_${workspaceSlug}_${projectId}` : null,
    isEnabled ? () => issueService.getSyncState(workspaceSlug, projectId) : null,
    {
      refreshInterval: WORK_ITEMS_SYNC_POLL_INTERVAL,
      refreshWhenHidden: false,
      revalidateOnFocus: true,
      shouldRetryOnError: false,
    }
  );

  // start from a fresh baseline when switching projects
  useEffect(() => {
    lastSyncToken.current = undefined;
  }, [workspaceSlug, projectId]);

  useEffect(() => {
    if (!syncState || !workspaceSlug || !projectId) return;
    const syncToken = `${syncState.latest_issue_updated_at ?? ""}|${syncState.latest_activity_at ?? ""}`;
    const previousSyncToken = lastSyncToken.current;
    lastSyncToken.current = syncToken;
    // the first response only sets the baseline, the views were just loaded
    if (previousSyncToken === undefined || previousSyncToken === syncToken) return;

    if (pathname?.includes(`/projects/${projectId}/issues`)) {
      refreshIssues(workspaceSlug, projectId).catch((error) => console.error("Error refreshing work items", error));
    }

    if (peekIssue && peekIssue.projectId === projectId) {
      fetchIssue(peekIssue.workspaceSlug, peekIssue.projectId, peekIssue.issueId).catch((error) =>
        console.error("Error refreshing the work item", error)
      );
    }

    if (pathname?.includes("/browse/")) {
      void mutate((key) => typeof key === "string" && key.startsWith("ISSUE_DETAIL_"));
    }
    // only react to sync state changes, the other values are read at that moment
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [syncState]);
};
