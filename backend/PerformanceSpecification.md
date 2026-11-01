# Project Performance Specification Details

The additional PDF is published to **Project Holding Page** (`1882196103`),
**AI Data** (`file_mkza7y37`). It contains selectable text with the exact 18 CAD
labels in the requested order. Resolved dropdowns contain exact permitted strings;
resolved U-values contain two decimal places (decimal HALF_UP rounding). Genuinely
absent values display **Not Provided**. Verified conflicting values and unresolved
translations publish **Review required** details beside the affected field, including
the raw product or competing values and roof scopes. These fields keep null CAD
values in `translation_json.values`; their PDF text is stored in
`translation_json.fieldReviews`. Unverified evidence, incomplete source reads and
invalid model responses still retain the last successful PDF and retry.
**Incoming Information** is always blank. Bauder Roof Num
is optional and extracted only when explicitly stated in email content.

The specification uses the configured design extraction model in a separate,
schema-constrained LLM translation call. There is no deterministic material-alias
translator. Backend checks reject unsupported enum strings, invented text/IDs,
missing-value inventions and changed numeric facts. The model receives shared field
definitions, the source texts, alternative roof/drawing candidates and CRM provenance.
One targeted recovery pass revisits known unresolved values. A remaining null for a
populated field publishes its raw value and translation warning in a field review
note. The raw value, translation, warnings and field notes remain in
`performance_specifications.review_json` with status `published_with_review`
(`rendered_with_review` in shadow mode). A brand such as BauderROCK does not establish
a particular CAD product variant without evidence; the PDF explains that unresolved
mapping. The model's response schema remains strict; application code adds the review
notes after validation. Resolving the fields clears the notes on the next refresh.

Gemini requests use the shared `gemini_response_schema` helper, which omits array
`minItems`/`maxItems` constraints from the wire schema to avoid provider decoding
complexity errors. Local Pydantic validation still enforces all evidence, assessed-field
and warning limits; enum choices and field definitions remain in the API schema.
Synthetic live checks reproduced HTTP 400 with the original evidence schema and
succeeded after removing its array bounds. The same helper is used for CAD translation.

If Gemini still returns HTTP 400 for a structured extraction/translation request,
the worker makes one compatibility attempt in JSON mode with the schema included
in the task. Every returned value still passes the original Pydantic schema and
semantic/evidence checks. Malformed JSON and invented values cannot pass publication
through this path; unresolved known fields publish review details. The compatibility attempt is
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

This uses only synthetic DWG, ZIP, XLSX and encrypted-PDF bytes with no database or
network calls. Expected output includes `status: ok`, `pipeline_revision: output-v17`,
and all four checks with `passed: true`. It verifies that DWG and encrypted PDFs
are skipped, ZIP members are read and long spreadsheet cells retain their final
technical values. A `.dwg`
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

Contact defaults to the selected drawing's `mirror_11__1` column (or its configured
drawing mapping), with populated holding-item Contact values taking precedence.
Missing or empty drawing/holding values fall back to verified email evidence.
Project columns titled **Contact** are not automatically selected: they can roll
up contacts from several drawings and return unsupported nested mirrors. A project
Contact source requires an explicit `PERFORMANCE_SPECIFICATION_COLUMN_MAP` mapping,
for example `{"project":{"Contact":"request_contact"}}`, to a suitable requester
column. Explicit mappings still undergo normal source validation and remain below
populated drawing and holding values in precedence. Unreadable or conflicting
selected Contact values still stop publication and retain the previous PDF.
This selection fix requires redeploying the specification worker, with no database
migration or extraction-version bump. Its next refresh uses the corrected source.

For an explicitly reviewed item correction, a source manifest can also retain a
`reviewed_crm` snapshot alongside the original email evidence. Store its CRM IDs,
column values, content hash and `reviewedAt` separately from `observedAt`: a CRM
review time is not a client request date. Such evidence remains subject to the
same excerpt, scope and conflict checks. A differing requester in that snapshot
can publish both Contact alternatives for review, with their source names, while
leaving the CAD Contact value null. This is a dated review snapshot, not an implicit
project link or a live CRM override. Preserve the original extraction in the item
correction audit; subsequent input/pipeline changes require fresh evidence.

Each extraction retains a source manifest and field candidates with source identity,
content hash, page/table or worksheet/cell location, supporting excerpt, issuer,
roof/drawing scope and selection rationale. Excerpts are checked against source
text. Different roof values are not flattened into one specification: a confirmed
matching roof/drawing selects its candidates; otherwise verified conflicts publish
their alternatives in the field's review note.
A value common to all represented roofs can be used without selecting one roof.
Excerpt verification ignores paired Markdown bold/code markers added by PDF text
extraction while retaining contiguous text, punctuation and token boundaries.
It does not join excerpts across ellipses. A verified recovery candidate repairs a
rejected excerpt only for the same field, value, source, scope, issuer, request role
and date. Rejected candidates remain in provenance as `rejectedCandidates`, with
`repaired` or `unverified` status; different unverified claims still require review.
An attachment (including an archive member) may cite its own parent email's exact
outer Date header when its inherited timestamp matches that parent's timestamp.
Inline images can instead use the literal Date/Sent header of their unique containing
message, verified through Content-ID and message boundaries. Reused or unmatched
Content-IDs cannot establish a drawing's date. Repeated signatures can identify the
selected client when every occurrence belongs to that same sender. An unrelated
message or a date mentioned only in prose cannot establish attachment chronology.
Undated documents retain their carrying email time as `sourceObservedAt`, independently
of the candidate's fact time; a later email carrying an older image cannot silently
supersede a dated fact.

Company, Contact and Date Received share one selected client-request context: the
latest external sender addressing a configured TaperedPlus domain. A later internal
follow-up does not replace that client or enquiry date. When only an internal forward
is supplied, a verified client header within it may establish the context. Simultaneous
different client senders or unreadable candidate dates require review. Internal-only
correspondence supplies no client date. Unknown legacy headers retain existing checks.
Current verified CRM precedence is unchanged.

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
the new statuses. Pipeline revision `output-v12` makes the normal reconciliation
flow re-extract eligible items, including jobs previously blocked by encrypted
PDFs, DWG or ZIP attachments or long spreadsheet cells. No environment-variable or database migration change is needed for
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

Encrypted PDFs are also intentionally excluded, including email attachments,
configured supplementary sources and PDFs inside nested ZIP archives. `pypdf`
checks encryption before any PDF bytes are sent to Gemini, including owner-only
protection with an empty opening password. Skipped PDFs retain their filename,
source identity, content hash and `skipped:encrypted_pdf` status in the manifest,
with no extracted text. They cannot supply field evidence and do not block the
remaining readable sources or publication. Malformed PDFs still require
retry/review. Pipeline revision `output-v12` enables reprocessing of affected
eligible items. Install the updated requirements and deploy matching API,
design/reconciliation and specification workers; no database migration is needed.

The holding item's confirmed TP Ref selects the project. Multiple linked projects
still fail the refresh. Verified conflicting technical values publish their roof
scopes and alternatives for review. Drawing
selection prefers an exact requested reference; a unique active replacement in the
same numbered drawing series can supply a newer letter revision. An unrelated roof
or ambiguous series requires review. With no requested reference, a sole active
drawing can be selected. Candidate project-match scores do not select a project.

A verified baseline TP/AX drawing reference may identify the existing drawing being
amended when no requested reference is present. It passes the same source, excerpt,
value, issuer and date checks as requested evidence. Distinct baseline references
remain blocking even if their dates differ. This lookup does not promote historical
materials, calculated U-values or revision fields into requested specifications;
the selected drawing's current CRM fields retain the established precedence rules.
The baseline citations are retained under `source_json.drawingSelection`.

When a linked project has several drawings but the request has neither a requested
TP reference nor a verified baseline identity, the publisher now produces a PDF
with **Drawing Reference: Review required**. It uses verified email/attachment and
holding-item values plus the linked project's title, and skips project specification
rollups and all drawing-specific fields. Drawing Reference and Revision remain null
when absent; manufacturer references such as `RLTF-002` are not treated as TP refs.
`drawingSelection.status = unassigned` records the candidate drawing identities and
`drawingItemId` stays null. A populated holding Drawing Reference participates in
selection before drawing fields are read, so confirmation can resolve the review
on a later refresh. A conflicting or unverified requested/baseline TP identity,
an unmatched specified drawing among existing drawings, multiple linked projects,
or an incomplete source read still blocks replacement.

The October 31 two-email replay resolved `3312713363` through its verified baseline
`TP18675_26.01 - C`. For `3315502554`, the email-only review path retained the Atrium
request's `0.13` / `0.35` U-values and requester instead of importing another drawing's
CRM specification. These are local source-resolution checks, not production uploads.
Deploy the updated specification publisher; its existing retry schedule picks up
these items. This change uses the retained `output-v15` evidence and requires no
database migration, extraction rerun or environment-variable change.

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
3. Extraction identity is now `output-v16` (`output-v9` introduced source evidence;
   `output-v10` added DWG skips and bounded ZIP extraction; `output-v11` preserves
   long spreadsheet cells; `output-v12` skips encrypted PDFs; `output-v13` validates
   recovery against source text and retains request constraints; `output-v14` repairs
   request-note citations and verifies revisions from separate title-block cells;
   `output-v15` adds bounded drawing detail views and audited date/issuer/scope repairs;
   `output-v16` adds literal image regions, client-request context and applicable
   existing-deck evidence). The CAD publication contract is now
   `performance-specification-v4`. Existing canonical CSV rows
   remain unchanged. Run the normal design reconciliation sweep to re-extract eligible
   historical projects. Old extraction records without the versioned source/evidence
   contract wait for this refresh. Changing only the publisher flag does not refresh
   historical extraction evidence.
4. Review shadow artifacts and translation warnings in the private artifact bucket
   and `performance_specification_artifacts`, plus `performance_specifications.review_json`.
   Review statuses distinguish blocking `conflict`/`source_failure` from successful
   `published_with_review`/`rendered_with_review` outputs. A fully resolved refresh
   clears the review record. Then use the existing design-processing
   `allowlist` mode and `DESIGN_PROCESSING_ALLOWLIST_ITEM_IDS` to validate a few items
   before changing to `enabled`. These existing mode settings govern both workflows.
   `allowlist` now restricts extraction, CAD translation, private rendering and
   publication, rather than publication alone. Queued work outside the list remains
   pending and is filtered before claims. Owned work removed from the allowlist is
   fenced at checkpoints; already-sent external requests may finish.
5. Subscribe holding field/link/lifecycle changes and linked project/drawing changes
   to the existing authenticated Monday webhook endpoint. Include project subitem
   creation, name/column changes, archive/delete and restoration events. Mirror-source
   boards need corresponding event coverage. The dependency index is established on
   the first successful specification refresh; periodic checks repair missed events.

No credentials are stored in queue records. The publisher uses
`MONDAY_INGESTION_ACCESS_TOKEN`, configured Gemini credentials, and private
`DESIGN_PROCESSING_ARTIFACT_BUCKET` storage. It requires no CAD integration or public
artifact URLs. Enabling this setting does not provision webhooks or start a process.

For an isolated Render test of holding item `3320074395`, deploy the same updated
commit to the API, design-processing worker, reconciliation service and specification
worker. Configure their shared design settings consistently:

```dotenv
DESIGN_PROCESSING_MODE=allowlist
DESIGN_PROCESSING_ALLOWLIST_ITEM_IDS=3320074395
DESIGN_PROCESSING_WORKER_ENABLED=true
PERFORMANCE_SPECIFICATION_ENABLED=true
```

This policy update needs no database migration or extraction-version bump. Ensure
old worker instances have stopped; a worker on older code still uses its old claim
policy. Other ingestion/search services keep their existing scope.

In the deployed design-worker shell, run:

```bash
python -m backend.scripts.check_design_processing_gemini --live
python -m backend.app.services.design_processing_reconciliation --item-id 3320074395
```

Leave the design and specification workers running. The first command is a synthetic
provider check; the second queues or coalesces the item's extraction. When extraction
is current, the specification workflow runs automatically. To request an immediate
specification check after extraction finishes:

```bash
python -m backend.app.services.performance_specification --once --item-id 3320074395
```

Check the target's design job and `performance_specifications.last_error/review_json`.
The database's total backlog still includes held unlisted jobs; in allowlist mode
only listed items are eligible to be claimed. Changing to `enabled` makes the other
pending jobs eligible again. Historical uploaded files are preserved during the test.

The earlier evidence-matching and inline-review update introduced the
`performance-specification-v3` publication version, which created a new PDF
revision instead of reusing an older PDF that omitted unresolved field details.
Read-only replay of item `3320074395`'s stored extraction and recorded CRM precedence
cleared the scale, page size, title, contract-number and insulation evidence errors.
Decking publishes a review note listing `Timber (18mm)` and `Concrete` with their
roof scopes. `BauderROCK` is verified as the raw requested product; if translation
cannot establish an exact permitted CAD product, its field publishes the raw value
and unresolved mapping details. Confirming the target/current CRM values removes
these review notes. The new PDF can be published while these decisions are pending.
Check `review_json.status` and the artifact's `model_version`: a `published` older
artifact joined through `current_revision` can also be a retained earlier PDF if
an incomplete source read or invalid response blocks the latest refresh.

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
Each v2/v3/v4 artifact stores its immutable `source_json`, including field provenance and
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

The current release requires fresh `output-v17` extraction and uses
`performance-specification-v4` PDFs. Deploy matching API, design, reconciliation and
specification workers, then run the normal reconciliation for the allowlisted items.
No additional database migration is needed. The publisher compares the **full**
configured pipeline identity, including model/thinking settings; matching stored
desired/analyzed identities alone cannot authorize reuse of an older extraction.
Retained PDFs remain available until a verified replacement succeeds.

### November 1 source-email and retry corrections (`output-v17`)

The email reader preserves a separate, hashed HTML text alternative alongside the
plain body. Literal HTML Content-IDs associate signature images when Outlook/Apple
plain text contains only an image filename or object placeholder. HTML gateway
footers outside the outer Word email container belong to its sender, rather than
the last forwarded correspondent. Apple reply headers also delimit messages.
Company/Contact may use a verified earlier signature from the same sender; another
sender's signature remains excluded. The selected client enquiry date is unchanged,
and older signatures cannot date technical drawing/material facts. Excluded
identity candidates retain their reason in provenance.
An unsupported company citation to a website address can move to one literal logo
region only when its Content-ID proves it belongs to that same signature. Repeated
uses by that sender are allowed; branding associated with another sender is not.
The claimed company name is preserved exactly, never derived from the domain.

Contact can reorder an exact, single `From: "Surname, Given name" <address>` header
without inventing name parts. A complete literal drawing reference remains valid
when a response redundantly splits it at the underscore. Deterministic date repair
restores a uniquely verified equivalent literal Date/Sent header. If the model cites
another actual header in the same email, a technical excerpt can be rebound only
when it occurs in one distinct message and is absent from the cited message; the
literal containing header supplies both date and direct/forwarded role, with a
before/after audit. Invented dates and repeated quotations still block. Long request
notes can be split at whitespace only after the entire original contiguous excerpt
verifies; all words survive, each citation remains bounded, and unverified or
over-limit responses still block publication. No material aliases are inferred.

Actual NUL characters in extracted source text become spaces before prompting and
verification, preserving token boundaries and original file hashes. Literal
`\\u0000` text is unchanged. Model-introduced NULs are rejected before persistence.
Legacy stored payloads should be replaced by fresh extraction, not bulk-edited with
SQL string replacement. Empty/failed text and size-limit errors carry safe source
codes such as `no_readable_text` and `source_text_limit`.

CAD values are single-line fields. Before translation and validation, line breaks
and other whitespace runs in every resolved text value collapse to single spaces,
not only Company/Contact; for example, a two-line title-block project name for item
`3318408522` previously failed with `CAD translation contains control characters
in Drawing Title`. Words, case and punctuation must still match exactly, and a
translated value containing a control character is still rejected. The source
snapshot keeps the literal evidence, so specification revisions and already
published PDFs are unchanged; only the specification publisher needs redeploying.

Scale evidence still verifies literally: a lone `1:50` citing `scale: 1/50 @ A1` is
rejected. When the same claim (source, scope, issuer, role and date) also has a
verified literal candidate stating the same ordered ratio, a reformatted `1:50`,
`1/50` or `1 to 50` sibling is marked `repaired` instead of blocking extraction;
recovery may likewise replace it with that literal and record the audit entry.
`50:1`, `1:500` and values with extra text such as `1:50 @ A1` remain distinct.
This resolved item `3318413027` (job `5af08583`) offline from its stored failure
payload. It is a verifier change only, so `output-v17` is unchanged and no
re-extraction is forced; deploy the design worker and specification publisher.

The two valid PNG plans for item `3313351849` returned HTTP-success responses with
no text and `finish_reason=RECITATION` in the approved Gemini replay. The provider
defines this as content flagged for recitation ([Gemini API response reference](https://ai.google.dev/api/generate-content#FinishReason)).
Responses now preserve safe `blocked_response`, `incomplete_response` or
`empty_response` diagnostics, including finish/block reasons. Partial text cannot
be accepted as a complete source. Do not repeat unchanged blocked calls indefinitely,
silently skip the plans, or disable provider safeguards. This item needs a reviewed
source/transcription or a provider-side resolution before its source is complete.
No automatic OCR fallback is claimed to capture diagram geometry or outlet locations.

A controlled drawing-specific prompt trial on 1 November 2026 made one additional
request for each of those PNGs, using the same `gemini-3.8-flash` model and medium
thinking, no sampling override, no SDK retries and a 120-second request timeout.
Calder still returned `RECITATION` (26.458 seconds, no text). Whifflet returned
`STOP` with 29 text regions (19.470 seconds), but declared `READABILITY: PARTIAL`,
including obscured smoke-shaft specifications and unreadable labels/notes. Neither
result was accepted as a complete source, and the item remains blocked. A second
run used both content/model/prompt-keyed cached outcomes with zero network calls.
This two-image trial does not establish a production success rate or performance
benchmark; no runtime fallback was enabled. The next useful input is the original
drawing PDF or a higher-resolution export with the obscured notes reviewed.

The supplied 15 emails cover eight blocked items. Final local validation of both
sets of captured Gemini parameter responses succeeds for `3312713363`, `3313065024`,
`3312835171`, `3317334071`, `3318408522`, `3312851487` and `3302170223`. Conflicting
roof/material/U-value and other alternatives remain reviewable, rather than being
silently selected. Captured-response checks do not guarantee future model output.
The original bytes of both PNGs for `3313351849` were verified as readable PNGs;
the captured provider blocks now reproduce the explicit failure diagnostics.
Replays did not update Monday, the database or published PDFs. Source email bodies
contained no NULs, so the origin of the legacy NUL payload is not proven by these
files. Private emails, attachments and replay outputs are excluded from Git.

Evidence failures now retain private replay data under
`design_processing_jobs.result_json.specificationEvidenceFailure`, including source
inventory, evidence, field/note diagnostics and excluded candidates. Schema failures
retain their response and schema diagnostics there too. Source-read failures remain
under `sourceExtractionFailure`. Worker exception summaries omit source excerpts;
these private payloads are not suitable for public logs or issue reports.

Duplicate notifications preserve unchanged extraction retry deadlines. Exhausted
executions are not recreated by reconciliation/webhooks for the same input, pipeline
and execution kind. Changed inputs or a new pipeline identity permit fresh work.
After addressing a failure, an operator can explicitly retry one item with:

```bash
python -m backend.app.services.design_processing_reconciliation --item-id 3312851487 --retry-failed
```

`--retry-failed` requires `--item-id`; `--dry-run` previews it. Normal reconciliation
after deployment is sufficient for old pipeline identities. Periodic specification
reconciliation leaves already-scheduled refreshes and retry counts alone. Other
notifications preserve failure counts; an explicit immediate request or completed
fresh extraction wakes publication promptly. Successful refreshes clear the count.

Deploy matching API, design, reconciliation and specification workers. No schema
migration is required. Keep the existing admission cutoff and allowlist: the five
previously unregistered items need an eligibility check, not automatic historical
backfill. Verified roof/value conflicts continue to publish review details, while
unverified evidence retains the last successful PDF.

Images now transcribe coherent visible text into bounded `[TEXT REGION n]` blocks,
with visual descriptions separate. Evidence and constraint excerpts use those literal
regions, not descriptive prose. A Company citation can be restored to one uniquely
matching region within the same image without changing the claimed name, scope,
issuer, role or date. Before/after evidence, region ID and source hash are audited;
invented legal suffixes, separate logos and ambiguous matches still block extraction.
The model remains responsible for reading the image accurately.
Company/Contact raw evidence retains visual line breaks. CAD export collapses only
their whitespace to single spaces, preserving words, punctuation, case and legal
suffixes; other text/identifier validation remains exact.

Decking means the material **described** for the applicable roof. Verified baseline
deck construction therefore remains applicable; recovery cannot clear it merely by
returning a null scalar. Achieved U-values, withdrawn facts and unselected options
remain excluded from requested fields. An ambiguous description such as `polyroof deck`
retains its raw wording and baseline provenance, with a null CAD value and a
**Review required** note when no allowed structural material can be established.

Deploy matching API, design/reconciliation and specification workers, stop replaced
workers, and run normal reconciliation for item `3310414364` to request fresh extraction:

```bash
python -m backend.app.services.design_processing_reconciliation --item-id 3310414364
```

No database migration is required. `DESIGN_PROCESSING_INTERNAL_EMAIL_DOMAINS` defaults
to `taperedplus.co.uk`; provide comma-separated additional internal domains only when
needed, consistently across the services. Its normalized value contributes to the
full extraction identity. The new pipeline revision forces fresh transcription;
reusing old descriptive image text cannot validate a previously unsupported logo claim.

The October 31 full live Gemini replay of the two supplied emails for item
`3310414364` completed on `output-v16` with no remaining evidence or request-note
errors after recovery. It selected the external client's 26 October 14:56 request,
retained the literal company branding and the described `polyroof deck`, and verified
the plan's 22 October 16:36 forwarded-message date through its Content-ID. The replay
saved diagnostics locally and did not publish PDFs or modify Monday/database records.
It validates those captured responses; subsequent model responses still require
the same evidence checks.

Extraction now checks evidence against the manifest before completing analysis and
makes one bounded recovery attempt. A unique verified passage can repair an incorrect
attachment ID within the same email. Formatting-only value repairs preserve words,
identifiers and numbers. Repairs retain before/after evidence and content hashes in
`specificationEvidenceRepairs` and the artifact source snapshot. Unresolved citations
fail extraction so its worker retries analysis, rather than leaving the publisher
to retry permanently unusable evidence. A populated scalar can become null only when
recovery verifies that exact fact as achieved, baseline, withdrawn or unselected.

Request-note validation participates in the same single recovery call, even when all
scalar fields have verified values. Diagnostics identify rejected notes by zero-based
index. A corrected note replaces an invalid version only when the corrected excerpt
verifies and preserves the words, numbers, signs, negations, category and scope.
Label-colon formatting may be corrected; URL punctuation is preserved. A wrong
attachment ID can be repaired only by a unique passage within the same email.
Unchanged valid notes are retained, omitted invalid notes still block, and repairs
are recorded with before/after notes and source hashes in `specificationEvidenceRepairs`.

Revision evidence can use a single drawing-number cell and a single labelled revision
cell in one verified contiguous title-block excerpt, including `reference_parts`.
Multiple drawing numbers or revision labels require review; recovery cannot splice
separate passages or change the suffix. Page Size and Scale in an applicable drawing
title block are requested specification facts even if the email does not repeat them.
An unsupported Page Size still blocks extraction: PDF dimensions alone cannot verify
it. Failed field recovery now logs enum-only `evidence_roles` counts to distinguish
missing evidence from candidates classified as `other`, `lookup` or another role,
without logging their raw values or excerpts. A null or missing-value marker can
clear a populated scalar only with the existing verified non-request evidence check.

For the Render failures on items `3320074395`, `3320649967` and `3319921927`, deploy
matching API, design/reconciliation and specification workers with `output-v15`, stop
the replaced workers, and keep the existing allowlist. No database migration or
environment-variable change is needed. Run normal reconciliation after deployment:

```bash
python -m backend.app.services.design_processing_reconciliation --item-id 3320074395
python -m backend.app.services.design_processing_reconciliation --item-id 3320649967
python -m backend.app.services.design_processing_reconciliation --item-id 3319921927
```

The new pipeline identity schedules fresh extraction; do not manually change job
statuses or retry an obsolete execution identity. Confirm `output-v15` in runtime
logs and the full desired/analyzed pipeline identities in the diagnostic SQL.
These fixes cover reproduced recovery defects, not every possible unverified fact
in those jobs. Continued Page Size failures require inspection of the actual
source and its evidence role. Existing PDFs remain until verified replacements
succeed. Openpyxl data-validation-extension warnings and PDF duplicate `/PageMode`
warnings in the supplied logs did not themselves report failed extraction jobs.

The October 31 source replay reproduced recovery merging a corrected literal
forwarded-message date with its invalid reformatted predecessor. Recovery can now
replace that predecessor only for the same fact, source, scope and request role,
with the same local calendar date/time and a unique containing message whose
Date/Sent header exactly supplies the correction. It cannot move a fact to another
message or change an already literal date. ISO year-month-day timestamps retain
their month/day order. All accepted repairs retain before/after evidence and hashes.

A flattened bullet note can be restored to a unique contiguous source passage,
allowing only whitespace, star/bullet prefixes and sentence-ending full stops to
differ. Every word, sign and numeric fact must remain; omitted intervening text,
ellipses, competing matches and URL rewrites cannot pass this repair. The full literal
passage and before/after audit are retained, rather than accepting a shortened note
that loses constraints. A note's unsupported optional scope can be removed only when its full excerpt is
verified in the same source and the old scope is absent there. Verified scopes and
all constraint wording survive. Self-identifying TP/AX references can discard an
unquoted issuer or use literal quoted branding; bare/D.S references still require
verified TP provenance. Redundant `reference_parts` also validate when they reproduce
one complete reference already present verbatim in the excerpt. They cannot change
the reference or combine separate passages. Company names remain literal; recovery
cannot invent legal-name suffixes or replace an unsupported claim with an alias.
Drawing-title recovery may restore a possessive apostrophe in an alphabetic name
when the corrected value is literally supported in the same source. It cannot
change other words, numeric identifiers or negations; verified spelling alternatives
from different sources still remain separate candidates for review.

Large PDF drawings now include four overlapping detail views per large page in the
same Gemini transcription request, capped at 16 detail pages and 20 MiB of derived
PDF bytes. The original complete PDF remains present; ordinary small pages have no
detail views. Views stay in memory, retain the original source identity, and never
supply page-size evidence through their dimensions. PDF requests use high media
resolution and literal transcription instructions, including no invented labels,
dates or requested/achieved annotations. Explicit ambiguous paper-size labels must
remain evidence for review. Source transcription is still model-based; these changes
improve legibility without guaranteeing that every glyph is correct. Existing source
size/context limits and encrypted-PDF skips still apply. No new dependency is required.

The supplied log's new `output-v14` Company failure, job
`3eee61a8-077c-4e5e-a8a1-ae52d5f1f72f`, belongs to item `3312713363`, not one of the
initial three supplied emails. The subsequently supplied `3312713363.eml` completed
a full live Gemini diagnostic replay on `output-v15`: Company is `WALKERS WATERPROOFING`,
supported by its embedded signature-logo transcription, and Contact is `Jack Walker`.
Both request-constraint excerpts were retained. The file contains one embedded PNG;
the historical drawing/PDF filenames quoted in the email body are not embedded files
and were not read as attachments. No further validator change was needed for this
replay. The failed `output-v14` response was not saved, so its exact earlier mismatched
value/excerpt remains unknown; the successful replay does not reconstruct it.

At the October 31 follow-up database check, that `output-v14` job was cancelled as
superseded and a fresh `output-v15` job (`6817335c-b572-4e4c-8017-84f543eac802`) was
scheduled with zero attempts and no error. Let the worker claim the current job;
do not retry the obsolete execution identity. This was a read-only status check,
not evidence that production publication had completed. Worker commit/pipeline fields
distinguish old in-flight errors from failures produced by the replacement worker.

With the supplied three emails, provider replays and subsequent local replays of
the captured responses verified the `output-v15` fixes. Final captured-response
validation completed for all three while retaining review issues: title/product/deck
alternatives for `3320074395`, ambiguous paper size for `3320649967`, and drawing/roof
U-value alternatives for `3319921927`. These checks did not publish PDFs or modify
Monday/database records. They validate these captured responses, not every future
model response. Private source files and diagnostics remain outside Git.

Accepted AX drawing numbers retain their prefix and actual issuer. Separately quoted
drawing-number/revision cells may form `AX 16670_26.12 - B`; the derived CAD revision
remains `26.12 - B`. Bare and D.S references still require CRM or verified TP provenance.
Colon spacing and escaped literal asterisks no longer produce false mismatches.
Repeated material descriptions are consolidated only when one literally contains
the other in the same source passage, scope and request. This introduces no product
alias translation or automatic selection between conflicting roof specifications.

Inline-image Content-IDs are retained with source IDs, and HTML image positions stay
beside their surrounding scope captions. PDF page dimensions are independently read
and can flag a title-block size for review; they never replace its stated paper size.
An ambiguous `ISO AB` is reviewable, not automatically converted to `ISO A3`.

Fully read CRM material multi-selections publish a null CAD value and the alternatives
as review details. Unreadable required mirrors, conflicting or unmatched specified
drawing identities, and unverified facts still prevent replacement. Requests without
a TP drawing identity can use the unassigned review path described above. Verified dimensional limits, exclusions, allocations,
options and source-link excerpts appear after the 18 CAD fields under **Request
constraints and context**. They remain review excerpts, including source/date context;
external linked files are not downloaded or claimed to have been inspected. Notes
are stored in `source_json.requestNotes` and `translation_json.requestNotes` and cause
`published_with_review`/`rendered_with_review` status even if all scalar fields resolve.

```powershell
venv/Scripts/python.exe -m pytest backend/tests/test_performance_specification.py backend/tests/test_specification_drawing_selection.py backend/tests/test_specification_evidence.py backend/tests/test_specification_sources.py backend/tests/test_specification_publication_hardening.py backend/tests/test_specification_recovery_regressions.py backend/tests/test_specification_semantic_context.py backend/tests/test_pdf_detail_views.py backend/tests/test_source_archives.py backend/tests/test_encrypted_pdf_sources.py -q
```

Coverage includes LLM schema/semantic validation, optional values, exact PDF text
ordering, active-only scope, CRM clears, typed mirrors, revision ambiguity, cached
translation, failed reads, stale generations, expired leases, source changes during
translation/upload, lost upload responses, replacement safety and shadow mode.
PDF text validation uses Poppler `pdftotext` or optional `pypdf`.
Regression coverage also includes published field review details, combined roof
conflicts and unresolved products, cached review notes, review resolution, recovery,
full TP revision suffixes, conflicting roof decks, candidate recovery from a missing
scalar, HTML-only mail, date ordering, XLSM/hidden catalogues, uncached formulas,
source failures, People identity reads and immutable artifact snapshots.
