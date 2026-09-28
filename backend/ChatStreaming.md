# Streaming task chat

Task chat now uses `POST /api/chat/stream`. The original
`POST /api/chat/complete` JSON endpoint remains available.

The browser sends the same JSON request, application cookies, and CSRF header.
On Netlify, the native `chat-stream` Edge Function calls the fixed Render API
directly and relays its body. It returns the response itself, bypassing the
external `/api/*` rewrite for the two declared streaming paths. It does not use
`context.next()` or a Next.js `runtime: "edge"` route.

FastAPI retains `get_current_user`, `require_csrf_token`, and
`require_task_access`. These checks, and meaningful-access recording for chat,
finish before the streaming response starts. The relay preserves ordinary
HTTP errors, forwards the original CSRF header without manufacturing one,
and passes through any `Set-Cookie` response headers. Login, logout, cookie
creation, bearer-token verification, and Monday permission rules are unchanged.

## Limits and cancellation

- The relay waits up to **30 seconds for upstream response headers**, leaving
  margin within Netlify Edge's documented 40-second header limit. A timeout
  returns HTTP 504, without admitting the user or beginning an answer. This
  deadline includes transit, Render startup, and backend access checks after
  the relay begins fetching; earlier framework middleware has its own overhead.
- After successful authorisation, FastAPI starts the response before planning
  and retrieval. The relay clears its header timer when headers arrive.
- `CHAT_STREAM_TIMEOUT_SECONDS` defaults to **180**, configurable from 1 to 600.
  It bounds the streaming pipeline, including preparation and generation.
- Heartbeat comments are sent every 10 seconds while work is pending.
- Planning, embeddings, and generation use the asynchronous Gemini client.
  Disconnects, stopping, and the stream deadline cancel its producer and close
  the upstream response/client. The relay forwards client cancellation to its
  upstream fetch. Platform propagation must also be verified after deployment.
- Database reads run in worker threads with their own sessions. No request
  database connection is retained throughout model generation. Already-running
  synchronous SQL or Monday authorisation requests cannot be forcibly cancelled;
  they finish or time out and clean up their own resources. The relay deadline
  does not alter Monday's existing retry policy.

Authenticated streams are marked `no-store`; the relay also disables CDN caching
and avoids forwarding stale content-length or compression headers.

## Response events

SSE framing is used over a POST Fetch response, not browser `EventSource`.

| Event | JSON data |
| --- | --- |
| `status` | `message`: preparation progress |
| `delta` | `text`: readable answer text to append |
| `done` | `content`, `citations`, `ok`: authoritative final result |
| `error` | `code`, `message`: failure after headers have been sent |

Gemini still produces structured JSON. The backend extracts only the top-level
answer string incrementally, handles escaped characters and Unicode pairs, and
validates the complete model response, finish reason, and citation IDs before
completion. Unknown inline citations are rejected. The final coverage note and
public citation metadata are applied on completion.

The frontend replaces provisional text with `done.content`, including a grounded
fallback (`ok: false`) when synthesis or final validation fails. It parses UTF-8
and SSE independently of network chunk boundaries. An explicit error or EOF
without `done` leaves the answer incomplete. Stopped/failed assistant messages
are excluded from subsequent model history. No automatic retries replay a chat.

## Deployment

1. Deploy the backend first. No database migration is required.
2. Deploy `frontend/main_app`, including `netlify/edge-functions/chat-stream.ts`
   and the two `[[edge_functions]]` declarations in `netlify.toml`. Verify the
   Netlify deploy lists a native Edge Function named `chat-stream`.
3. Retain the current production `NEXT_PUBLIC_FASTAPI_BASE_URL` pointing to the
   Netlify frontend origin. A direct Render URL would bypass the relay and has
   different browser cookie requirements. Local development may use a local
   FastAPI base URL as before.
4. Verify the generated Next.js proxy/middleware chain, including existing
   Supabase session refresh behaviour. Do not remove or bypass authentication
   middleware to make a test pass.
5. Run the authenticated probe below through Netlify. Also test a real
   Monday-first user with no incidental Supabase session.

The Render origin is fixed in the Edge Function to prevent arbitrary upstream
destinations. Change that constant deliberately if the backend hostname changes.

## Authenticated transport probe

`POST /api/chat/stream/probe` exercises the same Edge relay, cookies, CSRF, and
Monday access checks without invoking Gemini. It is **disabled by default**:
set `CHAT_STREAM_PROBE_ENABLED=true` in the backend for a controlled staging or
deployment verification, and disable it afterwards. When disabled it returns
404 to authenticated requests. It neither returns project data nor records
meaningful chat access.

Request fields:

- `externalTaskKey`: an existing task the signed-in user may access.
- `durationSeconds`: defaults to 120, allowed 1–180.
- `initialDelaySeconds`: optional 0–60 delay before timestamp events.
- `pauseAtSeconds` and `pauseSeconds`: optional pause during the probe (up to
  60 seconds). Heartbeats continue through delays; the probe has a 250-second
  overall deadline.

From a signed-in task page on the deployed site, this browser-console snippet
prints only elapsed times and probe events. It never prints cookie values:

```javascript
const probeController = new AbortController();
const csrf = document.cookie.split("; ").find(value => value.startsWith("daa_csrf="));
const probeResponse = await fetch("/api/chat/stream/probe", {
  method: "POST",
  credentials: "include",
  headers: {
    "Content-Type": "application/json",
    "X-CSRF-Token": csrf ? decodeURIComponent(csrf.slice("daa_csrf=".length)) : "",
  },
  body: JSON.stringify({
    externalTaskKey: decodeURIComponent(location.pathname.split("/").pop()),
    durationSeconds: 120,
    initialDelaySeconds: 5,
    pauseAtSeconds: 30,
    pauseSeconds: 15,
  }),
  signal: probeController.signal,
});
console.log("Probe HTTP status:", probeResponse.status);
const probeStarted = performance.now();
const probeReader = probeResponse.body.pipeThrough(new TextDecoderStream()).getReader();
try {
  while (true) {
    const { done, value } = await probeReader.read();
    if (done) break;
    console.log(((performance.now() - probeStarted) / 1000).toFixed(2), value);
  }
} finally {
  probeReader.releaseLock();
}
```

In another console evaluation, `probeController.abort()` tests cancellation.
Treat a missing `done` event as an interruption, even if HTTP status was 200.
Do not share raw headers, cookies, or HAR files containing session credentials.

Acceptance checks:

- Events arrive progressively for 120 seconds and end in `done`.
- Invalid/expired/revoked sessions return 401; bad/missing CSRF and denied
  Monday access return 403 before streaming or model invocation.
- Slow access checks return a timeout before Netlify's header limit. Test this
  with a controlled upstream delay; the probe's delays start after access checks.
- Stopping/closing the page closes the relay and generation work where supported.
- Midstream disconnects never appear as successfully completed answers.
- Multiple concurrent chats stay responsive; the relay stays within Edge CPU
  limits and does not buffer or cache another user's response.

## Local verification

From the repository root:

```text
venv/Scripts/python.exe -m pytest backend/tests/test_chat_streaming.py backend/tests/test_chat_bounded_retrieval.py backend/tests/test_retrieval_batch.py backend/tests/test_monday_first_auth.py -q
```

From `frontend/main_app`:

```text
npm run test:chat
npm run test:task-polling
npm run build
```

Automated tests use fake providers and the installed Google SDK with an in-memory
HTTP transport; they make no real Gemini requests. They cover auth rejection,
Monday-first streaming, framing, Unicode, final citations, fallbacks, cancellation,
header deadlines, and relay behaviour. They do not prove Netlify's deployed
runtime duration or browser rendering; those require the deployment checks above.

Platform references:
[Netlify long-running Edge example](https://edge-functions-examples.netlify.app/example/long-running),
[Edge limits](https://docs.netlify.com/build/edge-functions/limits/),
[Edge request ordering](https://docs.netlify.com/build/edge-functions/declarations/#processing-order-caveats).
