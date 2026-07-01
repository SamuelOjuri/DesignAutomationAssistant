## Plan: Active Queue Auto-Sync

Implement active queue warming for monday board `1882196103` by adding a durable auto-sync orchestration layer around the existing sync pipeline. The existing item ingestion, extraction, Storage upload, chunking, embedding, and pgvector insert path remains the single source of truth. The new feature decides which items should be pre-indexed, when durable work should be scheduled, how lifecycle state is tracked, and when expired completed-item data is purged.

The important scope correction is that auto-sync uses an allow-list for the active designer queue. It must not bulk-index the `Completed Folder`, which currently contains the historical archive.

```text
AUTO_SYNC_BOARD_ID = "1882196103"

AUTO_SYNC_ACTIVE_GROUP_IDS = {
  "topics",          # Hub A - Outstanding
  "group_mkpbs35c",  # Hub B - Outstanding
  "group_mkqbx92r",  # Went Back For Info
}

AUTO_SYNC_EXCLUDED_GROUP_IDS = {
  "group_mkpbd6vy",  # IS&CS Landing Page
}

AUTO_SYNC_COMPLETED_GROUP_ID = "group_mkpbb3tx"  # Completed Folder
```

## Design Decisions

- Existing sync/extraction logic remains the ingestion engine. Do not fork `backend/app/services/sync_pipeline.py`.
- Auto-sync uses an active-group allow-list, not an "all groups except landing page" rule.
- `Completed Folder` is not part of initial bulk auto-sync or routine active reconciliation.
- The 30-day expiry starts when an indexed item enters the completed group, not when it is first auto-synced.
- Keep lightweight task lifecycle metadata after heavy data is purged, so reconciliation does not re-index intentionally expired completed items.
- User-facing authorization remains per-user. The service token prepares data but does not grant access.
- New auto-sync work must use durable database jobs. Do not rely on FastAPI in-memory `BackgroundTasks` for webhook, debounce, retry, reconciliation, or purge execution.
- Keep current handoff/manual sync behavior stable until it can be migrated safely. The first auto-sync implementation should not regress existing user-triggered sync paths.

## Current Codebase Constraints

- Canonical task identity is already `account_id:board_id:item_id` via `tasks.external_task_key`. There is no `Task.user_id` ownership field today.
- Read access is checked at request time through `UserMondayLink` and `can_read_item`, so service-indexed data should still be protected by existing user authorization checks.
- Current handoff/manual sync marks `sync_status = "syncing"` before background execution actually starts. Auto-sync should not repeat this behavior; it needs a distinct queued state.
- `run_sync_pipeline` already short-circuits unchanged snapshots through `compute_snapshot_version`. Auto-sync should reuse and eventually expose this source revision concept.
- `task_snapshots.task_context_json` can contain rich monday item context. Expiry must account for snapshots, not only vector chunks and raw files.
- `monday_client.py` currently does not pin a monday API version. Auto-sync should add a shared monday request helper with versioned headers.

## State Model

### Execution State

Execution state describes the current or latest ingestion attempt.

```text
queued      durable job exists but no worker has started it
syncing     worker has claimed the job and is running ingestion
completed   pipeline finished successfully
failed      pipeline failed after the current attempt
```

Use a separate field for result detail:

```text
last_sync_result = done | unchanged | skipped | failed
```

Avoid using `unchanged` as the primary public `sync_status` until the frontend is ready for it. Keep `completed` as the terminal success state and store `unchanged` in `last_sync_result`.

Useful task timestamps:

```text
sync_requested_at
sync_started_at
sync_finished_at
last_successful_sync_at
```

Useful trigger metadata:

```text
last_sync_trigger = webhook | backfill | reconciliation | handoff | manual | restore
```

### Lifecycle State

Lifecycle state describes how the auto-sync system should treat the task.

```text
active               item is in an active designer queue group
completed_retained   item was indexed while active and is now in Completed Folder retention
expired              heavy data was intentionally purged; lightweight tombstone remains
excluded             item is in an excluded group
paused               auto-sync is intentionally paused for this item
```

Example valid combinations:

```text
auto_sync_state = active
sync_status = failed

auto_sync_state = completed_retained
sync_status = completed

auto_sync_state = expired
sync_status = completed
last_sync_result = skipped
```

### Lifecycle Transitions

```text
Current lifecycle       Current monday group        New lifecycle          Action
unknown                 active group                active                 create task, queue sync
active                  another active group         active                 update group metadata, sync if source changed
active                  Completed Folder             completed_retained     set completed_at and purge_after
completed_retained      active group                 active                 clear expiry, queue restoration sync
completed_retained      Completed Folder             completed_retained     keep or extend retention only on meaningful access
any                     excluded group               excluded               cancel pending auto-sync; do not queue
expired                 Completed Folder             expired                do nothing automatically
expired                 active group                 active                 queue restoration sync
paused                  any                          paused                 no automatic work
```

## Data Model And Migrations

Add a new Alembic migration and matching SQLAlchemy models.

### Task Lifecycle Fields

Add these fields to `tasks`:

```text
auto_sync_enabled boolean default false
auto_sync_state string nullable
source_group_id string nullable
source_group_title string nullable
auto_synced_at timestamptz nullable
completed_at timestamptz nullable
purge_after timestamptz nullable
last_meaningful_access_at timestamptz nullable
sync_requested_at timestamptz nullable
sync_started_at timestamptz nullable
sync_finished_at timestamptz nullable
last_successful_sync_at timestamptz nullable
last_sync_trigger string nullable
last_sync_result string nullable
last_indexed_source_revision string nullable
retention_hold boolean default false
retention_hold_at timestamptz nullable
retention_hold_by string nullable
retention_hold_reason text nullable
ingestion_actor string nullable
```

Keep existing `done_at`, `delete_raw_after`, `raw_purged_at`, `sync_status`, `sync_completed_at`, and `sync_error` for compatibility. New auto-sync code should prefer `purge_after` and the newer sync timestamps, but existing routes should not be broken.

Add indexes for:

```text
tasks.auto_sync_state
tasks.purge_after
tasks.source_group_id
tasks.sync_status
tasks.last_indexed_source_revision
```

Task identity remains:

```text
external_task_key = account_id:board_id:item_id
```

Optionally add a defensive unique constraint on `(account_id, board_id, item_id)` if it does not conflict with existing data. Do not introduce task ownership by service user.

### Webhook Event Table

Add immutable audit records for monday notifications:

```text
monday_webhook_events
  id uuid primary key
  idempotency_key string unique not null
  monday_event_id string nullable
  subscription_id string nullable
  trigger_uuid string nullable
  board_id string nullable
  item_id string nullable
  group_id string nullable
  event_type string nullable
  column_id string nullable
  payload_json json not null
  received_at timestamptz not null
  authenticated boolean not null default false
  processed_at timestamptz nullable
  status string not null
  error text nullable
```

This table explains what monday reported. It should not be used as the executable queue.

### Durable Job Table

Add a separate table for executable auto-sync work:

```text
auto_sync_jobs
  id uuid primary key
  board_id string not null
  item_id string not null
  external_task_key string nullable
  trigger_type string not null
  desired_source_revision string nullable
  status string not null
  scheduled_for timestamptz not null
  attempt_count integer not null default 0
  max_attempts integer not null default 3
  next_retry_at timestamptz nullable
  locked_at timestamptz nullable
  locked_by string nullable
  heartbeat_at timestamptz nullable
  started_at timestamptz nullable
  completed_at timestamptz nullable
  last_error text nullable
  created_at timestamptz not null
  updated_at timestamptz not null
```

Job statuses:

```text
pending
scheduled
running
retry_wait
completed
skipped
failed
cancelled
```

Enforce item-level coalescing so multiple events for one item do not create parallel extraction work:

```text
one active job per board_id + item_id
where status in pending, scheduled, running, retry_wait
```

When a new relevant event arrives for an item with an active job, update the existing job's `scheduled_for`, `trigger_type`, and `desired_source_revision` instead of inserting another job.

## Configuration

Add backend-owned settings in `backend/app/config.py`:

```text
auto_sync_enabled
auto_sync_board_id
auto_sync_active_group_ids
auto_sync_excluded_group_ids
auto_sync_completed_group_id
auto_sync_retention_days
auto_sync_debounce_seconds
auto_sync_backfill_batch_size
auto_sync_worker_enabled
auto_sync_reconciliation_enabled
auto_sync_purge_enabled
monday_ingestion_access_token
monday_api_version
```

The producer frontend config in `producer/TechnicalDesignAssistant/frontend/src/lib/monday-columns.ts` can remain for item creation. It should not be the backend auto-sync source of truth.

## Monday API Support

Extend `backend/app/monday_client.py` without changing the existing `fetch_item_with_assets` contract.

1. Add a shared monday request helper that sets:

```text
Authorization
API-Version or monday version header required by the current API
timeout
```

2. Pin a configured monday API version, for example `MONDAY_API_VERSION=2025-04` or the current version chosen after testing.

3. Add helper queries for:

```text
fetch item metadata: account, board, group, updated_at
list item ids in active groups
fetch board group metadata for validation
fetch current source revision inputs
```

4. Use group IDs for decisions. Use group titles only for diagnostics and UI/logging.

## Source Freshness

Define freshness from source state, not elapsed time alone.

For MVP, use a source revision value that can be compared before queueing and after sync:

```text
source_updated_at
asset ids
asset names
asset sizes
asset created/updated timestamps when available
relevant file column values
group id
```

The existing `compute_snapshot_version(item)` already hashes monday item `updated_at` and asset IDs. Reuse it inside the pipeline, then expose or mirror the resulting value as `last_indexed_source_revision` after success. Auto-sync can start with this and later expand the fingerprint if file-column changes are not represented strongly enough.

Decision rule:

```text
if desired_source_revision == task.last_indexed_source_revision:
    mark job skipped/completed with last_sync_result = skipped or unchanged
else:
    queue or run sync
```

## Service Token Strategy

Auto-sync needs a non-interactive monday token.

Preferred path:

```text
dedicated monday service user connected through OAuth and stored in user_monday_links
```

Acceptable MVP path:

```text
MONDAY_INGESTION_ACCESS_TOKEN stored as a backend-only secret
```

Keep concepts separate:

```text
task identity = account_id + board_id + item_id
ingestion actor = service token or service user
requesting user = current Supabase-authenticated app user
authorized viewer = user who passes current monday access checks
```

The service token must not bypass user authorization for summary, sources, signed URLs, chat, or handoff.

## Release Sequence

### Release 1: Foundation

1. Add backend auto-sync config and policy helper.
2. Add lifecycle fields, webhook event table, durable job table, constraints, and indexes.
3. Add service-token retrieval.
4. Add monday API version support and metadata/listing helpers.
5. Document execution and lifecycle state machines.

### Release 2: Durable Worker And Active Backfill

6. Add a worker process command that claims due jobs with row locking or leases.
7. Worker transition:

```text
scheduled or pending -> running -> completed / retry_wait / failed / skipped
```

8. Worker updates task state:

```text
queued when job is scheduled
syncing when worker starts
completed when pipeline succeeds
failed when the attempt fails and no immediate success is available
```

9. Add stuck-job recovery using `locked_at`, `locked_by`, and `heartbeat_at`.
10. Add dry-run active backfill for `topics`, `group_mkpbs35c`, and `group_mkqbx92r` only.
11. Process the current active queue in small batches, initially 5-10 items.

At this point the main business benefit can be tested before webhook complexity is enabled.

### Release 3: Handoff Reuse And Reconciliation

12. Update `backend/app/routes/monday_handoff.py` so a task with a fresh completed snapshot is not forced back to `syncing`.
13. Keep handoff-triggered sync as a fallback for missed, stale, failed, expired, and force-refresh cases.
14. Add reconciliation that scans only the active groups and creates/coalesces jobs for missing, stale, failed, or stuck tasks.
15. Add lower-frequency completed-transition detection for active tasks that moved to `group_mkpbb3tx`.

### Release 4: Webhooks

16. Add `backend/app/routes/monday_webhooks.py` and include it in `backend/app/main.py`.
17. Implement monday challenge response.
18. Verify webhook authorization JWT with `MONDAY_SIGNING_SECRET`, expiry, and audience when available.
19. Persist webhook events only after authentication succeeds.
20. Normalize monday payloads into internal event fields.
21. Subscribe to and test actual monday event payload types before hardcoding dispatch names. For example, item creation may appear as `create_pulse`, and column-change payloads may include `columnId`, `value`, and `previousValue`.
22. For file changes, subscribe to relevant column-change events and filter by Email and AI Data column IDs.
23. Coalesce events into durable jobs with debounce. Do not start extraction directly from the request.

### Release 5: Completed Retention And Purge

24. When an indexed active item moves to `group_mkpbb3tx`, set:

```text
auto_sync_state = completed_retained
completed_at = now if missing
purge_after = completed_at + retention days
```

25. Do not apply 30-day expiry while an item remains in an active group.
26. Track meaningful access with `last_meaningful_access_at`. Do not let passive summary polling extend retention indefinitely.
27. Count these as meaningful access:

```text
chat message sent
signed URL opened/downloaded
explicit restore
explicit refresh
retention hold/pin action
```

28. Implement retention hold fields rather than only a bare `pinned` boolean.
29. Implement idempotent purge with a non-transactional Storage deletion flow.
30. Add on-demand restoration for expired completed items when an authorized user opens or explicitly restores them.

### Release 6: Frontend And Operations

31. Optionally update the task page to distinguish `pre-indexed`, `queued`, `syncing`, `expired`, `restoring`, and `failed` states.
32. Add admin/debug endpoints or scripts to inspect events/jobs, retry failed jobs, cancel jobs, trigger active-group reconciliation, and place/remove retention holds.
33. Add metrics for queue depth, oldest job age, sync duration, retries, failures, files, chunks, and purge errors.
34. Document deployment setup: worker command, scheduler command, monday webhook subscriptions, env vars, service token setup, active group IDs, retention policy, and safe initial backfill procedure.

## Webhook Requirements

Webhook route behavior:

```text
1. Detect and answer challenge requests.
2. Require HTTPS in deployed environments.
3. Extract Authorization JWT.
4. Verify signature using MONDAY_SIGNING_SECRET.
5. Verify token expiry.
6. Verify audience when monday provides or requires it.
7. Confirm payload boardId matches 1882196103.
8. Build event idempotency key.
9. Persist event.
10. Fetch current item group/source metadata.
11. Apply policy.
12. Create or coalesce a durable job.
13. Return 200 quickly.
```

Decision matrix:

```text
board != 1882196103
    ignore

group in active groups
    upsert Task
    auto_sync_state = active
    create or coalesce sync job

group == Completed Folder
    if task was previously indexed:
        auto_sync_state = completed_retained
        set completed_at and purge_after
    else:
        do not bulk-index

group == IS&CS Landing Page
    mark excluded if task exists
    cancel pending auto-sync jobs

unknown group
    ignore unless explicitly configured later
```

## Purge Semantics

Expired completed items should remove heavy data while preserving a minimal tombstone.

### Purge Matrix

```text
Data                                      Expiry action
task_chunks and embeddings                delete
Supabase raw objects                       delete
task_files                                 delete or mark purged after Storage delete succeeds
task_snapshots                             delete or minimize
task_context_json in snapshots             clear/minimize through snapshot deletion or redaction
main tasks row                             retain minimal lifecycle tombstone
webhook raw payload JSON                   redact or apply short retention
operational logs                           retain only non-sensitive metadata
audit fields                               retain
```

### Purge State Flow

Storage deletes and database deletes are not atomic. Use a retryable state flow:

```text
completed_retained
    -> purge_pending
    -> storage_deleting
    -> database_cleaning
    -> expired
```

If Storage deletion fails:

```text
keep file reference needed for retry
record delete_error
retry with backoff
do not mark task fully expired
```

## Relevant Files

- `backend/app/config.py` - add backend-owned auto-sync board/group/token/retention/API-version settings.
- `backend/app/models.py` - add task lifecycle fields, webhook event model, and durable job model.
- `backend/migrations/versions/0006_*.py` - add lifecycle fields, event/job tables, constraints, and indexes.
- `backend/app/monday_client.py` - add versioned request helper and board/group/item metadata queries.
- `backend/app/services/sync_pipeline.py` - reuse as-is for ingestion; expose result metadata only if needed.
- `backend/app/services/auto_sync_policy.py` - new eligibility and lifecycle policy helper.
- `backend/app/services/auto_sync.py` - new orchestration service for task upsert, lifecycle updates, source revision checks, and job coalescing.
- `backend/app/services/auto_sync_worker.py` - new durable job claim/run/retry logic.
- `backend/app/services/auto_sync_reconciliation.py` - new active-group reconciliation logic.
- `backend/app/services/auto_sync_purge.py` - new retention and purge logic.
- `backend/app/routes/monday_webhooks.py` - new monday webhook receiver.
- `backend/app/routes/monday_handoff.py` - skip visible sync when a fresh completed snapshot already exists; preserve fallback sync.
- `backend/app/routes/tasks.py` and `backend/app/routes/chat.py` - update meaningful-access tracking only where appropriate.
- `backend/app/main.py` - include webhook route. Do not rely on `main.py` alone as the durable worker host.
- `producer/TechnicalDesignAssistant/frontend/src/lib/monday-columns.ts` - leave current producer config alone.

## Verification

1. Unit-test auto-sync policy: active groups are included, `group_mkpbd6vy` is excluded, `group_mkpbb3tx` is completed-retention only, and unknown groups are ignored.
2. Unit-test lifecycle transitions: active to completed sets `completed_at` and `purge_after`; completed to active clears expiry and queues restoration; excluded avoids queueing; retention hold prevents purge.
3. Unit-test execution states: jobs transition from queued to syncing to completed or failed; `last_sync_result` captures done, unchanged, skipped, or failed.
4. Unit-test event dedupe: duplicate webhook payloads are accepted but do not create duplicate audit work.
5. Unit-test job coalescing: several different events for the same item within the debounce window produce one active job.
6. Test out-of-order events: a delayed active-group event arrives after the item moved to Completed; current-state lookup prevents wrong reactivation.
7. Integration-test monday webhook challenge and JWT verification with monday-style payloads.
8. Integration-test active backfill with a mocked monday client: only `topics`, `group_mkpbs35c`, and `group_mkqbx92r` item IDs are queued.
9. Integration-test handoff resolve for a pre-indexed task: it returns `externalTaskKey` without setting `sync_status` back to `syncing`.
10. Integration-test service-indexed authorization: unauthorized users cannot access summary, sources, signed URLs, or chat for a service-indexed task.
11. Worker crash test: a worker dies during extraction; lease expires and another worker can retry.
12. Stuck-sync test: task or job remains running beyond timeout and reconciliation marks it retryable.
13. Partial purge test: Storage deletion fails after some DB work; file references and errors remain retryable.
14. Token revocation test: service token invalidation does not break manual or handoff access.
15. Migration test: existing task rows receive safe defaults and are not accidentally classified as auto-synced.
16. Rate-limit test: monday or embedding APIs return transient throttling responses and the worker retries with backoff.
17. Deleted-item test: an active monday item is deleted or archived before the queued sync starts.
18. Restore test: an expired completed item moves back into an active group and is correctly rebuilt.
19. Run backend tests, then run Alembic migration against staging and verify indexes/constraints.
20. Run a staging backfill for a small batch first, inspect task status, file count, chunk count, Supabase Storage objects, and Gemini cost/timing before enabling full active-group backfill.

## Operational Acceptance Criteria

- All current active items can be reconciled without touching the 5,399-item historical archive.
- Repeating the same webhook many times results in one accepted event effect and one effective job.
- Several different events for one item inside the debounce window produce one sync job.
- A pre-indexed item opens through handoff without being incorrectly shown as `syncing`.
- An active item moving to Completed gets a retention deadline rather than immediate deletion.
- An intentionally expired item is not rebuilt by active reconciliation.
- An expired item opened by an authorized user can be restored through the fallback path.
- Service-token ingestion does not grant unauthorized access.
- A worker restart does not lose scheduled jobs.
- Partial Storage deletion remains visible and retryable.
- Metrics show queue depth, oldest job age, success rate, failures, durations, files, chunks, and retries.

## Further Considerations

1. Confirm monday webhook event names, payload shapes, Authorization JWT claims, and signature validation details against monday's current docs before implementation.
2. Confirm the monday API version to pin and add contract tests for item metadata lookup, active-group listing, asset retrieval, webhook payload handling, and group validation.
3. Decide whether old completed items opened after expiry should auto-reindex immediately or show a clear "restore context" action first.
4. Decide whether summary page load should count as meaningful access at most once per day, or not at all.
5. Decide whether webhook raw payloads need short retention or redaction to avoid retaining project context after expiry.