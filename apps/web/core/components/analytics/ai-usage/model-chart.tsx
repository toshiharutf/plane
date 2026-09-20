/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import {
  Bar,
  BarChart as CostBarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
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

const COST_CATEGORIES = [
  { key: "input", label: "Input", color: "#6366f1" },
  { key: "output", label: "Output", color: "#059669" },
  { key: "cache_write_5m", label: "Cache write (5m)", color: "#ca8a04" },
  { key: "cache_write_1h", label: "Cache write (1h)", color: "#db2777" },
  { key: "cache_read", label: "Cache read", color: "#0891b2" },
] as const;

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
      <section className="mb-6" aria-label={`${usage.model} cost and active duration`}>
        <h3 className="mb-2 text-16 font-medium">Actual cost (USD)</h3>
        <p className="mb-3 text-13 text-tertiary">
          Cost uses recorded prices. Unknown prices are gaps, never zero. Active time excludes human waiting.
        </p>
        <div
          className="h-56"
          role="figure"
          aria-label="Actual USD cost by completed work item; missing prices are unknown"
        >
          <ResponsiveContainer width="100%" height="100%">
            <CostBarChart
              data={usage.work_items.map((item) => ({
                name: `${item.project_identifier}-${item.sequence_id}`,
                cost: item.api_cost_usd == null ? null : Number(item.api_cost_usd),
              }))}
            >
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="name" />
              <YAxis />
              <Tooltip formatter={(value) => [`$${Number(value).toFixed(4)}`, "Actual USD"]} />
              <Bar dataKey="cost" name="Actual USD" fill="#6366f1" />
            </CostBarChart>
          </ResponsiveContainer>
        </div>
        <details className="mt-4 text-13">
          <summary className="cursor-pointer">Recorded USD by input, output and cache category</summary>
          <p className="my-3 text-tertiary">
            Historic runs without a stored price breakdown remain unknown. Category bars use recorded costs and do not
            reprice tokens.
          </p>
          <div
            className="h-64"
            role="figure"
            aria-label="Recorded cost categories in USD by work item; unknown breakdowns have no bars"
          >
            <ResponsiveContainer width="100%" height="100%">
              <CostBarChart
                data={usage.work_items.map((item) => ({
                  name: `${item.project_identifier}-${item.sequence_id}`,
                  ...Object.fromEntries(
                    COST_CATEGORIES.map(({ key }) => [
                      key,
                      item.cost_categories_usd?.[key] == null ? null : Number(item.cost_categories_usd[key]),
                    ])
                  ),
                }))}
              >
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="name" />
                <YAxis />
                <Tooltip formatter={(value) => `$${Number(value).toFixed(6)}`} />
                <Legend />
                {COST_CATEGORIES.map(({ key, label, color }) => (
                  <Bar key={key} dataKey={key} name={label} stackId="recorded-usd" fill={color} />
                ))}
              </CostBarChart>
            </ResponsiveContainer>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr>
                  <th>Work item</th>
                  {COST_CATEGORIES.map(({ label }) => (
                    <th key={label}>{label} USD</th>
                  ))}
                  <th>Runs missing breakdown</th>
                </tr>
              </thead>
              <tbody>
                {usage.work_items.map((item) => (
                  <tr key={item.id} className="border-t border-subtle">
                    <th className="font-normal py-2">
                      {item.project_identifier}-{item.sequence_id}
                    </th>
                    {COST_CATEGORIES.map(({ key }) => (
                      <td key={key}>
                        {item.cost_categories_usd?.[key] == null
                          ? "Unknown"
                          : `$${Number(item.cost_categories_usd[key]).toFixed(6)}`}
                      </td>
                    ))}
                    <td>{item.unknown_category_count ?? "Unknown"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
        <details className="mt-3 text-13">
          <summary className="cursor-pointer">Cost, known subtotal and active minutes</summary>
          <div className="overflow-x-auto">
            <table className="mt-2 w-full text-left">
              <thead>
                <tr>
                  <th>Work item</th>
                  <th>Actual USD</th>
                  <th>Known subtotal USD</th>
                  <th>Unpriced runs</th>
                  <th>Active minutes</th>
                </tr>
              </thead>
              <tbody>
                {usage.work_items.map((item) => (
                  <tr className="border-t border-subtle" key={item.id}>
                    <th className="font-normal py-2">
                      {item.project_identifier}-{item.sequence_id}
                    </th>
                    <td>{item.api_cost_usd == null ? "Unknown" : `$${Number(item.api_cost_usd).toFixed(4)}`}</td>
                    <td>
                      {item.known_api_cost_usd == null ? "Unknown" : `$${Number(item.known_api_cost_usd).toFixed(4)}`}
                    </td>
                    <td>{item.unknown_cost_count ?? "Unknown"}</td>
                    <td>{item.duration_seconds == null ? "Unknown" : (item.duration_seconds / 60).toFixed(1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      </section>
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
