/** Local browser fixtures only. This module is never imported by application code. */
export const FIELD_NAMES = ["Scale", "Page Size", "Drawing Reference", "Drawing Title", "Bauder Contract Number", "Bauder Roof Num", "Incoming Information", "Revision", "Date Received", "Company", "Contact", "Surveyor", "Target U Value", "Target Min U Value", "Membrane", "Tapered Insulation", "VCL", "Decking"];

export function createFixture(scenario = "new_enquiry", itemId = "101") {
  const amendment = scenario === "amendment" || scenario === "ambiguous";
  const now = "2026-11-09T10:00:00Z";
  const identity = { source_fingerprint: `source-${itemId}-1`, extraction_identity: `extraction-${itemId}-1`, source_policy_version: "review-source-v1", reference_parser_version: "drawing-reference-v1" };
  const mapping = { status: "unmapped", value: null, catalogue_version: null, target_identity: null };
  const citation = { source_id: `email-${itemId}`, excerpt: amendment ? "Please adjust AB17221_25.01 - A to achieve 0.16 W/m²K on roof A." : "Please design a new tapered scheme for Riverside School. The target U-value is 0.18 W/m²K.", location: "Current client request, paragraph 1", scope: "Roof A" };
  const project = { board_id: "200", item_id: "201" };
  const drawing = { board_id: "300", item_id: "301" };
  const projects = amendment ? [{ record: project, project_reference: "AB17221", project_title: "Riverside School", rationale: "Exact detected project reference", similarity_score: null }] : [];
  const drawings = amendment ? [{ record: drawing, project, literal_reference: "AB17221_25.01 - A", roof_scope: "Roof A", lifecycle: "active", match_kind: "exact_email", rationale: "Full drawing reference appears in the current client request." }] : [];
  if (scenario === "ambiguous") drawings.push({ record: { board_id: "300", item_id: "302" }, project, literal_reference: "AB17221_25.02 - B", roof_scope: "Roof B", lifecycle: "active", match_kind: "latest_in_scope", rationale: "Another current roof exists; confirm which roof the request concerns." });
  const candidates = { status: "available", projects, drawings, issues: scenario === "ambiguous" ? ["Several roofs are plausible. Select the relevant existing drawing."] : [] };
  const intent = { schema_version: 1, recommended_type: scenario === "ambiguous" ? "uncertain" : amendment ? "amendment" : "new_enquiry", explanation: amendment ? "The selected client asks to adjust an existing drawing and provides its reference." : "The selected client asks for a new scheme. No previous project details are imported.", evidence: [citation], detected_references: amendment ? [{ kind: "drawing", literal: "AB17221_25.01 - A", evidence: citation, role: "current_request" }] : [], matched_projects: projects, ambiguity_reasons: scenario === "ambiguous" ? ["The attachment also mentions roof B; confirm the intended scope."] : [] };
  const values = { Scale: "1:100", "Page Size": "A1", "Drawing Reference": amendment ? "AB17221_25.01 - A" : null, "Drawing Title": itemId === "102" ? "Northgate Library roof" : "Riverside School roof", "Bauder Contract Number": "BC-123", "Bauder Roof Num": "Roof A", "Incoming Information": "", Revision: amendment ? "25.01 - A" : null, "Date Received": "09/11/2026", Company: amendment ? "Riverside Estates Ltd" : "Riverside School", Contact: "Alex Morgan", Surveyor: "Jamie Patel", "Target U Value": amendment ? "0.16" : "0.18", "Target Min U Value": "0.20", Membrane: "Bauder Total Roof System", "Tapered Insulation": "PIR", VCL: "BauderTEC KSD", Decking: "Concrete" };
  const crmLabels = amendment ? ["Company", "Surveyor", "Membrane", "Tapered Insulation", "VCL", "Decking"] : [];
  const fields = {};
  const sourceFields = {};
  for (const [index, label] of FIELD_NAMES.entries()) {
    const value = values[label];
    const source = value === null ? "unassigned" : value === "" ? "default_blank" : crmLabels.includes(label) ? "monday_crm" : "email_context";
    const cad_mapping = value === null || value === "" ? { ...mapping, status: "not_applicable" } : { ...mapping };
    const email_evidence = source === "email_context" ? [citation] : [];
    const crm_evidence = source === "monday_crm" ? [{ record: label === "Company" ? project : drawing, column_id: `field-${index}`, scope: "Roof A" }] : [];
    const proposal = { proposal_id: `proposal-${itemId}-${index}-1`, display_value: value, source, rationale: source === "monday_crm" ? "Current email omits this value; recommendation comes from the selected existing project/drawing." : value === null ? "Assigned after design unless the reviewer specifies a value." : value === "" ? "Incoming Information defaults to blank and can be overridden." : "Extracted from the selected current client request.", email_evidence, crm_evidence, derived_from: null, cad_mapping };
    const sources = { email_facts: value && source !== "monday_crm" ? [{ value, evidence: [citation] }] : label === "Company" && amendment ? [{ value: "Riverside School", evidence: [citation] }] : [], crm_recommendations: crm_evidence.length ? [{ value, evidence: crm_evidence, rationale: "Recommendation from the selected project/drawing." }] : [], proposal, applicability: value === null ? "unassigned" : "applicable", scope: "Roof A", issues: label === "Company" && amendment ? [{ code: "company_disagreement", detail: "Email company Riverside School differs from the linked Account Riverside Estates Ltd. Reconcile this recommendation." }] : [] };
    sourceFields[label] = structuredClone(sources);
    fields[label] = { ...sources, decision: null, effective: { display_value: value, source, source_label: source === "email_context" ? "Email Context" : source === "monday_crm" ? "Monday CRM recommendation" : source === "unassigned" ? "Not assigned" : "Default blank", cad_mapping, reviewer_accepted: false, requires_reconfirmation: false, email_evidence, crm_evidence, derived_from: null } };
  }
  const revision = `pdf-${itemId}-1`;
  const pdf = { revision, filename: `Performance Specification ${itemId}.pdf`, status: "published", published: true, size_bytes: 640, url: `/api/specification-review/items/100/${itemId}/pdf/${revision}` };
  return { board_id: "100", item_id: itemId, status: "awaiting_final_review", review_version: 0, source_fingerprint: identity.source_fingerprint, decision_digest: `decisions-${itemId}-0`, desired_publication_identity: `publication-${itemId}-0`, review: { schema_version: 1, review_version: 0, selections: { selected_request_type: null, selected_project: null, selected_drawing: null }, field_decisions: {}, source_dispositions: [] }, source: { schema_version: 1, identity, intent, request_type: amendment ? "amendment" : "new_enquiry", selected_project: null, selected_drawing: null, candidates, fields: sourceFields, source_issues: [] }, fields, candidates, queue: { requested_generation: 1, completed_generation: 1, running: false, failed: false }, pdf, published_pdf: structuredClone(pdf), approval: null, history: [{ event_id: `init-${itemId}`, command_type: "source_initialized", review_version: 0, actor_monday_user_id: null, created_at: now, explanation: null }], capabilities: { read: true, edit: true, approve: true }, scope_reason: null };
}

/** Injected by the localhost-only harness proxy before the real application boots. */
export function installHarness(fixtures) {
  const scenario = new URL(location.href).searchParams.get("scenario") || "new_enquiry";
  const clone = (value) => structuredClone(value);
  let context = { accountId: "1", boardId: "100", itemId: "101" };
  let current = clone(fixtures[scenario] || fixtures.new_enquiry);
  let contextListeners = [];
  let expired = false;
  let nextFailure = null;
  let receipts = new Map();
  let requests = [];
  let beforeAnalysis = null;
  const tell = (type, data) => parent.postMessage({ source: "specification-review-harness", type, data }, location.origin);
  const report = () => tell("requests", requests);
  const record = (path, method, body, headers) => {
    requests.push({ path, method, body, hasAuthorization: Boolean(new Headers(headers).get("Authorization")) });
    report();
  };
  window.mondaySdk = () => ({
    get: async (kind) => ({ data: kind === "context" ? clone(context) : "local-mocked-sdk-token" }),
    listen: (_kind, callback) => { contextListeners.push(callback); return () => { contextListeners = contextListeners.filter((listener) => listener !== callback); }; },
    execute: async (_kind, options) => { tell("oauth", options.url); return {}; },
  });
  function emitContext() { contextListeners.forEach((listener) => listener({ data: clone(context) })); }
  function sourceChanged() {
    current.source_fingerprint += "-changed";
    current.source.identity.source_fingerprint = current.source_fingerprint;
    current.source.fields["Target U Value"].proposal.display_value = "0.15";
    current.source.fields["Target U Value"].proposal.proposal_id += "-changed";
    current.fields["Target U Value"].proposal = clone(current.source.fields["Target U Value"].proposal);
    for (const field of Object.values(current.fields)) if (field.decision) field.effective.requires_reconfirmation = true;
    current.approval = null;
    current.status = "needs_review";
    current.desired_publication_identity += "-changed";
  }
  function publish() {
    current.pdf = { revision: `pdf-${context.itemId}-${current.review_version + 2}`, filename: `Performance Specification ${context.itemId}.pdf`, status: "published", published: true, size_bytes: 640, url: "unused-scoped-path" };
    current.published_pdf = clone(current.pdf);
    current.queue.completed_generation = current.queue.requested_generation;
    current.queue.running = false;
    current.queue.failed = false;
    current.status = "awaiting_final_review";
    tell("notice", "Matching PDF published. Use the review's Check for updates button, or wait for polling.");
  }
  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin || event.source !== parent || event.data?.source !== "specification-review-controls") return;
    switch (event.data.action) {
      case "analysis_pending":
        beforeAnalysis = clone(current);
        current.provisional = true;
        current.status = "analysis_pending";
        current.source_fingerprint = null;
        current.extraction_identity = `current-extraction-${context.itemId}`;
        current.pdf = null;
        current.approval = null;
        current.analysis = { ready: false, current_extraction: true, status: "running", stage: "matching",
          extraction_identity: current.extraction_identity, phase: "extracting_fields",
          source_counts: { total: 8, read: 5, skipped: 3 },
          created_at: new Date().toISOString(), started_at: new Date().toISOString(),
          updated_at: new Date().toISOString(), completed_at: null, heartbeat_at: new Date().toISOString() };
        break;
      case "analysis_rendering":
        if (current.analysis) current.analysis = { ...current.analysis, stage: "rendering", phase: "rendering", updated_at: new Date().toISOString() };
        break;
      case "analysis_complete":
        if (beforeAnalysis) current = { ...beforeAnalysis, provisional: false,
          extraction_identity: current.extraction_identity, analysis: { ...current.analysis, ready: true, status: "completed", completed_at: new Date().toISOString() } };
        break;
      case "regeneration_failure": nextFailure = "publication_failed"; break;
      case "concurrent_save": nextFailure = "stale_review"; break;
      case "sources_change": sourceChanged(); break;
      case "session_expiry": expired = true; break;
      case "switch_item": context = { ...context, itemId: context.itemId === "101" ? "102" : "101" }; current = clone(fixtures[context.itemId === "102" ? "second_item" : scenario] || fixtures.new_enquiry); expired = false; receipts = new Map(); emitContext(); break;
      case "leave_landing_zone": current.capabilities = { read: true, edit: false, approve: false }; current.status = "read_only"; current.scope_reason = "outside_landing_zone"; break;
      case "publish": publish(); break;
      case "inspect": report(); break;
    }
    tell("notice", `Applied ${event.data.action.replaceAll("_", " ")}.`);
  });
  const originalFetch = window.fetch.bind(window);
  const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json", "Cache-Control": "private, no-store" } });
  const error = (code, status) => json({ detail: { code, message: "Mocked boundary condition" } }, status);
  function audit() { return { actor: { app_user_id: "reviewer-local", monday_user_id: "10", monday_account_id: "1" }, saved_at: new Date().toISOString(), reviewed_source: clone(current.source.identity), explanation: null }; }
  function receipt(changed, approvalId = null) { return { review_version: current.review_version, source_fingerprint: current.source_fingerprint, decision_digest: current.decision_digest, desired_publication_identity: current.desired_publication_identity, approval_id: approvalId, changed, event_id: `event-${requests.length}` }; }
  function pdfBytes() {
    const lines = ["Performance Specification", `Item ${context.itemId}`, ...Object.entries(current.fields).flatMap(([label, field]) => [`${label}: ${field.effective.display_value ?? "Not assigned"}`, `Source: ${field.effective.source_label}`])];
    const escaped = lines.map((line) => line.replaceAll("\\", "\\\\").replaceAll("(", "\\(").replaceAll(")", "\\)").replace(/[^\x20-\x7e]/g, "?"));
    const stream = `BT /F1 10 Tf 30 800 Td 13 TL ${escaped.map((line) => `(${line}) Tj T*`).join(" ")} ET`;
    const objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>", "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`];
    let text = "%PDF-1.4\n";
    const offsets = [0];
    objects.forEach((object, index) => { offsets.push(text.length); text += `${index + 1} 0 obj\n${object}\nendobj\n`; });
    const start = text.length;
    text += `xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map((offset) => String(offset).padStart(10, "0") + " 00000 n \n").join("")}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`;
    return text;
  }
  window.fetch = async (input, options = {}) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input.url, location.origin);
    if (!url.pathname.startsWith("/api/specification-review")) return originalFetch(input, options);
    if (options.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    const method = options.method || "GET";
    const body = options.body ? JSON.parse(options.body) : null;
    // The log deliberately excludes credentials, including bootstrap sessionToken.
    record(url.pathname, method, body && url.pathname.endsWith("/session") ? { context: body.context } : body, options.headers);
    if (url.pathname.endsWith("/session")) {
      expired = false;
      return json({ status: "ready", access_token: "local-item-scoped-bearer", token_type: "Bearer", expires_at: new Date(Date.now() + 900000).toISOString(), capabilities: clone(current.capabilities), scope_reason: current.scope_reason, account_id: "1", user_id: "10", board_id: context.boardId, item_id: context.itemId });
    }
    if (expired) return error("authorization_expired", 401);
    const pathMatch = url.pathname.match(/\/items\/([^/]+)\/([^/]+)/);
    if (!pathMatch || pathMatch[1] !== context.boardId || pathMatch[2] !== context.itemId) return error("reviewer_forbidden", 403);
    if (url.pathname.includes("/pdf/")) return new Response(pdfBytes(), { headers: { "Content-Type": "application/pdf" } });
    if (method === "GET") {
      if (url.pathname.endsWith("/candidates")) return json({ candidates: clone(current.candidates), review_version: current.review_version, source_fingerprint: current.source_fingerprint });
      if (url.pathname.endsWith("/status")) { const { source, fields, review, candidates, history, ...status } = clone(current); return json(status); }
      return json(clone(current));
    }
    if (!current.capabilities.edit) return error("scope_changed", 409);
    if (current.provisional) return error("source_waiting", 202);
    if (receipts.has(body.idempotency_key)) return json(clone(receipts.get(body.idempotency_key)));
    if (nextFailure === "stale_review") { nextFailure = null; current.review_version++; current.review.review_version++; current.decision_digest += "-other"; return error("stale_review", 409); }
    if (body.expected_review_version !== current.review_version) return error("stale_review", 409);
    if (body.expected_source_fingerprint !== current.source_fingerprint) return error("source_changed", 409);
    if (url.pathname.endsWith("/approve")) {
      if (current.status !== "awaiting_final_review" || body.artifact_revision !== current.published_pdf?.revision || body.expected_decision_digest !== current.decision_digest || body.expected_publication_identity !== current.desired_publication_identity) return error("stale_review", 409);
      if (!current.review.selections.selected_request_type) return error("candidates_ambiguous", 409);
      current.approval = { id: `approval-${requests.length}`, artifact_revision: body.artifact_revision, review_version: current.review_version, actor_monday_user_id: "10", approved_at: new Date().toISOString() };
      current.status = "complete";
      const result = receipt(false, current.approval.id); receipts.set(body.idempotency_key, result); return json(result);
    }
    if (url.pathname.endsWith("/decisions")) {
      Object.assign(current.review.selections, body.selections || {});
      current.source.request_type = current.review.selections.selected_request_type || current.source.request_type;
      current.source.selected_project = clone(current.review.selections.selected_project);
      current.source.selected_drawing = clone(current.review.selections.selected_drawing);
      for (const [label, operation] of Object.entries(body.field_operations || {})) {
        const field = current.fields[label];
        let decision = operation;
        if (operation.operation === "restore_recommendation") {
          delete current.review.field_decisions[label]; field.decision = null;
        } else {
          if (operation.operation === "accept_recommendation") decision = { operation: "accept_recommendation", snapshot: clone(field.proposal) };
          if (operation.operation === "specify_value") decision = { ...operation, cad_mapping: { status: "unmapped", value: null, catalogue_version: null, target_identity: null } };
          field.decision = { decision, audit: audit() }; current.review.field_decisions[label] = clone(field.decision);
        }
        const manual = ["specify_value", "specify_blank"].includes(operation.operation);
        field.effective = { ...field.effective, display_value: operation.operation === "specify_value" ? operation.display_value : operation.operation === "specify_blank" ? "" : field.proposal.display_value, source: manual ? "reviewer_specified" : field.proposal.source, source_label: manual ? "Reviewer Specified" : field.proposal.source === "email_context" ? "Email Context" : field.proposal.source === "monday_crm" ? "Monday CRM recommendation" : field.proposal.source === "unassigned" ? "Not assigned" : "Default blank", reviewer_accepted: operation.operation === "accept_recommendation", requires_reconfirmation: false };
      }
      for (const disposition of body.source_dispositions || []) current.review.source_dispositions.push({ ...disposition, audit: audit() });
      current.review_version++; current.review.review_version = current.review_version;
      current.decision_digest = `decisions-${context.itemId}-${current.review_version}`;
      current.desired_publication_identity = `publication-${context.itemId}-${current.review_version}`;
    }
    current.approval = null;
    current.queue.requested_generation++;
    current.queue.running = false;
    current.status = nextFailure === "publication_failed" ? "publication_failed" : "regenerating_pdf";
    current.queue.failed = nextFailure === "publication_failed";
    nextFailure = null;
    current.history.unshift({ event_id: `event-${requests.length}`, command_type: url.pathname.endsWith("/refresh") ? "recommendations_refreshed" : "decisions_saved", review_version: current.review_version, actor_monday_user_id: "10", created_at: new Date().toISOString(), explanation: null });
    const result = receipt(true); receipts.set(body.idempotency_key, clone(result)); return json(result);
  };
  tell("ready", { scenario });
}
