"use client";

import { useId, useState } from "react";
import { ChevronDown, ChevronRight, ChevronUp, Sparkles, TriangleAlert } from "lucide-react";
import type { CandidateLookup, RequestIntent, ReviewSelections } from "@/lib/specification-review-api";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { cn } from "@/lib/utils";
import { evidenceSummaryClass } from "./FieldEditor";

export interface CandidateSelectorProps {
  candidates: CandidateLookup;
  extractedProjectTitle?: string | null;
  intent?: RequestIntent | null;
  selections: ReviewSelections;
  disabled?: boolean;
  unavailableMessage?: string;
  onChange: (patch: Partial<ReviewSelections>) => void;
}

const recordKey = (record: { board_id: string; item_id: string } | null) => record ? `${record.board_id}:${record.item_id}` : "";
const choiceCard = "flex min-w-0 cursor-pointer items-start gap-3 rounded-lg border bg-card p-3 text-sm shadow-xs transition-colors hover:bg-muted/60 has-[:checked]:border-primary has-[:checked]:bg-accent/40 has-[:checked]:ring-1 has-[:checked]:ring-primary has-[:focus-visible]:ring-[3px] has-[:focus-visible]:ring-ring/50 has-[:disabled]:cursor-not-allowed has-[:disabled]:opacity-60 has-[:disabled]:hover:bg-card";
const radioClass = "mt-0.5 size-4 shrink-0 accent-primary focus-visible:outline-none";
const warningItem = "flex items-start gap-2 text-warning-foreground";
const collapsedProjectLimit = 10;
const uniqueMessages = (messages: string[]) => Array.from(new Set(messages.map((message) => message.trim()).filter(Boolean)));

function formatTitleSimilarity(score: number) {
  const percentage = score * 100;
  // Match the PDF's ties-to-even rounding. At .25, toFixed would round up.
  const rounded = percentage % 1 === 0.25 ? Math.floor(percentage * 10) / 10 : percentage;
  return `${rounded.toFixed(1)}%`;
}

export default function CandidateSelector({ candidates, extractedProjectTitle, intent, selections, disabled = false, unavailableMessage, onChange }: CandidateSelectorProps) {
  const id = useId();
  const [projectsExpanded, setProjectsExpanded] = useState(false);
  const projectKey = recordKey(selections.selected_project);
  const drawingKey = recordKey(selections.selected_drawing);
  const drawings = candidates.drawings.filter((drawing) => recordKey(drawing.project) === projectKey);
  const requestLabel = intent?.recommended_type === "new_enquiry" ? "New Enquiry" : intent?.recommended_type === "amendment" ? "Amendment" : "Uncertain";
  const references = intent ? Array.from(new Set(intent.detected_references.map((reference) => reference.literal))) : [];
  const candidateIssues = uniqueMessages(candidates.issues);
  const intentReasons = uniqueMessages(intent?.ambiguity_reasons ?? []).filter((reason) => !candidateIssues.includes(reason));
  const foldProjects = candidates.projects.length > collapsedProjectLimit;
  const visibleProjects = foldProjects && !projectsExpanded
    ? candidates.projects.filter((project, index) => index < collapsedProjectLimit || recordKey(project.record) === projectKey)
    : candidates.projects;

  return (
    <section className="min-w-0" aria-labelledby={`${id}-title`}>
      <Card className="gap-0 py-0">
        <CardHeader className="border-b px-4 py-4 sm:px-6 [.border-b]:pb-4">
          <h2 id={`${id}-title`} className="text-lg font-semibold leading-tight">Request and drawing</h2>
          <CardDescription>Confirm the request type, then choose the project and drawing this specification applies to.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-6 px-4 py-5 sm:px-6 lg:grid-cols-2 lg:gap-8">
          <div className="min-w-0 space-y-5">
            {intent && (
              <div className="rounded-lg border bg-muted/40 p-4 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Sparkles className="size-4 text-brand" aria-hidden="true" />
                  <p className="font-semibold">Recommended request type</p>
                  <Badge variant="outline" className={intent.recommended_type === "uncertain"
                    ? "border-warning/40 bg-warning/10 text-warning-foreground" : "border-info/30 bg-info/10 text-info-foreground"}>{requestLabel}</Badge>
                </div>
                <p className="mt-2 leading-relaxed [overflow-wrap:anywhere]">{intent.explanation}</p>
                {intentReasons.length > 0 && (
                  <ul className="mt-3 space-y-1.5">
                    {intentReasons.map((reason) => <li key={reason} className={warningItem}><TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden="true" />{reason}</li>)}
                  </ul>
                )}
                {intent.evidence.length > 0 && (
                  <details className="group mt-3 text-xs">
                    <summary className={evidenceSummaryClass}><ChevronRight className="size-3 transition-transform group-open:rotate-90" aria-hidden="true" />Supporting email context</summary>
                    {intent.evidence.map((citation, index) => <blockquote key={index} className="mt-2 border-l-2 pl-2 text-muted-foreground [overflow-wrap:anywhere]"><p className="whitespace-pre-wrap text-foreground">{citation.excerpt}</p><p className="mt-1">{citation.location}</p></blockquote>)}
                  </details>
                )}
                {references.length > 0 && (
                  <div className="mt-3">
                    <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Detected references</p>
                    <div className="mt-1.5 flex flex-wrap gap-1.5">
                      {references.map((reference) => <Badge key={reference} variant="outline" className="bg-card font-mono font-normal">{reference}</Badge>)}
                    </div>
                  </div>
                )}
              </div>
            )}
            <div className="space-y-1.5">
              <Label htmlFor={`${id}-request-type`}>Your request type</Label>
              <NativeSelect id={`${id}-request-type`} value={selections.selected_request_type ?? ""} disabled={disabled}
                wrapperClassName="w-full" className="bg-card"
                onChange={(event) => {
                  const type = event.target.value as ReviewSelections["selected_request_type"];
                  onChange(type === "new_enquiry" ? { selected_request_type: type, selected_project: null, selected_drawing: null } : { selected_request_type: type || null });
                }}>
                <NativeSelectOption value="">Choose request type</NativeSelectOption>
                <NativeSelectOption value="new_enquiry">New Enquiry</NativeSelectOption>
                <NativeSelectOption value="amendment">Amendment</NativeSelectOption>
              </NativeSelect>
              <p className="text-xs text-muted-foreground">Saved internally for this specification. Monday business fields are maintained separately.</p>
            </div>
          </div>

          <div className="min-w-0 space-y-5">
            {candidateIssues.length > 0 && (
              <Alert variant="warning" role="note" className="py-3 [&>svg]:top-3.5">
                <TriangleAlert className="size-4" aria-hidden="true" />
                <AlertDescription>
                  <ul className="space-y-1" aria-label="Candidate review issues">{candidateIssues.map((issue) => <li key={issue}>{issue}</li>)}</ul>
                </AlertDescription>
              </Alert>
            )}
            {candidates.status !== "available" && <p role="status" className="text-sm text-muted-foreground">{unavailableMessage ?? (candidates.status === "pending" ? "Project and drawing recommendations are being prepared." : "Project and drawing recommendations are temporarily unavailable.")}</p>}

            <fieldset className="min-w-0">
              <legend className="text-sm font-semibold">Project candidates</legend>
              <dl className="mt-2 rounded-lg border bg-muted/40 px-3 py-2 text-sm">
                <dt className="text-xs text-muted-foreground">Extracted Project Title</dt>
                <dd className="mt-1 font-medium [overflow-wrap:anywhere]">{extractedProjectTitle?.trim() || "Not available from the current extraction."}</dd>
              </dl>
              <p className="mt-1 text-xs text-muted-foreground">A matching project supports the recommendation; it does not determine request type.</p>
              {foldProjects && <p className="mt-2 text-xs text-muted-foreground" role="status">Showing {visibleProjects.length} of {candidates.projects.length} projects.</p>}
              <div id={`${id}-projects`} className="mt-2 grid gap-2">
                <label className={choiceCard}>
                  <input type="radio" name={`${id}-project`} value="" checked={!projectKey} disabled={disabled} className={radioClass}
                    onChange={() => onChange({ selected_project: null, selected_drawing: null })} />
                  <span className="text-muted-foreground">No project selected</span>
                </label>
                {visibleProjects.map((project) => (
                  <label key={recordKey(project.record)} className={choiceCard}>
                    <input type="radio" name={`${id}-project`} value={recordKey(project.record)} checked={projectKey === recordKey(project.record)} disabled={disabled} className={radioClass}
                      onChange={() => onChange({ selected_project: project.record, selected_drawing: null })} />
                    <span className="min-w-0 [overflow-wrap:anywhere]">
                      <span className="block font-semibold">{project.project_reference?.trim() || "TP Ref unavailable"} — {project.project_title?.trim() || "Project title unavailable"}</span>
                      {typeof project.similarity_score === "number" && (
                        <span className="mt-1 block text-xs font-medium tabular-nums">Title similarity: {formatTitleSimilarity(project.similarity_score)}</span>
                      )}
                      <span className="mt-1 block text-muted-foreground">{project.rationale}</span>
                    </span>
                  </label>
                ))}
                {projectKey && !candidates.projects.some((project) => recordKey(project.record) === projectKey) && <p className={cn("rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm", warningItem)}>The saved project ({selections.selected_project?.item_id}) is no longer among the current candidates. Review the selection before completion.</p>}
              </div>
              {foldProjects && (
                <Button type="button" variant="outline" size="sm" className="mt-3" aria-expanded={projectsExpanded} aria-controls={`${id}-projects`}
                  onClick={() => setProjectsExpanded((expanded) => !expanded)}>
                  {projectsExpanded ? <ChevronUp aria-hidden="true" /> : <ChevronDown aria-hidden="true" />}
                  {projectsExpanded ? "Show fewer projects" : `Show all ${candidates.projects.length} projects`}
                </Button>
              )}
            </fieldset>

            <fieldset className="min-w-0">
              <legend className="text-sm font-semibold">Drawing and roof candidates</legend>
              <div className="mt-2 grid gap-2">
                <label className={choiceCard}>
                  <input type="radio" name={`${id}-drawing`} value="" checked={!drawingKey} disabled={disabled} className={radioClass}
                    onChange={() => onChange({ selected_drawing: null })} />
                  <span className="text-muted-foreground">No drawing selected</span>
                </label>
                {drawings.map((drawing) => (
                  <label key={recordKey(drawing.record)} className={choiceCard}>
                    <input type="radio" name={`${id}-drawing`} value={recordKey(drawing.record)} checked={drawingKey === recordKey(drawing.record)} disabled={disabled || drawing.lifecycle !== "active"} className={radioClass}
                      onChange={() => onChange({ selected_drawing: drawing.record })} />
                    <span className="min-w-0 [overflow-wrap:anywhere]">
                      <span className="block font-mono font-semibold">{drawing.literal_reference}</span>
                      {drawing.roof_scope && <span className="mt-1 block">Roof: {drawing.roof_scope}</span>}
                      <span className="mt-1.5 flex flex-wrap gap-1.5">
                        <Badge variant="outline" className={drawing.match_kind === "exact_email" ? "border-success/30 bg-success/10 text-success-foreground" : "bg-muted text-foreground"}>
                          {drawing.match_kind === "exact_email" ? "Matches email reference" : drawing.match_kind === "latest_in_scope" ? "Latest suitable existing drawing" : "Other candidate"}
                        </Badge>
                        {drawing.lifecycle !== "active" && <Badge variant="outline" className="bg-muted capitalize text-muted-foreground">{drawing.lifecycle}</Badge>}
                      </span>
                      <span className="mt-1.5 block text-muted-foreground">{drawing.rationale}</span>
                    </span>
                  </label>
                ))}
                {!projectKey && candidates.drawings.length > 0 && <p className="text-sm text-muted-foreground">Select a project to review its drawing and roof choices.</p>}
                {projectKey && drawings.length === 0 && <p className="text-sm text-muted-foreground">No drawing candidates are available for this project.</p>}
                {drawingKey && !drawings.some((drawing) => recordKey(drawing.record) === drawingKey) && <p className={cn("rounded-lg border border-warning/40 bg-warning/10 p-3 text-sm", warningItem)}>The saved drawing ({selections.selected_drawing?.item_id}) is no longer among the current choices. Select an appropriate drawing.</p>}
              </div>
            </fieldset>
          </div>
        </CardContent>
      </Card>
    </section>
  );
}
