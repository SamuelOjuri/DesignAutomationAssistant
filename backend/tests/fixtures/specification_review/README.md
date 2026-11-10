# Specification Review contract fixtures

These synthetic scenarios declare the source-policy outputs agreed for Phase 1
of `backend/PerformanceSpecificationReviewPlan.md`. They are examples of the
future review contract, not captured client emails or a live source resolver.

`scenarios.json` is a list of scenarios. Input maps use only the exact 18 PDF
labels; an omitted input field means the source does not supply a value. Each
scenario's `expected` map contains all 18 labels in PDF order. A `null` display
value means unavailable or intentionally unassigned, while `""` is the automatic
Incoming Information blank. Reviewer-specified blanks belong to the separate
decision contract and must not be confused with either case.

The expected source codes distinguish email facts, CRM recommendations, derived
Revision, unassigned new-enquiry identity, default blanks and unavailable values.
Issue codes describe review conditions, not processing failures. Context retains
the selected client request, distinct printed references and Monday identities,
holding-item values that cannot override the policy, and roof/drawing scope.

The cases cover:

- `new_enquiry_historical_match`: new work for a familiar client/site, an empty
  holding TP Ref relation, email-based fields and unassigned drawing identity.
- `amendment_email_precedence`: a relevant full email drawing reference,
  conflicting older technical CRM values, linked-Account/email-company
  disagreement and a Revision discrepancy.
- `amendment_missing_details`: email omissions with scoped CRM technical,
  Contact and Surveyor fallback; unsupported CRM fields remain unused.
- `amendment_without_reference`: amendment intent and project matching without
  an explicit email reference or holding relation, followed by an existing
  drawing recommendation.
- `uncertain_multiple_roofs`: conflicting intent and plausible roof candidates;
  technical recommendations remain unresolved rather than merging roofs.

General reference parsing, intent inference, remote lookups, persistence, PDF
rendering and publication are implemented in later phases. These fixtures must
not be used as evidence that those runtime paths already implement this policy.

The implementation contract lives in
`backend/app/services/specification_review_contract.py`. `FIELD_POLICY` is the
ordered source-policy matrix; `ReviewSourceSnapshot` keeps email facts, scoped
CRM recommendations, applicability, proposal, candidate lookup status and
source-level issues separate. Its `request_type` is the effective recommendation
branch and can differ from the AI's `intent.recommended_type` after a reviewer
selection. The shared `PDF_FIELD_NAMES` constant preserves the existing label
order without changing automated CAD constraints or extraction descriptions.

`SaveReviewDecisions` describes untrusted commands: expected review/source
versions, idempotency key, internal selection changes, field operations and
explicit source-issue dispositions. Actor, timestamp, accepted snapshot and CAD
mapping are server-authored and belong to `ReviewDecisionState`. The schema does
not implement authentication, CAS, transactions or idempotency storage.

Decision examples covered across all 18 fields:

| Decision | Effective display | Effective source |
| --- | --- | --- |
| Omitted/restored | Current automatic proposal | Current proposal's source |
| Accept recommendation | Saved proposal snapshot | Saved proposal's source |
| Specify value | Exact reviewer text, including custom unmapped text | Reviewer Specified |
| Specify blank | Intentional empty string, suppressing fallback | Reviewer Specified |

`effective_field_value` demonstrates only these decision semantics using an
already supplied proposal; it does not choose email/CRM sources. It retains
saved decisions and marks them for reconfirmation when their reviewed source
identity changes. Field edits do not erase `source_issues`. The PDF renderer
will add `Source: ` before the source label in Phase 4.

Each model exports JSON Schema with `model_json_schema()` for future API and
mocked frontend work. Run the offline Phase 1 checks from the repository root:

    venv/Scripts/python.exe -m pytest backend/tests/test_specification_review_contract.py -q
