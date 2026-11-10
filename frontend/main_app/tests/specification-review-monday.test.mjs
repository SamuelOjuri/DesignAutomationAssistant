import assert from "node:assert/strict";
import { test } from "node:test";
import {
  getMondayReviewSession, listenMondayReviewContext, normalizeMondayReviewContext,
  openMondayReviewAuthorization,
} from "../lib/specification-review-monday.ts";
import nextConfig from "../next.config.js";

const context = { accountId: "1", boardId: "2", itemId: "3" };

test("normalizes documented item-view context and rejects malformed identities", () => {
  assert.deepEqual(normalizeMondayReviewContext({ account: { id: 1 }, boardId: 2, itemId: 3 }), context);
  assert.deepEqual(normalizeMondayReviewContext({ account: { id: "1" }, boardId: "2", pulseId: "3" }), context);
  for (const invalid of [null, [], {}, { ...context, itemId: 0 }, { ...context, accountId: true },
    { ...context, boardId: " 2" }, { ...context, boardId: "02" }, { ...context, itemId: Number.MAX_SAFE_INTEGER + 1 }]) {
    assert.equal(normalizeMondayReviewContext(invalid), null);
  }
});

test("SDK session contains only the context accepted by the review API", async () => {
  const sdk = { get: async type => ({ data: type === "context" ? { ...context, user: { id: "4" }, untrusted: "ignored" } : "sdk-token" }) };
  assert.deepEqual(await getMondayReviewSession(sdk), { context, sessionToken: "sdk-token" });
  await assert.rejects(getMondayReviewSession({ get: async () => ({ data: null }) }), /Open Specification Review/);
});

test("aborting SDK work discards late responses and prevents starting already-aborted reads", async () => {
  const controller = new AbortController();
  const resolvers = [];
  const sdk = { get: () => new Promise(resolve => resolvers.push(resolve)) };
  const pending = getMondayReviewSession(sdk, controller.signal);
  controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
  resolvers[0]({ data: context });
  resolvers[1]({ data: "old-token" });
  let calls = 0;
  await assert.rejects(getMondayReviewSession({ get: () => { calls++; } }, controller.signal), { name: "AbortError" });
  assert.equal(calls, 0);
});

test("context listener clears invalid context and ignores callbacks after unsubscribe", () => {
  let callback;
  let unsubscribed = 0;
  const updates = [];
  const errors = [];
  const stop = listenMondayReviewContext({ listen: (_, cb) => { callback = cb; return () => unsubscribed++; } }, value => updates.push(value), error => errors.push(error.message));
  callback({ data: context });
  callback({ data: { ...context, itemId: "4" } });
  callback({ data: {} });
  stop();
  callback({ data: context });
  assert.equal(updates.length, 2);
  assert.equal(updates[1].itemId, "4");
  assert.equal(errors.length, 1);
  assert.equal(unsubscribed, 1);
});

test("OAuth uses Monday top-level action and rejects unsafe URLs", async () => {
  const calls = [];
  const sdk = { execute: async (...args) => calls.push(args) };
  await openMondayReviewAuthorization(sdk, "https://auth.monday.com/oauth2/authorize?state=signed-return");
  assert.equal(calls[0][0], "openLinkInTab");
  for (const url of ["javascript:alert(1)", "http://external.example/oauth", "https://user:password@example.com", "not a url"]) {
    await assert.rejects(openMondayReviewAuthorization(sdk, url), /authorization link is invalid/);
  }
  assert.equal(calls.length, 1);
});

test("embedding headers apply only to review and allow authenticated blob previews", async () => {
  const headers = await nextConfig.headers();
  assert.equal(headers.length, 1);
  assert.equal(headers[0].source, "/specification-review/:path*");
  const policy = headers[0].headers.find(header => header.key === "Content-Security-Policy").value;
  assert.match(policy, /frame-ancestors 'self' https:\/\/monday\.com https:\/\/\*\.monday\.com/);
  assert.match(policy, /frame-src 'self' blob:/);
  assert.equal(headers[0].headers.find(header => header.key === "Referrer-Policy").value, "no-referrer");
});
