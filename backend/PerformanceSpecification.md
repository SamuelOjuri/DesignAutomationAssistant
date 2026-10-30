# Project Performance Specification Details

The additional PDF is published to **Project Holding Page** (`1882196103`),
**AI Data** (`file_mkza7y37`). It contains selectable text with the exact 18 CAD
labels in the requested order. Dropdowns contain exact permitted strings; U-values
contain two decimal places (decimal HALF_UP rounding). Missing or unresolved values
display **Not Provided**. **Incoming Information** is always blank. Bauder Roof Num
is optional and extracted only when explicitly stated in email content.

The specification uses the configured design extraction model in a separate,
schema-constrained LLM translation call. There is no deterministic material-alias
translator. Backend checks reject unsupported enum strings, invented text/IDs,
missing-value inventions and changed numeric facts. Ambiguous material translations
become null/Not Provided, with warnings retained in the artifact's translation JSON.

## Scope and existing outputs

Only groups in `AUTO_SYNC_ACTIVE_GROUP_IDS` are eligible. The configured Landing Zone
and Completed Folder are subtracted even if also listed as active. Archived/deleted
items are excluded. Current scope is read before translation, after translation,
before/after publication and before cleanup. Moving to an excluded group cancels
queued work and fences running work; existing attachments remain. In-flight external
requests cannot be recalled, so a change during upload may briefly leave a tracked,
superseded PDF until the next eligible refresh replaces it.

The existing design-processing workflow, its Landing Zone admission, AI Data CSV,
preview PDF and matched-project PDF are preserved. The new publisher uses its own
tables and never deletes files outside its own artifact ledger. Its own PDFs are
excluded from source ingestion to avoid treating generated data as new evidence.

## Source resolution

Current readable Monday values take precedence over extracted evidence. Initially
empty fields fall back to email evidence; an observed clear of a previously populated
CRM source remains cleared on subsequent checks. Failed/incomplete reads preserve
the last published PDF and retry, exposing `last_error` in the refresh record.

The holding item's confirmed TP Ref selects the project. Multiple linked projects
or conflicting technical values fail the refresh rather than merge roofs. Drawing
selection prefers an exact requested reference; a unique active replacement in the
same numbered drawing series can supply a newer letter revision. An unrelated roof
or ambiguous series requires review. With no requested reference, a sole active
drawing can be selected. Candidate project-match scores do not select a project.

Holding Date Received and Accounts supply current enquiry date and Company. The
linked project's `text3__1` supplies its current title. These mappings assume Accounts
represents the company that should appear on the CAD drawing. Legacy drawing mirror
IDs are supported for Company, Contact, Surveyor, U-values, product, deck and revision.
Exact matching column titles cover other specification fields. Unconfigured missing
columns allow email fallback; explicitly configured missing columns fail the read.
Mirror reads retain typed values and linked item identities rather than splitting
comma-separated display strings.

For different board schemas, configure explicit IDs using JSON, for example:

```dotenv
PERFORMANCE_SPECIFICATION_COLUMN_MAP={"holding":{"Scale":"text_actual_scale"},"project":{"Membrane":"text_actual_membrane"},"drawing":{"VCL":"mirror_actual_vcl"}}
```

Keys are exact PDF field labels; valid scopes are `holding`, `project`, `drawing`.
Incoming Information is ignored even if mapped. Confirm live schema/token access
against the deployed `MONDAY_API_VERSION` before enabling broad publication.

## Deployment

1. Stop/drain API and worker versions that will be replaced. Apply migration
   `0017_performance_specification` with
   `python -m alembic -c backend/alembic.ini upgrade head`.
2. Deploy matching API, ingestion/metadata/design workers and the new publisher.
   Initially configure `PERFORMANCE_SPECIFICATION_ENABLED=true` and
   `DESIGN_PROCESSING_MODE=shadow`. Keep existing extraction and reconciliation
   services running. The new publisher runs as another service:
   `python -m backend.app.services.performance_specification`.
3. Extraction identity changes from `output-v7` to `output-v8` to collect raw
   specification evidence and the optional roof number. Existing canonical CSV rows
   remain unchanged. Run the normal design reconciliation sweep to re-extract eligible
   historical projects. Old extraction records without `performanceParameters` wait
   for this refresh instead of silently claiming to contain the new roof field.
4. Review shadow artifacts and translation warnings in the private artifact bucket
   and `performance_specification_artifacts`. Then use the existing design-processing
   `allowlist` mode and `DESIGN_PROCESSING_ALLOWLIST_ITEM_IDS` to validate a few items
   before changing to `enabled`. These existing mode settings govern both workflows.
5. Subscribe holding field/link/lifecycle changes and linked project/drawing changes
   to the existing authenticated Monday webhook endpoint. Include project subitem
   creation, name/column changes, archive/delete and restoration events. Mirror-source
   boards need corresponding event coverage. The dependency index is established on
   the first successful specification refresh; periodic checks repair missed events.

No credentials are stored in queue records. The publisher uses
`MONDAY_INGESTION_ACCESS_TOKEN`, configured Gemini credentials, and private
`DESIGN_PROCESSING_ARTIFACT_BUCKET` storage. It requires no CAD integration or public
artifact URLs. Enabling this setting does not provision webhooks or start a process.

The publisher polls its database every five seconds. Requests coalesce using the
existing debounce setting; idle completed records are rechecked every
`PERFORMANCE_SPECIFICATION_RECHECK_SECONDS` (default 3600). Existing active projects
with extraction records are discovered automatically; it does not ingest attachments
or call an LLM when no work is due. To target an existing item:

```powershell
venv/Scripts/python.exe -m backend.app.services.performance_specification --once --item-id 1234567890
```

## Recovery and CAD discovery

Source values, provenance, extraction identity and model/prompt/template version form
the specification revision. Polling timestamps and unrelated CRM edits do not change
it. Unchanged inputs reuse the persisted translation and verified PDF bytes.
Generation/lease checks fence superseded workers. Temporary source/model/storage
failures retry with capped backoff and preserve the last successful document.

Files use `Project_Performance_Specification_Details_<item_id>_<revision>.pdf`.
Configure CAD discovery to match this prefix/title, since AI Data also contains the
older preview PDF. Publication appends the verified replacement, records its asset
identity, then removes prior ledger-owned specifications. Lost upload responses are
recovered by deterministic name/size adoption; duplicate matches stop for review.
Publication is not an atomic Monday transaction. Reconciliation rechecks live scope
and inputs, and superseded uploads remain tracked for replacement cleanup.

The three new tables are independent of snapshot retention, like the existing
design-processing artifacts; completed projects retain their published documents.
Disable `PERFORMANCE_SPECIFICATION_ENABLED` to stop new processing. Do not downgrade
the database until all matching application/worker processes have stopped.

## Verification

```powershell
venv/Scripts/python.exe -m pytest backend/tests/test_performance_specification.py -q
```

Coverage includes LLM schema/semantic validation, optional values, exact PDF text
ordering, active-only scope, CRM clears, typed mirrors, revision ambiguity, cached
translation, failed reads, stale generations, expired leases, source changes during
translation/upload, lost upload responses, replacement safety and shadow mode.
PDF text validation uses Poppler `pdftotext` or optional `pypdf`.
