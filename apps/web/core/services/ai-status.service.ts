/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { API_BASE_URL } from "@plane/constants";
import type { TIssuePriorities } from "@plane/types";
// services
import { APIService } from "@/services/api.service";

export type TAIStatusBot = {
  id: string;
  display_name: string;
  avatar_url: string | null;
};

export type TAIStatusInProgressItem = {
  id: string;
  name: string;
  sequence_id: number;
  project_id: string;
  project_identifier: string;
  priority: TIssuePriorities;
  state_id: string;
  state_name: string;
  state_color: string;
  assignee_ids: string[];
  started_at: string;
  updated_at: string;
};

export type TAIStatusCompletedItem = {
  id: string;
  name: string;
  sequence_id: number;
  project_id: string;
  project_identifier: string;
  assignee_ids: string[];
  started_at: string;
  completed_at: string;
  duration_minutes: number;
};

export type TAIStatusResponse = {
  bots: TAIStatusBot[];
  in_progress: TAIStatusInProgressItem[];
  completed: TAIStatusCompletedItem[];
};

export class AIStatusService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  async getAIStatus(workspaceSlug: string, params?: { days?: number }): Promise<TAIStatusResponse> {
    return this.get(`/api/workspaces/${workspaceSlug}/ai-status/`, { params })
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }
}
