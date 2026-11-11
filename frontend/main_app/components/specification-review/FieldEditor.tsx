"use client";

import { useId } from "react";
import type { FieldOperation, ReviewField } from "@/lib/specification-review-api";

export interface FieldEditorProps {
  label: string;
  field: ReviewField;
  operation?: FieldOperation;
  disabled?: boolean;
  onChange: (operation: FieldOperation) => void;
}

const actionClass = "rounded-md border px-3 py-2 text-sm font-medium transition hover:bg-secondary disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary aria-pressed:border-primary aria-pressed:bg-accent aria-pressed:text-primary";

function display(value: string | null): string {
  return value === null ? "Not assigned" : value === "" ? "Intentionally blank" : value;
}

export default function FieldEditor({ label, field, operation, disabled = false, onChange }: FieldEditorProps) {
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

  return (
    <fieldset className="min-w-0 rounded-xl border bg-card p-4 sm:p-5" aria-describedby={hintId}>
      <legend className="px-1 text-base font-semibold text-primary">{label}</legend>
      <div className="mb-4 flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Current saved value</p>
          <p className="mt-1 whitespace-pre-wrap break-words font-medium [overflow-wrap:anywhere]">{display(current.display_value)}</p>
          <p className="mt-1 text-sm text-muted-foreground">Source: {current.source_label}</p>
          {current.reviewer_accepted && <p className="mt-1 text-xs text-muted-foreground">Recommendation accepted by reviewer</p>}
        </div>
        {operation && <span className="rounded-full bg-accent px-2.5 py-1 text-xs font-semibold text-primary">Unsaved correction</span>}
      </div>

      {current.requires_reconfirmation && (
        <p className="mb-4 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950" role="status">
          Sources or the selected project/drawing have changed. Check this saved decision and choose an action again to confirm it.
        </p>
      )}

      <div className="grid gap-3 text-sm sm:grid-cols-2">
        <div className="min-w-0 rounded-lg bg-secondary/60 p-3">
          <h3 className="font-semibold">Email value</h3>
          {field.email_facts.length ? field.email_facts.map((fact, index) => (
            <div key={index} className="mt-2">
              <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{fact.value}</p>
              <details className="mt-1 text-xs text-muted-foreground">
                <summary className="cursor-pointer rounded-sm focus-visible:outline-2 focus-visible:outline-primary">Email evidence</summary>
                {fact.evidence.map((citation, evidenceIndex) => (
                  <blockquote key={evidenceIndex} className="mt-2 border-l-2 pl-2 [overflow-wrap:anywhere]">
                    <p className="whitespace-pre-wrap">{citation.excerpt}</p>
                    <p className="mt-1">{citation.location}{citation.scope ? ` · ${citation.scope}` : ""}</p>
                  </blockquote>
                ))}
              </details>
            </div>
          )) : <p className="mt-1 text-muted-foreground">No email value</p>}
        </div>
        <div className="min-w-0 rounded-lg bg-secondary/60 p-3">
          <h3 className="font-semibold">CRM recommendation</h3>
          {field.crm_recommendations.length ? field.crm_recommendations.map((recommendation, index) => (
            <div key={index} className="mt-2">
              <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{recommendation.value}</p>
              <p className="mt-1 text-xs text-muted-foreground [overflow-wrap:anywhere]">{recommendation.rationale}</p>
              <details className="mt-1 text-xs text-muted-foreground">
                <summary className="cursor-pointer rounded-sm focus-visible:outline-2 focus-visible:outline-primary">CRM evidence</summary>
                <ul className="mt-2 space-y-1">
                  {recommendation.evidence.map((evidence, evidenceIndex) => (
                    <li key={evidenceIndex} className="[overflow-wrap:anywhere]">
                      Board {evidence.record.board_id}, item {evidence.record.item_id}, column {evidence.column_id}{evidence.scope ? ` · ${evidence.scope}` : ""}
                    </li>
                  ))}
                </ul>
              </details>
            </div>
          )) : <p className="mt-1 text-muted-foreground">No CRM recommendation</p>}
        </div>
      </div>

      <div className="mt-3 rounded-lg border p-3 text-sm">
        <p className="font-semibold">Current recommendation: <span className="font-normal whitespace-pre-wrap [overflow-wrap:anywhere]">{display(field.proposal.display_value)}</span></p>
        <p className="mt-1 text-xs text-muted-foreground">Source: {proposedSource}</p>
        <p className="mt-1 text-xs text-muted-foreground [overflow-wrap:anywhere]">{field.proposal.rationale}</p>
        {field.scope && <p className="mt-1 text-xs text-muted-foreground">Applies to: {field.scope}</p>}
      </div>

      {field.issues.length > 0 && (
        <ul className="mt-3 space-y-1 text-sm text-amber-900" aria-label={`${label} review issues`}>
          {field.issues.map((issue, index) => <li key={`${issue.code}-${index}`} className="[overflow-wrap:anywhere]">{issue.detail}</li>)}
        </ul>
      )}

      <p id={hintId} className="mt-3 text-xs text-muted-foreground">
        Specifying a value or blank labels this field “Source: Reviewer Specified” in the PDF. Accepting preserves the recommendation’s source.
      </p>
      <div className="mt-3 flex flex-wrap gap-2" role="group" aria-label={`${label} actions`}>
        <button type="button" className={actionClass} disabled={disabled || field.proposal.source === "unavailable"}
          aria-pressed={selectedOperation === "accept_recommendation"}
          onClick={() => onChange({ operation: "accept_recommendation", proposal_id: field.proposal.proposal_id })}>Use recommendation</button>
        <button type="button" className={actionClass} disabled={disabled} aria-pressed={selectedOperation === "specify_value"}
          onClick={() => onChange({ operation: "specify_value", display_value: operation?.operation === "specify_value" ? operation.display_value : current.display_value ?? "" })}>Specify value</button>
        <button type="button" className={actionClass} disabled={disabled} aria-pressed={selectedOperation === "specify_blank"}
          onClick={() => onChange({ operation: "specify_blank" })}>Specify blank</button>
        <button type="button" className={actionClass} disabled={disabled} aria-pressed={selectedOperation === "restore_recommendation"}
          onClick={() => onChange({ operation: "restore_recommendation" })}>Restore recommendation</button>
      </div>

      {operation?.operation === "specify_value" && (
        <div className="mt-4">
          <label htmlFor={textId} className="mb-1 block text-sm font-semibold">{label} — your value</label>
          <textarea id={textId} value={operation.display_value} maxLength={8192} rows={3} disabled={disabled}
            aria-invalid={invalid} aria-describedby={invalid ? `${hintId} ${errorId}` : hintId}
            onChange={(event) => onChange({ operation: "specify_value", display_value: event.target.value })}
            className="w-full min-w-0 rounded-md border bg-background p-3 text-sm disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary aria-invalid:border-destructive" />
          {invalid && <p id={errorId} className="mt-1 text-sm text-destructive">Enter a value, or choose Specify blank.</p>}
          <p className="mt-1 text-xs text-muted-foreground">Your exact display text will be saved. CAD mapping is checked separately.</p>
        </div>
      )}
      {operation?.operation === "specify_blank" && <p className="mt-3 text-sm text-muted-foreground">This field will remain intentionally blank; email and CRM fallback will be suppressed.</p>}
      {operation?.operation === "restore_recommendation" && <p className="mt-3 text-sm text-muted-foreground">The saved override will be removed and the current recommendation will be used.</p>}
      {current.cad_mapping.status === "unmapped" && current.display_value && (
        <p className="mt-3 text-xs text-muted-foreground">CAD mapping is unavailable for this saved value. Its PDF display text is preserved.</p>
      )}
    </fieldset>
  );
}
