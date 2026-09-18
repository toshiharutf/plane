/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { API_BASE_URL } from "@plane/constants";
// services
import { APIService } from "@/services/api.service";

export type THumanRequestKind = "approval" | "question";

export type THumanRequest = {
  id: string;
  kind: THumanRequestKind;
  question: string;
  requested_by_id: string | null;
  requested_by_display_name: string | null;
  requested_at: string;
  answer: string;
  decision: "" | "accept" | "deny" | "answered";
  note: string;
  is_open: boolean;
  resolved_by_id: string | null;
  resolved_at: string | null;
  issue_id: string;
  issue_name: string;
  issue_sequence_id: number;
  project_id: string;
  project_identifier: string;
  state_before_id: string | null;
};

// Same rule as the API (plane/utils/human_request.py): the first word decides an approval,
// so "yes", "Accept!" and "no - wait" match while "yesterday", "nope" and "ok" do not.
const APPROVAL_ANSWER_PATTERN = /^\s*(yes|accept|no|deny)(?![A-Za-z0-9_])/i;

/** Whether an answer text can be sent: never empty, and an approval starts with yes/accept or no/deny. */
export const isHumanAnswerSendable = (kind: THumanRequestKind, text: string): boolean => {
  if (!text.trim()) return false;
  return kind !== "approval" || APPROVAL_ANSWER_PATTERN.test(text);
};

export class HumanRequestService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  /** Open human requests the user can see, newest first; `issueId` limits them to one work item. */
  async listOpen(workspaceSlug: string, issueId?: string): Promise<THumanRequest[]> {
    return this.get(`/api/workspaces/${workspaceSlug}/human-requests/`, {
      params: issueId ? { issue_id: issueId } : {},
    })
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async answer(workspaceSlug: string, humanRequestId: string, answer: string): Promise<THumanRequest> {
    return this.post(`/api/workspaces/${workspaceSlug}/human-requests/${humanRequestId}/answer/`, { answer })
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }
}
