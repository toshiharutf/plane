/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { ArrowDownToLine, ArrowUpFromLine, Database, DatabaseZap } from "lucide-react";
import useSWR from "swr";
// i18n
import { useTranslation } from "@plane/i18n";
// components
import { SidebarPropertyListItem } from "@/components/common/layout/sidebar/property-list-item";
// services
import type { TIssueAIUsageTokens } from "@/services/issue/issue_ai_usage.service";
import { IssueAIUsageService } from "@/services/issue/issue_ai_usage.service";

const issueAIUsageService = new IssueAIUsageService();

const AI_USAGE_PROPERTIES: {
  key: keyof TIssueAIUsageTokens;
  i18nLabel: string;
  icon: React.FC<{ className?: string }>;
}[] = [
  { key: "input_tokens", i18nLabel: "issue.ai_usage.input_tokens", icon: ArrowDownToLine },
  { key: "output_tokens", i18nLabel: "issue.ai_usage.output_tokens", icon: ArrowUpFromLine },
  { key: "cache_creation_input_tokens", i18nLabel: "issue.ai_usage.input_cache_miss", icon: Database },
  { key: "cache_read_input_tokens", i18nLabel: "issue.ai_usage.input_cache_hit", icon: DatabaseZap },
];

type Props = {
  workspaceSlug: string;
  projectId: string;
  issueId: string;
};

/** Read-only token and cache metrics reported for the work item by AI agents, summed over all models. */
export function IssueAIUsageProperties(props: Props) {
  const { workspaceSlug, projectId, issueId } = props;
  const { t } = useTranslation();

  const { data } = useSWR(
    workspaceSlug && projectId && issueId ? `ISSUE_AI_USAGE_${workspaceSlug}_${projectId}_${issueId}` : null,
    workspaceSlug && projectId && issueId
      ? () => issueAIUsageService.getAIUsageSummary(workspaceSlug, projectId, issueId)
      : null
  );

  return (
    <>
      {AI_USAGE_PROPERTIES.map(({ key, i18nLabel, icon }) => {
        const breakdown = data?.models.map((usage) => `${usage.model}: ${usage[key].toLocaleString()}`).join("\n");
        return (
          <SidebarPropertyListItem key={key} icon={icon} label={t(i18nLabel)}>
            <span className="flex h-7.5 items-center px-2 text-body-xs-regular text-secondary" title={breakdown}>
              {data ? data.totals[key].toLocaleString() : "-"}
            </span>
          </SidebarPropertyListItem>
        );
      })}
    </>
  );
}
