/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useMemo } from "react";
import { useTheme } from "next-themes";
// plane imports
import { CHART_COLOR_PALETTES } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { BarChart } from "@plane/propel/charts/bar-chart";
import type { TAIUsageModel, TBarItem } from "@plane/types";
// local imports
import AnalyticsSectionWrapper from "../analytics-section-wrapper";
import type { TAIUsageChartDatum, TAIUsageMetricKey } from "./utils";
import { AI_USAGE_METRICS, buildAIUsageChartData, formatTokenCount } from "./utils";

type TickProps = { x: number; y: number; payload: { value: number | string } };

function TokenYAxisTick({ x, y, payload }: TickProps) {
  return (
    <g transform={`translate(${x},${y})`}>
      <text dx={-10} textAnchor="middle" className="fill-tertiary text-13">
        {formatTokenCount(payload.value)}
      </text>
    </g>
  );
}

type TooltipProps = {
  active?: boolean;
  payload?: { dataKey?: string; value?: number; color?: string; payload?: TAIUsageChartDatum }[];
  labels: Record<string, string>;
  colors: Record<string, string>;
  totalLabel: string;
};

function AIUsageTooltip({ active, payload, labels, colors, totalLabel }: TooltipProps) {
  const datum = payload?.[0]?.payload;
  if (!active || !datum) return null;
  return (
    <div className="flex w-[16rem] flex-col gap-1.5 rounded-md border border-subtle bg-surface-1 p-2 shadow-raised-200">
      <div className="border-b border-subtle pb-2">
        <p className="text-11 font-medium text-primary">{datum.identifier}</p>
        <p className="truncate text-11 text-secondary">{datum.name}</p>
      </div>
      {AI_USAGE_METRICS.map(({ key }) => (
        <div key={key} className="flex items-center justify-between gap-2 text-11">
          <div className="flex items-center gap-2 truncate">
            <div className="size-2 flex-shrink-0 rounded-xs" style={{ backgroundColor: colors[key] }} />
            <span className="truncate text-tertiary">{labels[key]}:</span>
          </div>
          <span className="flex-shrink-0 font-medium text-secondary">{datum[key].toLocaleString()}</span>
        </div>
      ))}
      <div className="flex items-center justify-between gap-2 border-t border-subtle pt-1.5 text-11">
        <span className="text-tertiary">{totalLabel}:</span>
        <span className="font-medium text-primary">{datum.total.toLocaleString()}</span>
      </div>
    </div>
  );
}

type Props = {
  usage: TAIUsageModel;
};

export function AIUsageModelChart({ usage }: Props) {
  const { t } = useTranslation();
  const { resolvedTheme } = useTheme();

  const data = useMemo(() => buildAIUsageChartData(usage), [usage]);

  const { bars, labels, colors } = useMemo(() => {
    const palette = CHART_COLOR_PALETTES[0]?.[resolvedTheme === "dark" ? "dark" : "light"] ?? [];
    const labelMap: Record<string, string> = {};
    const colorMap: Record<string, string> = {};
    const nonZeroKeys = (datum: TAIUsageChartDatum): TAIUsageMetricKey[] =>
      AI_USAGE_METRICS.map(({ key }) => key).filter((key) => datum[key] > 0);
    const barItems: TBarItem<TAIUsageMetricKey>[] = AI_USAGE_METRICS.map(({ key, i18nLabel, colorIndex }, index) => {
      labelMap[key] = t(i18nLabel);
      colorMap[key] = palette[colorIndex] ?? palette[index] ?? "#6172E8";
      return {
        key,
        label: labelMap[key],
        stackId: "ai-usage",
        fill: colorMap[key],
        textClassName: "",
        showPercentage: false,
        showTopBorderRadius: (barKey: string, payload: TAIUsageChartDatum) => nonZeroKeys(payload).at(-1) === barKey,
        showBottomBorderRadius: (barKey: string, payload: TAIUsageChartDatum) => nonZeroKeys(payload)[0] === barKey,
      };
    });
    return { bars: barItems, labels: labelMap, colors: colorMap };
  }, [resolvedTheme, t]);

  return (
    <AnalyticsSectionWrapper
      title={usage.model}
      actions={
        <span className="text-13 text-tertiary">
          {t("sidebar.work_items")}: {data.length}
        </span>
      }
    >
      <BarChart
        className="h-[370px] w-full"
        data={data}
        bars={bars}
        barSize={data.length > 20 ? 16 : 40}
        margin={{ bottom: 30, left: 30 }}
        xAxis={{ key: "identifier", label: t("workspace_analytics.ai_usage.work_item"), dy: 30 }}
        yAxis={{ key: "total", label: t("workspace_analytics.ai_usage.tokens"), offset: -60, dx: -36 }}
        customTicks={{ y: TokenYAxisTick as React.ComponentType<unknown> }}
        legend={{ align: "center", verticalAlign: "bottom", layout: "horizontal", wrapperStyles: { bottom: -10 } }}
        customTooltipContent={({ active, payload }) => (
          <AIUsageTooltip
            active={active}
            payload={payload}
            labels={labels}
            colors={colors}
            totalLabel={t("workspace_analytics.ai_usage.tokens")}
          />
        )}
      />
    </AnalyticsSectionWrapper>
  );
}
