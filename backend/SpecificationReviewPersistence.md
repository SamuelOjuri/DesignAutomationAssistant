# Specification Review persistence (Phase 2)

Phase 2 adds internal review storage and transaction services. The existing
publisher is now connected in [Phase 4](SpecificationReviewSourcePolicy.md).
The [Phase 6 review API](SpecificationReviewAPI.md) now provides authenticated
access to these commands; the embedded UI remains Phase 7. Apply the additive
migration before deploying the review-aware publisher or API.

## Database

`0019_specification_review` follows `0018_specification_evidence` and adds:

- `performance_specification_reviews`: tenant-bound decisions, selections,
  monotonic review version, source identity, decision digest, desired publication
  identity and current approval pointer.
- `performance_specification_review_events`: immutable command receipts and audit,
  including original/replacement decisions and the reviewed source snapshot.
- `performance_specification_approvals`: immutable exact-artifact approvals.

Composite foreign keys prevent cross-item or cross-tenant approval pointers.
PostgreSQL triggers reject changes to audit and approval history. Historic
artifacts retain their existing identity and are never backfilled as reviewed.
The immutable artifact payload is protected from updates while publication
status, Monday asset ID and error/recovery bookkeeping remain mutable.
The current approval pointer is cleared on meaningful decision/source changes;
the historical approval remains available.

From the repository root, with the intended database configured:

```powershell
venv/Scripts/python.exe -m alembic -c backend/alembic.ini upgrade head
```

PostgreSQL tests exercise migration rollback in isolated schemas. Keep the additive schema
in place while any review-aware process is running.

## Transaction service

`app/services/specification_review.py` exposes:

- `initialize_review` / `synchronize_review_source`: register a trusted source
  snapshot; changed sources preserve decisions, invalidate completion and queue
  regeneration. Replacements require `expected_source_fingerprint` from before
  the remote refresh so a delayed result cannot replace a newer source identity.
  Changed snapshots require a distinct fingerprint; producers must include the
  actual recommendation context and pipeline identities in that fingerprint.
- `save_review_decisions`: apply the Phase 1 command contract with review/source
  compare-and-swap, server-authored audit, immutable recommendation snapshots and
  field-specific intentional blanks/restores.
- `approve_review`: persist approval only for an exact current published review
  artifact, with no newer generation or worker lease.
- `review_state`: reconstruct the typed decision state from the review record.

Each command requires a Session without an active transaction and commits once.
It locks the specification row before the review row, updates the durable queue
directly, and makes no Monday, storage or model call. A meaningful save increments
the review version and generation; a normalized no-op records its command receipt
without rewriting decision attribution or scheduling work. Repeated identical
idempotency keys return the original receipt even after later edits; a different
request using the same key conflicts. Monday account/user identify the actor for
the database uniqueness boundary; app identity is checked on replay too.

These are trusted internal primitives. Before calling them, the Phase 6 API must
validate the authenticated actor, configured account/board, live item access,
Landing Zone eligibility, specification-specific mode/allowlist and feature
enablement (`PERFORMANCE_SPECIFICATION_MODE` and
`PERFORMANCE_SPECIFICATION_ALLOWLIST_ITEM_IDS`, independently of design mode). Perform
those remote checks before opening the transaction. Approval also requires fresh
source and asset/integrity verification at that boundary. Database approval does
not make a private/shadow PDF eligible for completion. Version 1 grants reviewer
authorization to authenticated Monday CRM users with live item access; explicit
reviewer-role authorization is deferred to version 2. Phase 2 alone enables no
route; Phase 6 supplies this boundary.

Selections use patch semantics: omitted members remain unchanged and explicit
null clears a member. New project/drawing selections must be current candidates;
a drawing must belong to its selected project and have active lifecycle. Source
snapshots explicitly bind their `selected_project` and `selected_drawing` to the
roof supplying recommendations. After a context change, refresh recommendations
before accepting values or approving; old accepted values stay visible but need
reconfirmation. A selection and acceptance can share one command when the trusted
snapshot already describes that exact context. An unavailable proposal cannot be
accepted. Reviewer-entered text
is preserved literally; authoritative CAD mapping is a later resolver concern.

## Artifact integration contract

`app/services/specification_review_artifacts.py` builds deterministic decision
digests and `build_review_artifact_metadata(state, source)`. The review-aware
publisher puts that metadata at `artifact.source_json["review"]`, includes it
in its immutable artifact digest alongside the PDF/model/template versions, and
fences promotion against the current generation and desired publication identity.
It renders the effective values represented there. Merely adding metadata to
an older PDF does not establish that its content reflects reviewer decisions.

Metadata includes review version, decision digest, recommendation fingerprint,
extraction/source-policy/parser identities, selections and provenance/effective
values for every one of the 18 PDF labels. Actor/time/approval events do not
change publication identity. Accepted recommendations retain source evidence;
reviewer-specified values and blanks carry `Reviewer Specified` without fabricated
email/CRM citations. The original candidates and facts remain in event snapshots.

## Verification

```powershell
venv/Scripts/python.exe -m pytest backend/tests/test_specification_review_contract.py backend/tests/test_specification_review_persistence.py -q
```

PostgreSQL tests require `TEST_DATABASE_URL` pointing to a disposable test database
with pgvector available for the existing migration history.
They use unique isolated schemas, run the Alembic migration history and clean up
only their generated schema. SQLite checks do not establish concurrency safety.

```powershell
venv/Scripts/python.exe -m pytest backend/tests/test_specification_review_postgres.py backend/tests/test_specification_review_postgres_concurrency.py -q
```
