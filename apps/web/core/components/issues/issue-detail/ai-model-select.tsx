/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
import { Bot } from "lucide-react";
// plane imports
import { AI_MODEL_NONE, AI_MODEL_OPTIONS } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { CustomSearchSelect } from "@plane/ui";
import { cn } from "@plane/utils";
// components
import { SidebarPropertyListItem } from "@/components/common/layout/sidebar/property-list-item";

type Props = {
  value: string | undefined;
  onChange: (value: string) => void;
  disabled: boolean;
  buttonClassName?: string;
};

export const IssueAIModelProperty = observer(function IssueAIModelProperty(props: Props) {
  const { value, onChange, disabled, buttonClassName } = props;
  const { t } = useTranslation();
  const selected = value || AI_MODEL_NONE;

  const options = AI_MODEL_OPTIONS.map((option) => ({
    value: option.value,
    query: `${option.value} ${option.provider ?? ""}`,
    content: (
      <div className="flex w-full items-center justify-between gap-2">
        <span className="truncate">{option.value}</span>
        {option.provider && <span className="text-placeholder">{option.provider}</span>}
      </div>
    ),
  }));

  return (
    <SidebarPropertyListItem icon={Bot} label={t("issue.ai_model.label")}>
      <CustomSearchSelect
        value={selected}
        onChange={(val: string) => {
          if (val !== selected) onChange(val);
        }}
        options={options}
        disabled={disabled}
        noResultsMessage={t("issue.ai_model.no_results")}
        className="h-7.5 w-full grow"
        customButtonClassName="h-7.5 w-full rounded-sm px-2 text-left"
        customButton={
          <span
            className={cn(
              "truncate text-body-xs-medium",
              selected === AI_MODEL_NONE && "text-placeholder",
              buttonClassName
            )}
          >
            {selected}
          </span>
        }
        maxHeight="lg"
      />
    </SidebarPropertyListItem>
  );
});
