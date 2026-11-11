import type { AnalysisProgress } from "./specification-review-api.ts";

export interface ReviewStatusContext {
  status: string;
  scopeReason?: string | null;
  hasSource?: boolean;
  hasPdf?: boolean;
  analysis?: AnalysisProgress | null;
}

export interface ReviewStatusDescription {
  title: string;
  detail: string;
  tone: string;
}

const descriptions: Record<string, ReviewStatusDescription> = {
  source_waiting: { title: "Waiting for source analysis", detail: "Current email and attachment analysis is not ready yet. This view will check for updates.", tone: "bg-secondary text-foreground" },
  analysis_pending: { title: "Validated fields available", detail: "The current email and attachment fields are shown below. Matching and document preparation are still pending. Editing and completion become available when analysis finishes.", tone: "bg-secondary text-foreground" },
  source_unavailable: { title: "Sources temporarily unavailable", detail: "Recommendations could not be checked. Your saved decisions and the previous PDF are retained.", tone: "bg-amber-50 text-amber-950" },
  needs_review: { title: "Needs review", detail: "Check the request, drawing choices and field recommendations, then save your corrections.", tone: "bg-amber-50 text-amber-950" },
  regenerating_pdf: { title: "Regenerating PDF", detail: "Saved decisions are waiting for their matching PDF. Completion is unavailable until publication succeeds.", tone: "bg-secondary text-foreground" },
  awaiting_final_review: { title: "Awaiting final review", detail: "Check the current published PDF before marking this specification complete.", tone: "bg-accent text-primary" },
  complete: { title: "Complete", detail: "The current published specification has been reviewed. A relevant source or decision change will reopen review.", tone: "bg-emerald-50 text-emerald-950" },
  publication_failed: { title: "Update failed; previous PDF available", detail: "Your decisions remain saved. Retry the update before completing the specification.", tone: "bg-amber-50 text-amber-950" },
  review_required_again: { title: "Review required again", detail: "Relevant sources have changed. Your saved decisions are retained; check them against the new context.", tone: "bg-amber-50 text-amber-950" },
};

const readOnlyReasons: Record<string, { title: string; reason: string }> = {
  outside_landing_zone: { title: "Outside Landing Zone", reason: "This item is outside Landing Zone." },
  completed_folder: { title: "Completed Folder", reason: "This item is in Completed Folder." },
  review_disabled: { title: "Review access disabled", reason: "Specification review access is disabled." },
  processing_off: { title: "Specification processing off", reason: "Specification processing is turned off." },
  item_not_allowlisted: { title: "Not included in specification pilot", reason: "This item is not included in the current specification pilot." },
  board_not_managed: { title: "Outside review board", reason: "This board is not configured for Specification Review." },
  item_archived: { title: "Archived item", reason: "This item is archived." },
  item_deleted: { title: "Deleted item", reason: "This item is deleted." },
};

export function getReviewStatusDescription({
  status, scopeReason, hasSource = false, hasPdf = false, analysis,
}: ReviewStatusContext): ReviewStatusDescription {
  if (status === "read_only") {
    const scope = scopeReason ? readOnlyReasons[scopeReason] : undefined;
    return {
      title: scope?.title ?? "Read-only",
      detail: [
        scope?.reason,
        "Editing and completion are unavailable in this view.",
        hasSource ? "Available source recommendations remain readable." : "Source analysis is unavailable in this view.",
        "Any saved decisions are retained.",
        hasPdf ? "The retained PDF remains available to inspect." : "No PDF is available in this view.",
      ].filter(Boolean).join(" "),
      tone: "bg-secondary text-foreground",
    };
  }
  if (status === "source_unavailable" && !hasPdf) {
    return { ...descriptions.source_unavailable,
      detail: "Recommendations could not be checked. Any saved decisions are retained. No PDF is available in this view." };
  }
  if (status === "publication_failed" && !hasPdf) {
    return { ...descriptions.publication_failed, title: "Update failed",
      detail: "Your decisions remain saved. No PDF is available in this view. Retry the update before completing the specification." };
  }
  if (analysis && !analysis.ready && ["source_waiting", "analysis_pending"].includes(status)) {
    const stage = analysis.stage === "matching" ? "Matching projects and drawings"
      : analysis.stage === "rendering" ? "Preparing design documents"
      : analysis.phase === "reading_sources" ? "Reading email and attachments"
      : analysis.phase === "extracting_fields" ? "Extracting and validating specification fields"
      : "Waiting for current analysis";
    const counts = analysis.source_counts;
    const completed = (counts.read || 0) + (counts.skipped || 0) + (counts.review || 0) + (counts.failed || 0);
    const observed = counts.total ? ` ${completed} of ${counts.total} sources processed (${counts.read || 0} read, ${counts.skipped || 0} skipped${counts.review ? `, ${counts.review} need review` : ""}${counts.failed ? `, ${counts.failed} failed` : ""}).` : "";
    const state = ["failed", "retry_wait"].includes(analysis.status)
      ? " The latest attempt did not finish; waiting for a retry."
      : analysis.status === "running" ? " This is the latest recorded worker stage." : " Waiting for the worker to continue.";
    return { ...(descriptions[status] ?? descriptions.source_waiting),
      detail: `${stage}.${observed}${state} ${status === "analysis_pending" ? descriptions.analysis_pending.detail : "This view will check for updates."}` };
  }
  return { ...(descriptions[status] ?? descriptions.needs_review) };
}
