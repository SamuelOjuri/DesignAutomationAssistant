## Plan: Monday-First Authentication Upgrade

Upgrade the authentication plan so Monday OAuth becomes the primary identity proof for Monday-launched users. Supabase remains the database, pgvector, and Storage provider, but Supabase Auth/email-password should no longer be required for the Monday handoff path. The backend should create an internal app user from verified `monday_account_id + monday_user_id` and issue a secure HTTP-only app session cookie.

**Steps**
1. Update `c:/Users/SamuelOjuri/OneDrive - Tapered Plus/Documents/CodeProjects/DesignAutomationAssistant/backend/AutheticationPlan.md` to revise the authentication architecture: replace Supabase Auth/email-first onboarding language with Monday-first identity and backend session-cookie language.
2. Amend the plan goals and key implementation details to state that email is optional metadata, optional secondary login, optional notification/account-recovery data, and not required for Monday-first onboarding.
3. Revise Step 2 and Step 3 of the plan so the handoff path is: Monday Item View creates handoff code, main app opens `/monday-handoff/{code}`, backend starts Monday OAuth if no app session exists, callback verifies OAuth identity against the handoff code, backend creates/finds an app user, sets an HTTP-only session cookie, then resolves handoff.
4. Add a new identity/session data model phase: create `app_users`; change `user_monday_links.target_user_id` toward `app_user_id`; store nullable `monday_email` and `monday_user_name`; enforce `unique(monday_account_id, monday_user_id)`; keep `handoff_codes` tied to Monday account/user/board/item.
5. Add a backend auth implementation phase: replace `get_current_user` as Supabase-JWT-only for Monday-first routes with a session-cookie-aware dependency; add signed OAuth state containing `handoff_code`, `return_to`, `mode=monday_first`, nonce, and expiry; add session creation/logout helpers; configure secure cookie flags.
6. Add a Monday OAuth callback phase: exchange OAuth code, call Monday `me { id name email account { id } }`, require `id` and `account.id`, treat `email` as nullable, compare OAuth account/user with the stored handoff code, upsert the Monday token/link, and set the app session cookie.
7. Add a handoff resolve phase: resolve using the backend app session instead of a Supabase bearer token; keep one-time code, expiry, user/account mismatch rejection, `can_read_item`, and background sync behavior.
8. Add a frontend integration phase: remove Supabase bearer-token dependency from `/monday-handoff/{code}`, `/tasks/{externalTaskKey}`, signed-url, sync, and chat calls; use `credentials: "include"`; redirect unauthenticated Monday handoffs to backend Monday OAuth start; keep email/password pages only as optional secondary login surfaces.
9. Add security/ops updates: fix or verify Monday session-token verification secret usage (`MONDAY_SIGNING_SECRET` vs current `MONDAY_CLIENT_SECRET`), tighten CORS to configured frontend origins when cookies are used, set `SameSite=None; Secure` for cross-site deployed cookie flows, add CSRF protection for state-changing endpoints, add session expiry/logout/revocation strategy, and keep audit trails.
10. Add CAD traceability guidance: CAD outputs should link to task, Monday account/board/item, app user, Monday user, source snapshot, generated parameter payload, CAD job/export metadata, and timestamps rather than relying on email.
11. Update tests listed in the plan: backend tests for Monday-first OAuth state/callback, account/user mismatch rejection, cookie session dependency, handoff resolve without Supabase JWT, task/chat/signed-url access with cookie session, and frontend tests or manual checks confirming no email login prompt blocks the Monday handoff.
12. Preserve the wider MVP plan for sync, extraction, retrieval, storage, and purge; only change the auth/session portions unless execution later discovers a direct dependency.

**Relevant files**
- `c:/Users/SamuelOjuri/OneDrive - Tapered Plus/Documents/CodeProjects/DesignAutomationAssistant/backend/AutheticationPlan.md` — update the written plan to reflect Monday-first identity, optional email, backend session cookies, revised handoff/OAuth flow, schema changes, security details, and verification steps.
- `c:/Users/SamuelOjuri/OneDrive - Tapered Plus/Documents/CodeProjects/DesignAutomationAssistant/backend/app/auth.py` — current app auth is Supabase JWKS bearer-token validation; future implementation should add or replace with backend session-cookie auth for Monday-first routes.
- `c:/Users/SamuelOjuri/OneDrive - Tapered Plus/Documents/CodeProjects/DesignAutomationAssistant/backend/app/routes/monday_auth.py` — current Monday OAuth requires `CurrentUser`; future implementation should allow handoff-code-started Monday OAuth and issue app sessions.
- `c:/Users/SamuelOjuri/OneDrive - Tapered Plus/Documents/CodeProjects/DesignAutomationAssistant/backend/app/routes/monday_handoff.py` — current init stores Monday account/user from iframe token; future resolve should use backend app session instead of Supabase bearer JWT while keeping mismatch and access checks.
- `c:/Users/SamuelOjuri/OneDrive - Tapered Plus/Documents/CodeProjects/DesignAutomationAssistant/backend/app/models.py` — current `UserMondayLink` stores Monday account/user under `target_user_id`; future schema should add `AppUser`, `app_user_id`, nullable metadata, and uniqueness on Monday identity.
- `c:/Users/SamuelOjuri/OneDrive - Tapered Plus/Documents/CodeProjects/DesignAutomationAssistant/backend/migrations/versions/0002_create_task_tables.py` and a new migration — current migration lacks the Monday identity uniqueness constraint; future migration should add canonical app-user/session identity structures safely.
- `c:/Users/SamuelOjuri/OneDrive - Tapered Plus/Documents/CodeProjects/DesignAutomationAssistant/frontend/main_app/app/monday-handoff/[code]/page.tsx` — current page requires Supabase session; future page should call backend with cookies or redirect to backend Monday OAuth.
- `c:/Users/SamuelOjuri/OneDrive - Tapered Plus/Documents/CodeProjects/DesignAutomationAssistant/frontend/main_app/app/tasks/[externalTaskKey]/page.tsx` — current API calls attach Supabase bearer tokens; future calls should use cookie credentials.
- `c:/Users/SamuelOjuri/OneDrive - Tapered Plus/Documents/CodeProjects/DesignAutomationAssistant/frontend/main_app/app/connect-monday/connect-monday-client.tsx` and `frontend/main_app/app/login/login-client.tsx` — current connect flow depends on Supabase login; future Monday-first flow should not require these for onboarding.

**Verification**
1. Review `backend/AutheticationPlan.md` after edit and confirm it explicitly states: Monday OAuth is primary for Monday-launched users; `monday_account_id + monday_user_id` is canonical; email is nullable and optional; backend session cookie replaces Supabase Auth for this path.
2. Run `python -m compileall -q backend/app` after implementation changes to catch Python syntax/import regressions.
3. Run focused backend tests covering the revised auth flow, starting with new tests for Monday OAuth state/callback and existing handoff tests adapted away from Supabase JWT-only auth.
4. Run the frontend type/build check for `frontend/main_app` after changing token calls to cookie credentials.
5. Manually test the full flow: Monday Item View open -> handoff code -> backend Monday OAuth -> app session cookie set -> `/monday-handoff/{code}` resolves -> `/tasks/{externalTaskKey}` loads summary/sources/chat without email/password login.
6. Manually test negative cases: expired code, reused code, OAuth Monday user/account mismatch, missing app session, no item read access, and logout/session expiry.

**Decisions**
- Choose Option A: backend-controlled HTTP-only session cookie for Monday-first users.
- Keep Supabase for Postgres, pgvector, and Storage.
- Do not require Supabase Auth, email/password, magic links, or synthetic email addresses for Monday-first onboarding.
- Keep email/password as optional secondary login only if later needed.
- Canonical identity for Monday-launched users is `monday_account_id + monday_user_id`.
- `monday_email` is optional metadata only.
- CAD integration should trace outputs to Monday item/task/user and app user/session, not to email as the primary key.

**Further Considerations**
1. Cookie deployment details need environment-specific choices: `SameSite=None; Secure` is likely required for cross-site production if frontend and backend are on different domains; local development may need relaxed settings.
2. Session storage can be signed stateless JWT cookies or opaque session IDs backed by Postgres. Recommendation: opaque session table if logout/revocation/audit is important; short-lived signed cookie if implementation speed matters.
3. Supabase Auth compatibility can be retained for non-Monday direct access later, but it should be treated as a separate optional auth provider and not block Monday handoff.