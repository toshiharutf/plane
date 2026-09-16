/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useMemo } from "react";
import { useTheme } from "next-themes";
import { CHART_COLOR_PALETTES } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { BarChart } from "@plane/propel/charts/bar-chart";
import { EmptyStateCompact } from "@plane/propel/empty-state";
import type { TBarItem } from "@plane/types";
// services
import type { TAIStatusCompletedItem } from "@/services/ai-status.service";
// local imports
import { buildDurationHistogram, formatDuration, getDurationStats } from "./helper";

type Props = {
  items: TAIStatusCompletedItem[];
  days: number;
};

export function AIStatusDurationHistogram(props: Props) {
  const { items, days } = props;
  const { t } = useTranslation();
  const { resolvedTheme } = useTheme();

  const durations = useMemo(() => items.map((item) => item.duration_minutes), [items]);
  const histogram = useMemo(() => buildDurationHistogram(durations), [durations]);
  const stats = useMemo(() => getDurationStats(durations), [durations]);

  const bars: TBarItem<"count">[] = useMemo(
    () => [
      {
        key: "count",
        label: t("ai_status_page.histogram.work_items"),
        stackId: "bar-one",
        fill: CHART_COLOR_PALETTES[0]?.[resolvedTheme === "dark" ? "dark" : "light"]?.[0] ?? "#6172E8",
        textClassName: "",
        showPercentage: false,
        showTopBorderRadius: () => true,
        showBottomBorderRadius: () => true,
      },
    ],
    [resolvedTheme, t]
  );

  return (
    <section className="flex flex-col gap-3">
      <div>
        <h3 className="text-16 font-semibold text-primary">{t("ai_status_page.histogram.title")}</h3>
        <p className="text-13 text-tertiary">{t("ai_status_page.histogram.description", { days })}</p>
      </div>
      {!histogram || !stats ? (
        <EmptyStateCompact
          assetKey="unknown"
          assetClassName="size-20"
          rootClassName="border border-subtle px-5 py-10"
          title={t("ai_status_page.histogram.empty")}
        />
      ) : (
        <>
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            {[
              { label: t("ai_status_page.histogram.completed"), value: `${stats.count}` },
              { label: t("ai_status_page.histogram.median"), value: formatDuration(stats.median) },
              { label: t("ai_status_page.histogram.mean"), value: formatDuration(stats.mean) },
              { label: t("ai_status_page.histogram.bin_width"), value: formatDuration(histogram.binWidth) },
            ].map((stat) => (
              <div key={stat.label} className="flex flex-col gap-1">
                <div className="text-13 text-tertiary">{stat.label}</div>
                <div className="text-20 font-bold text-primary">{stat.value}</div>
              </div>
            ))}
          </div>
          <BarChart
            className="h-[370px] w-full"
            data={histogram.bins}
            bars={bars}
            margin={{ bottom: 30 }}
            xAxis={{ key: "name", label: t("ai_status_page.histogram.duration"), dy: 30 }}
            yAxis={{
              key: "count",
              label: t("ai_status_page.histogram.work_items"),
              allowDecimals: false,
              offset: -60,
              dx: -26,
            }}
          />
        </>
      )}
    </section>
  );
}
