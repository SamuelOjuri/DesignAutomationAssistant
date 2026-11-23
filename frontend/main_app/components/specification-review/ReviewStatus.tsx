"use client";

import { CircleCheck, ClipboardCheck, Info, LoaderCircle, Lock, TriangleAlert, type LucideIcon } from "lucide-react";
import { getReviewStatusDescription, type ReviewStatusContext, type ReviewStatusTone } from "@/lib/specification-review-status";
import { cn } from "@/lib/utils";

export interface ReviewStatusProps extends ReviewStatusContext {
  hasUnsavedChanges?: boolean;
  className?: string;
}

const toneClasses: Record<ReviewStatusTone, { panel: string; icon: string; fallback: LucideIcon }> = {
  neutral: { panel: "border-border bg-card text-foreground", icon: "text-muted-foreground", fallback: Info },
  warning: { panel: "border-warning/40 bg-warning/10 text-warning-foreground", icon: "text-warning", fallback: TriangleAlert },
  info: { panel: "border-info/30 bg-info/10 text-info-foreground", icon: "text-info", fallback: ClipboardCheck },
  success: { panel: "border-success/30 bg-success/10 text-success-foreground", icon: "text-success", fallback: CircleCheck },
};
const inProgress = new Set(["preparing_draft", "publishing_pdf", "source_waiting", "analysis_pending", "regenerating_pdf"]);

export default function ReviewStatus({ hasUnsavedChanges = false, className, ...context }: ReviewStatusProps) {
  const { status, analysis } = context;
  const description = getReviewStatusDescription(context);
  const tone = toneClasses[description.tone];
  const spinning = inProgress.has(status);
  const Icon = status === "read_only" ? Lock : spinning ? LoaderCircle : tone.fallback;
  return (
    <div className={cn("flex gap-3 rounded-xl border p-4 shadow-xs", tone.panel, className)} role="status" aria-live="polite" aria-atomic="true">
      <Icon className={cn("mt-0.5 size-5 shrink-0", tone.icon, spinning && "motion-safe:animate-spin")} aria-hidden="true" />
      <div className="min-w-0 space-y-1">
        <p className="font-semibold leading-snug">{description.title}</p>
        <p className="text-sm leading-relaxed">{description.detail}</p>
        {analysis?.updated_at && !analysis.ready && <p className="text-xs opacity-80">Last worker update: <time dateTime={analysis.updated_at}>{new Date(analysis.updated_at).toLocaleString("en-GB", { timeZone: "Europe/London" })}</time></p>}
        {hasUnsavedChanges && <p className="pt-1 text-sm font-semibold">{status === "read_only"
          ? "Your unsaved corrections remain in this tab. Editing is currently unavailable."
          : "You have unsaved corrections. Save them and check the regenerated PDF before completion."}</p>}
      </div>
    </div>
  );
}
