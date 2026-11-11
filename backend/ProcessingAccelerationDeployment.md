# Processing acceleration deployment

Prepared: 11 November 2026. This runbook describes the `output-v20` release and
does not record a production deployment. Deploy the API, design workers,
reconciliation, specification publisher and frontend from one matching revision.

The release adds a private source-transcription cache, conservative native PDF
reading, shared Gemini request admission and early read-only Specification Review
previews. Existing design/specification mode settings, Landing Zone scope checks,
source identity checks, reviewer decisions and publication gates still apply.

## Apply the migration before starting the release

From the repository root in the intended backend environment:

```bash
python -m alembic -c backend/alembic.ini current
python -m alembic -c backend/alembic.ini heads
python -m alembic -c backend/alembic.ini upgrade head
python -m alembic -c backend/alembic.ini current
```

The head must be `0021_processing_acceleration`, after
`0020_monday_item_availability`. It adds `source_transcription_cache` and
`gemini_request_leases`. Apply it before deploying code that uses these tables.
Use the existing migration/pre-deploy process and the intended database; the
commands above change that database. On Windows, replace `python` with
`venv/Scripts/python.exe`.

On PostgreSQL, 0021 enables row-level security on both new tables and revokes
default access from `PUBLIC`, `anon` and `authenticated`. Literal source text
must remain private when the tables are in Supabase's public schema. Use the
same privileged database role for migration and backend runtime, as in the
existing deployment, or a runtime role with appropriate grants and `BYPASSRLS`.
The backend role must own the new tables or have the required privileged access;
ordinary browser/Supabase user roles must not read or write them.

## Configure matching backend services

Native PDF reading and transcription caching default to enabled:

```dotenv
DESIGN_PROCESSING_NATIVE_PDF_TEXT_ENABLED=true
DESIGN_PROCESSING_TRANSCRIPTION_CACHE_ENABLED=true
DESIGN_PROCESSING_TRANSCRIPTION_CACHE_TTL_DAYS=30
```

The cache stores literal PDF/image transcription text, scoped to its Monday
board and source hash, model/thinking settings and pipeline contract. It does
not cache field decisions, request attribution or reviewer approvals. Current
request context, evidence validation and source checks run again when text is
reused. Successful source text can be reused after another source fails. Cache
rows are private database data; expired text is not returned.

The full extraction identity includes `native-pdf-1` while native reading is
enabled (`native-pdf-0` when disabled). Changing that flag requires fresh
extraction, so use the same value on the API, design/reconciliation and publisher
services. Cache enablement and TTL do not change the extraction identity or
invalidate otherwise reusable entries. TTL applies when a cache row is written;
changing it does not rewrite the expiry timestamps of existing rows.

Native reading accepts only sufficiently readable text-only PDFs. Images,
form values, annotations, outlined graphics and uncertain mappings retain the
existing whole-PDF Gemini transcription. The supplied St Clere's School drawing
is mixed content: its visible contract number, reference and scale are absent
from the native text layer, so it correctly keeps OCR. Its repeated-read
improvement comes from the transcription cache. PDF source manifests record
`pdfTextMethod` and, when applicable, `pdfNativeTextFallbackReason`.

Shared Gemini admission defaults to disabled. Enable it before running the two
design workers together. Every backend process using the same provider project
must use the same PostgreSQL database, budget scope and limits:

```dotenv
GEMINI_SHARED_RATE_LIMIT_ENABLED=true
GEMINI_SHARED_BUDGET_SCOPE=tapered-plus-gemini-project
GEMINI_SHARED_MAX_CONCURRENT=8
GEMINI_SHARED_REQUESTS_PER_MINUTE=60
GEMINI_SHARED_LEASE_SECONDS=900
GEMINI_REQUEST_TIMEOUT_SECONDS=240
GEMINI_SHARED_WAIT_SECONDS=300
```

Replace the scope with one stable project identifier shared by the relevant
services, including the specification publisher. `8` concurrent requests and
`60` requests per minute are application defaults, not verified provider quotas;
set them within the actual model/project quota and reserve capacity for other
usage. The limiter accounts for requests and concurrent generations; token-rate
limits still depend on document size and provider quota. Changing API keys for
the same project must not create separate budget scopes.

Use PostgreSQL for coordination across worker processes. Admission uses a short
transaction and advisory lock; database locks and transactions are released
before model requests. Every retry obtains admission again. Keep lease seconds
greater than request timeout; the example is 900 versus 240 seconds. A busy
budget waits up to 300 seconds, then follows the existing failure/retry path.
Missing admission tables or database access do not bypass an enabled budget.

Apply the environment values and deploy every affected service, checking
service-specific overrides. Preserve the existing independent
`DESIGN_PROCESSING_*`, `PERFORMANCE_SPECIFICATION_*` and `AUTO_SYNC_*` rollout
policies. See [the specification deployment runbook](SpecificationReviewDeployment.md).

## Run two design workers and one specification worker

Create two separate design-worker instances/services, each using the same
repository root, release, database and environment. Use this command on each:

```bash
python -m backend.app.services.design_processing_worker --claim-limit 1
```

Leave `--worker-id` unset so each process generates its own identity. Preserve
`DESIGN_PROCESSING_WORKER_ENABLED=true` and the intended design admission mode.
One claimed item is processed at a time per worker; raising `--claim-limit` does
not itself create parallel worker processes. Existing attachment threads operate
inside each extraction child and share the admission budget with other services.

Keep one separate specification publisher:

```bash
python -m backend.app.services.performance_specification --poll-interval-seconds 5
```

It runs only when the existing specification master/mode settings permit work.
Keep the existing reconciliation schedule. Additional design workers reduce
queue delay across items; they do not shorten every individual PDF model call.

## Verify one matching backend and frontend release

Run the synthetic source check in each deployed backend environment:

```bash
python -m backend.scripts.check_design_processing_sources
python -m backend.scripts.check_design_processing_gemini
```

These commands make no model calls or external writes. The source probe must
report `status: ok`, `pipeline_revision: output-v20`, eight passing checks,
`max_source_mib: 40`, and a `native-text.pdf` check using method `native`.
Runtime fields report `native_pdf_text_enabled`, `transcription_cache_enabled`,
`transcription_cache_ttl_days`, `gemini_shared_budget_mode`,
`gemini_shared_max_concurrent` and `gemini_shared_requests_per_minute`. With the
example configuration, budget mode is `shared_database`. This synthetic probe
checks code capabilities and settings; it does not establish database migration,
cache hits or cross-process admission by itself.

From the repository root, run the focused local checks:

```powershell
venv/Scripts/python.exe -m pytest backend/tests/test_native_pdf_text.py backend/tests/test_processing_acceleration_pipeline.py backend/tests/test_processing_acceleration_storage.py backend/tests/test_specification_early_preview.py backend/tests/test_design_processing_source_diagnostic.py -q
```

Run migration/cache/admission concurrency checks against a disposable test
PostgreSQL database before the multi-worker rollout. Build and test the matching
frontend from `frontend/main_app`:

```bash
npm run test:specification-review
npm run build
```

Deploy that frontend to the existing Netlify site, preserving API/OAuth rewrites
and its existing public API base. The new API progress and preview fields need
the matching frontend; a backend-only release does not provide the complete UX.

For an already eligible pilot item, inspect then queue current extraction:

```bash
python -m backend.app.services.design_processing_reconciliation --dry-run --item-id 1234567890
python -m backend.app.services.design_processing_reconciliation --item-id 1234567890
```

Replace the example item ID with the actual pilot. The second command queues the
existing design workflow and its configured outputs. `output-v20` requires fresh
extraction; changing only specification publisher flags does not refresh old
pipeline identities.

Keep the Item View visible. It polls automatically and shows current processing
stages/counts. The extraction parent coalesces count updates to at most once per
second, while phase changes and failures are reported immediately. Once current validated extraction is durable, fields may appear
as a provisional read-only preview while project matching and legacy rendering
continue. Review saves and completion become available through the normal full
analysis/review gates. Retained PDFs stay labelled separately from current
provisional information. After completion, check the correct current PDF and
complete the ordinary reviewer journey.

Confirm the two design workers have different runtime worker IDs, and inspect
`Source transcription reused` log entries on later reads of unchanged sources.
Successful analysis also logs `analysis_stage_timings`; individual successful
model calls log `Gemini request completed` with stage and duration only.
Do not infer cache effectiveness from the first run, which still needs to read
uncached mixed/scanned documents. Record queue, first-fields and PDF-ready times
separately rather than promising a fixed end-to-end duration.

The durable extraction payload records `processingTimings` with
`reading_sources_seconds`, `extracting_fields_seconds`, `project_title_seconds`
and `analysis_seconds`. These measure source reading, field extraction, optional
project-title extraction and their combined analysis time. They exclude queue
waiting, initial asset downloading, later CRM matching, legacy rendering and specification publication;
keep those milestones separate when assessing user-visible latency. Compare a
first uncached run with a later unchanged-source read rather than treating a
cache hit as a first-run processing estimate.

Stored attachment-check results from before this release remain historical
diagnostic evidence. New request-header parsing and extraction behavior should
be assessed with a new current-pipeline run; do not relabel or overwrite an old
result as if the updated parser originally produced it.

Local verification on 11 November exercised the backend tests, PostgreSQL checks,
44 frontend Node tests and the frontend production build. Browser journey
assertions were not completed: the in-app browser was unavailable and the
Windows computer-use URL policy blocked that fallback. These local results do
not claim browser journey or deployed Monday acceptance coverage.

## Retention and rollback

Expiry is checked on every cache read, and successful writes remove expired rows
within the same private scope. Inspect the bounded expiry cleanup, then remove
expired entries from inactive scopes with the same backend environment/database:

```bash
python -m backend.scripts.purge_processing_cache --dry-run --limit 1000
python -m backend.scripts.purge_processing_cache --limit 1000
```

The first command reports eligible counts without deleting rows. The second
deletes expired optimization data only, up to 1,000 rows from each of the cache
and request-lease tables; it does not remove review decisions or artifacts.
`--limit` defaults to 1,000 and accepts 1 through 10,000. Add the second command
to a daily maintenance cron job when cache data can remain on inactive boards.
If a reported count reaches the batch limit, run further bounded batches or
increase the limit/frequency until the expired backlog is cleared. Cache reads
still reject expired entries before physical cleanup runs.

To disable acceleration features, redeploy with
`DESIGN_PROCESSING_NATIVE_PDF_TEXT_ENABLED=false` and/or
`DESIGN_PROCESSING_TRANSCRIPTION_CACHE_ENABLED=false`. Reduce back to one design
worker before disabling shared admission. Keep `0021` and the additive tables in
place under running services. Reverting extraction code requires the API and
workers to return to the same pipeline version; retained decisions and artifacts
must remain intact.
