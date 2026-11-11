/** Item-scoped Specification Review contract. Credentials live only in this module's closures. */
export const FIELD_NAMES = [
  "Scale", "Page Size", "Drawing Reference", "Drawing Title", "Bauder Contract Number",
  "Bauder Roof Num", "Incoming Information", "Revision", "Date Received", "Company",
  "Contact", "Surveyor", "Target U Value", "Target Min U Value", "Membrane",
  "Tapered Insulation", "VCL", "Decking",
] as const;

export type FieldName = typeof FIELD_NAMES[number];
export type RequestType = "new_enquiry" | "amendment";
export type ReviewContext = { accountId: string; boardId: string; itemId: string };
export type ReviewCapabilities = { read: boolean; edit: boolean; approve: boolean };
export type MondayRecord = { board_id: string; item_id: string };
export type EmailCitation = { source_id: string; excerpt: string; location: string; scope: string | null };
export type CRMFieldRead = { record: MondayRecord; column_id: string; scope: string | null };
export type SourceIdentity = {
  source_fingerprint: string; extraction_identity: string; source_policy_version: string; reference_parser_version: string;
};
export type CADMapping = {
  status: "mapped" | "unmapped" | "not_applicable";
  value: string | null; catalogue_version: string | null; target_identity: string | null;
};
export type FieldSource = "email_context" | "monday_crm" | "derived" | "unassigned" | "default_blank" | "unavailable" | "reviewer_specified";
export type AutomaticProposal = {
  proposal_id: string; display_value: string | null; source: Exclude<FieldSource, "reviewer_specified">;
  rationale: string; email_evidence: EmailCitation[]; crm_evidence: CRMFieldRead[];
  derived_from: "Drawing Reference" | null; cad_mapping: CADMapping;
};
export type ProjectMatch = {
  record: MondayRecord; project_reference?: string | null; project_title?: string | null;
  rationale: string; similarity_score: number | null;
};
export type DrawingCandidate = {
  record: MondayRecord; project: MondayRecord; literal_reference: string; roof_scope: string | null;
  lifecycle: "active" | "archived" | "deleted" | "unknown";
  match_kind: "exact_email" | "latest_in_scope" | "other"; rationale: string;
};
export type CandidateLookup = {
  status: "available" | "unavailable" | "pending"; projects: ProjectMatch[]; drawings: DrawingCandidate[]; issues: string[];
};
export type RequestIntent = {
  schema_version: 1; recommended_type: RequestType | "uncertain"; explanation: string; evidence: EmailCitation[];
  detected_references: { kind: "project" | "drawing"; literal: string; evidence: EmailCitation; role: string }[];
  matched_projects: ProjectMatch[]; ambiguity_reasons: string[];
};
export type DecisionAudit = {
  actor: { app_user_id: string; monday_user_id: string; monday_account_id: string };
  saved_at: string; reviewed_source: SourceIdentity; explanation: string | null;
};
export type SavedFieldDecision = {
  decision: { operation: "accept_recommendation"; snapshot: AutomaticProposal }
    | { operation: "specify_value"; display_value: string; cad_mapping: CADMapping }
    | { operation: "specify_blank" };
  audit: DecisionAudit;
};
export type FieldOperation = { operation: "accept_recommendation"; proposal_id: string }
  | { operation: "specify_value"; display_value: string }
  | { operation: "specify_blank" }
  | { operation: "restore_recommendation" };
export type SourceIssue = { source_id: string; code: string; detail: string };
export type SourceDispositionCommand = { source_id: string; issue_code: string; explanation: string };
export type SourceDisposition = SourceDispositionCommand & { audit: DecisionAudit };
export type ReviewSelections = {
  selected_request_type: RequestType | null; selected_project: MondayRecord | null; selected_drawing: MondayRecord | null;
};
export type ReviewDecisionState = {
  schema_version: 1; review_version: number; selections: ReviewSelections;
  field_decisions: Partial<Record<FieldName, SavedFieldDecision>>; source_dispositions: SourceDisposition[];
};
export type ReviewField = {
  email_facts: { value: string; evidence: EmailCitation[] }[];
  crm_recommendations: { value: string; evidence: CRMFieldRead[]; rationale: string }[];
  proposal: AutomaticProposal; applicability: "applicable" | "unassigned" | "not_applicable" | "unresolved";
  scope: string | null; issues: { code: string; detail: string }[]; decision: SavedFieldDecision | null;
  effective: {
    display_value: string | null; source: FieldSource; source_label: string; cad_mapping: CADMapping;
    reviewer_accepted: boolean; requires_reconfirmation: boolean; email_evidence: EmailCitation[];
    crm_evidence: CRMFieldRead[]; derived_from: "Drawing Reference" | null;
  };
};
export type ReviewSource = {
  schema_version: 1; identity: SourceIdentity; intent: RequestIntent; request_type: RequestType | null;
  selected_project: MondayRecord | null; selected_drawing: MondayRecord | null;
  candidates: CandidateLookup; fields: Record<FieldName, Omit<ReviewField, "decision" | "effective">>; source_issues: SourceIssue[];
};
export type PdfArtifact = {
  revision: string; filename: string; status: string; published: boolean; size_bytes: number;
  /** Informational only: the client constructs its own authenticated, item-scoped PDF path. */
  url: string;
};
export type Approval = {
  id: string; artifact_revision: string; review_version: number; actor_monday_user_id: string; approved_at: string;
};
export type ReviewStatus = "read_only" | "source_waiting" | "source_unavailable" | "publication_failed"
  | "regenerating_pdf" | "complete" | "awaiting_final_review" | "needs_review";
export type ReviewStatusSnapshot = {
  board_id: string; item_id: string; status: ReviewStatus; review_version: number;
  source_fingerprint: string | null; decision_digest: string | null; desired_publication_identity: string | null;
  queue: { requested_generation: number; completed_generation: number; running: boolean; failed: boolean } | null;
  pdf: PdfArtifact | null; published_pdf: PdfArtifact | null; approval: Approval | null;
  capabilities: ReviewCapabilities; scope_reason: string | null;
};
export type ReviewWorkspace = ReviewStatusSnapshot & {
  review: ReviewDecisionState; source: ReviewSource | null; fields: Partial<Record<FieldName, ReviewField>>;
  candidates: CandidateLookup;
  history: { event_id: string; command_type: string; review_version: number; actor_monday_user_id: string | null; created_at: string; explanation: string | null }[];
};
export type ReadyReviewSession = {
  status: "ready"; access_token: string; token_type: "Bearer"; expires_at: string;
  capabilities: ReviewCapabilities; scope_reason: string | null;
  account_id: string; user_id: string; board_id: string; item_id: string;
};
export type ReviewSession = ReadyReviewSession | { status: "oauth_required"; authorization_url: string };
export type ReviewDraft = {
  selections: Partial<ReviewSelections>; field_operations: Partial<Record<FieldName, FieldOperation>>;
  source_dispositions: SourceDispositionCommand[];
};
export type ReviewCommandIdentity = { expected_review_version: number; expected_source_fingerprint: string; idempotency_key: string };
export type SaveDecisionsCommand = ReviewCommandIdentity & ReviewDraft & { schema_version: 1; explanation?: string };
export type RefreshCommand = ReviewCommandIdentity & { explanation?: string };
export type ApproveCommand = ReviewCommandIdentity & {
  artifact_revision: string; expected_decision_digest: string; expected_publication_identity: string; explanation?: string;
};
export type ReviewCommandReceipt = {
  review_version: number; source_fingerprint: string; decision_digest: string;
  desired_publication_identity: string; approval_id: string | null; changed: boolean; event_id: string | null;
};

const ERROR_MESSAGES: Record<string, string> = {
  authorization_expired: "Your Monday session has expired. Reconnect to continue; your unsaved corrections are still here.",
  reviewer_forbidden: "You no longer have permission to change this specification. Reconnect to check your access.",
  source_waiting: "Waiting for current source analysis. Try again shortly.",
  source_unavailable: "Current recommendations are temporarily unavailable. Your corrections have been kept.",
  candidates_ambiguous: "Choose the relevant project and drawing before continuing.",
  stale_review: "Another reviewer has saved changes. Compare the latest review before saving your corrections.",
  source_changed: "The sources have changed. Compare the latest recommendations before saving your corrections.",
  scope_changed: "This item is outside the editable Landing Zone. Its retained PDF is still available.",
  publication_failed: "The PDF update failed. Your saved decisions and the previous PDF are retained.",
  idempotency_conflict: "This retry no longer matches the original command. Reload the latest review before saving again.",
  invalid_request: "Check the review entries and try again.",
  network_error: "The review service could not be reached. Your corrections have been kept; try again.",
};

export class ReviewApiError extends Error {
  readonly code: string;
  readonly status: number;
  constructor(code: string, status = 0) {
    super(ERROR_MESSAGES[code] ?? "The review request could not be completed. Your corrections have been kept.");
    this.name = "ReviewApiError";
    this.code = code;
    this.status = status;
  }
}

type ReviewFetch = typeof fetch;
const endpoint = (apiBase: string, suffix: string) => `${apiBase.replace(/\/+$/, "")}/api/specification-review${suffix}`;

async function request<T>(fetcher: ReviewFetch, url: string, options: RequestInit, blob = false): Promise<T> {
  // Keep the deadline active through JSON/PDF consumption, since headers can arrive
  // while a response body stalls. Context cancellation also releases the timer.
  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(), 30_000);
  const clearDeadline = () => clearTimeout(timer);
  options.signal?.addEventListener("abort", clearDeadline, { once: true });
  const signal = options.signal ? AbortSignal.any([options.signal, timeout.signal]) : timeout.signal;
  try {
    const response = await fetcher(url, { ...options, signal, credentials: "omit", cache: "no-store", redirect: "error", referrerPolicy: "no-referrer" });
    if (blob && response.ok && response.status !== 202) return await response.blob() as T;
    let data: unknown;
    try { data = await response.json(); } catch (error) {
      if (signal.aborted) throw error;
      throw new ReviewApiError("source_unavailable", response.status);
    }
    const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : null;
    if (!response.ok || response.status === 202 || detail) {
      const suppliedCode = detail && typeof detail === "object" && "code" in detail ? (detail as { code: unknown }).code : null;
      const fallback = response.status === 401 ? "authorization_expired" : response.status === 403 ? "reviewer_forbidden" : "source_unavailable";
      // Never surface response bodies, upstream URLs or credentials through exception messages.
      throw new ReviewApiError(typeof suppliedCode === "string" && suppliedCode in ERROR_MESSAGES ? suppliedCode : fallback, response.status);
    }
    return data as T;
  } catch (error) {
    if (options.signal?.aborted || error instanceof ReviewApiError) throw error;
    throw new ReviewApiError("network_error");
  } finally {
    clearDeadline();
    options.signal?.removeEventListener("abort", clearDeadline);
  }
}

export async function bootstrapReviewSession(
  apiBase: string, sessionToken: string, context: ReviewContext, signal?: AbortSignal, fetcher: ReviewFetch = fetch,
): Promise<ReviewSession> {
  const result = await request<ReviewSession>(fetcher, endpoint(apiBase, "/session"), {
    method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ sessionToken, context }), signal,
  });
  if (result.status === "ready" && (result.account_id !== context.accountId || result.board_id !== context.boardId || result.item_id !== context.itemId)) {
    throw new ReviewApiError("reviewer_forbidden", 403);
  }
  return result;
}

/** A client cannot be rebound to another item. Destroy it on each Monday context switch. */
export function createReviewApi(apiBase: string, session: ReadyReviewSession, fetcher: ReviewFetch = fetch) {
  let token: string | null = session.access_token;
  const controller = new AbortController();
  const context: ReviewContext = { accountId: session.account_id, boardId: session.board_id, itemId: session.item_id };
  const base = `/items/${encodeURIComponent(context.boardId)}/${encodeURIComponent(context.itemId)}`;
  const call = <T>(suffix: string, method: string, command?: unknown, signal?: AbortSignal, blob = false) => {
    if (!token) return Promise.reject(new ReviewApiError("authorization_expired", 401));
    return request<T>(fetcher, endpoint(apiBase, `${base}${suffix}`), {
      method, headers: { Authorization: `Bearer ${token}`, Accept: blob ? "application/pdf" : "application/json",
        ...(command === undefined ? {} : { "Content-Type": "application/json" }) },
      ...(command === undefined ? {} : { body: JSON.stringify(command) }),
      signal: signal ? AbortSignal.any([controller.signal, signal]) : controller.signal,
    }, blob);
  };
  return {
    context,
    workspace: (signal?: AbortSignal) => call<ReviewWorkspace>("", "GET", undefined, signal),
    status: (signal?: AbortSignal) => call<ReviewStatusSnapshot>("/status", "GET", undefined, signal),
    candidates: (signal?: AbortSignal) => call<{ candidates: CandidateLookup; review_version: number; source_fingerprint: string | null }>("/candidates", "GET", undefined, signal),
    save: (command: SaveDecisionsCommand, signal?: AbortSignal) => call<ReviewCommandReceipt>("/decisions", "PUT", command, signal),
    refresh: (command: RefreshCommand, signal?: AbortSignal) => call<ReviewCommandReceipt>("/refresh", "POST", command, signal),
    approve: (command: ApproveCommand, signal?: AbortSignal) => call<ReviewCommandReceipt>("/approve", "POST", command, signal),
    pdf: (revision: string, signal?: AbortSignal) => call<Blob>(`/pdf/${encodeURIComponent(revision)}`, "GET", undefined, signal, true),
    destroy: () => { token = null; controller.abort(); },
  };
}

export type ReviewApi = ReturnType<typeof createReviewApi>;
