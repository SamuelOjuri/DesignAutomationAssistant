# Specification Review: references and intent (Phase 3)

Implemented on 9 November 2026. Runtime extraction is `output-v20`, evidence
version `3`, request-intent schema `1`, parser `specification-reference-v1`.
Existing extraction is not relabelled: the changed pipeline identity requires a
fresh extraction. The pinned producer manifest and historical fixtures are retained.

`specification_intent.py` validates the model's typed sidecar against readable
sources and the selected external client request. It also provides a conservative
fallback for explicit new-work/change wording. An unrelated quoted historical
message cannot classify the current request. Missing or conflicting context stays
`uncertain`; project-name similarity and an exact reference remain supporting
evidence rather than a human decision or an amendment probability.

The canonical 21 CSV rows and 18 PDF field labels are unchanged. Reason for Change
is `New Enquiry (AI recommendation)`, `Amendment (AI recommendation)`, or
`Reviewer decision required`, with source `AI recommendation from Email Context`.
The matched-project PDF explains this distinction and labels its score as title
similarity. The human-owned Monday classification column is never written.

The extraction JSON now contains `specificationRequestIntent`,
`requestIntentSchemaVersion` and `referenceParserVersion`. Matching enriches the
sidecar with typed project matches and persists `specificationCandidates` in the
matching result. This includes drawing inventories/recommendations, account
context, source availability and dependency identities. Preparation needs no
holding Accounts, TP Ref or Date Received. It reads at most 12 candidate projects
per pass; a wider result remains visible with a pending/narrowing issue.

Direct project lookup uses parameterized read-only queries on the configured
project board and fully paginates results. It has no creation-date restriction.
Printed references and Monday item IDs stay separate. Bare CRM project names are
confirmed only through child drawings with the exact prefix and project digits;
AB00123 and CD00123 are never aliased by stripping their prefixes. Exact and title
matches retain separate rationales. Read caches are credential/board-bound and
expire after 30 seconds. Unavailable reads are distinct from valid empty results;
optional CRM failures retain the email extraction for review.

`specification_references.py` is the shared parser used by extraction/evidence,
recovery, source selection and candidate ranking. It preserves literal references,
leading zeros and full revision suffixes; it normalizes case, spacing and dashes
for comparison. All two-letter alphabetic prefixes are structurally valid without
a prefix-only warning. Source evidence still establishes applicability and issuer;
bare and D.S references retain explicit legacy confirmation gates.

Drawing selection prefers a unique relevant existing target. Current-request
references take priority over attachment and forwarded context. Other candidates
remain visible, including historical/inactive targets and current alternatives.
Within each roof/drawing series, integer numeric components and alphabetic
revision ordering (`Y`, `Z`, `AA`, `AB`) determine the latest active candidate.
Several roofs or duplicate latest identities require selection. Archived/deleted
drawings and Monday item IDs never supply a latest-drawing fallback. No future
revision or prefix is generated.

This phase prepares recommendations and updates reference handling. The
email-first/reviewer PDF resolver, Landing Zone lifecycle, review API and embedded
view remain subsequent phases. It does not enable publishing or review mutations.

Focused verification includes `test_specification_intent.py`,
`test_specification_references.py`, `test_specification_project_lookup.py`,
`test_specification_phase3_integration.py`, and the affected extraction, evidence,
drawing, publication/recovery and Phase 2 contract/persistence suites.
