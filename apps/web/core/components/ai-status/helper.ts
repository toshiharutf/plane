/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

const MINUTES_PER_HOUR = 60;
const MINUTES_PER_DAY = 24 * MINUTES_PER_HOUR;

/** Candidate histogram bin widths in minutes, smallest first. */
export const AI_STATUS_BIN_WIDTHS = [1, 2, 5, 10, 15, 30, 60, 120, 240, 480, 720, 1440];
export const AI_STATUS_MAX_BINS = 12;

export type TDurationBin = {
  key: string;
  name: string;
  start: number;
  end: number;
  count: number;
};

export type TDurationHistogram = {
  binWidth: number;
  bins: TDurationBin[];
};

export type TDurationStats = {
  count: number;
  median: number;
  mean: number;
};

const countBins = (min: number, max: number, width: number) => Math.floor(max / width) - Math.floor(min / width) + 1;

/**
 * Smallest "nice" bin width (minutes) that keeps the histogram at or below `maxBins` bins.
 * Falls back to whole days once the day width is not enough.
 */
export const getDurationBinWidth = (min: number, max: number, maxBins: number = AI_STATUS_MAX_BINS): number => {
  const width = AI_STATUS_BIN_WIDTHS.find((candidate) => countBins(min, max, candidate) <= maxBins);
  if (width) return width;
  let days = 2;
  while (countBins(min, max, days * MINUTES_PER_DAY) > maxBins) days *= 2;
  return days * MINUTES_PER_DAY;
};

const formatNumber = (value: number) => `${Math.round(value * 10) / 10}`;

const getDurationUnit = (minutes: number): "min" | "h" | "d" => {
  if (minutes > 0 && minutes % MINUTES_PER_DAY === 0) return "d";
  if (minutes > 0 && minutes % MINUTES_PER_HOUR === 0) return "h";
  return "min";
};

const toUnit = (minutes: number, unit: "min" | "h" | "d") =>
  unit === "d" ? minutes / MINUTES_PER_DAY : unit === "h" ? minutes / MINUTES_PER_HOUR : minutes;

/** Human readable duration, e.g. 45 -> "45 min", 90 -> "1.5 h", 2880 -> "2 d". */
export const formatDuration = (minutes: number): string => {
  if (minutes < MINUTES_PER_HOUR) return `${formatNumber(minutes)} min`;
  if (minutes < MINUTES_PER_DAY) return `${formatNumber(minutes / MINUTES_PER_HOUR)} h`;
  return `${formatNumber(minutes / MINUTES_PER_DAY)} d`;
};

/** Bin label, e.g. "0–5 min", "1–2 h", "30 min–1 h". */
export const formatBinLabel = (start: number, end: number): string => {
  const endUnit = getDurationUnit(end);
  const startUnit = start === 0 ? endUnit : getDurationUnit(start);
  if (startUnit === endUnit)
    return `${formatNumber(toUnit(start, startUnit))}–${formatNumber(toUnit(end, endUnit))} ${endUnit}`;
  return `${formatDuration(start)}–${formatDuration(end)}`;
};

/** Histogram of durations (minutes) with an adaptive bin width; empty bins inside the range are kept. */
export const buildDurationHistogram = (
  durations: number[],
  maxBins: number = AI_STATUS_MAX_BINS
): TDurationHistogram | undefined => {
  const values = durations.filter((value) => Number.isFinite(value) && value >= 0);
  if (values.length === 0) return undefined;

  const min = Math.min(...values);
  const max = Math.max(...values);
  const binWidth = getDurationBinWidth(min, max, maxBins);
  const firstBin = Math.floor(min / binWidth);
  const bins: TDurationBin[] = Array.from({ length: countBins(min, max, binWidth) }, (_, index) => {
    const start = (firstBin + index) * binWidth;
    const end = start + binWidth;
    return { key: `${start}`, name: formatBinLabel(start, end), start, end, count: 0 };
  });
  values.forEach((value) => {
    bins[Math.floor(value / binWidth) - firstBin].count += 1;
  });

  return { binWidth, bins };
};

export const getDurationStats = (durations: number[]): TDurationStats | undefined => {
  // oxlint-disable-next-line unicorn/no-array-sort
  const values = durations.filter((value) => Number.isFinite(value) && value >= 0).sort((a, b) => a - b);
  if (values.length === 0) return undefined;
  const middle = Math.floor(values.length / 2);
  const median = values.length % 2 === 0 ? (values[middle - 1] + values[middle]) / 2 : values[middle];
  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  return { count: values.length, median, mean };
};
