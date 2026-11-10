# Specification Review Item View

Phase 7 adds a separate embedded route to `frontend/main_app`. The existing
`index.html` / `app.js` design-team launcher remains a separate Item View.
`specification-review.item-view.json` records the new feature settings for
deployment; it is a configuration checklist, not a Monday import manifest.
See the [Phase 7 implementation and local browser harness](../../backend/SpecificationReviewFrontend.md)
for source layout, commands and mocked journey checks.

In the existing Monday app's Developer Center, add an **Item View** named
**Specification Review**. Choose an externally hosted iframe URL and set it to
`https://<deployed-main-app-origin>/specification-review`. Add the feature to
items on **Projects Holding Page**. Use the same app/client secret and user OAuth
connection as the Phase 6 backend. Do not point the new feature at the task
launcher or `/monday-handoff`.

The [Monday SDK](https://developer.monday.com/apps/docs/introduction-to-the-sdk)
loads from its documented CDN. The view obtains
[context and sessionToken](https://developer.monday.com/apps/docs/mondayget),
subscribes to [context changes](https://developer.monday.com/apps/docs/mondaylisten),
and opens OAuth through the SDK's
[openLinkInTab](https://developer.monday.com/apps/docs/mondayexecute).
Credentials are sent to the review API in request bodies/headers and remain in
memory. Board/item query parameters in the OAuth return URL cannot establish
identity; the embedded view bootstraps again through the SDK.

The review route bypasses Supabase session refresh. Its CSP allows the current
Monday `https://*.monday.com` parent origins and same-origin/blob PDF frames;
other application routes retain their previous headers and authentication.
If a proxy or host adds `X-Frame-Options: DENY` / `SAMEORIGIN` or another CSP,
remove that conflicting override for this route. Verify the response headers
on the deployed origin. The retained PDF is fetched with the review bearer and
displayed from a temporary object URL; no token is placed in a PDF URL.

`NEXT_PUBLIC_FASTAPI_BASE_URL` may select the existing API origin, or leave it
unset for the existing same-origin `/api/*` Netlify relay. When using a separate
API origin, add the exact deployed frontend origin to backend
`CORS_ALLOWED_ORIGINS`; allow the `Authorization` header. Monday's parent origin
does not replace the frontend's actual request origin. Configure the existing
Monday OAuth callback/return URLs for the deployed frontend and backend.

Start with `PERFORMANCE_SPECIFICATION_MODE=off` (the default, including upgrades),
`PERFORMANCE_SPECIFICATION_ENABLED=false` and
`PERFORMANCE_SPECIFICATION_REVIEW_ENABLED=false`. The specification mode never
inherits `DESIGN_PROCESSING_MODE`; existing CSV/preview/matched-report output can
remain design-enabled, and `AUTO_SYNC_*` ingestion settings remain independent.
Follow the [deployed setup and rollout guide](../../backend/SpecificationReviewFrontend.md#deployed-setup-and-rollout)
to deliberately enter specification shadow, then its own item allowlist.

Shadow rendering needs `PERFORMANCE_SPECIFICATION_ENABLED=true` plus
`PERFORMANCE_SPECIFICATION_MODE=shadow`; reviewer edits additionally need the
separate review flag. Shadow cannot publish or approve specifications. For
`PERFORMANCE_SPECIFICATION_MODE=allowlist`, use a nonempty
`PERFORMANCE_SPECIFICATION_ALLOWLIST_ITEM_IDS` CSV or JSON array of positive
Monday item IDs; its default is `[]`. That list limits specification processing,
edits/publication/approval and does not limit the existing design outputs.
Restart all affected review API, publisher and specification-enqueue processes
with matching settings after changes. No reviewer-user list is required in
deployment version 1. This source change does not register a live feature, alter
OAuth scopes or enable production publishing. Developer Center registration
requires the deployment's app identity and URL.

Before rollout, use an explicitly allowlisted item in the deployed Monday
browser: connect OAuth, verify New Enquiry and Amendment candidates and all 18
fields, save a correction, inspect the regenerated PDF and complete that exact
revision. Verify third-party-cookie blocking, a narrow iframe, keyboard focus,
PDF preview/download, session renewal and item-context switching. Move the item
manually and verify retained PDF access with editing/approval disabled. These
live checks belong to the staged Phase 8 release; local mocked checks do not
establish deployed embedding or Monday permissions.
