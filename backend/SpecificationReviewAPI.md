# Specification Review API (Phase 6)

Phase 6 exposes the review persistence, source policy and publication ledger
through a direct API. It opens independently of active-project ingestion, chat,
`Task` and `TaskSnapshot`. Phase 7 implements the separate
[embedded Monday workspace](SpecificationReviewFrontend.md); actual Item View
registration and allowlisted deployed verification remain Phase 8.

## Version 1 identity and capabilities

Any authenticated Monday CRM user in the configured account with live access to
the holding item has reviewer authorization. No reviewer role or approved-user
list is required. Explicit reviewer-role authorization is deployment version 2.

The view posts its Monday SDK `sessionToken` and `context` (`accountId`, `boardId`,
`itemId`) to `POST /api/specification-review/session`. The backend verifies the
SDK signature and expiry, resolves the exact user's server-side OAuth link, and
reads both the live OAuth identity and item using that user's access token. The
actual board and account must match. A backend processing token never proves
human permission.

The response has `status: ready`, an `access_token`, `token_type: Bearer`,
`expires_at`, and `capabilities` (`read`, `edit`, `approve`). Keep the bearer only
in browser memory and send it in the `Authorization` header. It is bound to one
account/user/board/item and has a distinct issuer, audience and signing key from
SDK, webhook and ordinary app sessions. Every request rechecks the OAuth link,
live user identity and item access. Newly granted capabilities require bootstrap
again; an existing read-only bearer cannot acquire edit access by itself.

If OAuth is missing or expired, bootstrap returns `status: oauth_required` and
an `authorization_url`. Open that URL through a top-level authorization flow.
The signed review-specific return context binds account, user, board and item.
The OAuth callback verifies that identity and item access, stores the user link
and returns to `/specification-review?boardId=...&itemId=...`. Resume the embedded
view and bootstrap again. This flow does not create handoffs, Tasks, ingestion
jobs or an ordinary app cookie session.

## Controls

| Setting | Behaviour |
| --- | --- |
| `PERFORMANCE_SPECIFICATION_REVIEW_ENABLED` | Default `false`; controls reviewer mutations. |
| `PERFORMANCE_SPECIFICATION_REVIEW_SESSION_TTL_SECONDS` | Default 900; allowed range 60–3600 seconds. |
| `PERFORMANCE_SPECIFICATION_REVIEW_MONDAY_ACCOUNT_ID` | Optional account pin; configured board and verified OAuth identity are always checked. |
| `PERFORMANCE_SPECIFICATION_ENABLED` | Master specification processing flag; must be `true` for rendering even in shadow, publication and approval. A paused publisher can leave reviewer decisions saved/pending. |
| `PERFORMANCE_SPECIFICATION_MODE` | Independent `off` / `shadow` / `allowlist` / `enabled` specification policy, default `off`. Never inherits `DESIGN_PROCESSING_MODE`. `off` is read-only; `shadow` allows edits/private PDFs but no publication or approval; `allowlist` admits only listed items; `enabled` admits all otherwise eligible items. |
| `PERFORMANCE_SPECIFICATION_ALLOWLIST_ITEM_IDS` | Specification-only positive Monday item IDs, as CSV or a JSON array; default `[]`, and must be nonempty in specification `allowlist` mode. Governs specification processing, edits and approval without limiting design outputs. |
| `DESIGN_PROCESSING_MODE` / `DESIGN_PROCESSING_ALLOWLIST_ITEM_IDS` | Independent design CSV/preview/matched-report policy. It can remain `enabled` while specification review uses `off`, `shadow` or its own item allowlist. |
| `MONDAY_INGESTION_ACCESS_TOKEN` | Existing backend source/publication identity; separate from the reviewer's OAuth access. |

The canonical design-processing board/Landing Zone settings and Completed Folder
exclusion remain shared with the publisher. Moving an item out of Landing Zone
removes edit and approval capabilities while retaining authorized PDF access.
Pausing publication can leave decisions saved and pending; completion requires
their successfully published PDF. No reviewer-user-ID setting is needed in V1.
API enablement starts no workers and provisions no webhooks. Apply migration
`0019_specification_review` before deploying these routes.

Upgrades default the new specification mode to `off`, even when the existing
publisher flag is `true` and design processing is `enabled`. Set the specification
mode deliberately and deploy/restart the review API, publisher and specification
enqueue/reconciliation processes with matching values. Keep the existing
`AUTO_SYNC_*` ingestion controls unchanged. See the
[deployed setup and rollout guide](SpecificationReviewFrontend.md#deployed-setup-and-rollout)
for staged examples; no environment or production enablement is performed by
adding these settings.

## Endpoints

Item base: `/api/specification-review/items/{board_id}/{item_id}`.

| Method/path | Result |
| --- | --- |
| `POST /api/specification-review/session` | Bootstrap identity/capabilities or request OAuth. |
| `GET base` | All 18 field proposals/evidence/decisions/effective values, candidates, versions, queue, PDF, approval and recent history. |
| `PUT base/decisions` | Atomically save classification, selections, field operations and source dispositions. |
| `POST base/refresh` | Refresh recommendations while preserving decisions; record an idempotent command receipt. |
| `GET base/candidates` | Current bounded candidates and review/source versions. |
| `GET base/status` | Lightweight publication/completion polling; no recommendation lookup. |
| `GET base/pdf/{revision}` | Authenticated PDF bytes from the scoped artifact ledger with size/SHA-256 verification. |
| `POST base/approve` | Approve the exact current published PDF. |

Opening an eligible, already registered item can initialize its review from
current matching extraction. Unknown items report `source_waiting`; GET does not
register historical items or start extraction/backfill. Read-only views use
retained snapshots and documents. Relevant live source changes preserve saved
decisions, invalidate current completion and queue regeneration.

Decisions, refresh and approval require `expected_review_version`,
`expected_source_fingerprint` and `idempotency_key`. Approval additionally requires
`artifact_revision`, `expected_decision_digest` and
`expected_publication_identity`. Identical retries return the original receipt;
reusing a key for another command conflicts. Actors, audit attribution and source
labels are server-authored. Unknown/forged actor, provenance or CAD-mapping fields
are rejected at the typed request boundary.

Remote access/source/storage reads occur outside database transactions. The final
save/refresh/approval checks lock specification then review rows and compare the
observed identities before committing. Approval verifies retained PDF bytes and
the unique exact Monday AI Data asset, refreshes sources, then checks the current
artifact, decisions and absence of newer queued/running work under lock. An
approval event generates no PDF. Reviewer routes perform no Monday business-field,
project, drawing or group mutations.

Private responses use `Cache-Control: private, no-store`; PDFs also use
`nosniff` and an inline, sanitized filename. Validation errors omit submitted
credentials. Use configured frontend CORS origins; iframe/CSP and preview browser
verification are part of Phase 7.

Errors have `detail.code` and `detail.message`: `authorization_expired` (401),
`reviewer_forbidden` (403), stale review/source/scope or idempotency conflicts
(409), `source_waiting` (202), `source_unavailable` (503), and
`publication_failed` (502). Invalid bodies return 422 without echoing token input.
The workspace can return a waiting/failure state alongside a retained PDF so the
reviewer can still inspect the previous document.

## Verification

From the repository root:

```powershell
venv/Scripts/python.exe -m pytest backend/tests/test_specification_review_auth.py backend/tests/test_specification_review_api.py backend/tests/test_specification_review_workspace.py backend/tests/test_specification_phase6_integration.py backend/tests/test_monday_first_auth.py -q
```

With `TEST_DATABASE_URL` pointing to a disposable PostgreSQL database containing
pgvector, also run the PostgreSQL suites. They migrate isolated schemas and do
not contact Monday, a model or storage:

```powershell
venv/Scripts/python.exe -m pytest backend/tests/test_specification_review_api_postgres.py backend/tests/test_specification_review_postgres.py backend/tests/test_specification_review_postgres_concurrency.py -q
```

SQLite tests verify API and orchestration behaviour; PostgreSQL suites establish
actual row-lock, uniqueness and competing-transaction guarantees. Live Monday
embedding and allowlisted New Enquiry/Amendment journeys remain later release
verification.

PostgreSQL verification on 9 November 2026: all 15 tests passed with no skips
across the review migration, review persistence concurrency, review API
concurrency and Phase 5 specification lifecycle suites. The fixtures used the
configured test database and cleaned up their isolated UUID schemas. The shared
concurrency fixture uses a schema name within PostgreSQL's 63-byte identifier
limit, preserving the complete UUID for isolation.
