"use client";

import { getReviewStatusDescription } from "@/lib/specification-review-status";
import type { AnalysisProgress } from "@/lib/specification-review-api";

export interface ReviewStatusProps {
  status: string;
  scopeReason?: string | null;
  hasSource?: boolean;
  hasPdf?: boolean;
  hasUnsavedChanges?: boolean;
  analysis?: AnalysisProgress | null;
}

export default function ReviewStatus({ status, scopeReason, hasSource = false, hasPdf = false, hasUnsavedChanges = false, analysis }: ReviewStatusProps) {
  const description = getReviewStatusDescription({ status, scopeReason, hasSource, hasPdf, analysis });
  return (
    <div className={`rounded-xl border p-4 ${description.tone}`} role="status" aria-live="polite" aria-atomic="true">
      <p className="font-semibold">{description.title}</p>
      <p className="mt-1 text-sm">{description.detail}</p>
      {analysis?.updated_at && !analysis.ready && <p className="mt-1 text-xs">Last worker update: <time dateTime={analysis.updated_at}>{new Date(analysis.updated_at).toLocaleString("en-GB", { timeZone: "Europe/London" })}</time></p>}
      {hasUnsavedChanges && <p className="mt-2 text-sm font-semibold">{status === "read_only"
        ? "Your unsaved corrections remain in this tab. Editing is currently unavailable."
        : "You have unsaved corrections. Save them and check the regenerated PDF before completion."}</p>}
    </div>
  );
}
