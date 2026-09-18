/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { observer } from "mobx-react";
import useSWR from "swr";
import { useTranslation } from "@plane/i18n";
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import { TextArea } from "@plane/ui";
// hooks
import { useIssueDetail } from "@/hooks/store/use-issue-detail";
// services
import { HumanRequestService, isHumanAnswerSendable } from "@/services/human-request.service";

const humanRequestService = new HumanRequestService();

type Props = {
  workspaceSlug: string;
  projectId: string;
  issueId: string;
};

/** The open human request of a work item: its question, an answer box and a Send button (Enter adds a line). */
export const HumanRequestBanner = observer(function HumanRequestBanner(props: Props) {
  const { workspaceSlug, projectId, issueId } = props;
  // states
  const [answer, setAnswer] = useState("");
  const [isSending, setIsSending] = useState(false);
  // hooks
  const { t } = useTranslation();
  const {
    issue: { getIssueById },
    fetchIssue,
  } = useIssueDetail();
  // The state is part of the key, so the banner reloads when the item moves to or out of Awaiting Human.
  const stateId = getIssueById(issueId)?.state_id;
  const swrKey = workspaceSlug && issueId ? `HUMAN_REQUESTS_${workspaceSlug}_${issueId}_${stateId ?? ""}` : null;
  const { data, mutate } = useSWR(swrKey, () => humanRequestService.listOpen(workspaceSlug, issueId), {
    refreshInterval: 60_000,
  });

  const humanRequest = data?.[0];
  if (!humanRequest) return null;

  const isApproval = humanRequest.kind === "approval";
  const canSend = isHumanAnswerSendable(humanRequest.kind, answer);

  const handleSend = async () => {
    if (!canSend || isSending) return;
    setIsSending(true);
    try {
      await humanRequestService.answer(workspaceSlug, humanRequest.id, answer);
      setAnswer("");
      await mutate((requests) => requests?.filter((request) => request.id !== humanRequest.id), { revalidate: false });
      // The answer moves the item back to its state before the request.
      await fetchIssue(workspaceSlug, projectId, issueId).catch(() => undefined);
    } catch (error) {
      const message = (error as { error?: string } | undefined)?.error;
      setToast({
        type: TOAST_TYPE.ERROR,
        title: t("human_request.send_failed"),
        message: message ?? t("common.error.message"),
      });
    } finally {
      setIsSending(false);
    }
  };

  return (
    <div
      className="flex flex-col gap-2 rounded-lg border border-accent-strong bg-layer-1 p-3"
      data-testid="human-request-banner"
    >
      <div className="flex flex-col gap-0.5">
        <span className="text-11 font-medium text-accent-primary">
          {isApproval ? t("human_request.approval") : t("human_request.question")}
          {humanRequest.requested_by_display_name
            ? ` · ${t("human_request.asked_by", { name: humanRequest.requested_by_display_name })}`
            : ""}
        </span>
        <p className="text-13 break-words whitespace-pre-wrap text-primary">{humanRequest.question}</p>
      </div>
      <div className="flex items-end gap-2">
        <TextArea
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          // Enter (with or without Shift) keeps its default of a new line; only the button sends.
          onKeyDown={(e) => {
            if (e.key === "Enter") e.stopPropagation();
          }}
          placeholder={isApproval ? t("human_request.approval_placeholder") : t("human_request.placeholder")}
          aria-label={t("human_request.answer")}
          className="min-h-16 w-full resize-y"
          disabled={isSending}
        />
        <Button variant="primary" size="base" onClick={handleSend} disabled={!canSend} loading={isSending}>
          {t("human_request.send")}
        </Button>
      </div>
      {isApproval && !canSend && <span className="text-11 text-tertiary">{t("human_request.approval_hint")}</span>}
    </div>
  );
});
