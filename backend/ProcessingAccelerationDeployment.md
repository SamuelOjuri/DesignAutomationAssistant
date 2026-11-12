# Processing acceleration deployment

Prepared: 11 November 2026. This runbook describes the `output-v20` release and
does not record a production deployment. Deploy the API, design workers,
reconciliation, specification publisher and frontend from one matching revision.

Operational update: 12 November 2026. The bounded database pools, sanitized
database diagnostics and short admission retry are operational fixes within
`output-v20`. They require a matching backend deployment, with no new migration
or extraction pipeline bump. The required database head remains
`0021_processing_acceleration`.

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

## Check database access with one design instance

While investigating connection/admission failures, keep one design-worker
instance. Deploy the operational fix to every backend role using this database:
API processes, design workers and their extraction children, reconciliation,
specification publishers and maintenance processes. Check service-specific
environment overrides and the deployed commit. Use these bounded settings:

```dotenv
DATABASE_POOL_SIZE=2
DATABASE_MAX_OVERFLOW=0
DATABASE_EXTRACTION_POOL_SIZE=1
DATABASE_EXTRACTION_MAX_OVERFLOW=0
DATABASE_POOL_TIMEOUT_SECONDS=30
DATABASE_POOL_RECYCLE_SECONDS=300
DATABASE_CONNECT_TIMEOUT_SECONDS=10
```

These limits apply per process. Count all API workers, background services and
extraction children when assessing total connection demand. Reducing one
service's pool does not change the others. Keep the existing independent design,
specification and ingestion modes. Do not raise Gemini quotas, disable the
shared admission gate or change web-service scaling to conceal a database
failure.

Run the new diagnostic from the repository root in **each affected deployed
environment**, using that process's existing `DATABASE_URL` and runtime role:

```bash
python -m backend.scripts.check_processing_database
```

It opens one short-lived, nonpooled connection and uses a read-only transaction.
It checks `SELECT 1`, the Alembic revision, the presence of both acceleration
tables, `SELECT`/`INSERT`/`UPDATE`/`DELETE` privileges and whether row security is
active for the runtime role. It reports only revision IDs, fixed table names,
booleans and sanitized error classifications. It does not print the connection
URL, host, username, source text, cache rows or request leases. It performs no
SQL writes, model calls, queue actions or publication. `--help` does not open a
connection. Exit status is zero only when all checks pass; SQLite is explicitly
unsupported for this deployment check.

Use the reported classification to choose the next action:

| Result | Action |
| --- | --- |
| `migration_required` / `missing_relation` | Apply the existing 0021 migration to the intended database, then rerun the diagnostic. |
| `permission_denied` / `row_security_blocks_runtime` | Correct the backend runtime role's grants and privileged RLS access; preserve the migration's browser-role restrictions. |
| `connection_limit` / `pool_timeout` | Check connection demand across all processes and deployment overlap; apply the bounded pools consistently and keep one design instance while checking. |
| `connection_failure`, timeout, resolution or unavailable classifications | Check connectivity from that deployed environment and its configured database endpoint. |
| `authentication` / `connection_security` | Correct that service's database credentials or connection trust configuration. |

A local successful probe establishes access only from the local environment at
that time. It does not prove Render connectivity, historical pool availability
or the cause of an earlier `http_status=None` transcription exception. The
supplied 11 November log has cache `OperationalError` warnings, but its redacted
transcription summaries alone do not prove `MaxClientsInSessionMode` or a Gemini
provider outage. Connection/network or client-pool failures are plausible;
missing-table and permission failures normally have `ProgrammingError` and
SQLSTATE details instead. Check those prerequisites independently and inspect
the new sanitized `database_error`/`sqlstate` fields.

On 12 November, a read-only probe from the local configured backend environment
returned `status: ok`: revision `0021_processing_acceleration`, both required
tables present, all four privileges available, runtime ownership and RLS bypass
reported true, and `row_security_active: false` for both tables. The probe used
one nonpooled connection and metadata SELECTs only. This establishes current
local access; it does not establish Render's environment/role or explain the
previous evening's `OperationalError` warnings. Repeat it inside the affected
deployed services after applying their matching code and pool settings.

Shared admission makes up to three attempts for recognized transient database
failures, with 0.5- and 1-second delays. Missing relations, permission failures and
unknown operational errors do not become quota bypasses; admission stays closed
when it cannot be verified. Confirm the database diagnostic passes and ordinary
work progresses before restoring the second design instance described below.

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

After the database checks pass and the single-worker pilot is stable, create
two separate design-worker instances/services, each using the same
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
python -m backend.scripts.check_processing_database
```

These commands make no model calls or external writes; the database command
does make read-only metadata queries against the configured database. The source probe must
report `status: ok`, `pipeline_revision: output-v20`, eight passing checks,
`max_source_mib: 40`, and a `native-text.pdf` check using method `native`.
Runtime fields report `native_pdf_text_enabled`, `transcription_cache_enabled`,
`transcription_cache_ttl_days`, `gemini_shared_budget_mode`,
`gemini_shared_max_concurrent` and `gemini_shared_requests_per_minute`. With the
example configuration, budget mode is `shared_database`. This synthetic probe
checks code capabilities and settings; it does not establish database migration,
cache hits or cross-process admission by itself.

The database diagnostic must independently return `status: ok`. A successful
synthetic source/model-schema check cannot establish database connectivity,
table access or role privileges.

From the repository root, run the focused local checks:

```powershell
venv/Scripts/python.exe -m pytest backend/tests/test_native_pdf_text.py backend/tests/test_processing_acceleration_pipeline.py backend/tests/test_processing_acceleration_storage.py backend/tests/test_specification_early_preview.py backend/tests/test_design_processing_source_diagnostic.py backend/tests/test_processing_database_diagnostic.py -q
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

## Retry exhausted work only after its cause is resolved

The 12 November operational fix retains `output-v20`. Redeployment does not reset
the attempt budget of an already failed job with the same input/pipeline
identity. Ordinary reconciliation can report `attempts_exhausted` and leave it
failed. Do not repeatedly queue unchanged work or manufacture a pipeline bump.

After the deployed database checks pass, inspect the failed job and confirm it
still belongs to the current item/input/pipeline. An explicit operator retry is
available, for example for item `3340068482` and its **logged** failed job:

```bash
python -m backend.app.services.design_processing_operations --operator-id Samuel retry-failed-job --job-id a2688ee0-27be-43c2-9926-0629dec265ce
```

This command queues real processing and can make model calls and publish normal
design outputs under the existing modes. It is an operator action, not part of
the read-only diagnostic, and was not executed during the log assessment.
Confirm the job is still failed/current before using its historical ID. For
uncached workbook formulas, first recalculate and save a readable workbook or
replace the source. For citation/request-context failures, first inspect the
retained evidence and correct the source or extraction cause. Database repair
alone does not validate those facts. See the
[12 November log assessment](../output/design-worker-log-assessment-2026-11-12.md)
for the seven unresolved items and four recoveries observed in that log.

The afternoon update (see the
[12 November 13:11 assessment](../output/design-worker-log-assessment-2026-11-12-1311.md))
keeps `output-v20` and needs no migration. It makes a job fail on its first attempt
when every failed source was rejected on its own bytes (size, unreadable PDF/ZIP,
workbook or archive limits); reconciliation then reports `attempts_exhausted` for
that input until a replacement file arrives. Provider and transcription failures
still retry. It also raises the design workbook allowance to 40 worksheets and fixes
three evidence-verifier false negatives described in
[the specification notes](PerformanceSpecification.md). Deploy the design workers,
reconciliation, API and specification publisher from the same revision. After that
deployment, retry the failed jobs for items `3352626143` (`4b0b514d`),
`3345436089` (`8bc1710e`) and `3342464181` (`80c7df2f`) with the operator command
above, once each is confirmed failed/current. Do not retry `3353889887` or
`3305268547` until their sources are replaced, `3345815639` until its request-note
citation and U-value conflict are reviewed, or `3353070362`, which is now outside
the Landing Zone.

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
