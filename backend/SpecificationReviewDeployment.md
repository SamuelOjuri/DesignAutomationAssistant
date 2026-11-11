# Specification Review Phase 8 deployment runbook

Prepared: 10 November 2026. This runbook describes deployment and acceptance;
it does not record a completed production rollout.

Use the existing Netlify proxy, existing Monday app and independent
specification rollout settings. Local tests and mocked browser journeys are
implementation evidence; actual allowlisted Monday journeys establish release
acceptance.

## 1. Preserve the working URLs and workflows

Keep the Netlify frontend setting:

```dotenv
NEXT_PUBLIC_FASTAPI_BASE_URL=https://design-automation-assistant.netlify.app
```

Keep the existing registered Monday Redirect URL:

```text
https://design-automation-assistant.netlify.app/auth/monday/callback
```

On the Render API and other relevant backend services, confirm:

```dotenv
MAIN_APP_BASE_URL=https://design-automation-assistant.netlify.app
BACKEND_BASE_URL=https://design-automation-assistant-api.onrender.com
MONDAY_OAUTH_REDIRECT_URI=https://design-automation-assistant.netlify.app/auth/monday/callback
```

`frontend/main_app/netlify.toml` already forwards `/api/*` and `/auth/monday/*`
to Render. The explicit OAuth redirect setting is needed to select the Netlify
callback instead of the backend's default Render callback. The frontend API
base and callback do not need to move to Render.

Retain the existing `DESIGN_PROCESSING_MODE=enabled` policy for normal AI Data
CSV, AI Data Preview PDF and matched-project report output. Retain existing
design worker/reconciliation controls, activation timestamp and `AUTO_SYNC_*`
ingestion configuration. Specification rollout does not require changing them.

## 2. Prepare one matching release and verify it locally

Deploy the same intended source revision to API, design/extraction,
reconciliation and specification publisher, together with its matching frontend.
Record the revision and the pilot item IDs in the release record.

From the repository root on Windows:

```powershell
venv/Scripts/python.exe -m pytest backend/tests/test_specification_rollout.py backend/tests/test_specification_rollout_independence.py backend/tests/test_specification_rollout_fencing.py backend/tests/test_specification_review_auth.py backend/tests/test_specification_review_workspace.py backend/tests/test_specification_phase6_integration.py -q
```

Run the existing affected specification/design/ingestion checks described in
[the implementation plan](PerformanceSpecificationReviewPlan.md#177-existing-checks-and-execution-notes).
Run PostgreSQL migration/concurrency tests against a disposable configured
`TEST_DATABASE_URL`, not production. See
[API verification](SpecificationReviewAPI.md#verification).

From `frontend/main_app`:

```powershell
npm run test:specification-review
npm run test:task-polling
npm run test:chat
npm run build
```

Repeat the [mocked browser journeys](SpecificationReviewFrontend.md#local-verification)
when frontend behavior has changed. These do not replace deployed acceptance.

## 3. Start with specification work disabled

Set the following consistently on the Render API, specification publisher and
design/reconciliation processes that enqueue specification work:

```dotenv
PERFORMANCE_SPECIFICATION_MODE=off
PERFORMANCE_SPECIFICATION_ENABLED=false
PERFORMANCE_SPECIFICATION_REVIEW_ENABLED=false
PERFORMANCE_SPECIFICATION_ALLOWLIST_ITEM_IDS=[]
PERFORMANCE_SPECIFICATION_REVIEW_SESSION_TTL_SECONDS=900
```

Optionally configure `PERFORMANCE_SPECIFICATION_REVIEW_MONDAY_ACCOUNT_ID` with
the actual numeric account ID. Version 1 needs no reviewer-user allowlist or
assigned reviewer role.

The new specification mode defaults to `off`, including upgrades with an
existing true master flag. It never inherits the design mode/list. Use a shared
Render environment group if suitable, and check individual service overrides.
Save and deploy environment changes to each affected service; saving alone does
not apply them to the running process. See
[Render environment variables](https://render.com/docs/configure-environment-variables).

Drain or stop replaced specification publishers before enabling the new policy.
Old processes retain their former configuration. Existing design and ingestion
services keep their own operating settings during their normal release restart.

## 4. Apply the additive migration

Using the release code and intended deployment database, run from repository root
in a Render shell or the existing migration/pre-deploy process:

```bash
python -m alembic -c backend/alembic.ini current
python -m alembic -c backend/alembic.ini heads
python -m alembic -c backend/alembic.ini upgrade head
python -m alembic -c backend/alembic.ini current
```

The current source head is `0020_monday_item_availability`, following
`0019_specification_review`. Apply 0020 before starting the updated backend
services: it adds the shared worker availability table. See
[Monday worker availability](MondayWorkerAvailability.md) for the common retry
settings, lifecycle recovery and operator retry commands. Independent specification
rollout controls retain their existing behavior.

On Windows use `venv/Scripts/python.exe` instead of `python`. The command writes
the database selected by the deployment environment. Keep the additive tables
in place during rollback. Render supports database migration commands in the
[pre-deploy step](https://render.com/docs/deploys) on supported service plans.

## 5. Deploy the API, frontend and worker code

Keep the existing API, extraction and reconciliation service commands, dependency
installation and required build tooling. Deploy their matching release versions.
The normal extraction worker command is:

```bash
python -m backend.app.services.design_processing_worker
```

Maintain the existing reconciliation schedule, such as the combined command:

```bash
python -m backend.app.services.monday_reconciliation
```

Broad design reconciliation requires the existing timezone-aware
`DESIGN_PROCESSING_ACTIVATION_TIMESTAMP`. Preserve its intended boundary.

Add or update a separate Render background worker for specification PDFs, using
repository root as its working directory and the existing backend dependency
build. Its start command is:

```bash
python -m backend.app.services.performance_specification
```

This worker exits while specification processing is disabled; start/redeploy it
when entering shadow or allowlist mode. API enablement does not start this worker.
[Render background workers](https://render.com/docs/background-workers) run
continuously without serving incoming HTTP requests.

Deploy `frontend/main_app` to the existing Netlify site with its existing Next.js
build integration and the API base in Step 1. Preserve the existing API/OAuth
rewrites and chat edge function. Rebuild the frontend when its public build-time
environment values change.

Check that `/specification-review` loads. It needs Monday SDK identity inside
the item view; opening the standalone URL is not an authenticated review test.

## 6. Verify runtime dependencies and source configuration

From the deployed backend repository root:

```bash
python -m backend.scripts.check_design_processing_sources
python -m backend.scripts.check_design_processing_gemini
```

These commands check synthetic source handling and report runtime configuration.
The Gemini command makes no model request unless `--live` is added; optional
`--live` performs a small synthetic model compatibility request.

The source diagnostic must report five passing checks, including same-Email-column
ZIP intake. ZIP files uploaded beside `.eml`/`.msg` files in the Email column are
now included automatically as supplementary extraction sources. An email remains
required; other standalone Email-column file types remain ignored. No
`DESIGN_PROCESSING_SOURCE_FILE_COLUMN_IDS` entry is needed for these ZIPs, and the
Email column must remain excluded from that setting. Existing ZIP size/nesting
bounds and document exclusions remain in effect.

Deploy this intake change consistently across the API, design worker,
reconciliation and specification worker. It retains `output-v18`; the newly
included ZIP metadata changes affected items' input revisions. Existing eligible
items can be inspected and queued using the item-scoped reconciliation commands
below. Keep `--dry-run` for inspection; the normal command can queue the existing
design workflow and its configured outputs. Email-only items need no global
pipeline-version bump for this change.

Confirm the services share the correct database, private artifact storage,
existing Gemini configuration and compatible extraction identity. Current
extraction uses the full configured `output-v18` pipeline and evidence version 3;
the publication contract is `performance-specification-v5`. Compare the full
pipeline identity, not just its suffix.

Verify the actual `DESIGN_PROCESSING_BOARD_ID`,
`DESIGN_PROCESSING_LANDING_GROUP_ID`, `DESIGN_PROCESSING_PROJECT_BOARD_ID` and
`AUTO_SYNC_COMPLETED_GROUP_ID`. Verify readable CRM field IDs in
`PERFORMANCE_SPECIFICATION_COLUMN_MAP` as needed. These are Monday source column
IDs; accepted CAD dropdown values are in `performance_specification_contract.py`.
Any downstream CAD compatibility acceptance requires its authoritative values
and importer checks; PDF completion alone does not establish that compatibility.

Confirm the existing backend processing identity can read necessary source,
project/drawing, Account and Contact records and attach specification PDFs to
the holding board's AI Data file column. Keep the private artifact bucket
accessible through the backend storage identity.

## 7. Verify Monday OAuth, the Item View and embedding

In the existing Monday app's Developer Center, confirm the app client identity
matches the backend. Keep the callback from Step 1 under OAuth & Permissions.
The callback must exactly match `MONDAY_OAUTH_REDIRECT_URI`; see
[Monday OAuth configuration](https://developer.monday.com/apps/docs/oauth).

Confirm reviewer OAuth has `me:read`, `account:read` and `boards:read`, retaining
other permissions required by existing workflows. Reauthorize users if granted
permissions need updating. User OAuth proves live user/item access; the backend
processing token does not substitute for it.

Create or confirm a separate Item View named **Specification Review**, targeting:

```text
https://design-automation-assistant.netlify.app/specification-review
```

Make its app version available to the test account through the existing app
version/installation process. Add the view from a Projects Holding Page item's
view selector. Keep the existing design-team launcher as its separate feature.
Feature and permission configuration live in the
[Developer Center](https://developer.monday.com/apps/docs/the-developer-center).

Check the final hosted route's CSP allows Monday parents and same-origin/blob
PDF frames. Resolve conflicting host-level `X-Frame-Options` or additional CSP
for this route. Test PDF iframe, open and download behavior in Monday's actual
browser sandbox and with third-party cookies blocked. Preserve existing CORS
settings; the accepted frontend API base uses same-origin Netlify requests.

## 8. Audit dependency webhooks

Use [specification_source_webhooks.graphql](scripts/specification_source_webhooks.graphql)
to audit existing subscriptions before creating only the missing ones against
the existing authenticated webhook endpoint.

- Holding inputs and lifecycle changes use the existing holding-board events.
- Project/Account/Contact/mirror-source boards need their relevant field/name and
  lifecycle notifications.
- Drawing/subitem subscriptions belong on the **parent project board**, not the
  hidden subitem board. The template includes create/change/name/move/archive/
  delete subitem events.
- Restored drawings and missed events are repaired by periodic source checks;
  do not invent a `subitem_restored` subscription.

Subscription creation is an explicit deployment action; application startup does
not provision it. Keep webhook authentication configured and verify a relevant
source change reaches the publisher. Generated output changes must not produce
an endless regeneration loop.

## 9. Verify private rendering with specification shadow

Apply to the same affected backend services, then deploy the new environment
values and start/redeploy the specification worker:

```dotenv
PERFORMANCE_SPECIFICATION_ENABLED=true
PERFORMANCE_SPECIFICATION_REVIEW_ENABLED=true
PERFORMANCE_SPECIFICATION_MODE=shadow
PERFORMANCE_SPECIFICATION_ALLOWLIST_ITEM_IDS=[]
```

Keep normal design outputs `DESIGN_PROCESSING_MODE=enabled` and existing
`AUTO_SYNC_*` settings in place. The master specification flag must be true even
for private rendering.

**Shadow processes all otherwise eligible registered specifications and ignores
the specification allowlist.** Account for the resulting private rendering/model
workload. Use a suitably isolated test corpus/environment if private testing
must be confined to a few items; an item CLI argument does not isolate shadow.

In the actual Item View, connect Monday OAuth if prompted, return to the embedded
tab and choose **I've connected — resume review**. Check New Enquiry, Amendment
and ambiguous/multiple-roof cases. Edit/save, inspect the private PDF and source
labels, and test failure/source-change behavior. Shadow must create no Monday
specification attachment and no completion. Existing design CSV/preview/report
publication continues under its own policy.

## 10. Enable only the pilot specification items

Select a New Enquiry and an Amendment in Landing Zone. Set the following using
their actual positive numeric item IDs:

```dotenv
PERFORMANCE_SPECIFICATION_ENABLED=true
PERFORMANCE_SPECIFICATION_REVIEW_ENABLED=true
PERFORMANCE_SPECIFICATION_MODE=allowlist
PERFORMANCE_SPECIFICATION_ALLOWLIST_ITEM_IDS=["1234567890","1234567891"]
```

The IDs above are examples. The list must be nonempty in allowlist mode. Deploy
matching values across the API, publisher and specification enqueuers/reconcilers.
Do not change the independent design allowlist merely to limit specifications.

If a pilot lacks current extraction, first inspect its existing design workflow:

```bash
python -m backend.app.services.design_processing_reconciliation --dry-run --item-id 1234567890
python -m backend.app.services.design_processing_reconciliation --item-id 1234567890
```

The second command can queue the normal design workflow, including its existing
outputs/scalar updates when design processing is enabled. Wait for matching
current extraction. It does not need a Task/chat snapshot to open review.

The running specification publisher revisits pending work. To enqueue a prompt
scope/source check for an already registered pilot, optionally use:

```bash
python -m backend.app.services.performance_specification --once --item-id 1234567890
```

This queues that item, then runs normal discovery and a due-item claim. It may
process another admitted due item; it is not an item-exclusive execution command.
The specification allowlist bounds the candidate set. It cannot create missing
extraction. Matching cached shadow artifacts/translations can be reused.

## 11. Complete the deployed acceptance journeys

For the New Enquiry, verify an empty holding TP Ref is supported, choose and save
the request type, and keep Drawing Reference/Revision unassigned unless explicitly
specified. For the Amendment, verify the detected reference, project/drawing
choices, email-first values, missing-value CRM fallback and Account disagreement.

For both pilot items:

1. Inspect all 18 fields, original evidence, current values and mapping limitations.
2. Accept populated recommendations or specify authoritative values/blanks, and
   address required source issues.
3. Save corrections. Confirm saved/pending state and regeneration, then inspect
   the exact current **published** PDF in the Item View and AI Data column.
4. Verify manual values/blanks have **Source: Reviewer Specified**; accepted
   recommendations retain their actual source. PDF text should be selectable.
5. Check the final-PDF acknowledgement and mark the specification complete.
6. Record item ID, review version, PDF/artifact revision, actor and approval time.
7. Move the item manually to an active group. Confirm retained PDF/history access
   and disabled reviewer edits/approval. Confirm normal active-group ingestion.

On designated pilot items also verify unlisted-item read-only access, stale saves
between two users, expired-session recovery with drafts kept, item-context
switching, source changes reopening review, early group moves, and failed
regeneration retaining the prior PDF. Check keyboard access, narrow layout,
blocked third-party cookies and PDF open/download fallbacks.

Attribute mutations to the correct workflow. Reviewer saves/approval must not
write Monday business columns, shared project/drawing values or group membership.
The independently enabled existing design worker may continue its normal output
and owned scalar writes; those are separate from reviewer commands. Confirm CSV,
preview PDF, matched-project report and active chat/ingestion still work.

## 12. Inspect diagnostics and decide whether to expand

Run the read-only [diagnostic SQL](scripts/diagnose_performance_specifications.sql)
with `scope_settings` set to the deployed board/group IDs and full expected
pipeline version. Check requested/completed generations, leases, last errors,
review/source/decision identities, current artifact status/asset and exact
approval matching. SQL observations do not replace a live Monday eligibility
or final-PDF check.

Record the two successful end-to-end journeys, provenance and lifecycle results,
source waiting/failures, regeneration latency and any unresolved mapping issues.
Routine logs should contain operational identities/outcomes, not email text,
model bodies, bearer tokens or signed URLs.

Expand only `PERFORMANCE_SPECIFICATION_ALLOWLIST_ITEM_IDS` in small batches after
acceptance. Deliberately switch `PERFORMANCE_SPECIFICATION_MODE=enabled` only
when all otherwise eligible registered Landing Zone items should participate.
Preserve activation boundaries. Backfill historical items explicitly in bounded
batches, with current extraction as the prerequisite. Retain historical and
active/completed PDFs and approval history.

## 13. Roll back specification work independently

Apply matching values to the affected services and deploy them:

```dotenv
PERFORMANCE_SPECIFICATION_MODE=off
PERFORMANCE_SPECIFICATION_REVIEW_ENABLED=false
PERFORMANCE_SPECIFICATION_ENABLED=false
```

This stops new specification work and makes review read-only. Retain review
decisions, audit, approvals, artifact ledger and attached PDFs. Keep migration
0019 and later additive schema in place; do not downgrade under running services
or restart the old publisher against the new artifact contract. Existing design
and auto-sync controls retain their accepted settings.

For a temporary publisher pause with permitted edits, set only the specification
master flag false while keeping an appropriate non-off mode and review enabled.
Saved decisions can then remain pending, with completion unavailable until their
matching PDF is actually published.
