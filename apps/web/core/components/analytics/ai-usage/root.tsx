/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
import { useParams } from "next/navigation";
import useSWR from "swr";
// plane imports
import { useTranslation } from "@plane/i18n";
import { EmptyStateCompact } from "@plane/propel/empty-state";
// hooks
import { useAnalytics } from "@/hooks/store/use-analytics";
import { AnalyticsService } from "@/services/analytics.service";
// local imports
import AnalyticsWrapper from "../analytics-wrapper";
import { ChartLoader } from "../loaders";
import { AIUsageModelChart } from "./model-chart";

const analyticsService = new AnalyticsService();

const AIUsage = observer(function AIUsage() {
  const { t } = useTranslation();
  const { selectedProjects } = useAnalytics();
  const params = useParams();
  const workspaceSlug = params.workspaceSlug?.toString() ?? "";
  const projectIds = selectedProjects?.length > 0 ? selectedProjects.join(",") : undefined;

  const { data, isLoading } = useSWR(workspaceSlug ? `ai-usage-analytics-${workspaceSlug}-${projectIds}` : null, () =>
    analyticsService.getAIUsageAnalytics(workspaceSlug, projectIds ? { project_ids: projectIds } : undefined)
  );

  const models = data?.models?.filter((usage) => usage.work_items.length > 0) ?? [];

  return (
    <AnalyticsWrapper i18nTitle="workspace_analytics.ai_usage.title">
      <p className="-mt-2 mb-8 text-13 text-tertiary md:-mt-4">{t("workspace_analytics.ai_usage.description")}</p>
      {isLoading ? (
        <ChartLoader />
      ) : models.length > 0 ? (
        <div className="flex flex-col gap-14">
          {models.map((usage) => (
            <AIUsageModelChart key={usage.model} usage={usage} />
          ))}
        </div>
      ) : (
        <EmptyStateCompact
          assetKey="unknown"
          assetClassName="size-20"
          rootClassName="border border-subtle px-5 py-10 md:py-20 md:px-20"
          title={t("workspace_analytics.ai_usage.empty_state")}
        />
      )}
    </AnalyticsWrapper>
  );
});

export { AIUsage };
