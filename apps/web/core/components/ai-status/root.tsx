/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import useSWR from "swr";
import { useTranslation } from "@plane/i18n";
import { EmptyStateCompact } from "@plane/propel/empty-state";
import { Loader } from "@plane/ui";
// services
import { AIStatusService } from "@/services/ai-status.service";
// local imports
import { AIStatusDurationHistogram } from "./duration-histogram";
import { AIStatusInProgressList } from "./in-progress-list";

const aiStatusService = new AIStatusService();
const COMPLETED_WINDOW_DAYS = 90;

type Props = {
  workspaceSlug: string;
};

export function AIStatusRoot(props: Props) {
  const { workspaceSlug } = props;
  const { t } = useTranslation();
  const { data, error, isLoading } = useSWR(
    workspaceSlug ? `AI_STATUS_${workspaceSlug}_${COMPLETED_WINDOW_DAYS}` : null,
    workspaceSlug ? () => aiStatusService.getAIStatus(workspaceSlug, { days: COMPLETED_WINDOW_DAYS }) : null,
    { refreshInterval: 60_000 }
  );

  if (error)
    return (
      <div className="px-page-x py-6">
        <EmptyStateCompact
          assetKey="unknown"
          assetClassName="size-20"
          rootClassName="border border-subtle px-5 py-10"
          title={t("ai_status_page.error")}
        />
      </div>
    );

  if (isLoading || !data)
    return (
      <Loader className="flex flex-col gap-6 p-page-x py-6">
        <Loader.Item height="200px" width="100%" />
        <Loader.Item height="370px" width="100%" />
      </Loader>
    );

  return (
    <div className="flex h-full w-full flex-col gap-10 overflow-y-auto px-page-x py-6">
      <AIStatusInProgressList
        workspaceSlug={workspaceSlug}
        items={data.in_progress}
        bots={data.bots}
        now={Date.now()}
      />
      <AIStatusDurationHistogram items={data.completed} days={COMPLETED_WINDOW_DAYS} />
    </div>
  );
}
