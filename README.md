# Design Automation Assistant

## Excel attachments in task chat

Task sync reads `.xls` and `.xlsx` files attached to emails or directly to Monday
items/updates. Sources labels these files `attachment_spreadsheet`. Searchable
text includes workbook names, worksheet names, cell coordinates and row ranges;
chat citations link to the original Excel file. This enriches task chat evidence,
not the separately generated AI Data CSV or its Summary table.

Deploy the backend with the updated `requirements.txt`. Existing completed
snapshots need a **forced sync** to extract previously stored Excel attachments:
send an authenticated `POST /api/tasks/{externalTaskKey}/sync` with JSON
`{"force": true}` and the normal CSRF header. The current **Sync task** button
does not request a forced sync, so unchanged completed snapshots can be skipped.
New or changed snapshots are processed normally. No database migration is needed.

The readers use saved cell values and do not execute macros, follow external
workbook links, or calculate formulas. For `.xlsx`, a formula without a cached
result is included with an explicit “cached result unavailable” label. `.xls`
uses the results saved by Excel. Save/recalculate the original in Excel before
syncing if calculated values are missing or stale. Numeric values are extracted
as underlying values, rather than reproducing all Excel display formats.

Extraction is bounded per workbook: 20 MiB input, 100 MiB expanded XLSX content,
20 worksheets, 10,000 rows, 256 columns, 200,000 scanned cells, 400 characters per
cell, and at most 400 searchable chunks (including any extraction notice).
Limits and unreadable/encrypted workbooks produce a searchable notice; the
original attachment remains downloadable when storage succeeds. The existing
sync memory guard still applies. Formats such as `.xlsm` and `.xlsb` are outside
this implementation.

Run the Excel parser and ingestion regression tests after installing backend
dependencies and pytest:

```powershell
python -m pytest backend/tests/test_spreadsheet_extraction.py backend/tests/test_sync_pipeline.py -q
```

## Design Processing Phase 1

The design-processing worker is disabled by default. Phase 1 pins the legacy
enquiry source snapshot, records reproducible extraction and matching fixtures,
validates worker configuration, and verifies private artifact storage without
calling Monday mutations.

The offline legacy source must be available at
`producer/TechnicalDesignAssistant`. The approved fixture email must be at
`data/FW_ Drawings Titley close_Walton House.msg`. Both inputs are verified by
SHA-256 before legacy code is executed.

Run the Phase 1 trust gates from the workspace root:

```powershell
& ".\venv\Scripts\python.exe" ".\backend\scripts\verify_legacy_enquiry_manifest.py"
& ".\venv\Scripts\python.exe" -m backend.scripts.generate_legacy_enquiry_fixtures
& ".\venv\Scripts\python.exe" -m backend.scripts.verify_design_processing_storage
& ".\venv\Scripts\python.exe" -m pytest ".\backend\tests\test_design_processing_phase1.py" -q
```

Fixture verification compares regenerated legacy output with the committed
versioned JSON and CSV byte-for-byte. Use `--write` only when intentionally
regenerating fixtures after an approved manifest and pipeline-version change.

Artifact storage uses `DESIGN_PROCESSING_ARTIFACT_BUCKET`, defaulting to
`design-processing-artifacts`. The storage verifier requires a private bucket,
uploads under the full design-processing identity namespace, verifies the
downloaded content hash, and deletes the probe object. On initial environment
setup, add `--create-bucket` to provision the configured bucket as private.

Configuration supports `off`, `shadow`, `allowlist`, and `enabled` modes.
`DESIGN_PROCESSING_ALLOWLIST_ITEM_IDS` accepts comma-separated decimal item IDs
or a JSON array and is required when mode is `allowlist`. Activation timestamps
must include a timezone. The pipeline version combines the full legacy manifest
digest, the separately pinned `gemini-2.5-flash` extraction model, and a
code-owned output revision that changes whenever rendered or extracted output
semantics change.

## Design Processing Operations

Run auditable operator commands from the workspace root. `--operator-id`
defaults to the current OS user and may be set explicitly before the command:

```powershell
& ".\venv\Scripts\python.exe" -m backend.app.services.design_processing_operations --operator-id "operator@example.com" enqueue-item --item-id 2657106977
& ".\venv\Scripts\python.exe" -m backend.app.services.design_processing_operations reconcile-item --item-id 2657106977 --dry-run
& ".\venv\Scripts\python.exe" -m backend.app.services.design_processing_operations retry-failed-job --job-id 00000000-0000-0000-0000-000000000000
& ".\venv\Scripts\python.exe" -m backend.app.services.design_processing_operations retry-artifact-cleanup --item-id 2657106977 --limit 20
& ".\venv\Scripts\python.exe" -m backend.app.services.design_processing_operations reconcile-mode-transition --dry-run --limit 100
& ".\venv\Scripts\python.exe" -m backend.app.services.design_processing_operations metrics --lease-timeout-seconds 3600
```

Remove `--dry-run` only after reviewing reconciliation output. Broad
mode-transition reconciliation requires
`DESIGN_PROCESSING_ACTIVATION_TIMESTAMP`; item-scoped reconciliation does not.
Commands are idempotent: enqueue and reconciliation coalesce into the existing
active job, failed-job retry reactivates the same immutable execution, and
cleanup only targets recorded `delete_pending` artifacts.

Operational mode and allowlist policy are re-evaluated at worker checkpoints
and before every Monday side effect. `off` prevents new claims, `shadow`
permits analysis only, `allowlist` permits publication only for configured item
IDs, and `enabled` permits all in-scope publication. Run mode-transition
reconciliation after changing mode or allowlist configuration so already
analyzed identities can receive publication-only work.

Worker, webhook, reconciliation, cleanup, and operator events are logged as
canonical JSON after the `design_processing_event=` prefix. The `metrics`
command returns queue and item-state counts, readiness age/checks, attempt
percentiles, lease health, supersessions, analyzed-not-published count,
publication latency, artifact cleanup state, and webhook child outcomes.
