"use client";

import { useId, type MouseEvent } from "react";
import { ArrowRight } from "lucide-react";
import { summarizeFieldOverview, type FieldOverviewTone } from "@/lib/specification-review-state";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export interface FieldOverviewEntry {
  label: string;
  anchorId: string;
  tone?: FieldOverviewTone;
}

export interface FieldOverviewProps {
  entries: FieldOverviewEntry[];
  /** False while saved completion state is unavailable, e.g. read-only or analysis pending. */
  showStatus: boolean;
}

const tones: Record<FieldOverviewTone, { dot: string; chip: string; status: string; count: (total: number) => string }> = {
  attention: { dot: "bg-warning", chip: "border-warning/50 bg-warning/10 hover:bg-warning/20", status: "needs attention", count: (total) => total === 1 ? "needs attention" : "need attention" },
  awaiting: { dot: "bg-info", chip: "", status: "awaiting decision", count: () => "awaiting decision" },
  unsaved: { dot: "bg-primary", chip: "border-primary/60", status: "unsaved correction", count: () => "unsaved" },
  saved: { dot: "bg-success", chip: "text-muted-foreground", status: "decision saved", count: () => "saved" },
  optional: { dot: "border border-muted-foreground/60", chip: "", status: "optional", count: () => "optional" },
};
const legendOrder: FieldOverviewTone[] = ["attention", "awaiting", "unsaved", "saved", "optional"];
const progressOrder: FieldOverviewTone[] = ["saved", "optional", "unsaved", "awaiting", "attention"];

function Dot({ tone }: { tone: FieldOverviewTone }) {
  return <span className={cn("size-2 shrink-0 rounded-full", tones[tone].dot)} aria-hidden="true" />;
}

/** Scroll to a field card and focus it so keyboard users continue from that field. */
export function jumpToField(anchorId: string, event?: MouseEvent) {
  const target = document.getElementById(anchorId);
  if (!target) return;
  event?.preventDefault();
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  target.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  target.focus({ preventScroll: true });
}

export default function FieldOverview({ entries, showStatus }: FieldOverviewProps) {
  const id = useId();
  const { counts, ready, next } = summarizeFieldOverview(entries);
  return (
    <nav aria-labelledby={`${id}-title`} className="rounded-xl border bg-card p-4 shadow-xs">
      <div className="flex flex-wrap items-start gap-x-4 gap-y-3">
        <div className="min-w-0 flex-1">
          <h3 id={`${id}-title`} className="font-semibold leading-tight">Field overview</h3>
          <p className="mt-1 text-sm text-muted-foreground">{showStatus ? `${ready} of ${entries.length} fields ready. ` : ""}Select a field to jump to it.</p>
        </div>
        {showStatus && next && (
          <Button type="button" variant="outline" size="sm" onClick={() => jumpToField(next.anchorId)}>
            Next to review: {next.label}<ArrowRight aria-hidden="true" />
          </Button>
        )}
      </div>
      {showStatus && (
        <>
          <div className="mt-3 flex h-1.5 gap-0.5 overflow-hidden rounded-full bg-muted" aria-hidden="true">
            {progressOrder.filter((tone) => counts[tone]).map((tone) => (
              <span key={tone} className={cn("h-full basis-0", tone === "optional" ? "bg-muted-foreground/30" : tones[tone].dot)} style={{ flexGrow: counts[tone] }} />
            ))}
          </div>
          <ul className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" aria-label="Field status summary">
            {legendOrder.map((tone) => (
              <li key={tone} className="flex items-center gap-1.5"><Dot tone={tone} /><span className="font-semibold tabular-nums text-foreground">{counts[tone]}</span> {tones[tone].count(counts[tone])}</li>
            ))}
          </ul>
        </>
      )}
      <ul className="mt-3 flex flex-wrap gap-1.5" aria-label="Fields">
        {entries.map((entry) => (
          <li key={entry.anchorId}>
            <a href={`#${entry.anchorId}`} onClick={(event) => jumpToField(entry.anchorId, event)}
              className={cn("inline-flex h-7 items-center gap-1.5 rounded-full border bg-card px-2.5 text-xs font-medium text-foreground transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50", entry.tone && tones[entry.tone].chip)}>
              {entry.tone && <Dot tone={entry.tone} />}{entry.label}
              {entry.tone && <span className="sr-only">: {tones[entry.tone].status}</span>}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
