import assert from "node:assert/strict";
import { test } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { relayChat } from "../netlify/edge-functions/chat-stream.ts";

const encoder = new TextEncoder();
function request(headers = {}, signal, path = "/api/chat/stream") {
  return new Request(`https://app.example.test${path}`, {
    method: "POST", headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify({ externalTaskKey: "a:b:c", message: "roof" }), signal,
  });
}

test("forwards unchanged credentials to a fixed Render endpoint and preserves HTTP failures", async () => {
  const response = await relayChat(request({ Cookie: "daa_session=test-session; daa_csrf=test-csrf", "X-CSRF-Token": "test-csrf", Origin: "https://app.example.test" }), {
    async fetchUpstream(url, options) {
      assert.equal(url, "https://design-automation-assistant-api.onrender.com/api/chat/stream");
      assert.equal(options.method, "POST");
      assert.equal(options.redirect, "manual");
      assert.equal(options.headers.get("Cookie"), "daa_session=test-session; daa_csrf=test-csrf");
      assert.equal(options.headers.get("X-CSRF-Token"), "test-csrf");
      assert.equal(options.headers.get("Origin"), "https://app.example.test");
      assert.equal(options.headers.has("Authorization"), false);
      assert.deepEqual(await new Response(options.body).json(), { externalTaskKey: "a:b:c", message: "roof" });
      return Response.json({ detail: "Invalid session" }, { status: 401 });
    },
  });
  assert.equal(response.status, 401);
  assert.deepEqual(await response.json(), { detail: "Invalid session" });
  assert.equal(response.headers.get("Netlify-CDN-Cache-Control"), "no-store");
});

test("does not manufacture CSRF headers and preserves the existing bearer path", async () => {
  const response = await relayChat(request({ Cookie: "daa_session=test; daa_csrf=test", Authorization: "Bearer test-token" }), {
    async fetchUpstream(_, options) {
      assert.equal(options.headers.has("X-CSRF-Token"), false);
      assert.equal(options.headers.get("Authorization"), "Bearer test-token");
      return Response.json({ detail: "Denied" }, { status: 403 });
    },
  });
  assert.equal(response.status, 403);
  await response.body.cancel();
});

test("returns headers and chunks without buffering and allows body duration beyond header deadline", async () => {
  let source;
  let signal;
  const response = await relayChat(request(), {
    headerTimeoutMs: 10,
    async fetchUpstream(_, options) {
      signal = options.signal;
      return new Response(new ReadableStream({ start(controller) { source = controller; } }), {
        headers: { "Content-Type": "text/event-stream", "Content-Encoding": "br", "Content-Length": "999", "Set-Cookie": "renewal=test; HttpOnly" },
      });
    },
  });
  assert.equal(response.headers.get("Content-Type"), "text/event-stream");
  assert.equal(response.headers.has("Content-Encoding"), false);
  assert.equal(response.headers.has("Content-Length"), false);
  assert.deepEqual(response.headers.getSetCookie(), ["renewal=test; HttpOnly"]);
  const reader = response.body.getReader();
  source.enqueue(encoder.encode("first"));
  assert.equal(new TextDecoder().decode((await reader.read()).value), "first");
  await delay(25);
  assert.equal(signal.aborted, false);
  source.enqueue(encoder.encode("second"));
  source.close();
  assert.equal(new TextDecoder().decode((await reader.read()).value), "second");
  assert.equal((await reader.read()).done, true);
});

test("a slow pre-stream response returns 504 rather than premature 200", async () => {
  let aborted = false;
  const response = await relayChat(request(), {
    headerTimeoutMs: 5,
    fetchUpstream: (_, { signal }) => new Promise((_, reject) => {
      signal.addEventListener("abort", () => { aborted = true; reject(new Error("aborted")); });
    }),
  });
  assert.equal(response.status, 504);
  assert.equal(aborted, true);
  assert.match((await response.json()).detail, /authorise/);
});

test("reader cancellation aborts upstream and closes its body", async () => {
  let signal;
  let cancelled = false;
  const response = await relayChat(request(), {
    async fetchUpstream(_, options) {
      signal = options.signal;
      return new Response(new ReadableStream({ cancel() { cancelled = true; } }));
    },
  });
  await response.body.cancel();
  assert.equal(signal.aborted, true);
  assert.equal(cancelled, true);
});

test("client abort is forwarded after response headers arrive", async () => {
  const client = new AbortController();
  let signal;
  const response = await relayChat(request({}, client.signal), {
    async fetchUpstream(_, options) {
      signal = options.signal;
      return new Response(new ReadableStream());
    },
  });
  client.abort();
  assert.equal(signal.aborted, true);
  await response.body.cancel();
});

test("unexpected redirects are rejected without following credentials elsewhere", async () => {
  const response = await relayChat(request(), { async fetchUpstream() { return Response.redirect("https://unexpected.example"); } });
  assert.equal(response.status, 502);
});

test("probe has a fixed upstream and unsupported paths or methods never fetch", async () => {
  const response = await relayChat(request({}, undefined, "/api/chat/stream/probe"), {
    async fetchUpstream(url) {
      assert.equal(url, "https://design-automation-assistant-api.onrender.com/api/chat/stream/probe");
      return Response.json({ detail: "Not found" }, { status: 404 });
    },
  });
  await response.body.cancel();
  const options = { fetchUpstream: () => assert.fail("Unexpected upstream request") };
  assert.equal((await relayChat(request({}, undefined, "/api/other"), options)).status, 404);
  assert.equal((await relayChat(new Request("https://app.example.test/api/chat/stream"), options)).status, 405);
});
