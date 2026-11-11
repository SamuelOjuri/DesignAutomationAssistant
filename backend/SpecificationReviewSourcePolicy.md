# Specification Review source policy and PDF (Phase 4)

Phase 4 connects the typed source and decision contracts to the specification
publisher. It uses `output-v20`, evidence version 3, source policy
`specification-review-source-v1`, the versioned reference parser, and
`performance-specification-v5`. Apply through migration
`0021_processing_acceleration` before running the current worker. Publishing remains disabled by default. Phase 5 supplies the
Landing Zone lifecycle, and [Phase 6](SpecificationReviewAPI.md) supplies the review
API/auth boundary. The embedded UI remains Phase 7. Phase 4 alone enables none of
these entry points and does not change group membership.

## Recommendations and decisions

The resolver returns `reviewSource`, a validated `ReviewSourceSnapshot` containing
all 18 fields, the request-intent recommendation, project/drawing context,
email citations, exact CRM record/column identities and review issues. A reviewer
selection chooses the policy branch independently of the AI recommendation.
Missing or uncertain intent stays unresolved instead of inferring an Amendment
from a holding-item relation.

New Enquiries use email facts. Drawing Reference and Revision remain unassigned;
Incoming Information defaults to blank. Amendments prefer email facts, with
selected project/drawing recommendations for missing technical fields, Contact
and Surveyor. Company recommends the selected project's Account and exposes a
differing email Company for reconciliation. Date Received comes from the email
request. Holding Accounts/date and shared project/drawing values are never written
by a review save.

Candidate roofs are kept separate. Saved selections scope live reads and
recommendations. Optional unreadable CRM recommendations remain visible for
review; they cannot erase an authoritative field decision or bypass required live
holding-item and extraction identity checks.

The worker loads durable decisions separately from processing `review_json`.
Specify value and Specify blank override every field, including Incoming
Information and Revision. Acceptance retains the exact saved recommendation and
its original provenance. Restoring a recommendation removes the decision and
uses current recommendations. Original email and CRM alternatives remain in the
immutable artifact snapshot and review audit.

## PDF and CAD mapping

The strict automated CAD schema remains separate from reviewer display text.
Application-authored publication metadata stores display values, source labels,
mapping limitations and the review version. Each field's source is printed on a
separate line, including exactly `Source: Reviewer Specified` for explicit values
and intentional blanks. Source lines are not part of the CAD value. PDF text is
selectable, uses embedded fonts and supports multi-page content.

Exact supported CAD choices can map through the versioned local contract without
another model call. Custom reviewer text remains literal in the PDF and is marked
unmapped when an authoritative target mapping is unavailable. Monday IDs and
display text do not establish TRICS identities. An intentional blank stays blank
and suppresses fallback.

## Identity, caching and recovery

New artifacts contain immutable `source_json.review`: review version, decision
digest, source/extraction identity, recommendation fingerprint, selections and
per-field provenance. The artifact revision includes that metadata and the
publication/model/template version. Historic artifacts are retained without
inventing review metadata or approval.

Translation caching uses the normalized automatic inputs and model version,
independently of provenance and review version. A provenance-only change can
produce a new PDF without another model call. Publication notes and source labels
are rebuilt even when the CAD translation is reused.

Source reads occur outside database locks. The worker checks queue generation,
lease and saved decisions before using results, after translation and around
upload. When relevant sources change, a short locked observation retains decisions,
invalidates current completion and records an event. Human decisions remain in the
review table through retries and replacement failures. Existing upload adoption,
integrity validation and ledger-owned cleanup remain in force.

A verified live input that is ahead of its extraction clears completion while
waiting for fresh analysis. Its invalidation event retains the previously
approved source identity; the worker does not claim that old extraction describes
the new request.

Run the Phase 4 source, PDF and worker suites alongside the affected existing
specification and review-persistence suites. All external services are stubbed in
these tests; deployment and actual Monday publication require the later rollout
checks in the implementation plan.

```powershell
venv/Scripts/python.exe -m pytest backend/tests/test_specification_review_sources.py backend/tests/test_specification_review_pdf.py backend/tests/test_specification_phase4_worker.py -q
```
