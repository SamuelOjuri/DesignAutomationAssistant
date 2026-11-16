"use client";

import { useId, type ReactNode } from "react";
import { Ban, Check, ChevronRight, Database, Info, Mail, PencilLine, RotateCcw, TriangleAlert, type LucideIcon } from "lucide-react";
import type { FieldOperation, ReviewField } from "@/lib/specification-review-api";
import type { FieldReviewState } from "@/lib/specification-review-state";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

export interface FieldEditorProps {
  label: string;
  field: ReviewField;
  operation?: FieldOperation;
  /** Saved completion state; omit when the review cannot currently be completed. */
  reviewState?: FieldReviewState;
  disabled?: boolean;
  onChange: (operation: FieldOperation) => void;
}

const stateBadges: Record<FieldReviewState, { text: string; className: string }> = {
  reconfirm: { text: "Reconfirm needed", className: "border-warning/40 bg-warning/10 text-warning-foreground" },
  resolve: { text: "Needs resolution", className: "border-warning/40 bg-warning/10 text-warning-foreground" },
  decide: { text: "Awaiting decision", className: "border-info/30 bg-info/10 text-info-foreground" },
  saved: { text: "Decision saved", className: "border-success/30 bg-success/10 text-success-foreground" },
  optional: { text: "Optional", className: "border-border bg-muted text-muted-foreground" },
};

export const evidenceSummaryClass = "inline-flex cursor-pointer list-none items-center gap-1 rounded-sm font-medium text-muted-foreground hover:text-foreground focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden";

function display(value: string | null): string {
  return value === null ? "Not assigned" : value === "" ? "Intentionally blank" : value;
}

function ValuePanel({ eyebrow, value, source, className, children }: {
  eyebrow: string; value: string | null; source: string; className?: string; children?: ReactNode;
}) {
  const placeholder = value === null || value === "";
  return (
    <div className={cn("min-w-0 p-3", className)}>
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{eyebrow}</p>
      <p className={cn("mt-1 whitespace-pre-wrap text-base [overflow-wrap:anywhere]", placeholder ? "italic text-muted-foreground" : "font-semibold")}>{display(value)}</p>
      <p className="mt-0.5 text-sm text-muted-foreground">Source: <span className="font-medium text-foreground">{source}</span></p>
      {children}
    </div>
  );
}

function EvidenceColumn({ icon: Icon, title, children }: { icon: LucideIcon; title: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <h3 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        <Icon className="size-3.5" aria-hidden="true" />{title}
      </h3>
      {children}
    </div>
  );
}

export default function FieldEditor({ label, field, operation, reviewState, disabled = false, onChange }: FieldEditorProps) {
  const id = useId();
  const textId = `${id}-value`;
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const current = field.effective;
  const invalid = operation?.operation === "specify_value" && !operation.display_value.trim();
  const proposedSource = field.proposal.source === "email_context" ? "Email Context"
    : field.proposal.source === "monday_crm" ? "Monday CRM recommendation"
      : field.proposal.source === "derived" ? "Derived from Drawing Reference"
        : field.proposal.source === "default_blank" ? "Default blank"
          : field.proposal.source === "unassigned" ? "Not assigned" : "Unavailable";
  const selectedOperation = operation?.operation ?? field.decision?.decision.operation;
  const hasEvidence = field.email_facts.length > 0 || field.crm_recommendations.length > 0;
  const needsAttention = reviewState === "reconfirm" || reviewState === "resolve";
  const badge = reviewState ? stateBadges[reviewState] : undefined;
  const actions: { operation: FieldOperation["operation"]; text: string; icon: LucideIcon; title: string; unavailable?: boolean; next: () => FieldOperation }[] = [
    { operation: "accept_recommendation", text: "Use recommendation", icon: Check, title: "Accept the current recommendation and keep its source",
      unavailable: field.proposal.source === "unavailable", next: () => ({ operation: "accept_recommendation", proposal_id: field.proposal.proposal_id }) },
    { operation: "specify_value", text: "Specify value", icon: PencilLine, title: "Enter your own value (Source: Reviewer Specified)",
      next: () => ({ operation: "specify_value", display_value: operation?.operation === "specify_value" ? operation.display_value : current.display_value ?? "" }) },
    { operation: "specify_blank", text: "Specify blank", icon: Ban, title: "Leave this field intentionally blank (Source: Reviewer Specified)",
      next: () => ({ operation: "specify_blank" }) },
    { operation: "restore_recommendation", text: "Restore recommendation", icon: RotateCcw, title: "Remove the saved override and use the current recommendation",
      next: () => ({ operation: "restore_recommendation" }) },
  ];

  return (
    <fieldset aria-describedby={hintId}
      className={cn("relative min-w-0 overflow-hidden rounded-xl border bg-card text-card-foreground shadow-xs",
        needsAttention && "border-warning/50", operation && "border-primary/50 ring-1 ring-primary/10")}>
      {/* A floated legend renders inside the card while still naming the fieldset. */}
      <legend className="float-left w-full border-b bg-muted/40 px-4 py-2.5 text-base font-semibold leading-6 sm:pr-80">{label}</legend>
      {(badge || operation) && (
        <div className="clear-both flex flex-wrap gap-1.5 px-4 pt-3 sm:absolute sm:right-4 sm:top-3 sm:justify-end sm:p-0">
          {badge && <Badge variant="outline" className={badge.className}><span className="size-1.5 rounded-full bg-current" aria-hidden="true" />{badge.text}</Badge>}
          {operation && <Badge>Unsaved correction</Badge>}
        </div>
      )}

      <div className="clear-both space-y-3 p-4">
        {current.requires_reconfirmation && (
          <Alert variant="warning" role="status" className="py-3 [&>svg]:top-3.5">
            <TriangleAlert className="size-4" aria-hidden="true" />
            <AlertDescription>Sources or the selected project/drawing have changed. Check this saved decision and choose an action again to confirm it.</AlertDescription>
          </Alert>
        )}

        <div className="overflow-hidden rounded-lg border">
          <div className="grid sm:grid-cols-2">
            <ValuePanel eyebrow="Current saved value" value={current.display_value} source={current.source_label}>
              {current.reviewer_accepted && <p className="mt-1.5 flex items-center gap-1.5 text-xs font-medium text-success-foreground"><Check className="size-3.5" aria-hidden="true" />Recommendation accepted by reviewer</p>}
              {current.cad_mapping.status === "unmapped" && current.display_value && (
                <p className="mt-1.5 flex items-start gap-1.5 text-xs text-muted-foreground"><Info className="mt-px size-3.5 shrink-0" aria-hidden="true" />CAD mapping is unavailable for this saved value. Its PDF display text is preserved.</p>
              )}
            </ValuePanel>
            <ValuePanel eyebrow="Current recommendation" value={field.proposal.display_value} source={proposedSource} className="border-t bg-muted/40 sm:border-l sm:border-t-0">
              {field.proposal.rationale && <p className="mt-1.5 text-sm text-muted-foreground [overflow-wrap:anywhere]">{field.proposal.rationale}</p>}
              {field.scope && <p className="mt-0.5 text-sm text-muted-foreground">Applies to: {field.scope}</p>}
            </ValuePanel>
          </div>
          <div className="border-t px-3 py-2.5 text-sm">
            {hasEvidence ? (
              <div className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
                <EvidenceColumn icon={Mail} title="Email value">
                  {field.email_facts.length ? field.email_facts.map((fact, index) => (
                    <div key={index} className="mt-1">
                      <p className="whitespace-pre-wrap font-medium [overflow-wrap:anywhere]">{fact.value}</p>
                      <details className="group text-xs">
                        <summary className={evidenceSummaryClass}><ChevronRight className="size-3 transition-transform group-open:rotate-90" aria-hidden="true" />Email evidence</summary>
                        {fact.evidence.map((citation, evidenceIndex) => (
                          <blockquote key={evidenceIndex} className="mt-2 border-l-2 pl-2 text-muted-foreground [overflow-wrap:anywhere]">
                            <p className="whitespace-pre-wrap text-foreground">{citation.excerpt}</p>
                            <p className="mt-1">{citation.location}{citation.scope ? ` · ${citation.scope}` : ""}</p>
                          </blockquote>
                        ))}
                      </details>
                    </div>
                  )) : <p className="mt-1 text-muted-foreground">No email value</p>}
                </EvidenceColumn>
                <EvidenceColumn icon={Database} title="CRM recommendation">
                  {field.crm_recommendations.length ? field.crm_recommendations.map((recommendation, index) => (
                    <div key={index} className="mt-1">
                      <p className="whitespace-pre-wrap font-medium [overflow-wrap:anywhere]">{recommendation.value}</p>
                      <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]">{recommendation.rationale}</p>
                      <details className="group text-xs">
                        <summary className={evidenceSummaryClass}><ChevronRight className="size-3 transition-transform group-open:rotate-90" aria-hidden="true" />CRM evidence</summary>
                        <ul className="mt-2 space-y-1 text-muted-foreground">
                          {recommendation.evidence.map((evidence, evidenceIndex) => (
                            <li key={evidenceIndex} className="[overflow-wrap:anywhere]">
                              Board {evidence.record.board_id}, item {evidence.record.item_id}, column {evidence.column_id}{evidence.scope ? ` · ${evidence.scope}` : ""}
                            </li>
                          ))}
                        </ul>
                      </details>
                    </div>
                  )) : <p className="mt-1 text-muted-foreground">No CRM recommendation</p>}
                </EvidenceColumn>
              </div>
            ) : (
              <p className="flex items-center gap-2 text-muted-foreground"><Info className="size-4 shrink-0" aria-hidden="true" />No email or CRM evidence for this field.</p>
            )}
          </div>
        </div>

        {field.issues.length > 0 && (
          <Alert variant="warning" role="note" className="py-3 [&>svg]:top-3.5">
            <TriangleAlert className="size-4" aria-hidden="true" />
            <AlertTitle>Needs attention</AlertTitle>
            <AlertDescription>
              <ul className="mt-1 space-y-1" aria-label={`${label} review issues`}>
                {field.issues.map((issue, index) => <li key={`${issue.code}-${index}`} className="[overflow-wrap:anywhere]">{issue.detail}</li>)}
              </ul>
            </AlertDescription>
          </Alert>
        )}
      </div>

      <div className="border-t bg-muted/60 px-4 py-3">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground" aria-hidden="true">Your decision</span>
          <div className="flex flex-wrap gap-2" role="group" aria-label={`${label} actions`}>
            {actions.map(({ operation: kind, text, icon: Icon, title, unavailable, next }) => (
              <Button key={kind} type="button" variant="outline" title={title}
                disabled={disabled || unavailable} aria-pressed={selectedOperation === kind}
                className="aria-pressed:border-primary aria-pressed:bg-primary aria-pressed:text-primary-foreground aria-pressed:hover:bg-primary/90 aria-pressed:hover:text-primary-foreground"
                onClick={() => onChange(next())}>
                <Icon aria-hidden="true" />{text}
              </Button>
            ))}
          </div>
        </div>

        {operation?.operation === "specify_value" && (
          <div className="mt-3 space-y-1.5">
            <Label htmlFor={textId}>{label} — your value</Label>
            <Textarea id={textId} value={operation.display_value} maxLength={8192} rows={3} disabled={disabled}
              aria-invalid={invalid} aria-describedby={invalid ? `${hintId} ${errorId}` : hintId}
              onChange={(event) => onChange({ operation: "specify_value", display_value: event.target.value })}
              className="min-w-0 bg-card" />
            {invalid && <p id={errorId} className="text-sm font-medium text-destructive">Enter a value, or choose Specify blank.</p>}
            <p className="text-xs text-muted-foreground">Your exact display text will be saved. CAD mapping is checked separately.</p>
          </div>
        )}
        {operation?.operation === "specify_blank" && <p className="mt-3 text-sm text-muted-foreground">This field will remain intentionally blank; email and CRM fallback will be suppressed.</p>}
        {operation?.operation === "restore_recommendation" && <p className="mt-3 text-sm text-muted-foreground">The saved override will be removed and the current recommendation will be used.</p>}
      </div>

      <p id={hintId} className="sr-only">
        Specifying a value or blank labels this field “Source: Reviewer Specified” in the PDF. Accepting preserves the recommendation’s source.
      </p>
    </fieldset>
  );
}
