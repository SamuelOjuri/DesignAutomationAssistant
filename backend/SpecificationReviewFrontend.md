# Specification Review frontend (Phase 7)

The dedicated `/specification-review` route in `frontend/main_app` implements the
Monday reviewer workspace against the [Phase 6 API](SpecificationReviewAPI.md).
It opens without the active-project task page, chat, ingestion or a Supabase
login. The source implementation is separate from registering the deployed Item
View and proving its behaviour inside the live Monday browser. Those deployment
and explicitly allowlisted checks remain Phase 8 work.

## Implementation

| Location under `frontend/main_app` | Responsibility |
| --- | --- |
| `app/specification-review/page.tsx` | Dedicated route, page title and no-index metadata. |
| `components/specification-review/ReviewWorkspace.tsx` | Item-scoped bootstrap, draft ownership, polling, save/refresh/approval commands, conflict recovery, history and authenticated PDF lifecycle. |
| `components/specification-review/FieldEditor.tsx` | All 18 PDF fields: saved effective value/source, email facts, CRM recommendations/evidence, issues, mapping notes and distinct reviewer operations. |
| `components/specification-review/CandidateSelector.tsx` | Cited AI request-type recommendation and internal request/project/drawing choices, retaining separate roof candidates. |
| `components/specification-review/PdfPreview.tsx` | Published/private preview state, authenticated blob iframe and open/download fallbacks. |
| `components/specification-review/ReviewStatus.tsx` | Business-facing review/publication state. |
| `components/specification-review/review.css` | Scoped focus, skip-link and narrow-layout styling. |
| `lib/specification-review-api.ts` | Phase 6 types, private fetches and a bearer client that cannot be rebound to another item. |
| `lib/specification-review-state.ts` | Draft validation, exact identities, command construction, safe status merging, conflict rebasing and completion blockers. |
| `lib/specification-review-monday.ts` | Official SDK loading, context/session reads, context subscription, cancellation/timeouts and top-level OAuth opening. |

The editor offers **Use recommendation**, **Specify value**, **Specify blank**
and **Restore recommendation** as separate commands. Manual display text is
preserved, including custom values without a CAD mapping. Reviewer values and
intentional blanks have the PDF source label **Reviewer Specified**; accepting a
recommendation retains its email/CRM provenance. All decisions target the review
API and do not write Monday business columns, shared project/drawing values or
group membership.

Save corrections regenerates the PDF. Completion remains disabled while edits
are unsaved, required decisions or source dispositions are outstanding, the
current matching PDF is pending/failed, or its protected preview has not loaded
and been checked. The approval command identifies the exact artifact revision,
review version, source fingerprint, decision digest and publication identity.
The server revalidates them and remains authoritative.

Polling updates publication/access state without replacing unsaved text. A
source or concurrent-review change requires explicitly loading the latest
context. That action keeps manually entered values and selections while asking
the reviewer to accept changed recommendations and acknowledge source issues
again. Failed requests and session renewal retain the draft for the same item;
mutation retries reuse their idempotency key. An item-context change aborts old
work, destroys its bearer and clears item-bound drafts/previews. Invalid context
also clears prior-item access. Leaving Landing Zone applies read-only
capabilities while keeping authorized retained-PDF access.

## Authentication and embedding

The view reads account, board and item identity through the Monday SDK and posts
its session token to `/api/specification-review/session`. The short-lived review
bearer exists only in memory and is sent in the Authorization header. OAuth
opens through the SDK and the view bootstraps again on return; URL parameters
and cookies do not grant review access.

`proxy.ts` excludes this route from Supabase session refresh. `next.config.js`
sets route-specific Monday frame ancestors, same-origin/blob PDF frames,
no-referrer and private-response headers. Existing routes retain their previous
behaviour. See the separate
[Item View setup](../frontend/monday_app/SpecificationReview.md) and
[configuration checklist](../frontend/monday_app/specification-review.item-view.json)
for the deployed iframe URL, CORS and OAuth setup. No production feature flag or
reviewer-user configuration is changed by Phase 7.

## Local verification

From `frontend/main_app`:

```powershell
npm run test:specification-review
npm run test:task-polling
npm run test:chat
npm run build
```

The reviewer helper command runs the API, state and Monday SDK test files. They
check item-bound headers and cancellation, stable errors without token/body
disclosure, command identities/idempotency input, draft preservation and
rebasing, field operations, completion blockers and embedding configuration.
They use local fixtures and do not contact Monday, a model or storage.

For the mocked browser journeys, start the real application in one terminal:

```powershell
npm run dev -- --hostname 127.0.0.1 --port 3107
```

In a second terminal, start the harness:

```powershell
npm run test:specification-review:browser
```

Open `http://127.0.0.1:3108`. This command starts a browser harness server; it does
not itself launch a browser. Click **Run browser journeys** to execute repeatable
assertions against the rendered React controls. Results appear in the page and
at `/__specification-review-browser-results` on the local harness. The harness proxies the real
review route from port 3107 and injects mock SDK/API behaviour before React
loads. Its target is restricted to localhost and it binds only to loopback.
`SPECIFICATION_REVIEW_HARNESS_TARGET` and
`SPECIFICATION_REVIEW_HARNESS_PORT` can change those local defaults. Fixtures and
controls are under `tests/specification-review-fixture.mjs` and
`tests/specification-review-harness.mjs`; application code imports neither.
There is no production demo or test-auth route.

Use the controls to exercise these journeys:

1. **New enquiry:** no TP Ref/project is needed. Choose the request type, check
   all 18 editors, accept populated recommendations and try manual values,
   intentional blanks and restoring recommendations. Save, verify completion
   is pending, publish the matching PDF from the harness, check it and complete.
2. **Amendment / detected reference:** inspect the cited reference, email-first
   technical value and differing project Account recommendation. Select the
   project and existing drawing, save, inspect the regenerated PDF and complete
   after resolving the recommendations.
3. **Ambiguous roofs:** keep both drawing/roof choices visible and require an
   explicit relevant selection before completion.
4. **Failures and stale edits:** trigger regeneration failure, conflicting save,
   source change and session expiry. Confirm the previous PDF remains available,
   drafts survive, stale approval is disabled and latest-context recovery does
   not silently accept changed recommendations.
5. **Lifecycle and access:** switch items and confirm credentials/drafts/previews
   belong to the new item. Leave Landing Zone and confirm all edit/approve
   controls are disabled while the retained document remains accessible.
6. **Accessibility:** use keyboard navigation and the skip link, inspect visible
   focus, labels/legends, evidence disclosure controls and errors, then test a
   narrow viewport for readable cards and no horizontal page overflow.

The harness's **Inspect requests** control exposes method/path and command
identities without credentials. Confirm mutations use only the review endpoints
and approval references the current published revision. The harness simulates
backend outcomes; backend authorization, storage integrity and PostgreSQL
concurrency are verified by their existing suites, not by these browser mocks.

Verification on 9 November 2026: all 28 reviewer helper tests, the existing 6
task-polling and 19 chat tests, 149 review API/authentication regressions and 433
existing specification regressions passed. The production Next.js build passed.
All 50 local Chrome journey assertions also passed; these are mocked frontend checks, not
live Monday publication or authorization acceptance.

Independent-rollout verification on 10 November 2026 passed the configuration,
policy, authorization and approval-fencing suites, 29 integration checks for
independent design/specification/ingestion behavior, and five worker rollout
fencing checks. The affected design-output, ingestion and specification
regressions passed, as did all 15 isolated PostgreSQL migration/concurrency and
lifecycle tests with no skips. Existing frontend reviewer, task-polling and chat
helper tests also passed. These checks did not enable production rollout.

## Independent specification controls

The specification workflow has its own operating mode and item allowlist. It
does not inherit `DESIGN_PROCESSING_MODE` or
`DESIGN_PROCESSING_ALLOWLIST_ITEM_IDS`. Existing design CSV, preview PDF and
matched-project report outputs can remain `DESIGN_PROCESSING_MODE=enabled`
throughout specification rollout. The existing `AUTO_SYNC_*` controls continue
to govern document/chat ingestion independently.

| Control | Effect |
| --- | --- |
| `PERFORMANCE_SPECIFICATION_MODE=off` | Default, including on upgrade. Stops specification processing and makes review read-only; retained authorized documents remain available. |
| `PERFORMANCE_SPECIFICATION_MODE=shadow` | Permits private specification rendering when its master processing flag is true. Review edits are available when separately enabled; specification publication and approval are unavailable. |
| `PERFORMANCE_SPECIFICATION_MODE=allowlist` | Restricts specification processing, reviewer edits, publication and approval to listed otherwise eligible items. |
| `PERFORMANCE_SPECIFICATION_MODE=enabled` | Permits those operations for all otherwise eligible items. Landing Zone/lifecycle, source and exact-approval checks still apply. |
| `PERFORMANCE_SPECIFICATION_ALLOWLIST_ITEM_IDS` | Specification-only positive item IDs, supplied as CSV such as `1234567890,1234567891` or JSON such as `["1234567890","1234567891"]`. Defaults to `[]`; must be nonempty in specification `allowlist` mode. |
| `PERFORMANCE_SPECIFICATION_ENABLED` | Master specification processing flag. Must be `true` for rendering, including shadow, publication and approval. Setting it false pauses that work; otherwise permitted saves can remain saved/pending. |
| `PERFORMANCE_SPECIFICATION_REVIEW_ENABLED` | Separate reviewer mutation flag, default false. Does not start a worker or enable publication by itself. |

The item allowlist limits rollout items, not reviewer identities. Version 1
requires no reviewer-user list: authenticated Monday CRM users with live access
to an eligible item follow the same controls. The independent mode defaults to
`off` even if the publisher master flag was already true and design processing
was enabled. Operators must deliberately select the new specification mode.

## Deployed setup and rollout

These are deployment instructions, not changes made to a local `.env` or a live
service. Phase 7 source implementation and local verification are complete;
actual Monday registration, explicitly allowlisted deployed acceptance and
production enablement remain Phase 8.

1. **Deploy in the safe starting state.** Apply migration through
   `0019_specification_review` before deploying code that uses its tables. Drain
   replaced processes and deploy matching API, specification publisher and
   design/reconciliation versions. Start with the specification settings below;
   keep the existing design/ingestion settings appropriate for their own work.

   ```dotenv
   PERFORMANCE_SPECIFICATION_MODE=off
   PERFORMANCE_SPECIFICATION_ALLOWLIST_ITEM_IDS=[]
   PERFORMANCE_SPECIFICATION_ENABLED=false
   PERFORMANCE_SPECIFICATION_REVIEW_ENABLED=false
   ```

   The publisher is a separate running service:
   `python -m backend.app.services.performance_specification`. Ensure the review
   API, publisher and processes that enqueue/reconcile specification work receive
   matching specification settings. Restart all affected processes after each
   mode or allowlist change; leaving an old publisher running retains its old
   policy. Enabling a flag does not start services or provision webhooks.

2. **Configure the deployed app and URL.** Deploy `frontend/main_app` and register
   a separate **Specification Review** Item View in the existing Monday app,
   targeting `https://<deployed-main-app-origin>/specification-review`. Keep the
   design-team launcher as its separate feature. Verify the configured
   `MAIN_APP_BASE_URL`, `BACKEND_BASE_URL`, app client identity/secret and
   `MONDAY_OAUTH_REDIRECT_URI` against the existing OAuth registration. Confirm
   user OAuth can read the holding item and the backend processing identity can
   read required sources and publish to AI Data. Verify shared canonical
   board/Landing Zone IDs; no second specification group setting is needed.

   For a direct API origin, set the frontend's
   `NEXT_PUBLIC_FASTAPI_BASE_URL` and include the exact frontend origin in backend
   `CORS_ALLOWED_ORIGINS`, including support for Authorization headers. Otherwise
   use the existing same-origin Netlify API relay. Check the deployed route's
   final CSP/iframe headers; hosting overrides must not block Monday framing or
   blob PDF frames. Test with third-party cookies blocked.

3. **Verify privately with specification shadow.** Set the following on the
   affected services and restart them together:

   ```dotenv
   PERFORMANCE_SPECIFICATION_ENABLED=true
   PERFORMANCE_SPECIFICATION_MODE=shadow
   PERFORMANCE_SPECIFICATION_ALLOWLIST_ITEM_IDS=[]
   PERFORMANCE_SPECIFICATION_REVIEW_ENABLED=true
   ```

   Keep `DESIGN_PROCESSING_MODE=enabled` if existing CSV, preview and matched-report
   output should continue normally, and leave `AUTO_SYNC_*` unchanged. Shadow
   covers otherwise eligible registered specifications; it is not limited by the
   specification allowlist. Obtain current matching extraction via the existing
   design workflow. For an older item lacking current extraction, run explicit
   item-scoped design reconciliation first; opening review or running the
   specification CLI does not create missing extraction or broadly backfill.

   Check New Enquiry, explicit-reference Amendment and ambiguous/multiple-roof
   cases. Save decisions, inspect private PDFs, provenance, source changes and
   failures. The specification workflow must publish no Monday PDF or approval
   in shadow. The independent design worker may still publish its existing
   outputs under its own enabled policy.

4. **Publish only selected test items.** Deliberately move specification mode to
   `allowlist` and supply a nonempty list of the intended positive Monday item
   IDs. For example, replace these sample IDs with the selected rollout items:

   ```dotenv
   PERFORMANCE_SPECIFICATION_ENABLED=true
   PERFORMANCE_SPECIFICATION_MODE=allowlist
   PERFORMANCE_SPECIFICATION_ALLOWLIST_ITEM_IDS=["1234567890","1234567891"]
   PERFORMANCE_SPECIFICATION_REVIEW_ENABLED=true
   ```

   Restart the review API, publisher and specification enqueuers/reconcilers with
   matching values. Existing shadow renders are revisited on their scheduled
   checks. To publish a selected already registered item immediately, run this
   from the repository root with the same allowlist settings (use `python` in
   the deployed Linux environment):

   ```powershell
   venv/Scripts/python.exe -m backend.app.services.performance_specification --once --item-id <selected-item-id>
   ```

   Unchanged shadow artifacts and translations are reused. This command cannot
   create missing extraction. Verify unlisted items cannot process,
   edit, publish or approve specifications. Do not populate or change the design
   allowlist merely to limit specifications.

5. **Complete deployed acceptance.** From the actual Monday Item View, complete
   an allowlisted New Enquiry and Amendment through correction, save,
   regeneration, inspection and exact-current-PDF approval. Verify all 18 fields,
   Reviewer Specified labels, candidate choices, stale saves, source changes,
   session renewal, item switching, narrow layout and keyboard access. Check PDF
   iframe/open/download behaviour in Monday's actual sandbox. Confirm reviewer
   commands make no Monday business-field/project/drawing/group mutations. Move
   the item manually and verify retained read-only document access afterward.

6. **Expand deliberately or roll back independently.** After acceptance, expand
   the specification allowlist; use `PERFORMANCE_SPECIFICATION_MODE=enabled` only
   when all otherwise eligible activation-bounded Landing Zone items should
   participate. Historical backfill remains explicit. For rollback, set
   specification mode `off`, reviewer mutations false and master processing
   false, then restart the affected services. Keep existing decisions, approvals,
   artifacts and attached files; retain the additive database schema. Existing
   design output and ingestion settings need not change.

If processing is paused after a save, the UI retains the saved/pending state and
completion stays unavailable until the exact matching PDF is published. A master
flag pause alone can retain permitted editing; use specification mode `off` or
the review mutation flag when edits must also stop. Local helper tests and
mocked browser journeys do not establish that deployed acceptance has happened.
