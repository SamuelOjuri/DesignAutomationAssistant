"use client";

import { getReviewStatusDescription } from "@/lib/specification-review-status";

export interface ReviewStatusProps {
  status: string;
  scopeReason?: string | null;
  hasSource?: boolean;
  hasPdf?: boolean;
  hasUnsavedChanges?: boolean;
}

export default function ReviewStatus({ status, scopeReason, hasSource = false, hasPdf = false, hasUnsavedChanges = false }: ReviewStatusProps) {
  const description = getReviewStatusDescription({ status, scopeReason, hasSource, hasPdf });
  return (
    <div className={`rounded-xl border p-4 ${description.tone}`} role="status" aria-live="polite" aria-atomic="true">
      <p className="font-semibold">{description.title}</p>
      <p className="mt-1 text-sm">{description.detail}</p>
      {hasUnsavedChanges && <p className="mt-2 text-sm font-semibold">{status === "read_only"
        ? "Your unsaved corrections remain in this tab. Editing is currently unavailable."
        : "You have unsaved corrections. Save them and check the regenerated PDF before completion."}</p>}
    </div>
  );
}
