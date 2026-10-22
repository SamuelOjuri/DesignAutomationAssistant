# Design Automation Assistant

Task chat supports authenticated response streaming through a native Netlify Edge
relay. Deployment order, timeout settings, and the protected 120-second transport
probe are documented in [ChatStreaming.md](backend/ChatStreaming.md).

## Gemini 3.8 Flash configuration

Chat, retrieval planning, and general PDF/image extraction default to
`gemini-3.8-flash`. The design-processing worker separately defaults to the same
model with `medium` thinking. Configure both selections explicitly in the root
`.env` and in each deployed API, worker, and reconciliation service:

```dotenv
GEMINI_MODEL=gemini-3.8-flash
DESIGN_PROCESSING_EXTRACTION_MODEL=gemini-3.8-flash
DESIGN_PROCESSING_THINKING_LEVEL=medium
```

The two model settings remain independent. Gemini 3.8 Flash supports `low`,
`medium`, and `high` thinking; settings validation and direct worker-client
construction reject `minimal` for this model. Chat planning and synthesis omit
deprecated sampling parameters and retain their JSON output schemas. See
[Google's migration guide](https://ai.google.dev/gemini-api/docs/generate-content/whats-new-gemini-3.8).

Deploy matching code and environment settings across services and coordinate
their restarts. Changing the extraction model changes the design-processing
pipeline identity automatically; no separate output-revision bump is needed for
this model change. Enabled reconciliation can enqueue eligible tracked items
again, so validate representative outputs in `shadow` or `allowlist` mode before
broad publication. The legacy verification snapshot and golden fixtures remain
pinned to their original source bytes.

The general extraction model also participates in asset-reuse compatibility.
Future processing will regenerate results from an incompatible model, while
completed snapshots with unchanged source hashes still return early. Use an
explicit forced sync when historical documents need re-extraction. The embedding
model remains `gemini-embedding-001` with 1536 dimensions; this migration needs no
database schema change.

Before deployment, run the affected backend tests and live checks for model
access, structured extraction, citations, PDF/image quality, and latency. The
streaming tests exercise the pinned Google SDK through a mocked HTTP transport;
they do not verify live account access or quota.

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
inspects at most `--limit` due items, choosing never-attempted and then least-recently
attempted items. Settled, successfully checked tasks are deferred for one hour by
default. Local failures, missing snapshots, restoration, unfinished work, and group
moves visible in discovery bypass this active freshness interval. `--page-size`
controls only Monday's API page size (1-500, default
500), not the inspection limit. Listing IDs still covers the entire candidate set
on every invocation; API cursors are not persisted. Duplicate IDs across groups
are inspected once per batch.

When both auto-sync and design reconciliation are scheduled, replace the two
separate cron commands with this combined command (for example, every five
minutes). Preview with `--dry-run` first:

```text
python -m backend.app.services.monday_reconciliation --limit 100 --completed-limit 100 --design-limit 100 --page-size 500
```

It discovers the union of the two reconcilers' groups once per board and passes
the same live inventory to both sweeps. Shared groups, including their later
pages, are fetched once. Distinct boards get separate inventories. All pages are
still read to preserve fair selection and full admission coverage; inspection
limits do not cap pagination. Inventories are not cached across runs. This
command also runs lifecycle checks, reusing active-check freshness as above.
Its `--force` flag applies to auto-sync freshness; explicit design item commands
remain the way to bypass design delays. Disable the separate schedules when
switching to this command, and prevent overlapping invocations. Existing commands
remain available. No additional database migration is required for these changes.

Progress is persisted per board/item and scope. Successful active reads also record
the item's lifecycle observation, so a following lifecycle sweep can inspect other
tasks instead of fetching the same item again. Source responses already contain the
typed CRM values: complete responses update current metadata and linked dependencies
in the same transaction, without scheduling another read. Pending or leased metadata
refreshes and generations changed during the source read are preserved for the worker.
Incomplete CRM responses retain last good values and use the existing retry queue.
Successful checks and their queue decisions commit together. Failed checks advance
rotation but retain any previous
successful-check time, so one failing or inaccessible item cannot monopolize the
batch. Completed-transition checks have separate progress and rotate past tasks
that remain active, deferring successful lifecycle observations for one hour by default.
They also inspect tracked tasks whose ingestion is incomplete and
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

Both inspection sweeps have finite defaults even when flags are omitted: active
uses `AUTO_SYNC_BACKFILL_BATCH_SIZE` (10); lifecycle uses
`AUTO_SYNC_RECONCILIATION_COMPLETED_BATCH_SIZE` (100, positive). Configure
`AUTO_SYNC_RECONCILIATION_ACTIVE_INTERVAL_SECONDS` and
`AUTO_SYNC_RECONCILIATION_LIFECYCLE_INTERVAL_SECONDS` (both default 3600, minimum 0)
to tune freshness; 0 disables the corresponding deferral. `--force` bypasses
deferral while retaining the batch limits. Explicit `--completed-item-id` or
`--completed-external-task-key` checks always inspect the target immediately.
`--dry-run` respects stored freshness unless forced and saves no new observations;
its reads cannot suppress a later lifecycle read. No new migration is needed.

Webhooks and requested document/CRM refreshes retain their existing cadence. A
missed webhook on a settled item can now wait for the freshness interval plus fair
rotation and execution time. With 100 unchanged active items, complete CRM responses,
and no pending metadata work, the hourly source cadence represents 2,400 item reads
per day shared across source, CRM, and lifecycle checks, versus the analysis's
86,400-request five-minute scenario. Discovery pages, account lookups, other items,
workers, retries, and other integrations are additional; this is a workload estimate,
not a production measurement or account-wide quota guarantee.

Configure `DATABASE_URL`, `MONDAY_INGESTION_ACCESS_TOKEN`, `AUTO_SYNC_ENABLED`,
`AUTO_SYNC_BOARD_ID`, and `AUTO_SYNC_ACTIVE_GROUP_IDS` for the scheduler. Keep the
separate ingestion worker running with `AUTO_SYNC_WORKER_ENABLED=true`. The
explicit reconciliation CLI does not consult `AUTO_SYNC_RECONCILIATION_ENABLED`;
that flag alone neither schedules nor stops this command. Per-item failures return
a nonzero exit status while keeping successfully committed progress.

Monday requests use up to four attempts for transient HTTP failures. Read queries
also retry recognized transient GraphQL errors, including rate/complexity limits
and temporary API blocks. Retries wait at least the `Retry-After` or
`retry_in_seconds` delay supplied by Monday, falling back to exponential backoff.
A requested delay over 120 seconds is surfaced as a transient failure for a later
job/run rather than sleeping indefinitely or retrying before the limit resets.
Permanent, unknown, mixed permanent/transient, and mutation GraphQL errors are not
automatically replayed. Logs retain HTTP status, error codes, request IDs, retry
delays, and retry attempts; upstream messages, response bodies, and credentials
are omitted. Exhausted retries still produce a failed reconciliation run.

HTTP 429 and recognized GraphQL rate limits without a usable server delay now
use a 30-second cooldown plus 0-5 seconds of jitter. The longest server delay
still takes precedence, including reset times for exhausted quotas in Monday's
`RateLimit` header. Other transient failures retain exponential backoff. A
thread-safe cooldown is shared by GraphQL calls using the same credential in
one process, so the next item or reconciliation phase cannot immediately bypass
an exhausted request's cooldown. A remaining delay over 120 seconds fails
without sending another request; successful progress and the nonzero exit on
errors are preserved. Cooldowns are not shared across separate Render services
or process restarts, so continue using one scheduler and avoid overlapping runs.

Error logs include whether the delay came from Monday or the fallback, whether
the response was JSON, and known numeric quota/reset fields from `RateLimit`
and `RateLimit-Policy`. Raw headers and response bodies are not logged. See
[Monday's rate-limit headers](https://developer.monday.com/api-reference/docs/rate-limits#rate-limit-headers).
Completed-transition sweeps reuse each stored task's account ID, eliminating the
extra account lookup previously sent for every item. Standalone metadata callers
can still omit the account ID to resolve it through Monday.

Completed-transition metadata lookups that return `404: monday item not found`
are reported as `source_unavailable` warnings, not errors. The summary includes a
`source_unavailable` count (also included in `skipped`); these warnings alone do
not fail the CLI. They do not prove deletion: snapshots, stored files, task state,
and retention dates remain unchanged. Non-dry runs save the attempted check but
preserve the previous successful-check time, then retry through normal fair
rotation after `AUTO_SYNC_RECONCILIATION_UNAVAILABLE_INTERVAL_SECONDS` (default
3600, positive integer). The delay uses the last unavailable attempt, including
when the item has never had a successful check. Repeated unavailable results
restart that delay. Deferred items consume no inspection slots and are counted
as `source_unavailable_deferred`, separately from unavailable lookups actually
performed in that run. Coverage still reports items with no successful check as
`never_checked`; deferral does not manufacture a successful observation.

This backoff applies to routine lifecycle sweeps in both the standalone and
combined reconciliation commands. `--force` on either command, and the standalone
`--completed-item-id` or `--completed-external-task-key` options bypass it; dry runs
respect it unless explicitly bypassed and never advance the retry clock.
Webhooks and active-group discovery remain immediate recovery paths. A visible
item resumes normal group reconciliation automatically, and a successful
lifecycle lookup returns to the ordinary lifecycle interval. Changing only
`AUTO_SYNC_RECONCILIATION_LIFECYCLE_INTERVAL_SECONDS` does not change this
unavailable-item delay. Deploy the updated reconciliation code and optionally
set the new variable in its environment; the one-hour default needs no new
database migration or backfill. Previously recorded unavailable attempts are
honored immediately. With five persistently unavailable items, hourly retries
represent about 120 item reads/day instead of 1,440 at five-minute intervals.
Missed restoration can wait for the retry interval plus scheduler/fair rotation
time; stored data and retention policy are preserved throughout.

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

Treat the ignored `producer/TechnicalDesignAssistant` directory as an immutable
verification snapshot; keep active legacy development in a separate checkout.
The manifest pins exact bytes, including line endings and the final newline.
If source verification fails, restore the original files matching those hashes
rather than updating the manifest or regenerating expected outputs to accept
the changed checkout. The local snapshot drift found on 22 October 2026 was
repaired from hash-matching Git blobs, with the replaced files backed up under
`tmp/legacy_snapshot_backup_20261022T093508Z`. The manifest and golden fixtures
were retained unchanged.

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
digest, the separately pinned `gemini-3.8-flash` extraction model, and a
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

### Admitting and continuing design processing in active groups

New design-processing items can be admitted in Landing Zone or any group in
`AUTO_SYNC_ACTIVE_GROUP_IDS`, independently of the auto-sync enable switches.
Items created directly in an active group, or moved there before registration,
can start analysis and publication without a prior Landing Zone record. Creation,
move, name and source-email webhooks use the item's current group. A group move
alone does not change the email revision or repeat completed extraction;
duplicate events coalesce into the existing active job.

`AUTO_SYNC_COMPLETED_GROUP_ID` takes precedence over the eligible groups. Moving
there marks the item ineligible and cancels outstanding design work with reason
`completed_folder`. Archived/deleted items and other boards/groups are also
excluded. Retry, reconciliation, final publication and old-file cleanup all check
current eligibility. Existing files are retained on completion. These are the
design worker's rules; document ingestion and retention keep their separate policy.

In active groups, Date Received, Hour Received and Zip Code are filled only when
their current Monday value is demonstrably empty. Existing, missing-from-response
or malformed values are omitted from the mutation. Midnight is a populated time.
The values are read again immediately before a write, including retry attempts;
reads and writes are not an atomic conditional update. Landing Zone retains its
existing scalar-write behaviour. AI Data remains email-derived, with Monday
values taking precedence in the assistant.

Production extraction runs in a spawned child process. Every five seconds, the
parent reloads local job state to check lease ownership, cancellation, desired
input/pipeline version and execution policy, without reading Monday. While waiting,
it also validates the current Monday source every 60 seconds, configurable with
`DESIGN_PROCESSING_EXTRACTION_REMOTE_VALIDATION_SECONDS` (positive integer seconds).
Webhook-recorded changes take effect at the next local check; changes not yet
recorded locally are detected at the next remote validation. A ten-minute wait
therefore uses about 10 periodic Monday reads instead of 120. Fresh Monday reads
still guard extraction start/completion, stage checkpoints and publication writes.
The parent terminates the extraction process on cancellation. Allow for the extra
extraction-process memory in the worker service.
Cancellation takes effect when detected; an external request already accepted
cannot be recalled. Read failures stop further work rather than bypassing gates.

If extraction finishes during an eligibility check, the parent drains its pending
result before deciding whether the child exited without a result, then checks
eligibility again before accepting it. Child-process failure messages include the
PID, natural exit code, POSIX signal name where available, and elapsed seconds.
These details precede the error text so they survive the job's `last_error` length
limit. An exit code of zero with no result still means the result protocol failed;
a missing exit code means the child had not finished exiting within the bounded
diagnostic wait. A `SIGKILL` alone does not establish an out-of-memory kill; check
Render's resource metrics/events to distinguish causes.

The shared Supabase client normalizes the SDK-derived `/storage/v1/` endpoint
before storage initialization. This addresses the trailing-slash warning without
changing dependency versions, credentials, bucket names, or stored objects.

Broad design reconciliation enumerates all pages in Landing Zone and configured
active groups, deduplicates items that move during discovery, and also checks
previously registered unfinished items outside those groups, including old
cancelled items. `DESIGN_PROCESSING_ACTIVATION_TIMESTAMP` bounds new admission
by the item's Monday creation time (at or after the timestamp), not recovery of
existing records. Item-scoped operator commands and live webhooks retain their
existing behaviour without that broad-scan cutoff. Completed Folder is never
included in new-item discovery, even if also configured as an active group.
`--limit`
covers the combined due candidates, ordered by last reconciliation attempt (or
recorded item update/creation time before their first check); use a single
non-overlapping scheduler. Completed items can
be revisited for eligibility without resuming processing while still completed.
Moving them back to an eligible group permits normal reconciliation again.

Deploy matching API, design-worker and reconciliation code together. Apply
migration `0016_design_reconciliation` for the check records described below.
No output-version bump is required. Previously rendered artifacts can be reused
when both email revision and pipeline version match. For a known cancelled item,
preview recovery, then enqueue it using the production configuration:

```powershell
python -m backend.app.services.design_processing_operations reconcile-item --item-id 3244851930 --dry-run
python -m backend.app.services.design_processing_operations reconcile-item --item-id 3244851930
```

The active-group admission change needs no additional migration or output-version
bump. After deployment, the next broad reconciliation can register previously
unseen active items created at or after the activation timestamp, including items
like `3249971235` and `3249996676`. Use item-scoped reconciliation for an intentional
older-item backfill. Existing name/email readiness and publication-mode rules still
apply; discovery and queueing do not mean extraction or upload has finished.

Run the design-processing regression tests, including
`backend/tests/test_design_processing_active_groups.py`, before deployment.

### Unavailable items and delayed reconciliation

Apply the additive migration before running the updated reconciler:

```powershell
python -m alembic -c backend/alembic.ini upgrade head
```

`design_processing_reconciliation_checks` stores one observation per board/item:
the last attempt, last successful lookup, outcome, reason, last observed group,
and next check time. Existing rows need no backfill: the first scan records their
outcomes, and subsequent scans respect the delays.

A valid empty Monday item response records `unavailable` with reason
`item_unavailable_to_worker`. It schedules a recheck after
`DESIGN_PROCESSING_UNAVAILABLE_RECHECK_SECONDS` (default 3600, one hour).
The intake query explicitly includes inactive items. An empty response does not
establish deletion or completion; existing processing history, files, and
retention dates are preserved.

Confirmed exclusions, including Completed Folder, are rechecked after
`DESIGN_PROCESSING_EXCLUDED_RECHECK_SECONDS` (default 21600, six hours).
Their precise policy reason and observed group ID are recorded. Both settings
must be positive integers. Delays affect broad reconciliation only: an explicit
`reconcile-item --item-id ...` or reconciliation `--item-id ...` reads immediately,
including with `--dry-run`. Webhooks can still queue newly eligible work without
waiting for the broad scan. Successful eligible reads clear an unavailable or
exclusion delay.

Settled eligible results (`ignored:ready`, including analysis already completed
in shadow mode) instead schedule their next check after
`DESIGN_PROCESSING_SETTLED_RECHECK_SECONDS` (default 3600, positive). Queued,
coalesced and failed work gets no settled delay. Deferred items do not consume
inspection slots. A pipeline change, newly permitted publication, newer local
item state, or an eligible group move seen in discovery bypasses the settled
delay. Explicit item commands and webhooks also remain immediate. Changes missed
by webhooks can wait up to this interval plus scheduler/batch rotation time.
For 36 unchanged settled items, hourly reads represent about 864 item reads per
day, versus 3,456 with a 15-minute sweep; discovery and other reads are additional.

The obsolete `not_registered_in_landing_zone` exclusion no longer delays a
candidate rediscovered within the broad admission boundary. Reconciliation checks
its live eligibility immediately; other exclusion and unavailable delays remain
in effect. This does not admit old or undiscovered items merely because they have
a check record.

Unavailable items produce a warning and a separate `unavailable` count; items
whose deadline has not arrived produce a `deferred` count without an item lookup
or using a `--limit` slot. Authentication, throttling, network, malformed-response,
and database failures remain errors. Failed attempts also advance reconciliation
ordering so a small batch cannot repeatedly select the same failing records.
Dry runs persist neither observations nor processing changes.

The reconciliation cron prints a compact JSON summary with outcome/reason counts
and exits nonzero for genuine errors. Item-specific runs include item details;
the operations CLI retains its full structured result. Inspect stored observations
in Supabase with:

```sql
SELECT item_id, last_outcome, last_reason, last_group_id,
       last_attempted_at, last_checked_at, next_check_at
FROM design_processing_reconciliation_checks
WHERE board_id = '1882196103'
ORDER BY last_attempted_at DESC;
```

These check records do not create a purge deadline. Extending retention cleanup
to design artifacts and defining retention for unavailable items are separate
changes; the existing ingestion purge still covers only its current data scope.

### Task polling and webhook request usage

Visible task pages poll summaries every 12 seconds while ingestion is pending or
running, and every 60 seconds after ingestion completes or fails. They still
discover metadata-only changes and newly queued work; focus/visibility return
and manual refresh trigger an immediate read. Hidden tabs pause reads, requests
never overlap, and failed summary requests retry on the fast cadence. A settled
visible tab uses about 60 permission lookups/hour instead of 300, excluding focus
events and other user actions. Background changes can take up to 60 seconds to
appear on an already settled page.

Permission decisions are deliberately not cached: each backend summary/source,
chat, sync and download request retains its existing access check, with no new
permission-revocation grace period. The immutable token-to-account identity is
cached separately (up to 32 successful identities per API/worker process). Token
rotation uses a new entry; failed identity requests are never cached. This removes
repeated `me.account` lookups from webhook bursts without granting item access.
The cache is not shared between processes and a fresh process must read the
identity once.

Webhook dispatchers decide whether a claimed consumer needs a current snapshot
before any remote reads. Disabled/non-actionable design consumers and locally
handled linked-board metadata events can complete without a snapshot. Required
snapshots are shared by both consumers. Design output-file events still fetch a
snapshot when auto-sync needs to index the changed file. Retry claims continue
to skip successful consumers and snapshot failures affect only consumers that
need the read.

### Project matching API usage

Project matching uses one batched word-specificity lookup and a paginated
candidate query on the configured project board. The candidate filter is
`date9__1 >= 2021-01-01 AND (text3__1 contains word A OR word B OR ...)`.
The date condition stays outside the OR group. Search terms are passed as
GraphQL variables; no semantic-search endpoint or API-version change is needed.

The matcher selects the longest matching word signature locally, with ties
broken by the existing word-specificity order. It preserves substring matching,
the first 500 results for the winning combination, similarity ranking, and the
existing fuzzy fallback when there are no word matches. Candidate pagination
must finish before choosing a match; malformed pages, failed requests, and
repeated cursors raise errors rather than reporting a project as new. Existing
remote execution checks still surround each logical gateway operation.

Deploy the updated design worker with its matching gateway/client code. There
is no database migration or output-version bump. The pinned producer snapshot
and legacy fixtures remain unchanged. Matching requests now scale with result
pages, rather than the number of possible word subsets; very broad candidate
sets and the existing full-board fuzzy fallback still require pagination.

Run the offline parity, pagination, and request-budget tests:

```powershell
venv/Scripts/python.exe -m pytest backend/tests/test_project_matching_efficiency.py backend/tests/test_design_processing_phase3.py backend/tests/test_design_processing_phase5.py -q
venv/Scripts/python.exe -m pytest backend/tests/test_project_matching_efficiency.py -k no_match_requests -q -s
```

The request-budget evaluation compares the prior subset algorithm with the
production gateway and new matcher using fake Monday responses. For a no-match
search with one candidate page and one fuzzy-fallback page, it counts 9 requests
including source-item validation, versus 3,075 for a ten-word title previously.
Retries and other pipeline stages are excluded. No live Monday calls are made.

### Live comparison with `search.items` (22 October 2026)

The service token authenticated successfully. The implemented candidate filter
also passed a live read using the configured `2025-04` API version. A separate
read-only evaluation used `search.items` on `2026-07`, scoped to project board
`1825117125`, with `limit: 20` and both `BALANCED` and `QUALITY` strategies.
The four verified projects have numeric item names; their descriptive titles
were read from `text3__1`. The fixture IDs are synthetic and were not used as
live project IDs.

| Query | Implemented matcher found expected project | BALANCED | QUALITY |
| --- | --- | --- | --- |
| Bracken Dale, LA | Yes | Yes | Yes |
| VIE06 - Austria | Yes | Yes | Yes |
| 100 New Kings Road | Yes | Yes | No |
| Wickside, Hepscott Road | Yes | Yes | Yes |
| 100 New Kings Road SW6 4LX | Yes | Yes | No |

`BALANCED` returned the four exact-title projects at rank 1, and the postcode
variant's expected project at rank 2. Searching the reference `16771` was a
successful control. Both strategies returned 20 raw results for each of two
invented project titles. After checking the current board, active state, custom
`date9__1` cutoff, and applying the existing 0.55 similarity threshold, neither
negative case retained a search match. Raw nonempty search results therefore
must not be interpreted as proof that the queried project already exists.

The implemented matcher required 2-4 matching reads for the positive cases.
Search used one request per query, including the live fields needed for
validation. A cold negative required 21 matching reads: the word-count and
candidate queries plus 19 pages covering 9,160 dated active projects for the
fuzzy fallback. That complete fallback scan and identical queries were reused
between evaluation cases only; the production matcher has no new shared cache.
These counts exclude worker source-item validation and complexity weighting.

The comparison harness recorded 52 successful HTTP requests and one connection
retry, separate from the initial connection probes. All operations were reads.
This initial sample suggested testing `BALANCED` further as a fast first pass.
The expanded comparison below supersedes that provisional recommendation.
`QUALITY` was less reliable on these particular known projects. Neither
evaluation switched production to `search.items`, changed the global API
version, or deployed the code.

### Expanded matcher evaluation and recommendation (22 October 2026)

**Keep the optimized paginated board filter as the primary matcher for the
existing project-review workflow.** `BALANCED` retrieved all known intended
projects in this sample, including titles held only in `text3__1`, but omitted
alternative candidates that the current matching report can present for review.

The expanded read-only benchmark used a complete snapshot of 9,160 dated active
projects, live word-specificity counts, and 36 cases: 20 exact titles, eight
altered titles with a known intended source project, four invented nonmatches,
and four ambiguous broad queries. Sixteen exact-title projects were sampled
using seed `20261022`; four were the previously verified anchors. All 20 have
descriptive titles absent from their numeric item names. Both methods use the
same board, active-state, and `date9__1` validation. Two selection policies were
tested over the live `BALANCED` results to separate retrieval from ranking:

| Test | Optimized board matcher | BALANCED + same matching policy | BALANCED + 0.55 similarity threshold |
| --- | --- | --- | --- |
| Exact titles: intended project ranked first | 20/20 | 20/20 | 20/20 |
| Altered titles: intended project ranked first | 8/8 | 8/8 | 6/8 |
| Invented nonmatches rejected | 4/4 | 4/4 | 4/4 |

The threshold-only search approach excluded the intended `Bracken Dale, LA`
project for `LA Dale Bracken`, accepting a different project instead. It returned
no accepted project for `7AW N12 Rd Holden 40-42`, whose intended source was
`40-42 Holden Rd N12 7AW`. Both intended projects were present in the raw search
results and recovered when the same word-selection policy was applied. These
are selection-policy failures, not failures of raw semantic retrieval.

Even with identical selection rules, the complete ordered candidate reports
agreed for only 24 of 28 intended-project cases, and none of the four ambiguous
queries. Broad-query examples illustrate the retrieval difference:

| Query | Optimized report candidates | Raw BALANCED results | Those optimized candidates present in raw search |
| --- | --- | --- | --- |
| Kings Road | 24 | 20 | 9 |
| High Street | 59 | 20 | 4 |
| Industrial Estate | 11 | 20 | 8 |
| School | 500 (existing report cap) | 20 | 10 |

Not every semantic result is a board-filter match, and some raw results fail the
custom-date validation. These counts measure compatibility and candidate
coverage, not independent proof that the board match is semantically superior.
Search's maximum of 20 results and lack of pagination prevent it from preserving
the current candidate-report contract. Its index is asynchronous; resolving
`live_data` validates returned records but cannot restore omitted candidates.
One of four fresh repeat queries changed its candidate set and order, although
the intended project remained present in all four repeats.

The optimized matcher modeled a median of two matching HTTP reads for exact
titles (range 2-7), and three for altered titles (range 2-21). Four actual gateway
probes confirmed both the modeled counts (2, 4, 2, 21) and full matching outputs.
The snapshot model filters the original `text3__1` field, including blanks,
rather than the client's fallback display title. `BALANCED` with the similarity
policy used one HTTP request per query, with median request latency 0.851 seconds;
this is not an end-to-end speed comparison. Using the same global word-specificity
ordering with search would also require a word-count read when uncached.

The remaining request hotspot is the existing full-board fuzzy fallback: 19
additional pages in this snapshot, for 21 matching reads on a cold nonmatch or
the tested Austria typo. A shared incremental project index is a separate
optimization to investigate. The new matcher already removes the exponential
subset-query loop; switching to semantic search is not required for that saving.
`BALANCED` remains suitable for an optional suggestions interface if the product
only needs a few ranked results.

The benchmark recorded 73 successful HTTP requests, no connection failures,
and no search GraphQL errors. Shared snapshot/count reads were reused only by
the benchmark; production has no new cross-request cache. Request counts exclude
worker validation, retries, complexity weighting, and other applications using
the account. Twenty exact titles, eight generated variants, and four negative
controls do not establish production-wide recall or false-positive rates.
The targeted matching, gateway, and fixture suite passed: **124 tests**.

Detailed case results, request ledger, and limitations are saved in
[`tmp/project_matcher_benchmark_results.json`](tmp/project_matcher_benchmark_results.json).
The benchmark is [`tmp/benchmark_project_matchers.py`](tmp/benchmark_project_matchers.py);
its full-board response cache was removed after analysis. Application matching
logic was not changed by this expanded evaluation.

### Matcher release verification (22 October 2026)

After repairing the local legacy snapshot, all ten pinned source hashes verify
and the committed golden JSON/CSV reproduce byte-for-byte. No manifest hashes,
expected fixtures, pipeline versions, or database schemas were changed for
this repair or the matching optimization.

```powershell
venv/Scripts/python.exe -m pytest backend/tests -q --disable-warnings --tb=short
# 694 passed, 8 skipped (PostgreSQL integration tests require TEST_DATABASE_URL).
venv/Scripts/python.exe -m pytest backend/tests/test_design_processing_phase1.py -q --disable-warnings --tb=short
# 18 passed, including both legacy verification checks.
git diff --check
```

The matcher update is ready to commit and deploy to the design-processing
worker using the existing Monday API version and configuration. PostgreSQL
integration coverage was not rerun against a database; the update makes no
database changes. Local evaluation reports, recovery utilities, downloaded Git
history, and replaced-source backups are excluded through `.gitignore`.
No commit, push, or production deployment was performed by this verification.

See Monday's [search reference](https://developer.monday.com/api-reference/reference/search)
and [search guide](https://developer.monday.com/api-reference/docs/searching-across-your-account)
for result limits, indexing behavior, and strategy options.
