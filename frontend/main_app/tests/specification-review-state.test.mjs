import assert from "node:assert/strict";
import { test } from "node:test";
import {
  approvalCommand, canComplete, completionIssues, draftValidationErrors, emptyDraft, hasUnsavedChanges,
  mergeStatus, rebaseDraft, sameReviewIdentity, saveCommand, updateField, updateSelection,
} from "../lib/specification-review-state.ts";
import { FIELD_NAMES } from "../lib/specification-review-api.ts";

function workspace(overrides = {}) {
  const pdf = { revision: "revision-1", published: true, filename: "spec.pdf", status: "published" };
  return {
    board_id: "10", item_id: "20", review_version: 3, source_fingerprint: "source-1",
    decision_digest: "decisions-1", desired_publication_identity: "publication-1",
    status: "awaiting_final_review", capabilities: { read: true, edit: true, approve: true },
    queue: { requested_generation: 4, completed_generation: 4, running: false, failed: false },
    pdf, published_pdf: pdf, approval: null, fields: { Decking: { effective: { display_value: "Timber" } } },
    review: { selections: { selected_request_type: "new_enquiry", selected_project: null, selected_drawing: null } },
    ...overrides,
  };
}

test("all 18 PDF fields support distinct manual, blank, recommendation and restore commands", () => {
  assert.equal(FIELD_NAMES.length, 18);
  for (const label of FIELD_NAMES) {
    for (const operation of [
      { operation: "specify_value", display_value: "Reviewer text – é" },
      { operation: "specify_blank" },
      { operation: "accept_recommendation", proposal_id: "proposal-1" },
      { operation: "restore_recommendation" },
    ]) {
      const draft = updateField(emptyDraft(), label, operation);
      const command = saveCommand(workspace(), draft, "retry-1");
      assert.deepEqual(command.field_operations[label], operation);
      assert.equal(command.expected_review_version, 3);
      assert.equal(command.expected_source_fingerprint, "source-1");
      assert.equal(command.idempotency_key, "retry-1");
      assert.equal("actor" in command, false);
    }
  }
});

test("intentional blank remains different from invalid empty manual text", () => {
  const blank = updateField(emptyDraft(), "Incoming Information", { operation: "specify_blank" });
  assert.deepEqual(draftValidationErrors(blank), {});
  const emptyText = updateField(emptyDraft(), "Revision", { operation: "specify_value", display_value: "  " });
  assert.match(draftValidationErrors(emptyText).Revision, /Specify blank/);
  assert.throws(() => saveCommand(workspace(), emptyText, "retry-1"), /highlighted/);
});

test("changing a project clears a previous drawing while explicit null clears selections", () => {
  let draft = updateSelection(emptyDraft(), { selected_project: { board_id: "30", item_id: "40" }, selected_drawing: { board_id: "31", item_id: "41" } });
  draft = updateSelection(draft, { selected_project: { board_id: "30", item_id: "50" } });
  assert.equal(draft.selections.selected_drawing, null);
  draft = updateSelection(draft, { selected_request_type: null });
  const command = saveCommand(workspace(), draft, "retry-2");
  assert.equal(command.selections.selected_request_type, null);
  assert.deepEqual(command.selections.selected_project, { board_id: "30", item_id: "50" });
  assert.equal(hasUnsavedChanges(draft), true);
});

test("polling changed sources cannot silently rebase unsaved corrections or replace evidence", () => {
  const initial = workspace();
  const draft = updateField(emptyDraft(), "Decking", { operation: "specify_value", display_value: "My unsaved correction" });
  const newer = workspace({ source_fingerprint: "source-2", review_version: 4, status: "regenerating_pdf" });
  assert.equal(sameReviewIdentity(initial, newer), false);
  const merged = mergeStatus(initial, newer);
  assert.equal(merged.status, "regenerating_pdf");
  assert.equal(merged.source_fingerprint, "source-1");
  assert.strictEqual(merged.fields, initial.fields);
  assert.equal(saveCommand(merged, draft, "retry-3").expected_review_version, 3);
  assert.equal(draft.field_operations.Decking.display_value, "My unsaved correction");
});

test("status from another item never crosses the context boundary", () => {
  const initial = workspace();
  assert.strictEqual(mergeStatus(initial, workspace({ item_id: "another" })), initial);
});

test("polling an item leaving Landing Zone exposes its read-only reason while preserving available data", () => {
  for (const ready of [false, true]) {
    const initial = workspace({ status: ready ? "needs_review" : "source_waiting",
      source: ready ? { identity: { source_fingerprint: "source-1" } } : null,
      pdf: ready ? workspace().pdf : null, published_pdf: ready ? workspace().published_pdf : null });
    const next = { ...initial, status: "read_only", scope_reason: "outside_landing_zone",
      capabilities: { read: true, edit: false, approve: false } };
    const merged = mergeStatus(initial, next);
    assert.equal(merged.status, "read_only");
    assert.equal(merged.scope_reason, "outside_landing_zone");
    assert.equal(merged.capabilities.edit, false);
    assert.strictEqual(merged.source, initial.source);
    assert.strictEqual(merged.pdf, initial.pdf);
    assert.equal(canComplete(merged, emptyDraft(), "revision-1"), false);
    assert.equal(initial.status, ready ? "needs_review" : "source_waiting");
  }
});

test("completion requires the loaded current published PDF and settled saved identities", () => {
  const ready = workspace();
  assert.equal(canComplete(ready, emptyDraft(), "revision-1"), true);
  assert.equal(canComplete(ready, emptyDraft()), false);
  assert.equal(canComplete(ready, emptyDraft(), "old-revision"), false);
  assert.equal(canComplete(ready, updateField(emptyDraft(), "Scale", { operation: "specify_blank" }), "revision-1"), false);
  for (const status of ["regenerating_pdf", "publication_failed", "source_unavailable", "source_waiting", "read_only", "complete"]) {
    assert.equal(canComplete(workspace({ status }), emptyDraft(), "revision-1"), false, status);
  }
  for (const patch of [{ running: true }, { failed: true }, { requested_generation: 5 }]) {
    assert.equal(canComplete(workspace({ queue: { ...ready.queue, ...patch } }), emptyDraft(), "revision-1"), false);
  }
  assert.equal(canComplete(workspace({ capabilities: { read: true, edit: false, approve: false } }), emptyDraft(), "revision-1"), false);
});

test("approval sends the exact artifact, decision and publication identities", () => {
  assert.deepEqual(approvalCommand(workspace(), "approve-retry"), {
    expected_review_version: 3, expected_source_fingerprint: "source-1", idempotency_key: "approve-retry",
    artifact_revision: "revision-1", expected_decision_digest: "decisions-1", expected_publication_identity: "publication-1",
  });
  assert.throws(() => approvalCommand(workspace({ published_pdf: null }), "approve-retry"), /published/);
});

test("retry payload stays stable when the reviewer keeps editing their draft", () => {
  const draft = updateField(emptyDraft(), "Contact", { operation: "specify_value", display_value: "Original contact" });
  const command = saveCommand(workspace(), draft, "stable-retry");
  draft.field_operations.Contact.display_value = "Later edit";
  assert.equal(command.field_operations.Contact.display_value, "Original contact");
});

function reviewedWorkspace() {
  const identity = { source_fingerprint: "source-1", extraction_identity: "extraction-1", source_policy_version: "policy-1", reference_parser_version: "parser-1" };
  const fields = Object.fromEntries(FIELD_NAMES.map((label) => [label, {
    effective: { source: "email_context", requires_reconfirmation: false }, issues: [],
    decision: { decision: { operation: "accept_recommendation" } },
  }]));
  // These three automatic values do not need fabricated reviewer acceptance.
  for (const [label, source] of [["Drawing Reference", "unassigned"], ["Revision", "unassigned"], ["Incoming Information", "default_blank"]]) {
    fields[label] = { effective: { source, requires_reconfirmation: false }, issues: [], decision: null };
  }
  return workspace({ fields, source: { identity, request_type: "new_enquiry", selected_project: null, selected_drawing: null,
    candidates: { drawings: [] }, source_issues: [] },
    review: { selections: { selected_request_type: "new_enquiry", selected_project: null, selected_drawing: null }, source_dispositions: [] } });
}

test("completion requires a saved request type and explicit field choices while allowing unassigned new drawing identity", () => {
  const reviewed = reviewedWorkspace();
  assert.deepEqual(completionIssues(reviewed), []);
  assert.equal(canComplete(reviewed, emptyDraft(), "revision-1"), true);
  reviewed.review.selections.selected_request_type = null;
  assert.ok(completionIssues(reviewed).includes("Choose and save the request type."));
  reviewed.review.selections.selected_request_type = "new_enquiry";
  reviewed.fields.Decking.decision = null;
  assert.ok(completionIssues(reviewed).includes("Accept or specify Decking."));
  assert.equal(canComplete(reviewed, emptyDraft(), "revision-1"), false);
  reviewed.fields.Decking.effective.source = "reviewer_specified";
  reviewed.fields.Decking.decision = { decision: { operation: "specify_blank" } };
  assert.deepEqual(completionIssues(reviewed), []);
});

test("amendment completion requires an active selected drawing under the selected project", () => {
  const reviewed = reviewedWorkspace();
  reviewed.review.selections.selected_request_type = "amendment";
  reviewed.source.request_type = "amendment";
  assert.ok(completionIssues(reviewed).includes("Choose and save the amendment project and drawing."));
  const project = { board_id: "100", item_id: "101" };
  const drawing = { board_id: "200", item_id: "201" };
  reviewed.review.selections.selected_project = reviewed.source.selected_project = project;
  reviewed.review.selections.selected_drawing = reviewed.source.selected_drawing = drawing;
  reviewed.source.candidates.drawings = [{ record: drawing, project, lifecycle: "archived" }];
  assert.ok(completionIssues(reviewed).includes("Select a currently active drawing for this project."));
  reviewed.source.candidates.drawings[0].lifecycle = "active";
  assert.deepEqual(completionIssues(reviewed), []);
});

test("source changes require reconfirmed choices and current source dispositions", () => {
  const reviewed = reviewedWorkspace();
  reviewed.fields.Company.effective.requires_reconfirmation = true;
  reviewed.source.source_issues.push({ source_id: "attachment-1", code: "unread_file", detail: "Unread attachment" });
  reviewed.review.source_dispositions.push({ source_id: "attachment-1", issue_code: "unread_file", explanation: "Checked separately",
    audit: { reviewed_source: { ...reviewed.source.identity, source_fingerprint: "old-source" } } });
  assert.deepEqual(completionIssues(reviewed), ["Reconfirm Company against the current sources.", "Review source issue: Unread attachment"]);
  reviewed.fields.Company.effective.requires_reconfirmation = false;
  reviewed.review.source_dispositions[0].audit.reviewed_source = reviewed.source.identity;
  assert.deepEqual(completionIssues(reviewed), []);
});

test("explicit draft rebasing preserves manual text, blanks and restore but drops stale acceptances and source acknowledgements", () => {
  const draft = {
    selections: { selected_request_type: "amendment" },
    field_operations: { Contact: { operation: "specify_value", display_value: "Keep my text" }, Revision: { operation: "specify_blank" },
      Decking: { operation: "restore_recommendation" }, Company: { operation: "accept_recommendation", proposal_id: "old-proposal" } },
    source_dispositions: [{ source_id: "file", issue_code: "unread", explanation: "Old acknowledgement" }],
  };
  const rebased = rebaseDraft(draft);
  assert.deepEqual(rebased.field_operations, { Contact: draft.field_operations.Contact, Revision: draft.field_operations.Revision, Decking: draft.field_operations.Decking });
  assert.deepEqual(rebased.selections, draft.selections);
  assert.deepEqual(rebased.source_dispositions, []);
  assert.equal(draft.field_operations.Company.proposal_id, "old-proposal");
});
