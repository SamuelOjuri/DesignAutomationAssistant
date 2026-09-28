// Native Netlify Edge Function. Returning a Response bypasses /api/* rewrites.
// Keep model work and parsing in FastAPI; this function only relays bytes.
const RENDER_ORIGIN = "https://design-automation-assistant-api.onrender.com";
const PATHS = new Set(["/api/chat/stream", "/api/chat/stream/probe"]);
const HEADER_TIMEOUT_MS = 30_000;

function privateHeaders(source?: Headers): Headers {
  const headers = new Headers(source);
  // Fetch may have decoded the upstream body. Never forward stale wire lengths
  // or encodings on the response we construct from that body.
  for (const name of ["content-length", "content-encoding", "transfer-encoding", "connection", "age", "etag"]) {
    headers.delete(name);
  }
  headers.set("Cache-Control", "no-store, no-transform");
  headers.set("CDN-Cache-Control", "no-store");
  headers.set("Netlify-CDN-Cache-Control", "no-store");
  return headers;
}

function failure(status: number, detail: string): Response {
  const headers = privateHeaders();
  headers.set("Content-Type", "application/json");
  return new Response(JSON.stringify({ detail }), { status, headers });
}

export async function relayChat(
  request: Request,
  { fetchUpstream = fetch, headerTimeoutMs = HEADER_TIMEOUT_MS }: {
    fetchUpstream?: typeof fetch;
    headerTimeoutMs?: number;
  } = {},
): Promise<Response> {
  const path = new URL(request.url).pathname;
  if (!PATHS.has(path)) return failure(404, "Not found");
  if (request.method !== "POST") {
    const response = failure(405, "Method not allowed");
    response.headers.set("Allow", "POST");
    return response;
  }

  const headers = new Headers({ Accept: "text/event-stream", "Accept-Encoding": "identity" });
  for (const name of ["Content-Type", "Cookie", "X-CSRF-Token", "Authorization", "Origin"]) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  // Forward CSRF unchanged: never manufacture its header from the cookie.
  const controller = new AbortController();
  const abort = () => controller.abort();
  request.signal.addEventListener("abort", abort, { once: true });
  if (request.signal.aborted) abort();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, headerTimeoutMs);
  const cleanup = () => {
    clearTimeout(timer);
    request.signal.removeEventListener("abort", abort);
  };

  let upstream: Response;
  try {
    upstream = await fetchUpstream(`${RENDER_ORIGIN}${path}`, {
      method: "POST",
      headers,
      body: request.body,
      // Required by Node's Fetch implementation in tests; accepted by Deno.
      ...(request.body ? { duplex: "half" } : {}),
      signal: controller.signal,
      redirect: "manual",
    });
  } catch {
    cleanup();
    return failure(timedOut ? 504 : 502, timedOut
      ? "The server took too long to authorise this request. Please try again."
      : "The chat server is unavailable. Please try again.");
  }
  // The deadline covers receiving upstream headers, not the stream lifetime.
  clearTimeout(timer);
  if (upstream.status >= 300 && upstream.status < 400) {
    cleanup();
    controller.abort();
    await upstream.body?.cancel();
    return failure(502, "Unexpected chat server redirect.");
  }
  const responseHeaders = privateHeaders(upstream.headers);
  if (!upstream.body) {
    cleanup();
    return new Response(null, { status: upstream.status, headers: responseHeaders });
  }

  const reader = upstream.body.getReader();
  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    cleanup();
    reader.releaseLock();
  };
  const body = new ReadableStream<Uint8Array>({
    async pull(output) {
      try {
        const { done, value } = await reader.read();
        if (done) {
          release();
          output.close();
        } else {
          output.enqueue(value);
        }
      } catch (error) {
        release();
        output.error(error);
      }
    },
    async cancel(reason) {
      controller.abort();
      try { await reader.cancel(reason); } finally { release(); }
    },
  });
  // Includes upstream 401/403/422 responses and any Set-Cookie headers.
  return new Response(body, { status: upstream.status, headers: responseHeaders });
}

export default function handler(request: Request): Promise<Response> {
  return relayChat(request);
}
