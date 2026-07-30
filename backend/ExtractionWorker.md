**Revised Functional Contract**

An existing Monday item becomes eligible when:

- Board is `1882196103`.  
- Current group is Landing Zone, `group_mkpbd6vy`.  
- Item ‘`name`’ field (Field ID: ‘`name`’) has been filled by user (Human only; worker must never write or clear)  
- Email column `file_mkpbm883` contains at least one supported `.eml` or `.msg` asset.  
- Its Email-only input revision has not already been processed by the current pipeline version.

The worker processes that same Monday item. It must never create a duplicate item.

| Field | ID | Ownership |
| :---- | :---- | :---- |
| Date Received | `date_mkpb23av` | Worker |
| Hour Received | `hour_mkpbb3j1` | Worker |
| Zip Code | `dropdown_mkpbafca` | Worker |
| AI Data | `file_mkza7y37` | Worker uploads CSV |
| Matched Projects | `file_mm59rntf` | Worker uploads PDF |
| New Enq / Amend | `dropdown_mkpb98es` | **Human only; worker must never write or clear** |
| TP Ref | `board_relation_mkpbm5np` | **Human only; worker must never write or clear** |
| Accounts | `board_relation_mm3c4g5x` | **Human only; worker must never write or clear** |
| Project Name | `lookup_mkpb44am` | Mirror field; never written directly (written automatically by Monday CRM) |
| Email | `file_mkpbm883` | Input only; never re-uploaded or cleared |

All other columns, including Accounts, Priority, Status, Designer, and group membership, remain untouched.

**Exact Legacy Logic**

GitHub repo 'https://github.com/SamuelOjuri/TechnicalDesignAssistant/tree/main'

Use exact legacy code logic for the following \-

Preserve these parts from the GitHub repo:

\- Parameter prompt, parsing, date/time override, postcode handling, and insulation mapping from parameter\_extraction.py  
\- Project-name extraction and the complete matching/ranking algorithm  
\- Monday project-detail extraction and latest-revision selection  
\- Email-versus-CRM precedence rules currently implemented in App.tsx  
\- Cleaning, date/hour/dropdown formatting, and AI Data CSV ordering from monday.py

Only replace framework and infrastructure dependencies: Flask \`current\_app\`, request/response objects, uploaded-file wrappers, and the old HTTP client. These should become injected FastAPI-era settings and gateways using the current retry-enabled Monday and Gemini clients.

**Matching Contract**

The legacy matching algorithm remains unchanged. Its candidate fields map into a stable internal contract:

{

  "schemaVersion": 1,

  "extractedProjectTitle": "100 New Kings Road SW6 4LX",

  "candidateCount": 2,

  "candidates": \[{

    "rank": 1,

    "mondayItemId": "internal-monday-id",

    "projectReference": "16771",

    "projectTitle": "100 New Kings Road",

    "similarity": 0.818,

    "matchPercentage": "81.8%",

    "createdDate": "2025-02-10"

  }\]

}

`projectReference` is the candidate’s legacy `name` value, currently displayed in brackets. `mondayItemId` is retained for audit purposes but need not be shown prominently.

The worker must not:

- Select `best_match`.  
- Infer the final enquiry type from the candidate list.  
- retrieve and merge a candidate project’s CRM parameters.  
- populate New Enq / Amend or TP Ref or Accounts fields

Legacy `exists` and `best_match` values may be retained for parity diagnostics, but they are not business decisions.

**PDF Report**

Use a server-side PDF generator such as `reportlab`, which is not currently installed. The report uploaded to `file_mm59rntf` should contain:

1. Report title and source Monday item.  
2. Extracted project title.  
3. Account: ‘Company’ from extracted parameters.  
4. Total potential-match count.  
5. Candidates in legacy similarity order.  
6. For every candidate: project title, project reference in brackets, and percentage to one decimal place.  
7. Optional created date.  
8. A clear reviewer action: set New Enq / Amend and TP Ref before moving the item to an active group.

“No matches found” is a valid report outcome. A Monday search/API failure is not equivalent to no matches and must instead retry or fail the job without publishing a misleading report.

Use a revisioned filename such as:

Matched\_Projects\_\<item\_id\>\_\<input\_revision\_12\>.pdf

**Separate Queue Model**

Add migration `0010_design_processing_queue.py` with two independent concepts:

- `design_processing_items`: latest desired and processed revision, business state, extracted outputs, match result, artifact IDs, warnings, and timestamps.  
- `design_processing_jobs`: durable execution records with scheduling, attempts, locks, heartbeat, stage, errors, and completion metadata.

Job statuses should be `scheduled`, `running`, `retry_wait`, `completed`, `failed`, and `cancelled`. Stages should include `waiting_for_email`, `extracting`, `matching`, `rendering`, `writing_columns`, `uploading_ai_data`, and `uploading_match_report`.

Create a PostgreSQL partial unique index allowing only one active design-processing job per board item. Do not reuse `Task.sync_status` or `AutoSyncJob`; those belong to the indexing workflow in auto\_sync\_worker.py.

**Input Revision**

Calculate the design input revision solely from assets referenced by `file_mkpbm883`:

SHA-256(sorted(asset\_id, filename, size, created\_at))

After downloading, also record each file’s content SHA-256. Never include item `updated_at`, AI Data, Matched Projects, or unrelated attachments in this revision. Otherwise, the worker’s own Monday writes would continually trigger new processing.

Include a pinned legacy commit SHA and model identifier in `pipeline_version`. Reprocessing is required when either the Email revision or pipeline version changes.

**Webhook Dispatch**

Extend the authenticated receiver in monday\_webhooks.py to dispatch independently:

1. Authenticate and deduplicate the webhook as it does today.  
2. Fetch current item state once; do not trust an out-of-order payload’s group.  
3. Keep Landing Zone excluded from `auto_sync_jobs` in config.py.  
4. Dispatch Landing Zone Email events to `design_processing_jobs`.  
5. Coalesce `create_item`, move-to-Landing, and Email-column events for the same item.  
6. Ignore AI Data and Matched Projects changes for design processing.

A create event may arrive before the Email asset is available. Queue it in `waiting_for_email`, retry readiness without consuming normal failure attempts, and retain a periodic Landing Zone reconciliation command as a missed-webhook safety net.

Add a child dispatch table, so one webhook can record outcomes such as `auto_sync=excluded` and `design_processing=queued`.

**Worker Pipeline**

1. Claim with PostgreSQL `FOR UPDATE SKIP LOCKED`, using the lease and heartbeat pattern already implemented.  
2. Re-fetch board, group, item ‘name’ column,  Email column, and assets immediately before processing.  
3. Cancel safely if the item has left Landing Zone or the queued revision is superseded.  
4. Download each Email asset once to temporary storage.  
5. Run the pinned legacy email, attachment, PDF/image, parameter, and project-title extraction logic.  
6. Run the complete legacy matching algorithm against board `1825117125`.  
7. Persist extracted parameters and matches before external writes so upload retries do not repeat Gemini calls.  
8. Generate the canonical AI Data CSV and matching PDF.  
9. Update Date Received, Hour Received, and Zip Code.  
10. Upload AI Data, then upload Matched Projects last; report presence becomes the visible “ready for human review” signal.  
11. Mark the item state `ready_for_review`.

The AI Data CSV should retain all 16 extracted parameters. Since no project is selected, it contains email-derived values only. Any default inserted by a business rule should have source `Business Rule`; it must not be represented as a human CRM decision.

**Idempotent Monday Writes**

Extend monday\_client.py with typed helpers for column updates, file upload, file-column inspection, and automation-owned file replacement.

File uploads are at-least-once side effects. Before retrying an uncertain upload, inspect the target column for the deterministic filename and adopt the existing asset. On a new Email revision, upload the replacement first and delete only prior asset IDs recorded as automation-owned. Never clear an entire file column.

Before every column mutation, construct an allow-listed payload and assert that neither `dropdown_mkpb98es` nor `board_relation_mkpbm5np` is present.

**Implementation Layout**

- `services/design_processing_queue.py`: state upsert and job coalescing.  
- `services/design_processing_worker.py`: claim, lease, heartbeat, retry loop.  
- `services/design_processing_pipeline.py`: staged orchestration.  
- `services/legacy_enquiry/`: framework-independent legacy extraction and matching code.  
- `services/match_report.py`: report DTO and PDF rendering.  
- `services/design_processing_reconciliation.py`: Landing Zone recovery scan.  
- monday\_client.py: queries, mutations, and multipart uploads.  
- monday\_webhooks.py: dual-queue dispatch only.

Pin the extraction model separately, initially to the legacy `gemini-2.5-flash`, rather than inheriting the current general `gemini_model`.

**Verification And Rollout**

Golden tests should compare legacy and new extraction parsing, candidate membership, ordering, references, and percentages. Mutation tests must prove New Enq / Amend and TP Ref remain unchanged under success, retry, and reprocessing.

Also test webhook coalescing, Email-only revision stability, partial upload recovery, no-match reports, search failures, stale jobs, PDF text contents, and the transition where a reviewer updates both human-owned fields and moves the item into an active group.

Deploy initially in shadow mode with Monday writes disabled, compare results and p50/p95 timings against historical emails, then enable writes for selected item IDs. Do not automatically backfill every existing Landing Zone item; use an activation timestamp or explicit item-scoped backfill before enabling reconciliation broadly.

To meet or beat the legacy app:

\- Do not run RAG embedding or the existing \`run\_sync\_pipeline()\` inside design processing.  
\- Download the email once and use temporary files for attachments.  
\- Preserve legacy Gemini batching while bounding per-job attachment concurrency.  
\- Persist each completed stage so retries do not repeat successful Gemini extraction.  
\- Cache Monday board-column metadata briefly.  
\- Run one memory-heavy job per worker initially, then increase concurrency from measured memory and API limits.  
\- Compare parameter equality, candidate ordering, Monday payloads, and p50/p95 processing time against a fixed historical test corpus in shadow mode before enabling writes.

