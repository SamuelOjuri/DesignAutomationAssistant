import assert from "node:assert/strict";
import { test } from "node:test";
import { getReviewStatusDescription } from "../lib/specification-review-status.ts";

test("outside Landing Zone without source analysis or a PDF reports availability without inventing job progress", () => {
  const description = getReviewStatusDescription({
    status: "read_only", scopeReason: "outside_landing_zone", hasSource: false, hasPdf: false,
  });
  assert.equal(description.title, "Outside Landing Zone");
  assert.match(description.detail, /Editing and completion are unavailable/);
  assert.match(description.detail, /Source analysis is unavailable in this view/);
  assert.match(description.detail, /No PDF is available in this view/);
  assert.doesNotMatch(description.detail, /stopped|cancelled|running|resume|before|retained PDF/i);
});

test("outside Landing Zone keeps available recommendations, decisions and a retained PDF readable", () => {
  const description = getReviewStatusDescription({
    status: "read_only", scopeReason: "outside_landing_zone", hasSource: true, hasPdf: true,
  });
  assert.equal(description.title, "Outside Landing Zone");
  assert.match(description.detail, /source recommendations remain readable/);
  assert.match(description.detail, /saved decisions are retained/);
  assert.match(description.detail, /retained PDF remains available to inspect/);
  assert.doesNotMatch(description.detail, /Source analysis is unavailable|No PDF|resume/i);
});

test("source recommendations and PDF availability are independent", () => {
  const sourceOnly = getReviewStatusDescription({ status: "read_only", hasSource: true, hasPdf: false });
  assert.match(sourceOnly.detail, /source recommendations remain readable/);
  assert.match(sourceOnly.detail, /No PDF is available/);
  const pdfOnly = getReviewStatusDescription({ status: "read_only", hasSource: false, hasPdf: true });
  assert.match(pdfOnly.detail, /Source analysis is unavailable/);
  assert.match(pdfOnly.detail, /retained PDF remains available/);
});

test("Completed Folder explains read-only scope without claiming a PDF exists", () => {
  const description = getReviewStatusDescription({ status: "read_only", scopeReason: "completed_folder" });
  assert.equal(description.title, "Completed Folder");
  assert.match(description.detail, /This item is in Completed Folder/);
  assert.match(description.detail, /No PDF is available/);
});

for (const [reason, expected] of [
  ["review_disabled", /review access is disabled/],
  ["processing_off", /processing is turned off/],
  ["item_not_allowlisted", /not included in the current specification pilot/],
]) {
  test(`${reason} explains its specific access restriction`, () => {
    const description = getReviewStatusDescription({ status: "read_only", scopeReason: reason });
    assert.match(description.detail, expected);
    assert.match(description.detail, /Editing and completion are unavailable/);
    assert.doesNotMatch(description.detail, /retained PDF|resume/i);
  });
}

test("unknown and absent access reasons stay generic rather than guessing a cause", () => {
  for (const scopeReason of [undefined, null, "future_restriction"]) {
    const description = getReviewStatusDescription({ status: "read_only", scopeReason });
    assert.equal(description.title, "Read-only");
    assert.match(description.detail, /Editing and completion are unavailable/);
    assert.doesNotMatch(description.detail, /outside Landing Zone|pilot|disabled|turned off/i);
  }
});

test("normal source waiting preserves the existing description without claiming a job is running", () => {
  const description = getReviewStatusDescription({ status: "source_waiting" });
  assert.equal(description.title, "Waiting for source analysis");
  assert.equal(description.detail, "Current email and attachment analysis is not ready yet. This view will check for updates.");
  assert.equal(description.tone, "bg-secondary text-foreground");
  assert.doesNotMatch(description.detail, /queued|running|retained PDF/i);
});

test("source failures only mention a previous PDF when one is available", () => {
  for (const status of ["source_unavailable", "publication_failed"]) {
    const absent = getReviewStatusDescription({ status, hasPdf: false });
    assert.match(absent.detail, /No PDF is available/);
    assert.doesNotMatch(`${absent.title} ${absent.detail}`, /previous PDF|retained PDF/);
    const available = getReviewStatusDescription({ status, hasPdf: true });
    assert.match(`${available.title} ${available.detail}`, /previous PDF/);
  }
});

test("normal review states and unknown status fallback retain their existing descriptions", () => {
  for (const [status, title] of [
    ["needs_review", "Needs review"], ["regenerating_pdf", "Regenerating PDF"],
    ["awaiting_final_review", "Awaiting final review"], ["complete", "Complete"],
    ["review_required_again", "Review required again"], ["future_status", "Needs review"],
  ]) {
    assert.equal(getReviewStatusDescription({ status }).title, title);
  }
});
