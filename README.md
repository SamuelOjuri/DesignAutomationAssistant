# Design Automation Assistant

## Incremental task sync

Snapshot hashes still use Monday's `updated_at` and asset IDs. A different hash
creates a new snapshot, but unchanged files can reuse results from the latest
completed snapshot of the same task. Sync downloads files to verify SHA-256;
matching content, filename, file role, extension, and processing version reuse
stored objects, derived email attachments, CSV parameters, and existing vectors
without another extraction, embedding, or upload. Removed assets are omitted.
Current item metadata is saved, and column text is re-embedded only if it changes.

Reuse provenance is stored privately in `TaskSnapshot.task_context_json` under
`_asset_results`; it is omitted from public summaries and chat context. Asset reuse
itself needs no schema migration; durable refreshes below require migration 0013.
Legacy snapshots without this manifest need one normal
processing pass on their next changed revision (or an explicit forced sync).
Unchanged completed snapshots retain their existing short-circuit behavior.
`force=true` bypasses reuse. Failed, missing, purged, memory-skipped, or
incompletely extracted results are not reused.

New object paths include content hashes so forced refreshes cannot overwrite
different bytes still referenced by other snapshots. Reuse stays within one task;
task-level purge deduplicates object deletion and preserves outside references.
Deploy the pipeline, storage, and purge changes together. Reconciliation coverage
is described below; rollout allowlisting remains separate follow-up work.

When parser, prompt, chunking, limits, or extraction semantics change, bump
`PROCESSING_VERSION` in `backend/app/services/sync_asset_reuse.py`. The configured
Gemini extraction model and embedding model/dimension are also part of reuse
compatibility. Use a forced sync to upgrade a snapshot whose source hash has
not changed. File verification still costs download time; this does not promise
a zero-network handoff.

## Durable task refreshes

Handoff and manual refreshes for `AUTO_SYNC_BOARD_ID` now enqueue or coalesce
durable jobs instead of using API-process background tasks. Authorization and
CSRF checks still use the user's session and Monday access. Jobs contain no
access tokens; execution uses `MONDAY_INGESTION_ACCESS_TOKEN`. Other boards retain
the existing user-token background path.

A handoff with a matching completed snapshot opens without ingestion, including
while another job is pending. Stale, missing, expired, or unknown-freshness data
queues an ordinary incremental refresh; an existing completed snapshot remains
available to chat. Explicit `force=true` is persisted and bypasses extraction
reuse. A freshness lookup failure does not imply a forced rebuild.

Each job tracks desired and execution generations, source revisions, trigger,
and force intent. Claim captures the execution state. Completion locks the job
then the task, records the revision actually processed, and atomically schedules
one successor when requests arrived during execution. Failed or expired attempts
preserve force intent, and a healthy heartbeat keeps a long-running lease valid.
Retries remain `queued` until claimed or exhausted; the task page polls both
queued and syncing states. This is durable, at-least-once processing, not an
exactly-once guarantee for pipeline side effects.

Managed requests that need ingestion require `AUTO_SYNC_WORKER_ENABLED=true`
and a configured service token; otherwise they return HTTP 503 without consuming
the handoff code. A fresh handoff still works. Explicit authorized refreshes do
not require `AUTO_SYNC_ENABLED` or active-group eligibility. These configuration
checks do not detect an offline worker process; operate and monitor the worker
as a separate service.

Deployment order:

1. Drain and stop old workers and API background ingestion before switching versions.
2. Apply `0013_sync_generations` before starting the updated API or worker:
	`python -m alembic -c backend/alembic.ini upgrade head`
3. Deploy the matching API and worker code, configure the worker flag and service
	token in both services, then run `python -m backend.app.services.auto_sync_worker`.
4. Deploy the task-page queued polling change. Do not mix old and new workers.

The migration preserves queued jobs and defaults existing requests to generation
1. An interrupted legacy running job can be conservatively requeued on lease
recovery. Migration SQL can be inspected without a database connection using
`python -m alembic -c backend/alembic.ini upgrade 0012_ai_data_pdf_preview:head --sql`.

## Fair reconciliation and refresh reasons

The current CRM fields and their additional worker/deployment requirements are documented
in [MondayMetadata.md](backend/MondayMetadata.md). Apply migration `0015_monday_metadata`
before deploying this version of the API or workers.

Apply migration `0014_reconciliation_checks` before deploying the updated
reconciliation command: `python -m alembic -c backend/alembic.ini upgrade head`.
It adds `auto_sync_reconciliation_checks`; no existing progress is inferred from
task update or sync timestamps. The first sweep therefore treats every candidate
as unchecked. No production migration or scheduler is installed by these changes.

Active reconciliation enumerates all pages in the configured active groups, then
inspects only `--limit` items, choosing never-attempted and then least-recently
attempted items. `--page-size` controls only Monday's API page size (1-500, default
500), not the inspection limit. Listing IDs still covers the entire candidate set
on every invocation; API cursors are not persisted. Duplicate IDs across groups
are inspected once per batch.

Progress is persisted per board/item and scope. Successful checks and their queue
decisions commit together. Failed checks advance rotation but retain any previous
successful-check time, so one failing or inaccessible item cannot monopolize the
batch. Completed-transition checks have separate progress and rotate past tasks
that remain active. They also inspect tracked tasks whose ingestion is incomplete and
revisit confirmed archived/deleted tasks to detect restoration. Monday item `state`
takes precedence over its retained group: archival disables automatic work, cancels
jobs, and preserves stored data without starting a purge deadline. Restoration to an
eligible active group reenables refreshes. See the archival rollout commands in
[MondayMetadata.md](backend/MondayMetadata.md); this correction needs no new migration.
`--dry-run` writes neither jobs nor check progress. Overlapping
invocations can duplicate checks; use one scheduler and disable overlapping runs.

Run from the repository root every five minutes (scheduler expression
`*/5 * * * *`), initially adding `--dry-run` to inspect the selected candidates:

```text
python -m backend.app.services.auto_sync_reconciliation --limit 100 --page-size 500 --completed-transitions --completed-limit 100
```

Configure `DATABASE_URL`, `MONDAY_INGESTION_ACCESS_TOKEN`, `AUTO_SYNC_ENABLED`,
`AUTO_SYNC_BOARD_ID`, and `AUTO_SYNC_ACTIVE_GROUP_IDS` for the scheduler. Keep the
separate ingestion worker running with `AUTO_SYNC_WORKER_ENABLED=true`. The
explicit reconciliation CLI does not consult `AUTO_SYNC_RECONCILIATION_ENABLED`;
that flag alone neither schedules nor stops this command. Per-item failures return
a nonzero exit status while keeping successfully committed progress.

Completed-transition metadata lookups that return `404: monday item not found`
are reported as `source_unavailable` warnings, not errors. The summary includes a
`source_unavailable` count (also included in `skipped`); these warnings alone do
not fail the CLI. They do not prove deletion: snapshots, stored files, task state,
and retention dates remain unchanged. Non-dry runs save the attempted check but
preserve the previous successful-check time, then retry through normal fair
rotation. A visible item resumes normal group reconciliation automatically.
Authentication, rate-limit, other API errors, and progress-write failures remain
errors. This handling does not change active-group reconciliation or ingestion.

Tune cadence and batch size for the required freshness target: a stable population
of 1,000 candidates at 100 inspections every five minutes needs roughly 50 minutes
for a full sweep, plus execution time. An invocation succeeding is not proof of
coverage. Monitor the `auto_sync.reconciliation_coverage` log fields separately
for `active` and `completed_transition`: `candidate_count`, `never_checked`,
`oldest_checked_at`, and `max_check_age_seconds`. Alert when unchecked candidates
persist or check age exceeds the target. A null maximum age means no successful
checks exist for that candidate set, not zero lag. Coverage measures source checks,
not completed ingestion; also monitor the durable job backlog and failures.

`auto_sync.refresh_decision` logs carry trigger, action, reason, task key, desired
and indexed revisions, job ID, and desired/execution generations. Reasons include
`fresh`, `stale`, `missing`, `missing_snapshot`, `restore`, `failed`, `stuck`,
`already_queued`, `source_revision_unknown`, `freshness_unavailable`,
`source_unavailable`, `force`, and `newer_request_during_execution`, plus policy reasons for excluded items. These
decision events may precede transaction commit or be repeated on retry; use job
state and committed check rows for durable outcomes. They contain no tokens or
source contents. The CLI enables INFO logging; API/worker logging must retain
INFO for `backend.app.services.auto_sync` to capture their decisions.

Inspect attempted-item history with:

```sql
SELECT board_id, item_id, scope, last_outcome, last_reason,
	   last_attempted_at, last_checked_at,
	   EXTRACT(EPOCH FROM (NOW() - last_checked_at)) AS seconds_since_successful_check
FROM auto_sync_reconciliation_checks
WHERE board_id = '1882196103'
ORDER BY last_checked_at ASC NULLS FIRST;
```

Rows exist only after an attempted non-dry-run check; the coverage log combines
them with the current candidate inventory to count never-checked items. Archived
history is not enumerated by active-group reconciliation.

Separately audit the live Monday webhook subscriptions for the configured board:
verify item creation, group moves, relevant column edits, file/attachment changes,
and update attachments using the event types supported by the installed Monday
app/API version. Exercise each change on a test item and verify receipt, dispatch,
and the resulting durable job. Reconciliation remains the fallback for uncovered
events. This implementation does not provision subscriptions or verify their live
coverage; that requires deployment access.

## Excel attachments in task chat

Task sync reads `.xls` and `.xlsx` files attached to emails or directly to Monday
items/updates. Sources labels these files `attachment_spreadsheet`. Searchable
text includes workbook names, worksheet names, cell coordinates and row ranges;
chat citations link to the original Excel file. This enriches task chat evidence,
not the separately generated AI Data CSV or its Summary table.

Deploy the backend with the updated `requirements.txt`. Existing completed
snapshots need a **forced sync** to extract previously stored Excel attachments:
send an authenticated `POST /api/tasks/{externalTaskKey}/sync` with JSON
`{"force": true}` and the normal CSRF header. The current **Sync task** button
does not request a forced sync, so unchanged completed snapshots can be skipped.
New or changed snapshots are processed normally. No database migration is needed.

The readers use saved cell values and do not execute macros, follow external
workbook links, or calculate formulas. For `.xlsx`, a formula without a cached
result is included with an explicit “cached result unavailable” label. `.xls`
uses the results saved by Excel. Save/recalculate the original in Excel before
syncing if calculated values are missing or stale. Numeric values are extracted
as underlying values, rather than reproducing all Excel display formats.

Extraction is bounded per workbook: 20 MiB input, 100 MiB expanded XLSX content,
20 worksheets, 10,000 rows, 256 columns, 200,000 scanned cells, 400 characters per
cell, and at most 400 searchable chunks (including any extraction notice).
Limits and unreadable/encrypted workbooks produce a searchable notice; the
original attachment remains downloadable when storage succeeds. The existing
sync memory guard still applies. Formats such as `.xlsm` and `.xlsb` are outside
this implementation.

Run the Excel parser and ingestion regression tests after installing backend
dependencies and pytest:

```powershell
python -m pytest backend/tests/test_spreadsheet_extraction.py backend/tests/test_sync_pipeline.py -q
```

## Design Processing Phase 1

The design-processing worker is disabled by default. Phase 1 pins the legacy
enquiry source snapshot, records reproducible extraction and matching fixtures,
validates worker configuration, and verifies private artifact storage without
calling Monday mutations.

The offline legacy source must be available at
`producer/TechnicalDesignAssistant`. The approved fixture email must be at
`data/FW_ Drawings Titley close_Walton House.msg`. Both inputs are verified by
SHA-256 before legacy code is executed.

Run the Phase 1 trust gates from the workspace root:

```powershell
& ".\venv\Scripts\python.exe" ".\backend\scripts\verify_legacy_enquiry_manifest.py"
& ".\venv\Scripts\python.exe" -m backend.scripts.generate_legacy_enquiry_fixtures
& ".\venv\Scripts\python.exe" -m backend.scripts.verify_design_processing_storage
& ".\venv\Scripts\python.exe" -m pytest ".\backend\tests\test_design_processing_phase1.py" -q
```

Fixture verification compares regenerated legacy output with the committed
versioned JSON and CSV byte-for-byte. Use `--write` only when intentionally
regenerating fixtures after an approved manifest and pipeline-version change.

Artifact storage uses `DESIGN_PROCESSING_ARTIFACT_BUCKET`, defaulting to
`design-processing-artifacts`. The storage verifier requires a private bucket,
uploads under the full design-processing identity namespace, verifies the
downloaded content hash, and deletes the probe object. On initial environment
setup, add `--create-bucket` to provision the configured bucket as private.

Configuration supports `off`, `shadow`, `allowlist`, and `enabled` modes.
`DESIGN_PROCESSING_ALLOWLIST_ITEM_IDS` accepts comma-separated decimal item IDs
or a JSON array and is required when mode is `allowlist`. Activation timestamps
must include a timezone. The pipeline version combines the full legacy manifest
digest, the separately pinned `gemini-2.5-flash` extraction model, and a
code-owned output revision that changes whenever rendered or extracted output
semantics change.

## Design Processing Operations

Run auditable operator commands from the workspace root. `--operator-id`
defaults to the current OS user and may be set explicitly before the command:

```powershell
& ".\venv\Scripts\python.exe" -m backend.app.services.design_processing_operations --operator-id "operator@example.com" enqueue-item --item-id 2657106977
& ".\venv\Scripts\python.exe" -m backend.app.services.design_processing_operations reconcile-item --item-id 2657106977 --dry-run
& ".\venv\Scripts\python.exe" -m backend.app.services.design_processing_operations retry-failed-job --job-id 00000000-0000-0000-0000-000000000000
& ".\venv\Scripts\python.exe" -m backend.app.services.design_processing_operations retry-artifact-cleanup --item-id 2657106977 --limit 20
& ".\venv\Scripts\python.exe" -m backend.app.services.design_processing_operations reconcile-mode-transition --dry-run --limit 100
& ".\venv\Scripts\python.exe" -m backend.app.services.design_processing_operations metrics --lease-timeout-seconds 3600
```

Remove `--dry-run` only after reviewing reconciliation output. Broad
mode-transition reconciliation requires
`DESIGN_PROCESSING_ACTIVATION_TIMESTAMP`; item-scoped reconciliation does not.
Commands are idempotent: enqueue and reconciliation coalesce into the existing
active job, failed-job retry reactivates the same immutable execution, and
cleanup only targets recorded `delete_pending` artifacts.

Operational mode and allowlist policy are re-evaluated at worker checkpoints
and before every Monday side effect. `off` prevents new claims, `shadow`
permits analysis only, `allowlist` permits publication only for configured item
IDs, and `enabled` permits all in-scope publication. Run mode-transition
reconciliation after changing mode or allowlist configuration so already
analyzed identities can receive publication-only work.

Worker, webhook, reconciliation, cleanup, and operator events are logged as
canonical JSON after the `design_processing_event=` prefix. The `metrics`
command returns queue and item-state counts, readiness age/checks, attempt
percentiles, lease health, supersessions, analyzed-not-published count,
publication latency, artifact cleanup state, and webhook child outcomes.
