/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

// Keep in sync with AI_MODEL_VALUES in apps/api/plane/db/models/issue.py.
// Values follow the Claude Code and Codex model names, with the effort appended after "-".
export const AI_MODEL_NONE = "None";

export type TAIModelGroup = {
  provider: "Claude" | "OpenAI";
  models: { name: string; efforts: string[] }[];
};

export const AI_MODEL_GROUPS: TAIModelGroup[] = [
  {
    provider: "Claude",
    models: [
      { name: "fable", efforts: ["low", "medium", "high", "xhigh", "max"] },
      { name: "opus", efforts: ["low", "medium", "high", "xhigh", "max"] },
      { name: "sonnet", efforts: ["low", "medium", "high", "xhigh", "max"] },
      { name: "haiku", efforts: [] },
    ],
  },
  {
    provider: "OpenAI",
    models: [
      { name: "gpt-6-astra", efforts: ["low", "medium", "high", "xhigh", "max"] },
      { name: "gpt-5.6-sol", efforts: ["low", "medium", "high", "xhigh", "max"] },
      { name: "gpt-5.6-terra", efforts: ["low", "medium", "high", "xhigh", "max", "ultra"] },
      { name: "gpt-5.6-luna", efforts: ["low", "medium", "high", "xhigh", "max"] },
      { name: "gpt-5.5", efforts: ["low", "medium", "high", "xhigh"] },
    ],
  },
];

export const AI_MODEL_OPTIONS: { value: string; provider: TAIModelGroup["provider"] | null }[] = [
  { value: AI_MODEL_NONE, provider: null },
  ...AI_MODEL_GROUPS.flatMap(({ provider, models }) =>
    models.flatMap(({ name, efforts }) =>
      (efforts.length ? efforts.map((effort) => `${name}-${effort}`) : [name]).map((value) => ({ value, provider }))
    )
  ),
];
