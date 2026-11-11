import type {
  ApproveCommand, FieldName, FieldOperation, ReviewDraft, ReviewSelections,
  ReviewStatus, ReviewStatusSnapshot, ReviewWorkspace, SaveDecisionsCommand,
} from "./specification-review-api.ts";

export function emptyDraft(): ReviewDraft {
  return { selections: {}, field_operations: {}, source_dispositions: [] };
}

export function hasUnsavedChanges(draft: ReviewDraft): boolean {
  return Object.keys(draft.selections).length > 0 || Object.keys(draft.field_operations).length > 0 || draft.source_dispositions.length > 0;
}

export function updateField(draft: ReviewDraft, label: FieldName, operation: FieldOperation): ReviewDraft {
  return { ...draft, field_operations: { ...draft.field_operations, [label]: operation } };
}

export function updateSelection(draft: ReviewDraft, patch: Partial<ReviewSelections>): ReviewDraft {
  const selections = { ...draft.selections, ...patch };
  // A drawing from another project must never silently survive a project choice.
  if ("selected_project" in patch && !("selected_drawing" in patch)) selections.selected_drawing = null;
  return { ...draft, selections };
}

/** Explicit conflict recovery preserves typed corrections; recommendations must be reviewed again. */
export function rebaseDraft(draft: ReviewDraft): ReviewDraft {
  return { selections: structuredClone(draft.selections),
    field_operations: Object.fromEntries(Object.entries(draft.field_operations)
      .filter(([, operation]) => operation.operation !== "accept_recommendation")
      .map(([label, operation]) => [label, structuredClone(operation)])),
    source_dispositions: [],
  };
}

type ReviewIdentity = Pick<ReviewStatusSnapshot, "board_id" | "item_id" | "review_version" | "source_fingerprint" | "decision_digest" | "desired_publication_identity" | "provisional" | "extraction_identity">;

export function sameReviewIdentity(a: ReviewIdentity, b: ReviewIdentity): boolean {
  return a.board_id === b.board_id && a.item_id === b.item_id && a.review_version === b.review_version
    && a.source_fingerprint === b.source_fingerprint && a.decision_digest === b.decision_digest
    && a.desired_publication_identity === b.desired_publication_identity
    && Boolean(a.provisional) === Boolean(b.provisional)
    && (a.extraction_identity ?? null) === (b.extraction_identity ?? null);
}

/** Keep source proposals and user drafts untouched. Callers must explicitly reconcile changed identities. */
export function mergeStatus(workspace: ReviewWorkspace, status: ReviewStatusSnapshot): ReviewWorkspace {
  if (workspace.board_id !== status.board_id || workspace.item_id !== status.item_id) return workspace;
  if (!sameReviewIdentity(workspace, status)) {
    // Keep the baseline used to author drafts. A newer poll must not silently rebase a stale command.
    return { ...workspace, status: status.status, queue: status.queue, pdf: status.pdf, published_pdf: status.published_pdf,
      approval: status.approval, capabilities: status.capabilities, scope_reason: status.scope_reason,
      analysis: status.analysis, provisional: status.provisional };
  }
  return { ...workspace, ...status };
}

export function draftValidationErrors(draft: ReviewDraft): Partial<Record<FieldName, string>> {
  const errors: Partial<Record<FieldName, string>> = {};
  for (const [label, operation] of Object.entries(draft.field_operations)) {
    if (operation.operation === "specify_value") {
      if (!operation.display_value.trim()) errors[label as FieldName] = "Enter a value, or choose Specify blank.";
      else if (operation.display_value.length > 8192) errors[label as FieldName] = "Use 8,192 characters or fewer.";
    }
  }
  return errors;
}

export function commandIdentity(workspace: ReviewStatusSnapshot, idempotencyKey: string) {
  if (workspace.provisional || (workspace.analysis && !workspace.analysis.ready)) throw new Error("Wait for matching and analysis to finish before saving.");
  if (!workspace.source_fingerprint) throw new Error("Wait for current source analysis before saving.");
  return { expected_review_version: workspace.review_version, expected_source_fingerprint: workspace.source_fingerprint, idempotency_key: idempotencyKey };
}

export function saveCommand(workspace: ReviewWorkspace, draft: ReviewDraft, idempotencyKey: string): SaveDecisionsCommand {
  if (Object.keys(draftValidationErrors(draft)).length) throw new Error("Check the highlighted field entries.");
  if (draft.source_dispositions.some((entry) => !entry.explanation.trim())) throw new Error("Explain each source issue disposition before saving.");
  return { schema_version: 1, ...commandIdentity(workspace, idempotencyKey),
    selections: { ...draft.selections }, field_operations: structuredClone(draft.field_operations),
    source_dispositions: structuredClone(draft.source_dispositions) };
}

/** Explain the human choices required by the API's final approval checks. */
export function completionIssues(workspace: ReviewWorkspace): string[] {
  if (workspace.provisional || (workspace.analysis && !workspace.analysis.ready)) return ["Wait for current analysis and matching to finish."];
  const source = workspace.source;
  if (!source) return ["Wait for current source recommendations."];
  const issues: string[] = [];
  const selected = workspace.review.selections;
  const sameRecord = (a: { board_id: string; item_id: string } | null, b: { board_id: string; item_id: string } | null) =>
    a === null ? b === null : b !== null && a.board_id === b.board_id && a.item_id === b.item_id;
  if (!selected.selected_request_type) issues.push("Choose and save the request type.");
  if ((selected.selected_request_type && selected.selected_request_type !== source.request_type)
    || !sameRecord(selected.selected_project, source.selected_project) || !sameRecord(selected.selected_drawing, source.selected_drawing)) {
    issues.push("Refresh recommendations for the saved request type, project and drawing.");
  }
  if (selected.selected_request_type === "amendment" && (!selected.selected_project || !selected.selected_drawing)) {
    issues.push("Choose and save the amendment project and drawing.");
  }
  if (selected.selected_drawing && !source.candidates.drawings.some((drawing) => drawing.lifecycle === "active"
    && sameRecord(drawing.record, selected.selected_drawing) && sameRecord(drawing.project, selected.selected_project))) {
    issues.push("Select a currently active drawing for this project.");
  }
  for (const [label, field] of Object.entries(workspace.fields)) {
    if (field.effective.requires_reconfirmation) issues.push(`Reconfirm ${label} against the current sources.`);
    else if (field.effective.source === "unavailable" || (field.issues.length > 0 && !field.decision)) issues.push(`Resolve ${label}.`);
    else if (!["reviewer_specified", "unassigned", "default_blank"].includes(field.effective.source) && !field.decision) issues.push(`Accept or specify ${label}.`);
  }
  for (const issue of source.source_issues) {
    const addressed = workspace.review.source_dispositions.some((entry) => entry.source_id === issue.source_id && entry.issue_code === issue.code
      && Object.entries(source.identity).every(([key, value]) => entry.audit.reviewed_source[key as keyof typeof source.identity] === value));
    if (!addressed) issues.push(`Review source issue: ${issue.detail}`);
  }
  return issues;
}

/** Final approval is possible only after the exact published preview has loaded. */
export function canComplete(workspace: ReviewStatusSnapshot | ReviewWorkspace, draft: ReviewDraft, previewedRevision?: string | null): boolean {
  const queue = workspace.queue;
  return !workspace.provisional && (!workspace.analysis || workspace.analysis.ready)
    && workspace.capabilities.approve && workspace.status === "awaiting_final_review" && !hasUnsavedChanges(draft)
    && Boolean(workspace.source_fingerprint && workspace.decision_digest && workspace.desired_publication_identity)
    && Boolean(workspace.published_pdf?.published && workspace.pdf?.revision === workspace.published_pdf.revision
      && previewedRevision === workspace.published_pdf.revision)
    && Boolean(queue && !queue.failed && !queue.running && queue.requested_generation === queue.completed_generation)
    && (!("source" in workspace) || completionIssues(workspace).length === 0);
}

export function approvalCommand(workspace: ReviewStatusSnapshot, idempotencyKey: string): ApproveCommand {
  if (!workspace.published_pdf?.published || !workspace.decision_digest || !workspace.desired_publication_identity) {
    throw new Error("Wait for the current saved decisions to be published.");
  }
  return { ...commandIdentity(workspace, idempotencyKey), artifact_revision: workspace.published_pdf.revision,
    expected_decision_digest: workspace.decision_digest, expected_publication_identity: workspace.desired_publication_identity };
}

const STATUS_TEXT: Record<ReviewStatus, string> = {
  source_waiting: "Waiting for source analysis",
  analysis_pending: "Validated fields available; analysis pending",
  source_unavailable: "Recommendations temporarily unavailable",
  needs_review: "Needs review",
  regenerating_pdf: "Regenerating PDF",
  awaiting_final_review: "Awaiting final review",
  complete: "Complete",
  publication_failed: "Update failed; previous PDF available",
  read_only: "Read-only; retained PDF available",
};

export function reviewStatusText(status: ReviewStatus): string { return STATUS_TEXT[status] ?? "Needs review"; }

export function friendlyReviewError(error: unknown): string {
  if (error instanceof Error && error.name === "ReviewApiError") return error.message;
  return "The review request could not be completed. Your corrections have been kept; try again.";
}
