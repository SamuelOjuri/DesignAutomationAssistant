import assert from "node:assert/strict";
import { test } from "node:test";
import { bootstrapReviewSession, createReviewApi, ReviewApiError } from "../lib/specification-review-api.ts";

const session = {
  status: "ready", access_token: "private-review-bearer", token_type: "Bearer", expires_at: "2026-11-09T13:00:00Z",
  capabilities: { read: true, edit: true, approve: true }, scope_reason: null,
  account_id: "1", user_id: "2", board_id: "3", item_id: "4",
};
const context = { accountId: "1", boardId: "3", itemId: "4" };
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

test("bootstrap sends SDK credentials only in a private POST body", async () => {
  let request;
  const result = await bootstrapReviewSession("https://backend.test/", "sdk-secret", context, undefined, async (url, options) => {
    request = { url, options };
    return json(session);
  });
  assert.equal(result.status, "ready");
  assert.equal(request.url, "https://backend.test/api/specification-review/session");
  assert.equal(request.options.method, "POST");
  assert.equal(request.options.credentials, "omit");
  assert.equal(request.options.cache, "no-store");
  assert.deepEqual(JSON.parse(request.options.body), { sessionToken: "sdk-secret", context });
  assert.equal(request.url.includes("secret"), false);
});

test("bootstrap rejects a returned session for a different account or item", async () => {
  for (const patch of [{ account_id: "99" }, { item_id: "99" }, { board_id: "99" }]) {
    await assert.rejects(bootstrapReviewSession("", "sdk", context, undefined, async () => json({ ...session, ...patch })),
      (error) => error instanceof ReviewApiError && error.code === "reviewer_forbidden");
  }
});

test("OAuth-required bootstrap stays separate from active-project login", async () => {
  const oauth = { status: "oauth_required", authorization_url: "https://auth.monday.com/oauth2/authorize?state=signed" };
  assert.deepEqual(await bootstrapReviewSession("", "sdk", context, undefined, async () => json(oauth)), oauth);
});

test("PDF access uses the scoped endpoint with header credentials", async () => {
  let request;
  const api = createReviewApi("https://backend.test", session, async (url, options) => {
    request = { url, options };
    return new Response("%PDF-1.7", { headers: { "Content-Type": "application/pdf" } });
  });
  assert.equal(await (await api.pdf("revision/a")).text(), "%PDF-1.7");
  assert.equal(request.url, "https://backend.test/api/specification-review/items/3/4/pdf/revision%2Fa");
  assert.equal(request.options.headers.Authorization, "Bearer private-review-bearer");
  assert.equal(request.url.includes("bearer"), false);
  assert.equal(request.options.credentials, "omit");
  api.destroy();
});

test("pending 202 is an actionable source state rather than a successful workspace", async () => {
  const api = createReviewApi("", session, async () => json({ detail: { code: "source_waiting", message: "waiting" } }, 202));
  await assert.rejects(api.workspace(), (error) => error.code === "source_waiting" && error.status === 202);
  api.destroy();
});

test("stale edits and expiry keep stable error codes without echoing upstream secrets", async () => {
  for (const [code, status] of [["source_changed", 409], ["stale_review", 409], ["authorization_expired", 401], ["scope_changed", 409]]) {
    const api = createReviewApi("", session, async () => json({ detail: { code, message: "secret-token https://private.test" } }, status));
    await assert.rejects(api.status(), (error) => {
      assert.equal(error.code, code);
      assert.equal(error.message.includes("secret-token"), false);
      assert.equal(error.message.includes("private.test"), false);
      return true;
    });
    api.destroy();
  }
});

test("destroying a context aborts in-flight reads and discards item credentials", async () => {
  let signal;
  const api = createReviewApi("", session, async (_url, options) => {
    signal = options.signal;
    return new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError"))));
  });
  const pending = api.workspace();
  api.destroy();
  assert.equal(signal.aborted, true);
  await assert.rejects(pending, (error) => error.name === "AbortError");
  await assert.rejects(api.status(), (error) => error.code === "authorization_expired");
});

test("request abort signals and retry bodies pass through unchanged", async () => {
  let captured;
  const api = createReviewApi("", session, async (_url, options) => { captured = options; return json({ changed: true }); });
  const controller = new AbortController();
  const command = { expected_review_version: 3, expected_source_fingerprint: "source", idempotency_key: "same-retry", field_operations: {} };
  await api.save(command, controller.signal);
  assert.equal(captured.method, "PUT");
  assert.deepEqual(JSON.parse(captured.body), command);
  controller.abort();
  assert.equal(captured.signal.aborted, true);
  api.destroy();
});

test("a request deadline aborts hung connections and permits safe command retry", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let capturedSignal;
  const api = createReviewApi("", session, async (_url, options) => {
    capturedSignal = options.signal;
    return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError"))));
  });
  const pending = api.save({ idempotency_key: "stable-retry" });
  t.mock.timers.tick(30_000);
  await assert.rejects(pending, (error) => error.code === "network_error");
  assert.equal(capturedSignal.aborted, true);
  api.destroy();
});

test("the deadline also covers a stalled response body after headers arrive", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let bodyStarted = false;
  const api = createReviewApi("", session, async (_url, options) => ({
    ok: true, status: 200,
    json: () => {
      bodyStarted = true;
      return new Promise((_resolve, reject) => options.signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError"))));
    },
  }));
  const pending = api.workspace();
  await Promise.resolve();
  assert.equal(bodyStarted, true);
  t.mock.timers.tick(30_000);
  await assert.rejects(pending, (error) => error.code === "network_error");
  api.destroy();
});
