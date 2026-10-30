# Project Performance Specification Details

The additional PDF is published to **Project Holding Page** (`1882196103`),
**AI Data** (`file_mkza7y37`). It contains selectable text with the exact 18 CAD
labels in the requested order. Dropdowns contain exact permitted strings; U-values
contain two decimal places (decimal HALF_UP rounding). Genuinely absent values
display **Not Provided**. Known values with unresolved translations or conflicting
evidence hold publication for review and retain the last successful PDF.
**Incoming Information** is always blank. Bauder Roof Num
is optional and extracted only when explicitly stated in email content.

The specification uses the configured design extraction model in a separate,
schema-constrained LLM translation call. There is no deterministic material-alias
translator. Backend checks reject unsupported enum strings, invented text/IDs,
missing-value inventions and changed numeric facts. The model receives shared field
definitions, the source texts, alternative roof/drawing candidates and CRM provenance.
One targeted recovery pass revisits known unresolved values. A remaining null for a
populated field fails publication; the raw value, translation and warnings remain in
`performance_specifications.review_json` with status `unmapped`. A brand such as
BauderROCK does not establish a particular CAD product variant without evidence.

Gemini requests use the shared `gemini_response_schema` helper, which omits array
`minItems`/`maxItems` constraints from the wire schema to avoid provider decoding
complexity errors. Local Pydantic validation still enforces all evidence, assessed-field
and warning limits; enum choices and field definitions remain in the API schema.
Synthetic live checks reproduced HTTP 400 with the original evidence schema and
succeeded after removing its array bounds. The same helper is used for CAD translation.

If Gemini still returns HTTP 400 for a structured extraction/translation request,
the worker makes one compatibility attempt in JSON mode with the schema included
in the task. Every returned value still passes the original Pydantic schema and
semantic/evidence checks. Malformed JSON, invented values and unresolved known
fields cannot pass publication through this path. The compatibility attempt is
not used for PDF/image requests, authentication, throttling or server errors.

The design worker prints `design_extraction_runtime` at startup with its deployed
commit, worker ID, PID, pipeline revision, source capabilities, Python/SDK/Pydantic
versions, model and wire-schema fingerprint. Each failed job also logs its worker
ID, PID, commit and pipeline revision, so failures from different workers or deploys
can be distinguished. API failures
retain stage, status, mode, input size and runtime information across the extraction
process boundary; failed attachment manifests retain the same safe metadata. These
diagnostics do not contain source text, signed URLs, credentials or provider bodies.

After deploying matching workers, run this in the Render design-worker shell:

```bash
python -m backend.scripts.check_design_processing_gemini --live
```

This checks the actual service environment using synthetic text only, without
reading email, attachments or CRM records. Expected output includes `array_bounds: 0`
and `status: ok`, `response_validated: true`. Add `--json-mode` to test the recovery
transport directly. Without `--live`, it prints runtime metadata without an API call.
If the test succeeds but a real job fails, its stage and prompt/input sizes identify
the next request to investigate rather than attributing every HTTP 400 to the schema.

To check the deployed source readers independently of Gemini, run:

```bash
python -m backend.scripts.check_design_processing_sources
```

This uses only synthetic DWG, ZIP and XLSX bytes with no database or network calls.
Expected output includes `status: ok`, `pipeline_revision: output-v11`, and all
three checks with `passed: true`. It verifies that DWG is skipped, ZIP members are
read and long spreadsheet cells retain their final technical values. A `.dwg`
failure reported as `failed:ValueError` is inconsistent with the current DWG skip
path. Check the runtime fields on that failing job and ensure all design-worker
services run the matching release; a nearby startup message alone does not identify
which process produced an earlier/in-flight failure.

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

Current populated, readable Monday values take precedence over verified email and
attachment evidence. Empty CRM fields, including deliberately cleared fields, fall
back to that evidence on every refresh. Empty reads remain in provenance; they do
not erase an available source fact. Failed/incomplete reads preserve
the last published PDF and retry, exposing `last_error` in the refresh record.

`specification_semantics.py` supplies the same business meanings to extraction and
CAD translation. Drawing Reference means the **TaperedPlus-issued** reference; a
manufacturer's drawing/reference is not automatically a TP reference. Revision is
the **entire suffix after the underscore** (for example `26.01 - B`). It is derived
after the final reference is selected. A compatible letter-only CRM revision is
retained as provenance; an incompatible letter/suffix requires review. Company and
Contact describe the direct requester. Requested U-values remain distinct from
achieved calculations and unselected template options.

Each extraction retains a source manifest and field candidates with source identity,
content hash, page/table or worksheet/cell location, supporting excerpt, issuer,
roof/drawing scope and selection rationale. Excerpts are checked against source
text. Different roof values are not flattened into one specification: a confirmed
matching roof/drawing selects its candidates; otherwise conflicts require review.
A value common to all represented roofs can be used without selecting one roof.
Actual outer email sent timestamps establish request chronology, independently of
Monday asset IDs/upload order. The most recent outer request supplies the fallback
Date Received, excluding earlier dates inside the forwarded chain.
Forwarded-message facts retain their verified message-header dates and requester
roles, so an earlier sender cannot replace the direct requester. Multiple email
files with unreadable sent dates require chronology review.

The reader supports plain text and HTML-only email bodies, PDF/image attachments,
DOCX, text/CSV/TSV/JSON/XML/HTML and XLS/XLSX/**XLSM**. Workbook readers use cached
values and never execute macros or formulas. The design reader excludes hidden
worksheets and identifies visible selected values by worksheet/cell, so lookup
catalogues do not become selected specifications. Uncached formulas, extraction
limits, unreadable or unsupported sources hold extraction for retry/review rather
than imply absent fields. Failed per-source manifests are retained in the design
job's `result_json.sourceExtractionFailure`. PDFs are read individually to preserve
attachment identity. The extraction model also makes one targeted second pass for
missing/unverified fields before accepting the extraction.

Design extraction retains long spreadsheet cells in full, splitting them across
1,000-character chunks with repeated worksheet/cell references and continuation
labels. The search ingestion reader keeps its existing 400-character cell excerpt
limit. The design reader still enforces the workbook file, decompression, row,
column, cell and document limits, plus the overall 500,000-character context limit.
Uncached formulas and actual extraction limits still block incomplete evidence;
their safe reason is now retained in the job's error and source manifest.

DWG drawings are intentionally skipped, including inside ZIP archives. Their
identity/hash and `skipped:dwg` status remain in the source manifest; they are not
sent to the model and cannot supply field evidence. The remaining readable sources
can proceed to extraction and publication.

ZIP attachments and configured source ZIPs are expanded in memory. Each member
retains its own source ID, archive-qualified filename, content hash and parent
source ID. Nested archives share a budget of 100 MiB expanded bytes, 200 entries
(including directories) and three archive levels. Every archive and member must
also fit the existing 20 MiB source limit, and extracted text remains subject to the
500,000-character context limit. No archive paths are extracted to disk. Containers
are recorded as `expanded:archive`; only readable members can supply evidence.
Corrupt, encrypted, empty or oversized archives and failed/unsupported members
(other than the intentional DWG/image skips) hold extraction for retry/review.
Application-owned failure messages survive the process boundary in worker logs,
`last_error` and `result_json.sourceExtractionFailure` without exposing arbitrary
parser/provider exception text.

Deploy matching API, design/reconciliation and specification workers to recognize
the new statuses. Pipeline revision `output-v11` makes the normal reconciliation
flow re-extract eligible items, including jobs previously blocked by DWG or ZIP
attachments or long spreadsheet cells. No environment-variable or database migration change is needed for
this update. For a job still marked failed after deployment, use the operator command:

```bash
python -m backend.app.services.design_processing_operations retry-failed-job --job-id <failed-job-uuid>
```

Animated images (including GIF, APNG and animated WebP) are intentionally excluded.
They are recorded with their original identity/hash and `skipped:animated_image` in
the source manifest, are not sent to the model, and do not block extraction or PDF
publication. Static images continue to be processed. Skipped images cannot supply
field evidence; genuine failures in other sources still require retry/review. Deploy
the matching extraction and specification workers so both recognize this skip status.

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
comma-separated display strings. People fields/mirrors resolve person/team IDs to
names through Monday's Users/Teams API and retain both identities and names. The
token needs the corresponding read permissions; unavailable identities fail the read.

For different board schemas, configure explicit IDs using JSON, for example:

```dotenv
PERFORMANCE_SPECIFICATION_COLUMN_MAP={"holding":{"Scale":"text_actual_scale"},"project":{"Membrane":"text_actual_membrane"},"drawing":{"VCL":"mirror_actual_vcl"}}
```

Keys are exact PDF field labels; valid scopes are `holding`, `project`, `drawing`.
Incoming Information is ignored even if mapped. Confirm live schema/token access
against the deployed `MONDAY_API_VERSION` before enabling broad publication.

Embedded email attachments are included automatically. To include additional source
files on the holding item, configure their actual file-column IDs on **all** matching
API/design/reconciliation/publisher services:

```dotenv
DESIGN_PROCESSING_SOURCE_FILE_COLUMN_IDS=["file_actual_source_documents"]
```

The default is `[]`. These files participate in the extraction revision, so adding,
removing or replacing one triggers fresh extraction. Subscribe those columns to the
existing holding-board column-change webhook. Email/AI Data/matched-output column
IDs are rejected here; generated export filenames are also excluded. This setting
does not automatically include attachments on linked boards.

## Deployment

1. Stop/drain API and worker versions that will be replaced. Apply migration
   through `0018_specification_evidence` with
   `python -m alembic -c backend/alembic.ini upgrade head`.
2. Deploy matching API, ingestion/metadata/design workers and the new publisher.
   Initially configure `PERFORMANCE_SPECIFICATION_ENABLED=true` and
   `DESIGN_PROCESSING_MODE=shadow`. Keep existing extraction and reconciliation
   services running. The new publisher runs as another service:
   `python -m backend.app.services.performance_specification`.
3. Extraction identity is now `output-v11` (`output-v9` introduced source evidence;
   `output-v10` added DWG skips and bounded ZIP extraction; `output-v11` preserves
   long spreadsheet cells). The CAD contract remains
   `performance-specification-v2`. Existing canonical CSV rows
   remain unchanged. Run the normal design reconciliation sweep to re-extract eligible
   historical projects. Old extraction records without the versioned source/evidence
   contract wait for this refresh. Changing only the publisher flag does not refresh
   historical extraction evidence.
4. Review shadow artifacts and translation warnings in the private artifact bucket
   and `performance_specification_artifacts`, plus `performance_specifications.review_json`.
   Review statuses distinguish `conflict`, `unmapped` and `source_failure`; a successful
   refresh clears them. Then use the existing design-processing
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
Each v2 artifact stores its immutable `source_json`, including field provenance and
source texts; later refreshes cannot overwrite that revision's evidence. The current
record's `source_json` remains the last successful source snapshot. Pre-v2 artifact
snapshots are null because their historic inputs cannot be reconstructed reliably.
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
venv/Scripts/python.exe -m pytest backend/tests/test_performance_specification.py backend/tests/test_specification_sources.py backend/tests/test_source_archives.py -q
```

Coverage includes LLM schema/semantic validation, optional values, exact PDF text
ordering, active-only scope, CRM clears, typed mirrors, revision ambiguity, cached
translation, failed reads, stale generations, expired leases, source changes during
translation/upload, lost upload responses, replacement safety and shadow mode.
PDF text validation uses Poppler `pdftotext` or optional `pypdf`.
Regression coverage also includes known-product translation review, recovery,
full TP revision suffixes, conflicting roof decks, candidate recovery from a missing
scalar, HTML-only mail, date ordering, XLSM/hidden catalogues, uncached formulas,
source failures, People identity reads and immutable artifact snapshots.
