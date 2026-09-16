/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { TAIUsageModel, TAIUsageTokenMetrics } from "@plane/types";

export type TAIUsageMetricKey = keyof TAIUsageTokenMetrics;

export type TAIUsageChartDatum = TAIUsageTokenMetrics & {
  key: string;
  identifier: string;
  name: string;
  total: number;
};

/**
 * Stack order, bottom to top. recharts draws the first bar of a stack at the bottom,
 * and the legend and tooltip follow the same order.
 */
export const AI_USAGE_METRICS: { key: TAIUsageMetricKey; i18nLabel: string; colorIndex: number }[] = [
  { key: "input_tokens", i18nLabel: "workspace_analytics.ai_usage.input_tokens", colorIndex: 0 },
  { key: "output_tokens", i18nLabel: "workspace_analytics.ai_usage.output_tokens", colorIndex: 3 },
  { key: "cache_creation_input_tokens", i18nLabel: "workspace_analytics.ai_usage.cache_miss", colorIndex: 4 },
  { key: "cache_read_input_tokens", i18nLabel: "workspace_analytics.ai_usage.cache_hit", colorIndex: 5 },
];

const compactFormatter = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

export const formatTokenCount = (value: number | string | undefined): string => {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) ? compactFormatter.format(numeric) : String(value);
};

export const buildAIUsageChartData = (usage: TAIUsageModel): TAIUsageChartDatum[] =>
  usage.work_items.map((item) => {
    const metrics = Object.fromEntries(
      AI_USAGE_METRICS.map(({ key }) => [key, Number(item[key] ?? 0)])
    ) as TAIUsageTokenMetrics;
    return {
      ...metrics,
      key: item.id,
      identifier: `${item.project_identifier}-${item.sequence_id}`,
      name: item.name,
      total: AI_USAGE_METRICS.reduce((sum, { key }) => sum + metrics[key], 0),
    };
  });
