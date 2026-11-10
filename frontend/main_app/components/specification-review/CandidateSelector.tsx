"use client";

import { useId } from "react";
import type { CandidateLookup, RequestIntent, ReviewSelections } from "@/lib/specification-review-api";

export interface CandidateSelectorProps {
  candidates: CandidateLookup;
  intent?: RequestIntent | null;
  selections: ReviewSelections;
  disabled?: boolean;
  onChange: (patch: Partial<ReviewSelections>) => void;
}

const recordKey = (record: { board_id: string; item_id: string } | null) => record ? `${record.board_id}:${record.item_id}` : "";
const cardClass = "flex min-w-0 cursor-pointer items-start gap-3 rounded-lg border p-3 text-sm has-[:checked]:border-primary has-[:checked]:bg-accent has-[:disabled]:cursor-default has-[:disabled]:opacity-70";

export default function CandidateSelector({ candidates, intent, selections, disabled = false, onChange }: CandidateSelectorProps) {
  const id = useId();
  const projectKey = recordKey(selections.selected_project);
  const drawingKey = recordKey(selections.selected_drawing);
  const drawings = candidates.drawings.filter((drawing) => recordKey(drawing.project) === projectKey);
  const requestLabel = intent?.recommended_type === "new_enquiry" ? "New Enquiry" : intent?.recommended_type === "amendment" ? "Amendment" : "Uncertain";

  return (
    <section className="min-w-0 rounded-xl border bg-card p-4 sm:p-5" aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`} className="text-lg font-semibold">Request and drawing</h2>
      {intent && (
        <div className="mt-4 rounded-lg bg-secondary/60 p-3 text-sm">
          <p className="font-semibold">AI recommendation: {requestLabel}</p>
          <p className="mt-1 [overflow-wrap:anywhere]">{intent.explanation}</p>
          {intent.ambiguity_reasons.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-5 text-amber-900">{intent.ambiguity_reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}
          {intent.evidence.length > 0 && (
            <details className="mt-2 text-xs text-muted-foreground">
              <summary className="cursor-pointer rounded-sm focus-visible:outline-2 focus-visible:outline-primary">Supporting email context</summary>
              {intent.evidence.map((citation, index) => <blockquote key={index} className="mt-2 border-l-2 pl-2 [overflow-wrap:anywhere]"><p className="whitespace-pre-wrap">{citation.excerpt}</p><p className="mt-1">{citation.location}</p></blockquote>)}
            </details>
          )}
          {intent.detected_references.length > 0 && <p className="mt-2 text-xs text-muted-foreground [overflow-wrap:anywhere]">Detected references: {intent.detected_references.map((reference) => reference.literal).join(", ")}</p>}
        </div>
      )}
      <div className="mt-4">
        <label htmlFor={`${id}-request-type`} className="block text-sm font-semibold">Your request type</label>
        <select id={`${id}-request-type`} value={selections.selected_request_type ?? ""} disabled={disabled}
          className="mt-1 w-full rounded-md border bg-background p-2.5 text-sm disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
          onChange={(event) => {
            const type = event.target.value as ReviewSelections["selected_request_type"];
            onChange(type === "new_enquiry" ? { selected_request_type: type, selected_project: null, selected_drawing: null } : { selected_request_type: type || null });
          }}>
          <option value="">Choose request type</option>
          <option value="new_enquiry">New Enquiry</option>
          <option value="amendment">Amendment</option>
        </select>
        <p className="mt-1 text-xs text-muted-foreground">Saved internally for this specification. Monday business fields are maintained separately.</p>
      </div>

      {candidates.issues.length > 0 && <ul className="mt-4 space-y-1 text-sm text-amber-900" aria-label="Candidate review issues">{candidates.issues.map((issue, index) => <li key={index}>{issue}</li>)}</ul>}
      {candidates.status !== "available" && <p role="status" className="mt-3 text-sm text-muted-foreground">{candidates.status === "pending" ? "Project and drawing recommendations are being prepared." : "Project and drawing recommendations are temporarily unavailable."}</p>}

      <fieldset className="mt-5 min-w-0">
        <legend className="text-sm font-semibold">Project candidates</legend>
        <p className="mt-1 text-xs text-muted-foreground">A matching project supports the recommendation; it does not determine request type.</p>
        <div className="mt-2 grid gap-2">
          <label className={cardClass}>
            <input type="radio" name={`${id}-project`} value="" checked={!projectKey} disabled={disabled}
              className="mt-0.5 shrink-0 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
              onChange={() => onChange({ selected_project: null, selected_drawing: null })} />
            <span>No project selected</span>
          </label>
          {candidates.projects.map((project) => (
            <label key={recordKey(project.record)} className={cardClass}>
              <input type="radio" name={`${id}-project`} value={recordKey(project.record)} checked={projectKey === recordKey(project.record)} disabled={disabled}
                className="mt-0.5 shrink-0 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
                onChange={() => onChange({ selected_project: project.record, selected_drawing: null })} />
              <span className="min-w-0 [overflow-wrap:anywhere]"><span className="block font-semibold">Project item {project.record.item_id}</span><span className="mt-1 block text-muted-foreground">{project.rationale}</span></span>
            </label>
          ))}
          {projectKey && !candidates.projects.some((project) => recordKey(project.record) === projectKey) && <p className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">The saved project ({selections.selected_project?.item_id}) is no longer among the current candidates. Review the selection before completion.</p>}
        </div>
      </fieldset>

      <fieldset className="mt-5 min-w-0">
        <legend className="text-sm font-semibold">Drawing and roof candidates</legend>
        <div className="mt-2 grid gap-2">
          <label className={cardClass}>
            <input type="radio" name={`${id}-drawing`} value="" checked={!drawingKey} disabled={disabled}
              className="mt-0.5 shrink-0 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
              onChange={() => onChange({ selected_drawing: null })} />
            <span>No drawing selected</span>
          </label>
          {drawings.map((drawing) => (
            <label key={recordKey(drawing.record)} className={cardClass}>
              <input type="radio" name={`${id}-drawing`} value={recordKey(drawing.record)} checked={drawingKey === recordKey(drawing.record)} disabled={disabled || drawing.lifecycle !== "active"}
                className="mt-0.5 shrink-0 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary"
                onChange={() => onChange({ selected_drawing: drawing.record })} />
              <span className="min-w-0 [overflow-wrap:anywhere]">
                <span className="block font-semibold">{drawing.literal_reference}</span>
                {drawing.roof_scope && <span className="mt-1 block">Roof: {drawing.roof_scope}</span>}
                <span className="mt-1 block text-xs font-medium text-primary">{drawing.match_kind === "exact_email" ? "Matches email reference" : drawing.match_kind === "latest_in_scope" ? "Latest suitable existing drawing" : "Other candidate"}{drawing.lifecycle !== "active" ? ` · ${drawing.lifecycle}` : ""}</span>
                <span className="mt-1 block text-muted-foreground">{drawing.rationale}</span>
              </span>
            </label>
          ))}
          {!projectKey && candidates.drawings.length > 0 && <p className="text-sm text-muted-foreground">Select a project to review its drawing and roof choices.</p>}
          {projectKey && drawings.length === 0 && <p className="text-sm text-muted-foreground">No drawing candidates are available for this project.</p>}
          {drawingKey && !drawings.some((drawing) => recordKey(drawing.record) === drawingKey) && <p className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">The saved drawing ({selections.selected_drawing?.item_id}) is no longer among the current choices. Select an appropriate drawing.</p>}
        </div>
      </fieldset>
    </section>
  );
}
