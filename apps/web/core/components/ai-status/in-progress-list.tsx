/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import Link from "next/link";
import { useTranslation } from "@plane/i18n";
import { EmptyStateCompact } from "@plane/propel/empty-state";
import { renderFormattedDate, renderFormattedTime } from "@plane/utils";
// services
import type { TAIStatusBot, TAIStatusInProgressItem } from "@/services/ai-status.service";
// local imports
import { formatDuration } from "./helper";

type Props = {
  workspaceSlug: string;
  items: TAIStatusInProgressItem[];
  bots: TAIStatusBot[];
  now: number;
};

export function AIStatusInProgressList(props: Props) {
  const { workspaceSlug, items, bots, now } = props;
  const { t } = useTranslation();
  const botNames = new Map(bots.map((bot) => [bot.id, bot.display_name]));

  return (
    <section className="flex flex-col gap-3">
      <div>
        <h3 className="text-16 font-semibold text-primary">{t("ai_status_page.in_progress.title")}</h3>
        <p className="text-13 text-tertiary">{t("ai_status_page.in_progress.description")}</p>
      </div>
      {items.length === 0 ? (
        <EmptyStateCompact
          assetKey="unknown"
          assetClassName="size-20"
          rootClassName="border border-subtle px-5 py-10"
          title={t("ai_status_page.in_progress.empty")}
        />
      ) : (
        <div className="overflow-x-auto rounded-md border border-subtle">
          <table className="w-full text-left text-13">
            <thead className="border-b border-subtle text-tertiary">
              <tr>
                <th className="px-3 py-2 font-medium">{t("ai_status_page.in_progress.work_item")}</th>
                <th className="px-3 py-2 font-medium">{t("ai_status_page.in_progress.bots")}</th>
                <th className="px-3 py-2 font-medium">{t("ai_status_page.in_progress.state")}</th>
                <th className="px-3 py-2 font-medium">{t("ai_status_page.in_progress.started")}</th>
                <th className="px-3 py-2 text-right font-medium">{t("ai_status_page.in_progress.elapsed")}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => {
                const identifier = `${item.project_identifier}-${item.sequence_id}`;
                const elapsedMinutes = Math.max((now - new Date(item.started_at).getTime()) / 60000, 0);
                return (
                  <tr key={item.id} className="border-b border-subtle last:border-b-0">
                    <td className="px-3 py-2">
                      <Link
                        href={`/${workspaceSlug}/browse/${identifier}/`}
                        className="flex items-center gap-2 hover:underline"
                      >
                        <span className="flex-shrink-0 text-tertiary">{identifier}</span>
                        <span className="truncate text-primary">{item.name}</span>
                      </Link>
                    </td>
                    <td className="px-3 py-2 text-secondary">
                      {item.assignee_ids.map((id) => botNames.get(id) ?? id).join(", ")}
                    </td>
                    <td className="px-3 py-2">
                      <span className="inline-flex items-center gap-1.5 text-secondary">
                        <span className="size-2 rounded-full" style={{ backgroundColor: item.state_color }} />
                        {item.state_name}
                      </span>
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap text-secondary">
                      {renderFormattedDate(item.started_at)} {renderFormattedTime(item.started_at)}
                    </td>
                    <td className="px-3 py-2 text-right whitespace-nowrap text-secondary">
                      {formatDuration(elapsedMinutes)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
