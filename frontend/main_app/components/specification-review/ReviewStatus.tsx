"use client";

export interface ReviewStatusProps {
  status: string;
  hasUnsavedChanges?: boolean;
}

const descriptions: Record<string, { title: string; detail: string; tone: string }> = {
  source_waiting: { title: "Waiting for source analysis", detail: "Current email and attachment analysis is not ready yet. This view will check for updates.", tone: "bg-secondary text-foreground" },
  source_unavailable: { title: "Sources temporarily unavailable", detail: "Recommendations could not be checked. Your saved decisions and the previous PDF are retained.", tone: "bg-amber-50 text-amber-950" },
  needs_review: { title: "Needs review", detail: "Check the request, drawing choices and field recommendations, then save your corrections.", tone: "bg-amber-50 text-amber-950" },
  regenerating_pdf: { title: "Regenerating PDF", detail: "Saved decisions are waiting for their matching PDF. Completion is unavailable until publication succeeds.", tone: "bg-secondary text-foreground" },
  awaiting_final_review: { title: "Awaiting final review", detail: "Check the current published PDF before marking this specification complete.", tone: "bg-accent text-primary" },
  complete: { title: "Complete", detail: "The current published specification has been reviewed. A relevant source or decision change will reopen review.", tone: "bg-emerald-50 text-emerald-950" },
  publication_failed: { title: "Update failed; previous PDF available", detail: "Your decisions remain saved. Retry the update before completing the specification.", tone: "bg-amber-50 text-amber-950" },
  read_only: { title: "Read-only; retained PDF available", detail: "Editing and completion are unavailable in this view. You can still inspect the retained specification.", tone: "bg-secondary text-foreground" },
  review_required_again: { title: "Review required again", detail: "Relevant sources have changed. Your saved decisions are retained; check them against the new context.", tone: "bg-amber-50 text-amber-950" },
};

export default function ReviewStatus({ status, hasUnsavedChanges = false }: ReviewStatusProps) {
  const description = descriptions[status] ?? descriptions.needs_review;
  return (
    <div className={`rounded-xl border p-4 ${description.tone}`} role="status" aria-live="polite" aria-atomic="true">
      <p className="font-semibold">{description.title}</p>
      <p className="mt-1 text-sm">{description.detail}</p>
      {hasUnsavedChanges && <p className="mt-2 text-sm font-semibold">You have unsaved corrections. Save them and check the regenerated PDF before completion.</p>}
    </div>
  );
}
