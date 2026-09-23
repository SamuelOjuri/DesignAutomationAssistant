I’ll map the recommendations onto the existing storage, sync, and reconciliation code so the implementation plan covers reuse, cleanup, and updates that arrive while a job is running.


Searched for regex `def |object_path|TaskFile|TaskSnapshot|delete\(|bucket|distinct` (`**/backend/app/services/auto_sync_purge.py`), 31 results

Searched for regex `def list_item_ids_in_groups|def list_.*group|cursor|limit` (`**/backend/app/monday_client.py`), 38 results

Read , lines 240 to 398

Read , lines 1 to 76

The existing snapshot model can support reuse without replacing retrieval. I also found a reconciliation coverage risk: each run selects the first limited batch from the same ordered groups. I’ll verify that path and how derived email attachments are identified before proposing the changes.


Searched for regex `def ingest_derived_attachment_bytes|def download_asset_to_temp|parent_asset_id|derived_asset_id|sha256|DownloadedAsset` (`**/backend/app/services/storage_ingest.py`), 22 results

Read , lines 554 to 675

Read , lines 423 to 523

Read , lines 166 to 295

Read , lines 1 to 76

I recommend **incremental sync through the existing durable worker**, with handoff continuing to serve the last completed snapshot. This belongs in the auto-sync and ingestion code, not the design-processing worker.

**1. Separate Refresh From Extraction**
In `storage_ingest.py:190`, keep the current source revision initially, but introduce independent reuse keys:

| Identity | Purpose |
|---|---|
| Source revision | Detect that Monday changed. |
| Metadata fingerprint | Detect changes to item name and relevant column values. |
| Asset processing key | Decide whether a file needs extraction and embedding again. |

The asset processing key should cover **content SHA-256, extraction-relevant file type/role, and processing version**. The processing version should capture parser, prompt/model, chunking, and embedding changes.

Do not assume matching asset IDs prove identical bytes. Unless Monday provides a verified immutable-content guarantee, use the existing streamed download hash to check content. That still avoids the expensive extraction and embedding work.

**2. Reuse Completed Results**
Modify `sync_pipeline.py:152` so each asset follows this decision:

```text
Matching content + compatible completed processing result?
    Yes -> reuse stored files, extracted data, and embeddings
    No  -> process this asset
```

A minimal implementation can retain the existing snapshot structure:

- Create new snapshot-specific `TaskFile` rows referencing existing stored objects.
- Copy existing `TaskChunk` rows and vectors in SQL, without calling Gemini.
- Reuse the complete email family, including derived attachments, and the matching CSV parameters.
- Refresh relevant metadata and regenerate column embeddings only when their text changes.
- Omit removed assets from the new snapshot.
- Publish the new snapshot only after its required work completes; preserve the previous completed snapshot meanwhile.

Add explicit parent-asset ownership, processing-version, and completion metadata. Never treat an interrupted or incompletely extracted asset as a reusable success.

Because snapshots would share storage objects, update `auto_sync_purge.py:166` to delete each unique object once and avoid deleting objects still referenced by retained snapshots. Keep reuse scoped to the same task initially.

**3. Preserve Updates During Execution**
Fix `auto_sync.py:191` and `auto_sync_worker.py:506`:

- Track a desired generation that advances when new work arrives.
- Record the generation and revision being processed.
- At completion, lock the job and task and check whether newer work arrived.
- If it did, recheck or schedule a successor atomically instead of declaring all work satisfied.

Use generation tracking rather than blindly comparing hashes: a pipeline can legitimately fetch a revision newer than the one originally queued.

**4. Make Handoff A Consumer**
In `monday_handoff.py:175`, replace the direct background-pipeline fallback for managed tasks with enqueue/coalesce into the durable queue.

| Handoff state | Behaviour |
|---|---|
| Completed, fresh snapshot | Open immediately; no ingestion. |
| Completed snapshot, refresh needed | Open existing data; queue incremental refresh. |
| Freshness lookup unavailable | Preserve usable data; schedule a retry, not an assumed full rebuild. |
| No usable snapshot | Queue ingestion and show preparation status. |

Use the same queue for manual refresh, with a separate explicit option for forced re-extraction. Record the actual trigger, result, and timestamps consistently.

**5. Fix Reconciliation Coverage**
There is a concrete coverage risk in `auto_sync_reconciliation.py:60`: it repeatedly takes the first limited batch from sorted groups, with no saved progress. Stable ordering can leave later items unchecked indefinitely.

Separate API page size from sweep limits, and persist sweep progress or rotate fairly through all active items. Schedule reconciliation regularly and measure **time since each item was last checked**, not merely whether the scheduler ran. Separately verify webhook subscriptions cover relevant column and attachment changes.

**Implementation Order**
1. Add asset-result reuse and metadata-only refresh, retaining existing snapshot hashes.
2. Add race-safe completion and durable handoff/manual enqueueing.
3. Fix reconciliation coverage and add refresh-reason logging.
4. Roll out to a small allowlist before enabling broadly.

Regression tests should prove: unchanged files cause **zero extraction calls**, one added file processes only that file, removals disappear from retrieval, concurrent updates get a follow-up, every eligible item is eventually reconciled, and shared objects survive until safe deletion.

For this project, a timestamp-only change should then become a lightweight refresh rather than another six-minute extraction. Immediate access is achievable with a completed snapshot; completely fresh data still depends on background processing catching changes before handoff.