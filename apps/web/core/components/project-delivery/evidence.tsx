import { Dialog } from "@headlessui/react";
import type { ReactNode } from "react";
import { Link } from "react-router";
import type { TWorkflowRecord } from "@/services/workflow-v2.service";
import { valueLabel } from "./utils";

export function Evidence({ value }: { value: unknown }) {
  if (value == null) return <span className="text-tertiary">Unknown / not recorded</span>;
  if (Array.isArray(value))
    return value.length ? (
      <ul className="space-y-2">
        {value.map((item) => (
          <li key={JSON.stringify(item)} className="rounded border border-subtle p-2">
            <Evidence value={item} />
          </li>
        ))}
      </ul>
    ) : (
      <span className="text-tertiary">None recorded</span>
    );
  if (typeof value === "object")
    return (
      <dl className="space-y-2">
        {Object.entries(value).map(([key, item]) => (
          <div key={key}>
            <dt className="text-11 font-medium text-tertiary">{key.replaceAll("_", " ")}</dt>
            <dd className="text-13 break-words whitespace-pre-wrap">
              <Evidence value={item} />
            </dd>
          </div>
        ))}
      </dl>
    );
  // Evidence URLs are display-only unless they use a safe public web scheme.
  if (typeof value === "string" && /^https?:\/\//.test(value))
    return (
      <a className="break-all text-accent-primary underline" href={value} target="_blank" rel="noopener noreferrer">
        {value}
      </a>
    );
  return <span className="break-all">{valueLabel(value)}</span>;
}

export function EvidenceDrawer({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  return (
    <Dialog open onClose={onClose} className="relative z-50">
      <div className="fixed inset-0 bg-black/30" aria-hidden="true" />
      <div className="fixed inset-0 flex justify-end">
        <Dialog.Panel className="shadow-lg flex h-full w-full max-w-2xl flex-col bg-surface-1">
          <div className="flex items-center justify-between gap-4 border-b border-subtle p-5">
            <Dialog.Title className="text-18 font-semibold">{title}</Dialog.Title>
            <button
              type="button"
              className="rounded border border-subtle px-3 py-1"
              onClick={onClose}
              aria-label="Close details"
            >
              Close
            </button>
          </div>
          <div className="overflow-y-auto p-5">{children}</div>
        </Dialog.Panel>
      </div>
    </Dialog>
  );
}

export function WorkLinks({
  ids,
  records,
  workspace,
  project,
  verifiedIds = [],
}: {
  ids: string[];
  records: TWorkflowRecord[];
  workspace: string;
  project: string;
  verifiedIds?: string[];
}) {
  if (!ids.length) return <p>No applicable work.</p>;
  return (
    <ul className="space-y-3">
      {[...new Set(ids)].map((id) => {
        const work = records.find((record) => record.id === id);
        return (
          <li key={id} className="rounded border border-subtle p-3">
            <Link to={`/${workspace}/projects/${project}/issues/${id}`} className="text-accent-primary underline">
              {work?.name ?? id}
            </Link>
            <p className="text-13 text-secondary">
              {verifiedIds.includes(id) ? "Included in verified numerator" : "Not included in verified numerator"}
            </p>
            {work && (
              <Evidence
                value={Object.fromEntries(
                  Object.entries(work).filter(([key]) =>
                    ["execution_kind", "scope_revision", "next_action", "state", "evidence", "reasons"].includes(key)
                  )
                )}
              />
            )}
          </li>
        );
      })}
    </ul>
  );
}
