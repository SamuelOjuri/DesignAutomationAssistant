# Design Automation Assistant

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
must include a timezone. The initial pipeline version is derived from the full
legacy manifest digest and the separately pinned `gemini-2.5-flash` extraction
model.
