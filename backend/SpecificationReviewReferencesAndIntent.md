# Specification Review: references and intent (Phase 3)

Implemented on 9 November 2026. The attached-email update on 16 November 2026
advances the runtime pipeline to `output-v22`; evidence remains version `3`,
request-intent schema `1`, parser `specification-reference-v1`.
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

## Project-title matching across history

The title matcher now searches active project history without the custom Date
Created cutoff. An exact Project Name lookup runs before word searches. If it
finds candidates, they are returned without a word-frequency query or a broad
generic-word search. Several projects with the same title remain separate
reviewer choices.

When the exact lookup is empty, one selective token query retrieves possible
normalized or relaxed matches. Local whole-title normalization handles case,
whitespace and punctuation while preserving meaningful words and numbers.
Generic address words such as Street and Road support the comparison but cannot
independently qualify a relaxed match when identifying words are present.
For address-like titles, identifying words before the first street/road-style
suffix anchor the lookup. Thus Cardiff in `Pomeroy Street Cardiff` supplies
context and cannot independently admit an unrelated Cardiff project.

For identifiers of seven or more characters, retrieval uses a prefix with the
last two characters removed in that same selective query. This can retrieve
small spelling variants without adding another search stage. Admission still
requires a whole identifying word or a conservative near spelling: both words
must have at least five characters, differ in length by no more than one, share
their first three characters, and reach a character similarity of at least
0.88. Prefix agreement alone does not qualify a candidate. These are suggestions
for review, not reference confirmations or a general spelling-recovery
guarantee. Matching does not download the entire historical board for a fuzzy
comparison.

The two title-search stages share a four-page, 30-second elapsed budget, with
500 rows per page. The elapsed budget is checked between pages and limits the
next request's timeout. These bounded project queries make one transport attempt
per page; normal worker source validations and other Monday operations retain
their existing retry behavior. This is a title-query budget, not a deadline for
email extraction or the complete worker job.

Budget exhaustion preserves readable partial candidates with
`titleLookup.status = incomplete`. It cannot classify an empty partial result
as a new project. Project context stays pending, and the match PDF explains that
the lookup must be retried or narrowed before deciding whether the enquiry is
new. Provider failures remain unavailable rather than a valid empty result.

Candidates are qualified and ranked before the display limit. The matching
contract retains `candidateCount` as the number actually returned and adds
`searchDiagnostics` with `totalQualifiedCandidateCount`,
`qualifiedCandidateCountObserved`, `returnedCandidateCount`, and `truncated`.
The total is unknown (`null`) when remote retrieval is incomplete; the observed
count describes only the candidates read so far. A truncated report explicitly asks
for a narrower search; display truncation and incomplete remote retrieval are
separate states. Exact printed-reference resolution remains independent of the
title search and retains its existing prefix and drawing-confirmation checks.

## Attached client requests

Named EML/MSG files, unnamed RFC822 MIME attachments and embedded Outlook
messages are extracted as separate email bodies. Each retains literal headers,
plain/HTML alternatives, its own sent date and container hash, plus a
`parentSourceId` linking it to the carrying email. Its documents and inline
images retain that message's ownership rather than being flattened into the
outer message. Email files inside ZIPs use the same reader and shared limits.

Extraction allows three nested levels, 20 attached messages, 200 attachments,
40 MiB of aggregate parsed email bytes and 500,000 context characters. Broken
MIME structures and exhausted limits produce explicit permanent source
failures. They are never silently treated as an empty or successfully read
attachment inventory.

The outer selected external client remains the source of Company, Contact and
Date Received. An attached correspondent cannot replace that identity. A literal
pointer such as "amend as per the attached" adopts the attached message's own
current technical request. Each step of a deeper attachment chain needs its own
pointer. For-information-only, ignored, unowned and later-dated attached messages
cannot silently become the current request. Quoted history within an adopted
email remains historical; current plain and HTML alternatives may both support
literal citations when their message headers agree.

Applicable current technical evidence and baseline identities take precedence
over older quoted values within the same roof scope. Multiple current targets
remain a conflict, including competing adopted messages with different dates.
No drawing revision or Account decision is invented. The matching pipeline
continues to supply suggestions independently of the reviewer's confirmation.

The local re-extraction of item `3361067266` now retains the attached request's
plywood deck, cassette insulation constraint and baseline `TP10880_22.02-D`.
It preserves Luke as the requester and resolves the old baseline conflict.
Read-only source verification retains drawing selection and the CRM Account as
reviewer work: a printed `TP` reference is not automatically equated to a bare
CRM drawing/project number, and a requester signature does not fill a missing
project Account. No saved drawing choice or Account correction was made.
This local verification does not replace the deployed item's saved extraction.
Deploy the same `output-v22` code to the API, design/extraction, reconciliation
and specification workers before running a targeted refresh, then review its
current drawing and Account recommendations:

```bash
python -m backend.app.services.design_processing_reconciliation --item-id 3361067266 --limit 1
```

The changed pipeline identity requests fresh analysis through the normal fenced
queue. Do not relabel the old extraction or write the local replay JSON into
production. No database migration is required for these changes. Persistent
review blockers use the [needs-review wait state](SpecificationActivePublication.md).
