/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { API_BASE_URL } from "@plane/constants";
// services
import { APIService } from "@/services/api.service";

export type TIssueAIUsageTokens = {
  input_tokens: number;
  output_tokens: number;
  // input cache miss (cache write)
  cache_creation_input_tokens: number;
  // input cache hit (cache read)
  cache_read_input_tokens: number;
};

export type TIssueAIUsageSummary = {
  totals: TIssueAIUsageTokens;
  models: (TIssueAIUsageTokens & { model: string })[];
};

export class IssueAIUsageService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  async getAIUsageSummary(workspaceSlug: string, projectId: string, issueId: string): Promise<TIssueAIUsageSummary> {
    return this.get(`/api/workspaces/${workspaceSlug}/projects/${projectId}/issues/${issueId}/ai-usage/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }
}
