import type { AnalysisProgress, ReviewStatusSnapshot } from "./specification-review-api.ts";

export interface ReviewStatusContext {
  status: string;
  scopeReason?: string | null;
  hasSource?: boolean;
  hasPdf?: boolean;
  analysis?: AnalysisProgress | null;
  queue?: ReviewStatusSnapshot["queue"];
}

export type ReviewStatusTone = "neutral" | "warning" | "info" | "success";

export interface ReviewStatusDescription {
  title: string;
  detail: string;
  tone: ReviewStatusTone;
  showSpinner?: boolean;
}

const descriptions: Record<string, ReviewStatusDescription> = {
  preparing_draft: { title: "Draft PDF check pending", detail: "The app will check whether the current recommendations and saved corrections need a new draft.", tone: "neutral" },
  ready_to_publish: { title: "Unpublished changes ready for review", detail: "Inspect the draft and recommended changes, then publish when ready. Opening this view and saving corrections do not publish in active groups.", tone: "warning" },
  changes_recommended: { title: "Publication changes recommended", detail: "Review the latest recommendations. The published PDF remains unchanged until you publish an updated draft.", tone: "warning" },
  publishing_pdf: { title: "Publishing PDF", detail: "Publishing the draft you requested. The previous PDF is retained until the replacement succeeds.", tone: "neutral" },
  source_waiting: { title: "Waiting for source analysis", detail: "Current email and attachment analysis is not ready yet. This view will check for updates.", tone: "neutral" },
  analysis_pending: { title: "Validated fields available", detail: "The current email and attachment fields are shown below. Matching and document preparation are still pending. Editing and completion become available when analysis finishes.", tone: "neutral" },
  source_unavailable: { title: "Sources temporarily unavailable", detail: "Recommendations could not be checked. Your saved decisions and the previous PDF are retained.", tone: "warning" },
  needs_review: { title: "Needs review", detail: "Check the request, drawing choices and field recommendations, then save your corrections.", tone: "warning" },
  regenerating_pdf: { title: "Regenerating PDF", detail: "Saved decisions are waiting for their matching PDF. Completion is unavailable until publication succeeds.", tone: "neutral" },
  awaiting_final_review: { title: "Awaiting final review", detail: "Check the current published PDF before marking this specification complete.", tone: "info" },
  complete: { title: "Complete", detail: "The current published specification has been reviewed. A relevant source or decision change will reopen review.", tone: "success" },
  publication_failed: { title: "Update failed; previous PDF available", detail: "Your decisions remain saved. Retry the update before completing the specification.", tone: "warning" },
  review_required_again: { title: "Review required again", detail: "Relevant sources have changed. Your saved decisions are retained; check them against the new context.", tone: "warning" },
};

const readOnlyReasons: Record<string, { title: string; reason: string }> = {
  group_not_eligible: { title: "Outside specification groups", reason: "This item is outside Landing Zone and the configured active groups." },
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
  status, scopeReason, hasSource = false, hasPdf = false, analysis, queue,
}: ReviewStatusContext): ReviewStatusDescription {
  if (status === "preparing_draft") {
    let description = descriptions.preparing_draft;
    if (queue?.running === false) {
      description = { ...description, title: "Draft PDF check queued" };
    } else if (queue?.running && queue.phase === "rendering") {
      description = { ...description, title: "Generating draft PDF",
        detail: "A new draft is being generated from the current recommendations and saved corrections." };
    } else if (queue?.running) {
      description = { ...description, title: "Checking specification PDF",
        detail: "The app is checking whether the current recommendations and saved corrections need a new draft." };
    }
    return { ...description, detail: hasPdf ? `${description.detail} You can inspect the available PDF in the preview.` : description.detail };
  }
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
      tone: "neutral",
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
    if (analysis.status === "failed") {
      return { title: "Source analysis stopped", tone: "warning", showSpinner: false,
        detail: `The latest attempt ended before analysis finished.${observed} Contact the processing team with this item ID.` };
    }
    if (analysis.status === "retry_wait") {
      return { title: "Retry scheduled", tone: "neutral", showSpinner: false,
        detail: `The worker will retry source analysis.${observed} This view will check for updates.` };
    }
    const state = analysis.status === "running" ? " This is the latest recorded worker stage." : " Waiting for the worker to continue.";
    return { ...(descriptions[status] ?? descriptions.source_waiting),
      detail: `${stage}.${observed}${state} ${status === "analysis_pending" ? descriptions.analysis_pending.detail : "This view will check for updates."}` };
  }
  return { ...(descriptions[status] ?? descriptions.needs_review) };
}
