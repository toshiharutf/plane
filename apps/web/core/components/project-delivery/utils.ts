import type { TProgressMeasure, TWorkflowRecord } from "@/services/workflow-v2.service";

export function measureLabel(measure: TProgressMeasure): string {
  if (!measure.denominator) return "No applicable work";
  if (measure.numerator == null || ["unknown", "unverified", "revoked"].includes(measure.status ?? ""))
    return "Unverified";
  return `${measure.numerator} / ${measure.denominator} items`;
}
export function valueLabel(value: unknown): string {
  if (value == null || value === "") return "Unknown";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}
export function amountLabel(value: unknown, unit: "USD" | "min"): string {
  if (value == null || value === "" || !Number.isFinite(Number(value))) return "Unknown";
  return unit === "USD" ? `$${Number(value).toFixed(2)}` : `${Number(value).toFixed(1)} min`;
}
export function commandError(error: unknown): string {
  const data = error as { error?: string; code?: string; detail?: string; details?: unknown };
  return [
    data?.error ?? data?.detail ?? "The command could not be confirmed. Refresh before retrying.",
    data?.code,
    data?.details ? valueLabel(data.details) : undefined,
  ]
    .filter(Boolean)
    .join(" · ");
}
export function canAct(record: TWorkflowRecord, action: string, stale: boolean, now = Date.now()): boolean {
  if (stale || !record.allowed_actions?.includes(action)) return false;
  if (action === "resolve" && !["pending", "clarify"].includes(String(record.decision))) return false;
  if (
    action === "retry" &&
    (record.state !== "Recovery Required" ||
      (record.diagnosis as { cause?: string } | undefined)?.cause !== "non_code" ||
      record.discarded)
  )
    return false;
  if (record.expires_at && new Date(String(record.expires_at)).getTime() <= now) return false;
  if (record.revoked || record.revoked_at) return false;
  return true;
}
export function recordVersion(record: TWorkflowRecord): number | undefined {
  const version = record.state_version ?? record.version;
  return typeof version === "number" ? version : undefined;
}
export function metricIds(measure: TProgressMeasure): string[] {
  return [...new Set(measure.denominator_ids)];
}

export function localDateTime(value: unknown): string {
  if (typeof value !== "string" || !Number.isFinite(Date.parse(value))) return "";
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
}
