/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { add } from "date-fns";
import { Bot } from "lucide-react";
import { Controller, useForm } from "react-hook-form";
import { observer } from "mobx-react";
// plane imports
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import type { IApiToken, IWorkspaceAIBotMemberCreateData } from "@plane/types";
import { CustomSelect, EModalPosition, EModalWidth, Input, ModalCore } from "@plane/ui";
// components
import { GeneratedTokenDetails } from "@/components/api-token/modal/generated-token-details";
// hooks
import { useMember } from "@/hooks/store/use-member";

type Props = {
  isOpen: boolean;
  onClose: () => void;
  workspaceSlug: string;
};

type FormValues = {
  display_name: string;
  email: string;
  token_label: string;
  expiry: "1_year" | "never";
};

const defaultValues: FormValues = {
  display_name: "",
  email: "",
  token_label: "",
  expiry: "1_year",
};

const getOneYearExpiry = () => add(new Date(), { years: 1 }).toISOString();

const getErrorMessage = (error: unknown) => {
  if (typeof error === "string") return error;
  if (!error || typeof error !== "object") return "AI bot could not be created. Please try again.";

  const response = error as Record<string, string | string[] | undefined>;
  const firstError = Object.values(response).find(Boolean);
  if (Array.isArray(firstError)) return firstError[0];
  return firstError ?? "AI bot could not be created. Please try again.";
};

export const CreateAIBotMemberModal = observer(function CreateAIBotMemberModal(props: Props) {
  const { isOpen, onClose, workspaceSlug } = props;
  // states
  const [generatedToken, setGeneratedToken] = useState<IApiToken | null>(null);
  // store hooks
  const {
    workspace: { createAIBotMember },
  } = useMember();
  // form info
  const {
    control,
    formState: { errors, isSubmitting },
    handleSubmit,
    register,
    reset,
  } = useForm<FormValues>({ defaultValues });

  const handleClose = () => {
    onClose();
    const timeout = setTimeout(() => {
      setGeneratedToken(null);
      reset(defaultValues);
      clearTimeout(timeout);
    }, 300);
  };

  const onSubmit = async (formData: FormValues) => {
    const payload: IWorkspaceAIBotMemberCreateData = {
      display_name: formData.display_name.trim(),
      email: formData.email.trim() || undefined,
      token_label: formData.token_label.trim() || undefined,
      expired_at: formData.expiry === "never" ? null : getOneYearExpiry(),
    };

    try {
      const response = await createAIBotMember(workspaceSlug, payload);
      setGeneratedToken(response.api_token);
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: "Success!",
        message: "AI bot member created.",
      });
    } catch (error: unknown) {
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Error!",
        message: getErrorMessage(error),
      });
    }
  };

  return (
    <ModalCore isOpen={isOpen} handleClose={handleClose} position={EModalPosition.CENTER} width={EModalWidth.LG}>
      {generatedToken ? (
        <GeneratedTokenDetails tokenDetails={generatedToken} handleClose={handleClose} />
      ) : (
        <form onSubmit={handleSubmit(onSubmit)}>
          <div className="space-y-5 p-5">
            <div className="flex items-center gap-2">
              <Bot className="h-4 w-4 text-secondary" />
              <h3 className="text-16 leading-6 font-medium text-primary">Create AI bot</h3>
            </div>
            <div className="space-y-3">
              <div className="space-y-1">
                <Input
                  type="text"
                  placeholder="Display name"
                  hasError={Boolean(errors.display_name)}
                  className="w-full text-14"
                  {...register("display_name", {
                    required: "Display name is required.",
                    maxLength: {
                      value: 255,
                      message: "Display name should be less than 255 characters.",
                    },
                    validate: (value) => value.trim() !== "" || "Display name is required.",
                  })}
                />
                {errors.display_name && (
                  <span className="text-11 text-danger-primary">{errors.display_name.message}</span>
                )}
              </div>
              <div className="space-y-1">
                <Input
                  type="email"
                  placeholder="Email address"
                  hasError={Boolean(errors.email)}
                  className="w-full text-14"
                  {...register("email")}
                />
                {errors.email && <span className="text-11 text-danger-primary">{errors.email.message}</span>}
              </div>
              <Input
                type="text"
                placeholder="Token label"
                className="w-full text-14"
                {...register("token_label", {
                  maxLength: {
                    value: 255,
                    message: "Token label should be less than 255 characters.",
                  },
                })}
              />
              <Controller
                control={control}
                name="expiry"
                render={({ field: { value, onChange } }) => (
                  <CustomSelect
                    value={value}
                    onChange={onChange}
                    label={<span>{value === "never" ? "Never expires" : "Expires in 1 year"}</span>}
                    buttonClassName="w-full !justify-start border border-subtle"
                    className="w-full"
                    input
                  >
                    <CustomSelect.Option value="1_year">Expires in 1 year</CustomSelect.Option>
                    <CustomSelect.Option value="never">Never expires</CustomSelect.Option>
                  </CustomSelect>
                )}
              />
            </div>
          </div>
          <div className="flex items-center justify-end gap-2 border-t-[0.5px] border-subtle px-5 py-4">
            <Button variant="secondary" onClick={handleClose}>
              Cancel
            </Button>
            <Button variant="primary" type="submit" loading={isSubmitting}>
              {isSubmitting ? "Creating..." : "Create bot"}
            </Button>
          </div>
        </form>
      )}
    </ModalCore>
  );
});
