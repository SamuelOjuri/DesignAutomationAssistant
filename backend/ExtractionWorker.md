**Revised Functional Contract**

An existing Monday item becomes eligible when:

- Monday item state is exactly `active`; archived, deleted, missing, malformed, and unknown states are ineligible.
- Board is `1882196103`.  
- Current group is Landing Zone, `group_mkpbd6vy`.  
- Item ‘`name`’ field (Field ID: ‘`name`’) has been filled by user (Human only; worker must never write or clear)  
- Email column `file_mkpbm883` contains at least one supported `.eml` or `.msg` asset.  

For every eligible item, the desired processing identity is the full `(input_revision, pipeline_version)` pair. Analysis and publication are separate obligations: an identity can be analyzed without having been published to Monday. Scheduling uses the desired, analyzed, and published identities together with the configured operational mode; it must not use a single “processed” flag.

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

Use the local legacy repository at:

`C:\Users\SamuelOjuri\OneDrive - Tapered Plus\Documents\CodeProjects\DesignAutomationAssistant\producer\TechnicalDesignAssistant`

The parent repository intentionally ignores `producer/`, so legacy Git history is not the parity authority. Treat the local directory as read-only source input and pin the exact required file bytes with this canonical SHA-256 manifest:

```json
[{"path":"backend/app/routes/monday.py","sha256":"ac8268d144021a7139d9035de664ed6f217b539fec383421b34ce38f1733cdea"},{"path":"backend/app/services/file_processor.py","sha256":"02598c4fd713b91063686b5076c915689059db08d6446eeb534c5eba0ae0b27d"},{"path":"backend/app/services/parameter_extraction.py","sha256":"6e945ed3fcb4c6f2774fb137cc2797d8f3cc53ef9307e1f6ad37eb2623517893"},{"path":"backend/app/utils/email_extraction.py","sha256":"31a815d510aa068f94516c6f8767ee965e3aeb4175285a6b9e38c207a569e41c"},{"path":"backend/app/utils/helpers.py","sha256":"93e0513e1d346ca247ce384f0575392ab48df0a756baec8aae1ce8462a463a4d"},{"path":"backend/app/utils/image_extraction.py","sha256":"52a3fd468d7a38a35953e8723b39651c0604374b5834e81dbb3d0d07a5bde93d"},{"path":"backend/app/utils/llm_interface.py","sha256":"105d17a71e9deb5e0f7a8a289e3e00af687b905998a087088b0e48be59930cfa"},{"path":"backend/app/utils/monday_dot_com_interface.py","sha256":"5c299fadb2da28b89c41d56137a036feb09130ba5fc928c5d2ab17489c335232"},{"path":"backend/app/utils/pdf_extraction.py","sha256":"649c2b1795a0f8acd8e763445b866acc3e3e7bd14f4e1a12dffa0b5918d0dccd"},{"path":"backend/app/utils/thread_pool.py","sha256":"db5ce8ece62f916596d92b99013fc47d83e6784d2929912521b3553b11081ecc"}]
```

The manifest is ordered lexicographically by relative path and serialized as UTF-8 JSON with object keys in `path`, `sha256` order and no insignificant whitespace. Its SHA-256 is `82d5612a9efce97660c3a3fef36a731d45597cb3096e58365865727ba719e28e`.

Before copying or adapting legacy logic, verify every local file against its manifest hash and fail setup on any missing or changed file. Generate the legacy side of golden fixtures from those verified bytes before modifying them. Store this manifest as a tracked file beside the ported code in `services/legacy_enquiry/`; the ignored local directory must not be required at application runtime. Any later legacy upgrade requires an explicit plan change, a newly generated manifest and digest, regenerated golden fixtures, and a new `pipeline_version`.

Use exact legacy code logic for the following:

Preserve these parts from the pinned local source snapshot:

\- Parameter field semantics, date/time override, postcode handling, and insulation mapping from parameter\_extraction.py. Replace only the legacy line-oriented LLM response parser with Gemini JSON-schema structured output so field names cannot match prose inside another field. Require all schema fields, allow nullable string values, validate with Pydantic, and fail the analysis attempt on missing, malformed, or extra fields rather than falling back to text parsing.  
\- Project-name extraction and the complete matching/ranking algorithm  
\- Cleaning, date/hour/dropdown formatting, and AI Data CSV ordering from monday.py

The legacy project-selection flow is explicitly out of scope for this release. Do not port or invoke candidate `best_match` selection, project-detail or revision retrieval, Monday CRM parameter extraction, or the Email-versus-CRM merge rules from `App.tsx`. The worker stops after producing the email-only parameter set and the ordered candidate list. Consequently, `backend/app/services/monday_service.py` and `frontend/src/App.tsx` are not part of the pinned source manifest.

The extracted `Company` parameter remains an email-derived text value. It may be shown as context to help the reviewer choose the human-owned Accounts relation, but it is not a resolved Monday account and must never be written to `board_relation_mm3c4g5x`.

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

The report may label each `projectReference` as a candidate TP Ref. It must present all candidates in legacy order and must not select or write any candidate.

The worker must not:

- Select `best_match`.  
- Infer the final enquiry type from the candidate list.  
- retrieve and merge a candidate project’s CRM parameters.  
- populate New Enq / Amend or TP Ref or Accounts fields

Legacy `exists` and `best_match` values may be retained for parity diagnostics, but they are not business decisions.

**PDF Report**

Use a server-side PDF generator such as `reportlab`, which is not currently installed. The report uploaded to `file_mm59rntf` should contain:

1. Report title and source Monday item.  
2. Extracted Project Title.  
3. Extracted Company (Client Company Name) from the email parameters, clearly labelled as context for the reviewer’s Accounts decision rather than a resolved Monday account.  
4. Total potential-match count.  
5. Candidates in legacy similarity order.  
6. For every candidate: project title, project reference labelled as a candidate TP Ref, and match percentage to one decimal place.  
7. Optional created date.  
8. A clear reviewer action: choose Accounts using the extracted Company as context, decide New Enq / Amend, and, for an amendment, select the appropriate candidate TP Ref before moving the item to an active group.

“No matches found” is a valid report outcome. A Monday search/API failure is not equivalent to no matches and must instead retry or fail the job without publishing a misleading report.

Calculate `pipeline_digest` as the lowercase hexadecimal SHA-256 of the UTF-8 `pipeline_version`. Use both the input revision and pipeline digest in deterministic artifact filenames:

```text
AI_Data_<item_id>_<input_revision_12>_<pipeline_digest_12>.csv
Matched_Projects_<item_id>_<input_revision_12>_<pipeline_digest_12>.pdf
```

The 12-character values are filename labels only. Persist and compare the full input revision and `pipeline_version` when deciding whether an artifact is current or may be adopted.

The presence of any file in Matched Projects proves only that some result was published previously. It is never an authoritative readiness signal, and no consumer may infer `ready_for_review` from attachment presence or a filename alone.

**Separate Queue Model**

Add migration `0010_design_processing_queue.py` with four independent persistence concepts:

- `design_processing_items`: one row per board item containing `latest_desired_input_revision`, `latest_desired_pipeline_version`, `latest_analyzed_input_revision`, `latest_analyzed_pipeline_version`, `latest_published_input_revision`, `latest_published_pipeline_version`, business state, extracted outputs, match result, warnings, `supersession_requested_at`, and timestamps. Each identity pair is updated atomically; both members are null or both are non-null. The desired identity may be null while required input is unavailable.  
- `design_processing_jobs`: durable execution records with immutable nullable `execution_kind`, `execution_input_revision`, and `execution_pipeline_version`, scheduling, normal attempts, readiness checks, locks, heartbeat, stage, `superseded_by_revision`, errors, and completion metadata. `execution_kind` is `analysis` or `publication`. Execution kind and identity remain null during readiness waiting and are assigned together, once, under lock when the next required work is known.  
- `design_processing_artifacts`: automation-owned artifact records containing board item, target column, artifact kind, full input revision, full pipeline version, deterministic filename, durable internal `storage_bucket` and `storage_object_key`, content SHA-256, size in bytes, nullable Monday asset ID, status, last error, and timestamps. Artifact statuses are `rendered`, `uploading`, `published`, `superseded`, `delete_pending`, `deleted`, and `failed`. Add a unique constraint on `(board_id, item_id, column_id, artifact_kind, input_revision, pipeline_version)`. Exactly one row represents each tuple; rendering and publication retries update that row rather than inserting additional attempts.  
- `monday_webhook_dispatches`: one child result per webhook event and consumer, as defined under Webhook Dispatch.

Job statuses are `scheduled`, `running`, `retry_wait`, `completed`, `failed`, and `cancelled`. Active statuses are `scheduled`, `running`, and `retry_wait`. Stages include `waiting_for_name`, `waiting_for_email`, `extracting`, `matching`, `rendering`, `writing_columns`, `uploading_ai_data`, and `uploading_match_report`.

Item business states are `waiting_for_name`, `waiting_for_email`, `scheduled`, `processing`, `analyzed`, `publishing`, `ready_for_review`, `ineligible`, and `failed`. `analyzed` means the desired identity has completed analysis but is not currently published. If both required inputs are absent, `waiting_for_name` takes precedence. A missing name or absence of a supported Email asset is a readiness condition, not a processing failure: reschedule the active job with backoff, increment `readiness_check_count`, and do not increment normal `attempt_count`. Malformed Email-column data, incomplete metadata for a referenced supported asset, or a Monday/API failure is a retryable processing error rather than ordinary readiness.

Readiness waiting uses capped exponential backoff with configuration for the initial interval, maximum interval, and an alert threshold. It does not expire into `failed` or `ineligible` while the item remains in Landing Zone; a relevant webhook wakes the active readiness job immediately by moving `scheduled_for` to the current time. Crossing the alert threshold emits an operational warning but does not change business state.

Use full identity pairs for all state decisions:

```text
needs_analysis = desired_identity != analyzed_identity
needs_publication = publication_allowed(item)
                    and desired_identity == analyzed_identity
                    and desired_identity != published_identity
ready_for_review = desired_identity == published_identity
                   and current_ai_data.identity == published_identity
                   and current_ai_data.status == published
                   and current_ai_data.monday_asset_id is not null
                   and current_match_report.identity == published_identity
                   and current_match_report.status == published
                   and current_match_report.monday_asset_id is not null
```

When the desired identity changes, immediately move the item out of `ready_for_review` to `scheduled` or `processing`, even though the prior published identity and its Monday report may remain attached during reprocessing. Keep the prior published identity for audit until the replacement report is successfully uploaded or adopted. PostgreSQL state is authoritative. If readiness must be visible directly in Monday, provision a dedicated worker-owned status column and add it to this ownership contract; do not reuse a human-owned field.

Create a PostgreSQL partial unique index allowing only one active design-processing job per board item. Do not reuse `Task.sync_status` or `AutoSyncJob`; those belong to the indexing workflow in auto\_sync\_worker.py.

Coalescing must never mutate `execution_kind`, `execution_input_revision`, or `execution_pipeline_version` on a running job. An event received while a job is running updates only the item row’s latest desired identity and sets `supersession_requested_at` when it differs from the running execution identity. At each worker checkpoint, lock the item and job rows and compare desired identity with execution identity. On mismatch, set the old job to `cancelled`, record the superseding revision, clear its lease, and insert the successor job in the same transaction after flushing the terminal status so the active-job unique index remains valid. Recovery of an expired lease applies the same comparison before retrying work.

**Input Revision**

Treat the parsed `files` array in `file_mkpbm883.value` as the authoritative Email-column membership list. Fetch the current item by item ID, including:

- item ID, item `name`, board ID, and group ID;
- `file_mkpbm883` column `value`; and
- item asset metadata: `id`, `name`, `file_extension`, `file_size`, `created_at`, and a download URL.

Parse each Email-column `assetId` as a decimal string and join it to item asset metadata by asset ID. Only joined assets whose filename or `file_extension` identifies a supported `.eml` or `.msg` file are design-processing inputs. Unsupported files in the Email column are ignored for both eligibility and revision calculation. At least one supported joined asset is required.

Do not calculate a revision if `file_mkpbm883.value` is malformed, a referenced supported asset cannot be joined, or its ID, filename, size, `created_at`, or download URL is missing. Treat that condition as not ready or as a retryable Monday/API failure; it must not be interpreted as an empty Email column or as a new valid revision.

For every supported joined asset, create this normalized revision record:

```json
{
  "assetId": "252912371",
  "filename": "B263905 - Yatton Cottage - Tapered Request.msg",
  "size": 123456,
  "createdAt": "2026-07-29T09:45:00Z"
}
```

Normalize `assetId` to its base-10 string representation, `size` to an integer byte count, and `createdAt` to UTC RFC 3339. Preserve the filename exactly as returned in asset metadata. Sort records by numeric `assetId`, with filename as a deterministic tie-breaker, then serialize the array as UTF-8 canonical JSON with sorted object keys and no insignificant whitespace. The design input revision is the lowercase hexadecimal SHA-256 digest of those serialized bytes. Monday array order and download URLs do not affect the revision.

Process multiple supported Email assets in that same deterministic order. After downloading each asset once to temporary storage, record its content SHA-256 separately for audit and corruption detection; content hashes do not replace or alter the metadata-based input revision.

Never include item `updated_at`, AI Data, Matched Projects, unsupported Email-column files, or unrelated item/update attachments in this revision. Otherwise, the worker’s own Monday writes would continually trigger new processing.

Set the `pipeline_version` from the full legacy manifest digest, extraction model identifier, thinking level, and output revision: `legacy-files-82d5612a9efce97660c3a3fef36a731d45597cb3096e58365865727ba719e28e:model-gemini-3.5-flash:thinking-medium:output-v4`. Persist the full value; shortened forms may be used only in logs and artifact filenames. Reprocessing is required when the Email revision changes or any component of `pipeline_version` changes. Gemini 3.x requests must use `thinking_level` rather than `thinking_budget` and must omit `temperature`, `top_p`, `top_k`, and `candidate_count`. Parameter extraction must additionally set `response_mime_type` to `application/json` and pass the Pydantic JSON schema through `response_json_schema`.

Throughout this plan, “identity” means the full input revision and pipeline version pair. Never compare or advance an analyzed or published revision without comparing or advancing its pipeline version in the same transaction.

**Webhook Dispatch**

Extend the authenticated receiver in monday\_webhooks.py to dispatch independently:

1. Authenticate and deduplicate the webhook as it does today.  
2. Fetch current item state once; do not trust an out-of-order payload’s group.  
3. Keep Landing Zone excluded from `auto_sync_jobs` in config.py.  
4. Dispatch Landing Zone readiness and input events to `design_processing_jobs`.  
5. Coalesce `create_item`, move-to-Landing, Email-column, and item `name` events for the same item. A name change must wake an item that was waiting for its human-entered name.  
6. Ignore AI Data and Matched Projects changes for design processing.

A create event may arrive before the name or Email asset is available. Queue it in the corresponding readiness stage, retry readiness without consuming normal failure attempts, and retain a periodic Landing Zone reconciliation command as a missed-webhook safety net.

`monday_webhook_dispatches` contains `id`, `webhook_event_id`, `consumer`, `status`, `outcome`, nullable `job_id`, `attempt_count`, `processing_started_at`, `completed_at`, `error`, and `result_json`, with `UNIQUE(webhook_event_id, consumer)`. Consumers are initially `auto_sync` and `design_processing`. Child statuses are `pending`, `processing`, `succeeded`, and `failed`; outcomes include `queued`, `coalesced`, `excluded`, `ignored`, and `disabled`.

Use explicit transaction boundaries. The receiver first claims the parent and inserts any missing consumer children in one short transaction. It then fetches current Monday item state once for that receiver attempt and reuses that snapshot for unresolved children. Each child is claimed and completed in its own transaction: for an actionable child, queue/coalesce the consumer job and mark the child `succeeded` atomically; for a non-actionable child, mark it `succeeded` with a null `job_id` and its exact `excluded`, `ignored`, or `disabled` outcome. Design processing uses `disabled` when mode is `off`. Finally, aggregate parent status from committed child rows in a separate transaction. One consumer failure therefore cannot roll back another consumer’s successful dispatch.

On webhook retry, do not rerun a `succeeded` child; retry only `pending`, retryable `failed`, or lease-expired `processing` children. Re-fetch current Monday item state once for each receiver attempt that has unresolved children. Aggregate the parent `MondayWebhookEvent.status` as `processing`, `completed`, `partial_failed`, or `failed`, while child rows retain the detailed outcomes such as `auto_sync=excluded` and `design_processing=queued`. Parent deduplication must allow `partial_failed` and retryable `failed` events to be reclaimed without repeating successful children.

**Worker Pipeline**

1. Claim with PostgreSQL `FOR UPDATE SKIP LOCKED`, using the lease and heartbeat pattern already implemented.  
2. Check the operational mode and run the validation gate in readiness mode as described below. It refreshes the item’s latest desired identity from current Monday state. If the item is not ready, reschedule it without consuming a normal attempt.  
3. Under row locks, select the next obligation. If desired differs from analyzed, assign immutable execution kind `analysis`. Otherwise, if publication is allowed and desired equals analyzed but differs from published, assign immutable execution kind `publication`. Assign execution identity from the refreshed desired identity in the same transaction, increment normal `attempt_count`, and set the item to `processing` or `publishing`. If neither obligation exists, complete the redundant job without external side effects.  

For an analysis execution:

4. Download each Email asset once to temporary storage.  
5. Run the pinned legacy email, attachment, PDF/image, parameter, and project-title extraction logic.  
6. Run the complete legacy matching algorithm against board `1825117125`.  
7. Before persisting extracted outputs, lock the item and job and reject a superseded execution. Persist parameters and matches with their full execution identity so retries do not repeat Gemini calls or reuse stale results.  
8. Generate the canonical AI Data CSV and matching PDF with deterministic pipeline-aware filenames and store both in durable private Supabase object storage using the existing retry-enabled storage gateway. Use configured bucket `design_processing_artifact_bucket` and object key `design-processing/<board_id>/<item_id>/<input_revision>/<pipeline_digest>/<filename>`. The bucket must be private and accessible only through the backend service role; never generate public artifact URLs. Persist bucket, key, byte size, content hash, and artifact status. If a `rendered` artifact already exists for the full identity, verify its stored hash and reuse it rather than re-rendering. After a final locked desired-versus-execution comparison, atomically advance the analyzed identity, mark the analysis job `completed`, set the item to `analyzed`, and queue a publication successor only when publication is currently allowed. Analysis executions never issue Monday mutations, uploads, or deletes in any operational mode.  

For a publication execution:

9. Load only outputs and rendered artifacts belonging to the execution identity. A publication retry or a later shadow-to-publishing transition must not repeat Gemini extraction or matching.  
10. Run the execution validation gate immediately before updating Date Received, Hour Received, and Zip Code.  
11. Run the gate again immediately before uploading or adopting AI Data.  
12. Run the gate again immediately before uploading or adopting Matched Projects, which remains the final Monday side effect.  
13. Lock the item and job for a final desired-versus-execution comparison. In one transaction, verify both current artifacts have Monday asset IDs, mark AI Data and the match report `published`, advance the published identity, mark the job `completed`, and set the item to `ready_for_review` only when the readiness predicate above is true. Mark prior automation-owned artifacts `delete_pending`; delete them from Monday after commit as best-effort cleanup.

The AI Data CSV should retain all 16 parameter rows for schema compatibility. The worker must not infer New Enquiry or Amendment: `Reason for Change` is always `Reviewer decision required` with source `Business Rule`, and only the reviewer may record that decision in the human-owned New Enq / Amend column. Since no project is selected, every other parameter contains email-derived values only. Any default inserted by a business rule should have source `Business Rule`; it must not be represented as a human CRM decision.

Implement one validation gate with two explicit modes. In readiness mode, `refresh_current_target()` re-fetches item state, board, group, item `name`, Email-column membership, and joined asset metadata from Monday, requires item state to be exactly `active`, recomputes the Email input revision, and updates the locked item’s latest desired identity; it does not require or assign execution identity. If the refreshed desired identity differs from published identity, it also moves the item out of `ready_for_review` in that transaction. In execution mode, `assert_current_execution_target()` performs the same Monday re-fetch and revision calculation, then locks the item and claimed job to verify active item state, board `1882196103`, Landing Zone group `group_mkpbd6vy`, a non-empty human name, at least one supported Email asset, current remote identity and stored desired identity both equal to immutable execution identity, the configured pipeline version equal to execution pipeline version, continued lease ownership, and that the current operational mode still permits the execution kind for this item. No other remote work may occur between a successful execution-mode gate and its guarded side effect.

If the item is no longer active or has left Landing Zone, mark the job `cancelled` and the item `ineligible`. If input or pipeline identity is superseded, cancel and schedule a successor atomically. If readiness input disappears while the active item remains in Landing Zone, cancel the stale execution and schedule a readiness job. Because Monday mutations cannot participate in the database transaction, a change occurring after a successful gate can still cause a partial stale side effect; the next gate must stop later writes, the stale execution must never publish the final report or become `ready_for_review`, and the successor must repair worker-owned outputs.

**Idempotent Monday Writes**

Extend monday\_client.py with typed helpers for design-owned column updates, file upload, file-column inspection, and automation-owned file replacement. Do not expose an arbitrary design-processing column mutation dictionary.

The only scalar columns writable by design processing are exactly `date_mkpb23av`, `hour_mkpbb3j1`, and `dropdown_mkpbafca`. `update_design_owned_columns()` must assert that the payload key set is a subset of those three IDs and reject every unknown ID before issuing GraphQL. Missing, `Not found`, unparseable, or unmapped Date Received, Hour Received, or Zip Code values are omitted from the mutation and persisted as warnings; they never send null or empty values and never clear an existing worker-owned value.

File helpers accept only `file_mkza7y37` for AI Data or `file_mm59rntf` for Matched Projects. They must reject the Email column and every unrelated file column. Design processing must never invoke item creation, item rename, group movement, or generic mutation helpers.

File uploads are at-least-once side effects. Use the full `(board_id, item_id, column_id, artifact_kind, input_revision, pipeline_version)` tuple as the persisted idempotency identity. Before retrying an uncertain upload, inspect the target column for the exact deterministic filename. Adoption is allowed only when the corresponding artifact row already exists for that full identity and is in `uploading`, `failed`, or `published`; require the Monday asset filename to match and, when Monday returns size metadata, require its size to match the rendered artifact. If zero or multiple candidates satisfy those checks, do not infer ownership: retry or fail for operator review. Persist the adopted Monday asset ID on that existing row.

On a new execution identity, upload or adopt the replacement first. Only after both replacement artifacts are recorded as published and published identity advances may prior artifacts be marked `delete_pending` and their recorded Monday asset IDs deleted. Cleanup retries independently and never clears an entire file column. Never create ownership or infer readiness from a filename alone.

**Implementation Layout**

- `services/design_processing_queue.py`: state upsert and job coalescing.  
- `services/design_processing_worker.py`: claim, lease, heartbeat, retry loop.  
- `services/design_processing_pipeline.py`: staged orchestration.  
- `services/design_processing_artifacts.py`: durable artifact storage, publication adoption, and cleanup.  
- `services/legacy_enquiry/`: framework-independent legacy extraction and matching code.  
- `services/match_report.py`: report DTO and PDF rendering.  
- `services/design_processing_reconciliation.py`: Landing Zone recovery scan.  
- `scripts/verify_legacy_enquiry_manifest.py`: offline legacy hash verification and fixture-generation prerequisite.  
- monday\_client.py: queries, mutations, and multipart uploads.  
- monday\_webhooks.py: dual-queue dispatch only.

Add validated settings for `design_processing_mode`, `design_processing_worker_enabled`, `design_processing_reconciliation_enabled`, `design_processing_board_id` (`1882196103`), `design_processing_landing_group_id` (`group_mkpbd6vy`), `design_processing_project_board_id` (`1825117125`), `design_processing_extraction_model` (`gemini-3.5-flash`), `design_processing_thinking_level` (`medium`), `design_processing_artifact_bucket`, `design_processing_allowlist_item_ids`, `design_processing_activation_timestamp`, and readiness backoff/alert intervals. Pin the extraction model and thinking level separately rather than inheriting the current general `gemini_model`. Validate thinking level as `minimal`, `low`, `medium`, or `high`; validate mode as `off`, `shadow`, `allowlist`, or `enabled`; normalize allowlist IDs to strings; and fail configuration when `allowlist` mode has an empty allowlist.

**Implementation Readiness And Phases**

This contract is implementation-ready and should be delivered in the dependency order below. Do not begin a later phase until the preceding exit gate passes. Environment prerequisites are Monday credentials that can read both configured boards and mutate only the worker-owned intake columns, a private Supabase bucket available to the backend service role, PostgreSQL migration access, Gemini credentials for the pinned model, and an approved historical email corpus for parity tests.

**Phase 1: Legacy Baseline And Configuration**

- Store the canonical legacy manifest in `services/legacy_enquiry/` and add the offline verification script. Verify the ignored local source before copying any logic.  
- Generate and commit versioned golden fixtures from the verified legacy bytes, including extraction outputs and ordered matching results. Fixture generation must not call the new implementation.  
- Add the design-processing settings above, add `reportlab` to the root `requirements.txt`, and validate the configured Monday board/group IDs and model.  
- Provision or confirm the private artifact bucket and test the exact object-key namespace without writing to Monday.

Exit gate: manifest and digest verification pass; fixtures are reproducible; settings reject invalid modes and empty allowlists; a private artifact round trip succeeds; existing backend tests remain green.

**Phase 2: Persistence And State Machine Foundation**

- Add Alembic migration `0010_design_processing_queue.py` after current head `0009_snapshot_lifecycle`, plus matching SQLAlchemy models for all four persistence concepts.  
- Add database checks that each desired, analyzed, published, and execution identity pair is either fully null or fully populated; constrain statuses, stages, execution kinds, consumers, and artifact kinds to the values in this plan.  
- Add the active-job partial unique index, artifact identity unique constraint, webhook-child uniqueness, claim indexes, foreign keys, and timestamp fields.  
- Implement pure transition helpers for readiness, analysis, publication, completion, cancellation, retry, and supersession without Monday calls.

Exit gate: upgrade from `0009` and downgrade back to `0009` succeed on a disposable PostgreSQL database; ORM metadata matches the migration; invalid state combinations fail; transition and concurrent active-job tests pass.

**Phase 3: Monday Read Gateway And Input Revision**

- Add typed Monday queries for current intake item state, Email-column membership, joined asset metadata, file-column inspection, and read-only project-board matching inputs.  
- Implement strict parsing and canonical Email revision calculation exactly as specified, including deterministic multi-email ordering and separate downloaded-content hashes.  
- Implement `refresh_current_target()` and the read-only parts of `assert_current_execution_target()`.  
- Keep all mutation, upload, delete, item-create, rename, and move capabilities unavailable to design processing in this phase.

Exit gate: fixtures cover malformed membership, missing metadata, unsupported files, ordering changes, timestamp normalization, API failures, name/Email arrival order, and pipeline-only changes; revision tests are deterministic; no Monday mutation is reachable.

**Phase 4: Queue, Webhook Dispatch, And Reconciliation**

- Implement `design_processing_queue.py`, immutable execution assignment, desired/analyzed/published scheduling predicates, readiness backoff, supersession, and one-active-job enforcement.  
- Refactor the current single-consumer webhook route to the parent/child transaction boundaries above while preserving authentication and idempotency.  
- Dispatch create, Landing Zone move, Email, and name events; record exact non-actionable outcomes; keep Landing Zone excluded from auto-sync.  
- Implement item-scoped and activation-timestamp-bounded reconciliation. Mode or allowlist changes must trigger or be followed by reconciliation so publish-only work is discovered.

Exit gate: dual-dispatch failure isolation, retry of only unresolved children, event coalescing, stale child recovery, readiness wake-up, supersession, and reconciliation idempotency tests pass; exactly one active job exists after every tested race.

**Phase 5: Analysis Worker And Shadow Mode**

- Port only the pinned legacy extraction and matching scope into `services/legacy_enquiry/`, preserving field semantics, attachment prompts, batching, ranking, and output ordering while using the structured parameter response contract above.  
- Implement claim, lease, heartbeat, readiness retry, normal retry, checkpoint, and expired-lease recovery using the existing auto-sync worker pattern.  
- Implement analysis execution through persisted parameters, candidates, deterministic CSV/PDF rendering, private artifact storage, and analyzed-identity advancement.  
- Enforce that analysis code has no Monday write dependency and that `shadow` mode can create only internal database/storage effects.

Exit gate: golden parity passes for extraction and candidate membership/order/references/percentages; retries resume persisted stages without repeating successful Gemini calls; superseded analysis cannot advance analyzed identity; a shadow-mode mutation-spy test observes zero Monday writes.

**Phase 6: Publication And Artifact Lifecycle**

- Add the fail-closed typed Monday helpers for the exact three scalar and two file columns.  
- Implement publication-only execution, validation before every side effect, scalar writes, AI Data upload/adoption, final report upload/adoption, and atomic published-identity advancement.  
- Implement replacement-first cleanup, `delete_pending` retries, and recovery after uncertain upload outcomes.  
- Ensure partial writes never advance published identity or restore `ready_for_review`; a later publication retry must repair worker-owned outputs from persisted artifacts without repeating analysis.

Exit gate: mutation allow-list tests, invalid-value preservation, uncertain-upload adoption, ambiguous-adoption rejection, partial-write recovery, replacement-before-delete, cleanup-failure isolation, and full readiness-predicate tests pass.

**Phase 7: Operational Hardening**

- Complete `off`, `shadow`, `allowlist`, and `enabled` enforcement at dispatch, claim, and every execution gate.  
- Add structured logs and metrics for queue depth, readiness age/checks, attempts, leases, supersessions, analyzed-not-published items, publication latency, artifact cleanup, and webhook child outcomes.  
- Add operator commands for item-scoped enqueue/reconcile, failed-job retry, artifact cleanup retry, and mode-transition reconciliation; commands must be idempotent and auditable.  
- Run concurrency and fault-injection tests for revision changes, mode changes, lease expiry, process termination, Monday/API failures, storage failures, and database retries.

Exit gate: every documented race and mode transition has an automated test; stale work cannot publish or become ready; operational commands are idempotent; no test mutates human-owned fields or group membership.

**Phase 8: Staged Rollout**

- Deploy database/configuration and worker code with mode `off`; verify migrations, permissions, dashboards, and rollback procedure.  
- Move to `shadow` for the activation-bounded corpus and compare parameter equality, except for the documented neutral `Reason for Change` policy override, candidate ordering, generated artifacts, and p50/p95 timings against the fixed legacy fixtures.  
- Move to `allowlist` for selected real item IDs, verify the complete reviewer workflow and cleanup behavior, then expand gradually.  
- Move to `enabled` only after the verification criteria below pass; retain the activation boundary until an explicit backfill is approved.

Exit gate: shadow parity and performance are accepted, allowlisted items publish idempotently with no forbidden mutations, alerts and recovery procedures are exercised, and production enablement has an explicit rollback decision.

**Verification And Rollout**

Golden tests should compare legacy and new extraction parsing, candidate membership, ordering, references, and percentages. CRM project-detail extraction, latest-revision selection, and Email-versus-CRM merging are not parity targets for this release. Mutation tests must prove Accounts, New Enq / Amend, TP Ref, item `name`, Email, Project Name, group membership, and every unrelated column remain unchanged under success, retry, and reprocessing. Test that unknown scalar/file column IDs, item creation, rename, and move attempts fail before a Monday mutation is sent.

Also test that matching never calls a project-detail endpoint, and that the PDF contains extracted Company, every candidate project reference, every match percentage, and reviewer instructions for all three human-owned fields. Cover Email-before-name, name-before-Email, readiness checks not consuming normal attempts, revision B arriving while revision A runs, supersession immediately before output persistence and completion, lease recovery with a superseded revision, and only one active job after each transition.

Test dual dispatch where auto-sync succeeds and design processing fails, then prove retry executes only the unresolved design child. Test child uniqueness, stale child leases, aggregate parent statuses, Email-only revision stability, pipeline-only reprocessing, deterministic CSV/PDF names, same-name artifact adoption, and rejection of an artifact whose full identity differs. Test each pre-write gate, disappearance of readiness input, leaving Landing Zone, partial upload recovery, invalid extracted values preserving existing worker columns, no-match reports, search failures, stale jobs, PDF text contents, and the transition where a reviewer updates Accounts, New Enq / Amend, and TP Ref before moving the item into an active group.

Also test that revision A is no longer `ready_for_review` as soon as desired revision B is observed, even while A’s report remains attached; B becomes ready only after its full identity is published. Cover replacement upload failure, coexistence of old and new assets before cleanup, cleanup failure after successful publication, and prove that attachment presence and shortened filename hashes never determine readiness.

Configure one explicit `design_processing_mode`:

- `off`: do not create or claim design-processing work. Webhook dispatch records a disabled outcome.  
- `shadow`: analyze all in-scope eligible items and persist outputs and rendered artifacts internally, but issue no Monday mutations, uploads, or deletes.  
- `allowlist`: analyze all in-scope eligible items and publish only item IDs in the configured allowlist.  
- `enabled`: analyze and publish all in-scope eligible items.

Mode and allowlist changes do not alter processing identity. Reconciliation after a mode or allowlist change evaluates `needs_analysis` and `needs_publication`: an already analyzed current identity must receive a publication-only job when publication becomes allowed, without repeating Gemini or matching. If publication becomes disallowed during a publication job, the next execution gate cancels it without advancing published identity; any earlier partial Monday side effects remain repairable by a later publication retry. `off` also prevents new claims and causes a claimed job to stop at its next gate.

Deploy initially in `shadow`, compare results and p50/p95 timings against historical emails, then move to `allowlist` for selected item IDs before `enabled`. Test `shadow` to `allowlist` and `enabled` transitions, allowlist addition and removal, restart in every mode, mode changes during analysis and publication, pipeline-only changes, and revision B superseding analyzed-but-unpublished revision A. Do not automatically backfill every existing Landing Zone item; constrain reconciliation with an activation timestamp or explicit item-scoped backfill before enabling it broadly.

To meet or beat the legacy app:

\- Do not run RAG embedding or the existing \`run\_sync\_pipeline()\` inside design processing.  
\- Download the email once and use temporary files for attachments.  
\- Preserve legacy Gemini batching while bounding per-job attachment concurrency.  
\- Persist each completed stage so retries do not repeat successful Gemini extraction.  
\- Cache Monday board-column metadata briefly.  
\- Run one memory-heavy job per worker initially, then increase concurrency from measured memory and API limits.  
\- Compare parameter equality, except for the documented neutral `Reason for Change` policy override, candidate ordering, Monday payloads, and p50/p95 processing time against a fixed historical test corpus in shadow mode before enabling writes.

