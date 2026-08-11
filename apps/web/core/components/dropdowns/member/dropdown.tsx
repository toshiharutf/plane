/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
import { useParams } from "next/navigation";
import type { LucideIcon } from "lucide-react";
import { EUserBotType } from "@plane/types";
// hooks
import { useMember } from "@/hooks/store/use-member";
// local imports
import { MemberDropdownBase } from "./base";
import type { MemberDropdownProps } from "./types";

type TMemberDropdownProps = {
  icon?: LucideIcon;
  memberIds?: string[];
  onClose?: () => void;
  optionsClassName?: string;
  projectId?: string;
  renderByDefault?: boolean;
} & MemberDropdownProps;

export const MemberDropdown = observer(function MemberDropdown(props: TMemberDropdownProps) {
  const { memberIds: propsMemberIds, projectId } = props;
  // router params
  const { workspaceSlug } = useParams();
  // store hooks
  const {
    getUserDetails,
    project: { getProjectMemberIds, fetchProjectMembers },
    workspace: { workspaceMemberIds, fetchWorkspaceMembers },
  } = useMember();

  const projectMemberIds = projectId ? getProjectMemberIds(projectId, false) : null;
  const projectMemberIdSet = new Set(projectMemberIds ?? []);
  const workspaceAIBotMemberIds =
    projectId && !propsMemberIds && projectMemberIds
      ? (workspaceMemberIds ?? []).filter((userId) => {
          const userDetails = getUserDetails(userId);
          return (
            userDetails?.is_bot &&
            userDetails.bot_type === EUserBotType.AI_AGENT &&
            !projectMemberIdSet.has(userId)
          );
        })
      : [];

  const memberIds = propsMemberIds
    ? propsMemberIds
    : projectId
      ? projectMemberIds
        ? [...projectMemberIds, ...workspaceAIBotMemberIds]
        : null
      : workspaceMemberIds;

  const onDropdownOpen = () => {
    if (!projectMemberIds && projectId && workspaceSlug) fetchProjectMembers(workspaceSlug.toString(), projectId);
    if (!workspaceMemberIds && workspaceSlug) fetchWorkspaceMembers(workspaceSlug.toString());
  };

  return (
    <MemberDropdownBase
      {...props}
      getUserDetails={getUserDetails}
      memberIds={memberIds ?? []}
      onDropdownOpen={onDropdownOpen}
    />
  );
});
