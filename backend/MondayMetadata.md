# Current Monday CRM context

The holding board's five CRM fields refresh independently of email/document ingestion.
The UI and chat use the same `resolve_task_context()` result. Email CSV parameters remain
historical source evidence. Confirmed clears replace old values; failed/incomplete reads
retain the last successful values with a visible refresh error.

| Field | Column | Source |
| --- | --- | --- |
| Accounts | `board_relation_mm3c4g5x` | Linked item names on board `1654217230` |
| New Enq / Amend | `dropdown_mkpb98es` | Dropdown option IDs and labels |
| TP Ref | `board_relation_mkpbm5np` | Linked item names on board `1825117125` |
| Project Name | `lookup_mkpb44am` | Text `text3__1` on the TP Ref linked projects |
| Zip Code | `dropdown_mkpbafca` | Dropdown option IDs and labels |

## Deployment

1. Apply `python -m alembic -c backend/alembic.ini upgrade head` before deploying
   the updated API, ingestion worker, and reconciliation command. Migration
   `0015_monday_metadata` creates the metadata queue and dependency index. This migration
   does not call Monday or backfill project values.
2. Run an **additional worker service**, independently of document ingestion:
   `python -m backend.app.services.monday_metadata_worker`.
   Use the same `DATABASE_URL`, `MONDAY_INGESTION_ACCESS_TOKEN`, and auto-sync policy
   settings as the API. `AUTO_SYNC_ENABLED=true` is required. There is no extra
   feature flag. Keep the existing auto-sync worker running for document changes.
3. Keep `AUTO_SYNC_DEBOUNCE_SECONDS=90` (the default). Direct and linked webhook
   requests coalesce per holding item. Reconciliation can request immediate checks
   without postponing a pending webhook refresh. The metadata worker polls its database
   every five seconds and makes no Monday requests when no eligible work is due.
4. Continue the existing scheduled reconciliation command across **all active items**:
   `python -m backend.app.services.auto_sync_reconciliation --limit 100 --page-size 500 --completed-transitions --completed-limit 100`.
   Its fair rotation also queues metadata for otherwise fresh tasks. Running a complete
   sweep after deployment initializes the metadata and reverse dependency index for
   existing active projects. Linked events received before this initial sweep are
   recovered by subsequent reconciliation. Allow sweep coverage to complete before
   assessing linked-event latency.
5. Verify the service token can read the holding board and both linked boards. Confirm
   the deployed `MONDAY_API_VERSION` supports the typed query below; this change does
   not silently upgrade the integration's API version.
6. Audit and configure the subscriptions below against the existing authenticated
   `/api/monday/webhooks` endpoint. Subscriptions are not automatically provisioned
   by deploying this code. Use the app's existing webhook authentication mechanism.
7. Deploy the frontend. It refreshes the Summary every 12 seconds while visible and
   when focus returns, even after document sync completes. Requests retain the existing
   task access check, including Monday item authorization; they do not fetch CRM values
   or trigger ingestion. Passive polling does not extend retention.

## Webhook coverage

| Board | Events to verify |
| --- | --- |
| Holding `1882196103` | Existing creation, group moves, file changes; `change_column_value` or specific-column subscriptions covering all five mapped columns |
| Accounts `1654217230` | `change_name`, item archive/delete/restore |
| Projects `1825117125` | `change_name` (TP Ref display), `change_specific_column_value` with `columnId: text3__1`, item archive/delete/restore |

Mirror changes are handled through their source board; no assumption is made that a
mirror update emits a holding-board webhook or changes its `updated_at` timestamp.
The dependency index selects only active holding tasks and is replaced atomically when
links change. Every refresh rereads current Monday values. Leases, generation checks,
and retry backoff prevent an expired or superseded worker from publishing old data.

The ordinary auto-sync dispatcher skips document ingestion when it observes changed CRM
fields and identical remaining snapshot inputs, with no document job already running.
Uncertain changes, initial ingestion, explicit manual sync, and attachment changes still
use the existing full document pipeline and verified asset reuse. The metadata worker
itself never downloads attachments, extracts documents, or calls an LLM.

Completed/excluded tasks stop automatic metadata work. Existing metadata remains visible
as last checked; it is deleted with project data on retention expiry. Reopening an active
task schedules a fresh read. Archived/unreadable linked items that cannot be resolved
produce a retry and preserve last known values rather than an invented empty value.

## Holding-item archival and restoration

Lifecycle reads include Monday's `state`. An item with `state: archived` can still
report Hub B as its group; state takes precedence over group membership. A confirmed
archival sets the stored task to `auto_sync_state=archived` and
`auto_sync_enabled=false`. A confirmed `state: deleted` receives equivalent handling
with `auto_sync_state=deleted`. An empty/unavailable item response does not establish
either state and continues to produce `source_unavailable` without changing stored data.
Missing or unsupported state values cannot activate a task.

Confirmed inactive source items cancel scheduled/running document jobs and metadata
requests, invalidate metadata leases, and clear `purge_after`. Existing snapshots,
files, current metadata, dependencies, and retention holds remain stored. Archival
does not start completed-project retention or trigger data deletion. An external
document operation already in progress can finish; cancelling its job prevents the
worker from finalizing that job or scheduling a successor. Manual sync requests on
known archived/deleted tasks return HTTP 409 until restoration is observed.

The existing `completed_transition` reconciliation scope now covers tracked active
tasks even before ingestion completes, plus archived/deleted tasks. It records
`archived` / `item_archived` when appropriate and revisits inactive tasks to detect
restoration after missed webhooks. Restoration to an eligible active group reenables
auto-sync and schedules fresh metadata and an ordinary document refresh when needed.
Excluded/completed/unmanaged groups do not start automatic ingestion. When global
auto-sync is disabled, restoration is recorded as `reactivation_disabled` and automatic
work remains disabled. The command reports `archived` and `reactivated` counts.

This correction needs **no new migration**: the existing task lifecycle field is a
string. Deploy the matching API, ingestion worker, metadata worker, and reconciliation
code. Keep holding-board archive/delete/restore webhooks configured. Then run the
normal reconciliation command with `--completed-transitions` to repair previously
misclassified tasks. For the three confirmed archived items, targeted commands are:

```bash
python -m backend.app.services.auto_sync_reconciliation --skip-active --completed-transitions --completed-item-id 3076716400
python -m backend.app.services.auto_sync_reconciliation --skip-active --completed-transitions --completed-item-id 3116941506
python -m backend.app.services.auto_sync_reconciliation --skip-active --completed-transitions --completed-item-id 3181967879
```

Add `--dry-run` to preview without changing database records. Successful repair should
show `archived`, disabled auto-sync, no pending metadata/document work, and no purge
deadline; retained project data should remain intact. These commands read Monday and
update only the application database. Keep subsequent lifecycle sweeps running so
restoration can be detected. This code change does not itself update production rows.

## Read-only API validation / Postman

Generate the exact request body used by the implementation:

```powershell
venv/Scripts/python.exe -m backend.app.services.monday_metadata_worker --print-query --inspect-item 3238363578
```

POST that JSON body to `https://api.monday.com/v2`, with your Monday authorization token,
`Content-Type: application/json`, and the deployed `API-Version` header. Use a populated
holding item linked to both an account and a project to validate every field. Never
include tokens when sharing the response.

Alternatively, validate the configured service token without any database writes:

```powershell
venv/Scripts/python.exe -m backend.app.services.monday_metadata_worker --inspect-item 3238363578
```

### Live read validation (2026-09-24)

The configured service token and implementation's exact query/normalizer successfully
read these populated Hub B items, matching the supplied board screenshot:

| Holding item | TP Ref | Project Name | Zip Code |
| --- | --- | --- | --- |
| `3232569452` | `17324` | VIE06 - Austria | outside UK |
| `3236096392` | `15930` | Wickside, Hepscott Road | E |
| `3238363578` | `18675` | Bracken Dale, LA | LA |

Accounts resolved respectively to Kingsley Roofing (London) Limited, Axter Limited,
and Walkers Waterproofing Ltd. New Enq / Amend was empty for Olympus and Amendment
for Wickside and Bracken Dale. All three Project Name mirrors resolved to the same
linked project IDs as TP Ref. Names containing commas remained single values.

These were read-only API checks: no CRM or database records were changed. They validate
extraction and token access; deployment, webhook delivery, and open-UI refresh still
require the acceptance checks below. No additional Postman response is needed to
confirm the populated relation/mirror shape.

## Acceptance checks

- Set/change/clear each of the five fields; verify the open Summary and next chat answer
  agree after debounce and worker execution. Empty fields display `Not set`.
- Rename a linked account and change project `text3__1` without touching the holding
  item. Check the dependency event and new metadata revision.
- Test multiple accounts/projects/dropdown options and names containing commas.
- Relink TP Ref, then edit the former project; the former link should not trigger work.
- Send duplicate/out-of-order events and an edit during a metadata read. Only the latest
  generation can publish. Last good data survives a temporary Monday outage.
- Start document ingestion, then change a CRM field: the separate metadata worker can
  publish while the document snapshot is still building.
- Stop event delivery temporarily and confirm a full reconciliation sweep repairs the data.
- Move a task to an excluded/completed group and verify no further automatic metadata
  publication; reopen it to verify refresh resumes.

Current values are always included directly in assistant context. Once current metadata
exists, historical `monday_columns` chunks are excluded from retrieval and their file is
hidden from Sources. The Summary's **View current Monday details** link generates an
authenticated, uncached `monday_columns.txt` from the current metadata record.
