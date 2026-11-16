# Specification publication in active groups

Implemented: 13 November 2026. This document describes repository behaviour;
it does not record a production deployment. It supersedes the Landing Zone-only
scope and post-move read-only statements in the earlier review phase documents.

| Location | Processing and review | Monday PDF publication |
| --- | --- | --- |
| Landing Zone | Automatic analysis; editable review | Automatic under existing rollout controls |
| Configured active groups | Continuous preparation; editable review | Explicit reviewer request for each revision |
| Completed Folder or other excluded lifecycle/scope | Retained authorized document access | No new publication or editing |

The eligible set uses `DESIGN_PROCESSING_LANDING_GROUP_ID` and
`AUTO_SYNC_ACTIVE_GROUP_IDS`, with `AUTO_SYNC_COMPLETED_GROUP_ID` always excluded.
This reuses the group inventory, not the auto-sync enablement switches. Existing
specification mode, item allowlist, master flag and review flag still apply.
No extraction pipeline, source policy, PDF format or model version bump is needed.

## Reviewer workflow

In active groups, opening Specification Review and **Refresh recommendations**
may prepare a draft, but cannot publish it. **Save corrections** persists decisions
and prepares their matching draft. **Publish Specification PDF**, or **Update
Published PDF** when a publication already exists, explicitly requests that draft.
No artificial correction is required to publish satisfactory recommendations.

Publication is disabled while there are unsaved corrections, incomplete analysis,
a stale source identity, pending preparation, or a draft that has not loaded. A
reviewable PDF may still contain issues: final approval retains its stricter checks.
The view distinguishes draft and published PDFs, allows either to be inspected,
and shows changes in displayed field values/source labels against the publication.
Evidence-only changes can also require a new revision even when those values match.

Later source changes prepare recommendations/drafts and reopen review; they never
reuse an earlier click to publish changed content. Saved overrides remain retained
and can require reconfirmation. **Mark specification complete** approves the exact
published PDF separately. Historical PDFs and approvals remain retained.

Each **Source needs review** warning has an **I confirm this source issue has been
addressed** checkbox. Review the flagged file or missing information, check the
box, and use **Save corrections** to record the confirmation. No written
explanation is required. Saved confirmations retain the reviewer, time and source
version, and existing explanation-based reviews remain valid for their reviewed
sources. Uncheck and save to withdraw a confirmation. Source changes require
renewed confirmation, and every current warning must be confirmed before marking
the specification complete. The warnings remain visible; checkboxes do not read
unsupported files or change specification fields.

Deploy backend support before the updated frontend: checkbox commands include
`addressed: true` or `addressed: false`, which older backends do not accept.
These values use the existing JSON storage and need no database migration.
Older backends also cannot read saved boolean acknowledgements, so retain this
contract support when rolling back other changes.

## API and persistence

`POST /api/specification-review/items/{board_id}/{item_id}/publish` accepts
`artifact_revision`, `expected_review_version`, `expected_source_fingerprint`,
`expected_decision_digest`, `expected_publication_identity`, and `idempotency_key`.
It requires authenticated current item access and the publication-capable rollout
grant (the existing approval capability), verifies private artifact integrity,
and checks current sources before recording the request in a short transaction.
An immutable `publish` review event records the actor and exact revision. Identical
retries return the original receipt, without reauthorizing an already used request.

Migration `0022_specification_publication` adds two nullable columns to
`performance_specifications`: `prepared_revision` selects the latest prepared PDF
and `requested_publication_revision` authorizes one exact revision. Existing rows
start with no request. Source/decision/model changes revoke incompatible requests;
success and scope cancellation clear them. Transient failures retry the same
authorized draft. Saves and publication requests use the same specification-then-
review lock order, so a concurrent save cannot leave permission for its old PDF.

The worker rechecks live scope and exact permission before upload and promotion.
An automatic Landing Zone job moved to an active group becomes draft preparation.
Monday and the database are separate systems: an upload already in progress when
a move/source change occurs may leave a tracked attachment, but it cannot be
promoted as current. The previous PDF is retained. Cleanup removes only owned
prior specification attachments after a verified replacement, and stops in Completed.

## Deployment and recovery

1. Drain/stop old specification publishers. Their old policy cannot implement
   revision-specific authorization. Deploy matching API, design/reconciliation,
   specification worker and frontend revisions.
2. Apply the additive migration before starting code that reads the new columns:
   `python -m alembic -c backend/alembic.ini upgrade head`. Expected head is
   `0022_specification_publication`. Retain existing rollout settings consistently
   across services; begin with the specification item allowlist.
3. Scope policy is now `specification-active-review-v2`. Discovery includes known
   active groups and recovers missing/cancelled registered records. Those active
   items prepare drafts without publishing; existing attachments remain current.
   Older unregistered items or missing/current extraction still require explicit
   item-scoped design reconciliation; this release does not bulk-register history.
   Until current extraction exists, the worker records a `waiting` outcome rather
   than a failure: `review_json.status` is `source_waiting` with the reason, the
   item gets no `last_error` or retry escalation, and it is rechecked after
   `PERFORMANCE_SPECIFICATION_RECHECK_SECONDS`. Completed analysis wakes it at once.
4. Validate a Landing Zone automatic publication, active initial publication with
   no corrections, save/refresh without publication, source-change recommendations,
   explicit replacement, and a move into Completed Folder during work.
5. To pause, use the existing specification mode/master/review controls consistently.
   Keep the additive schema and retained artifacts. Do not run older publishers
   against this workflow or downgrade while updated services are running. A disabled
   worker exits immediately, logging `Performance specification processing is
   disabled`; Render then restarts it repeatedly, so suspend the service instead.
   The worker also logs INFO outcomes and waiting items; third-party libraries stay
   at WARNING.

Tests use fake Monday/model/storage gateways and isolated PostgreSQL schemas.
The browser harness uses the actual frontend with a local mocked API. Live Monday
acceptance and deployment remain separate operations.

## Persistent source-review waits

An unresolved `SpecificationReviewRequired` is parked with outcome and
`review_json.status` equal to `needs_review`. Its source snapshot, field issues,
and dependency links remain available to the review workspace. It has no scheduled
retry, no failure counter escalation, and no `last_error`; the worker logs an INFO
review-required outcome rather than a refresh-failed warning. A prior published
PDF remains retained, while incompatible pending publication permission and
completion approval are invalidated by the existing source-identity checks.

The parked record retains the desired/analyzed input and pipeline identities and
the source, parser, scope, and publication policy versions. Discovery excludes
an unchanged parked record before making Monday reads. A changed extraction or
policy identity, an explicit relevant dependency notification, a reviewer save,
or refreshed recommendations with changed sources can wake the record. Unchanged pending generations do not
become completed merely because the worker is waiting for review. Existing
`conflict` retry records move into this state on their next eligible refresh;
there is no database migration or bulk status rewrite.

Transient provider/source failures keep their bounded backoff and retry behavior.
Current-input extraction waiting remains the separate `source_waiting` state.
