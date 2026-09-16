/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
import { useParams } from "next/navigation";
import { useTranslation } from "@plane/i18n";
// components
import { AIStatusRoot } from "@/components/ai-status/root";
import { PageHead } from "@/components/core/page-title";
// hooks
import { useWorkspace } from "@/hooks/store/use-workspace";

function WorkspaceAIStatusPage() {
  const { workspaceSlug } = useParams();
  const { t } = useTranslation();
  const { currentWorkspace } = useWorkspace();
  // derived values
  const pageTitle = currentWorkspace?.name ? `${currentWorkspace.name} - ${t("ai_status")}` : undefined;

  return (
    <>
      <PageHead title={pageTitle} />
      <AIStatusRoot workspaceSlug={workspaceSlug?.toString() ?? ""} />
    </>
  );
}

export default observer(WorkspaceAIStatusPage);
