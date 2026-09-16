/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useState } from "react";
import { cn, getDate } from "@plane/utils";

type Props = {
  // date part shown by the sibling date dropdown (YYYY-MM-DD)
  date: string | null | undefined;
  // precise moment (ISO, UTC)
  datetime: string | null | undefined;
  onChange: (datetime: string) => void;
  disabled?: boolean;
  ariaLabel?: string;
  className?: string;
};

const pad = (value: number) => value.toString().padStart(2, "0");

/** HH:MM:SS of an ISO datetime in the browser timezone */
export const getLocalTimeString = (datetime: string | null | undefined): string => {
  if (!datetime) return "";
  const value = new Date(datetime);
  if (isNaN(value.getTime())) return "";
  return `${pad(value.getHours())}:${pad(value.getMinutes())}:${pad(value.getSeconds())}`;
};

/** Combine the local day of `datetime` (or `date`) with a HH:MM[:SS] time; returns a UTC ISO string */
export const buildDatetimeWithTime = (
  date: string | null | undefined,
  datetime: string | null | undefined,
  time: string
): string | undefined => {
  const match = /^(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(time);
  if (!match) return undefined;
  const base = datetime ? new Date(datetime) : getDate(date);
  if (!base || isNaN(base.getTime())) return undefined;
  const result = new Date(
    base.getFullYear(),
    base.getMonth(),
    base.getDate(),
    Number(match[1]),
    Number(match[2]),
    Number(match[3] ?? 0)
  );
  return result.toISOString();
};

export function IssueDatetimeTimeInput(props: Props) {
  const { date, datetime, onChange, disabled = false, ariaLabel, className } = props;
  const savedTime = getLocalTimeString(datetime);
  const [value, setValue] = useState(savedTime);

  useEffect(() => {
    setValue(savedTime);
  }, [savedTime]);

  // the time only makes sense together with a date
  if (!date && !datetime) return null;

  if (disabled)
    return <span className={cn("shrink-0 text-body-xs-regular text-secondary", className)}>{savedTime}</span>;

  const commit = () => {
    if (value === savedTime) return;
    const nextDatetime = buildDatetimeWithTime(date, datetime, value);
    if (!nextDatetime) {
      setValue(savedTime);
      return;
    }
    onChange(nextDatetime);
  };

  return (
    <input
      type="time"
      step={1}
      value={value}
      aria-label={ariaLabel}
      onChange={(e) => setValue(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.currentTarget.blur();
      }}
      className={cn(
        "h-7.5 shrink-0 rounded-sm bg-transparent px-1 text-body-xs-regular text-secondary outline-none hover:bg-layer-transparent-hover focus:bg-layer-transparent-hover",
        className
      )}
    />
  );
}
