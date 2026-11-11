# Monday worker availability and API usage

Processing workers share `monday_item_availability` rather than independently
retrying missing intake items. The key includes the registered board, item and a
SHA-256 fingerprint of `MONDAY_INGESTION_ACCESS_TOKEN`; the token is never stored
in this table. Reviewer OAuth visibility is not used as worker visibility.

## Deployment

1. Apply migration `0020_monday_item_availability` before starting this release:

   ```bash
   python -m alembic -c backend/alembic.ini upgrade head
   python -m alembic -c backend/alembic.ini current
   ```

2. Deploy/restart the API, specification publisher, design worker, ingestion and
   metadata workers, and reconciliation jobs with the same processing token and
   availability settings. An old running worker cannot honor the new suppression.
   Keep existing `DESIGN_PROCESSING_MODE`, `AUTO_SYNC_*` enablement and independent
   specification rollout settings appropriate for their existing workflows.

3. Verify lifecycle webhook subscriptions include `item_deleted`, `item_archived`,
   `item_restored` and the applicable group-movement events. Use the existing
   authenticated webhook endpoint. Lifecycle handlers confirm current state,
   respect event timestamps and ignore stale observations; events do not turn an
   empty lookup into confirmed deletion.

4. Observe availability-probe logs and reconciliation outcomes. A known retired
   item should generate no routine individual source reads. A missing item should
   be deferred by other workers until its common retry deadline. A current active
   item must continue its normal output and ingestion workflow.

No new migration deletes or resets old extraction, PDF, review or task history.
Existing retirement markers are honored without repeating retirement or importing
the 24 historical IDs into source code. The migration only adds a table/index.

## Defaults and settings

All settings are backend environment variables. Defaults are usable without
adding overrides:

```dotenv
MONDAY_ITEM_AVAILABILITY_RETRY_SECONDS=[3600,21600,86400,604800]
MONDAY_ITEM_AVAILABILITY_INACTIVE_RECHECK_SECONDS=604800
MONDAY_ITEM_AVAILABILITY_PROBE_LEASE_SECONDS=120
```

Repeated validated empty lookups back off for one hour, six hours, one day, then
one week, capped at one week. A successful newer observation resets the miss
count. Authentication, throttling and malformed or partial responses remain API
errors and do not mark items missing or deleted.

Confirmed retired items have no normal per-item recheck. Readable archived/deleted
items and positive cross-board moves suppress routine work and have an infrequent
recheck to recover from missed restoration events. Positive live inventory or a
new verified restoration event can trigger a fresh confirmation sooner. Repeated
inventory appearances alone do not reset a current unavailable backoff.

Lightweight due-status probes batch at most 100 item IDs per request and use
database leases so concurrent consumers do not own the same probe. Lease
completion and observation timestamps fence late responses. A failed or partial
batch cannot classify omitted items as missing.

The shared record suppresses unnecessary reads; it does not replace a current
permission/scope check before publication. Live eligibility checks around model
work and file publication remain in place. Specification processing remains
Landing Zone-only. Active-group ingestion, CSV, preview PDF and matched-project
reports retain their existing policies. A move outside the registered board
cancels that original registration's work and preserves its history.

## Operator retry

Preview a nonterminal availability reset (no Monday calls or database writes):

```bash
python -m backend.app.services.monday_item_availability_retry --item-id 1234567890
```

Apply it to make the common deadline and existing pending work due:

```bash
python -m backend.app.services.monday_item_availability_retry --item-id 1234567890 --apply
```

Multiple IDs are supported. `--board-id` defaults to `DESIGN_PROCESSING_BOARD_ID`;
specify it when retrying a registration on another configured board. This action
does not force publication, bypass rollout/group rules, erase saved decisions or
restart unrelated failed model jobs. Confirmed retirement is refused; restoration
requires a fresh positive source observation. Existing explicit item-scoped
reconciliation commands can perform that source check when required.

Each applied item commits separately. A refused retirement produces a nonzero
exit status; inspect all results if a batch contains mixed outcomes.

## Read-only diagnostics

```sql
SELECT board_id, item_id, state, reason, actual_board_id, actual_group_id,
       actual_item_state, consecutive_misses, last_attempted_at, last_observed_at,
       next_check_at, probe_until
FROM monday_item_availability
ORDER BY next_check_at NULLS LAST, board_id, item_id;
```

Several rows for an item can reflect processing-token rotation. A missing record
for an older retired item is expected: legacy retirement checks also suppress
reads. An empty group inventory does not prove deletion; retain unavailable
history until deletion is positively established.
