import assert from "node:assert/strict";
import { test } from "node:test";
import { startTaskPolling } from "../lib/task-polling.ts";

const settle = () => new Promise((resolve) => setImmediate(resolve));

function surface() {
  let timer;
  let visible = true;
  let listener;
  return {
    environment: {
      isVisible: () => visible,
      subscribe: (callback) => { listener = callback; return () => { listener = undefined; }; },
      schedule: (callback, milliseconds) => { assert.equal(milliseconds, 12000); timer = callback; return 1; },
      cancel: () => { timer = undefined; },
    },
    tick: async () => { const callback = timer; timer = undefined; callback?.(); await settle(); },
    focus: async () => { listener?.(); await settle(); },
    setVisible: (value) => { visible = value; },
    hasTimer: () => Boolean(timer),
    hasListener: () => Boolean(listener),
  };
}

test("discovers metadata revisions after sync completed without a snapshot change", async () => {
  const page = surface();
  let response = { snapshotVersion: "same", syncStatus: "completed", metadataRevision: "one", projectName: "Old" };
  const rendered = [];
  const stop = startTaskPolling(async (_, first) => { rendered.push({ ...response, first }); }, assert.fail, page.environment);
  await settle();
  response = { ...response, metadataRevision: "two", projectName: "Updated" };
  await page.tick();
  assert.equal(rendered.length, 2);
  assert.equal(rendered[1].projectName, "Updated");
  assert.equal(rendered[1].first, false);
  assert.equal(page.hasTimer(), true);
  stop();
});

test("pauses reads while hidden and refreshes immediately on return", async () => {
  const page = surface();
  let calls = 0;
  const stop = startTaskPolling(async () => { calls++; }, assert.fail, page.environment);
  await settle();
  page.setVisible(false);
  await page.tick();
  await page.focus();
  assert.equal(calls, 1);
  page.setVisible(true);
  await page.focus();
  assert.equal(calls, 2);
  stop();
});

test("focus events cannot overlap an in-flight read and stopping aborts it", async () => {
  const page = surface();
  let finish;
  let signal;
  let calls = 0;
  const stop = startTaskPolling(async (currentSignal) => {
    signal = currentSignal;
    calls++;
    await new Promise((resolve) => { finish = resolve; });
  }, assert.fail, page.environment);
  await page.focus();
  await page.focus();
  assert.equal(calls, 1);
  stop();
  assert.equal(signal.aborted, true);
  finish();
  await settle();
  assert.equal(page.hasTimer(), false);
  assert.equal(page.hasListener(), false);
});

test("a failed refresh reports the error and still schedules recovery", async () => {
  const page = surface();
  let calls = 0;
  const errors = [];
  const stop = startTaskPolling(async () => {
    if (++calls === 1) throw new Error("offline");
  }, (error) => errors.push(error.message), page.environment);
  await settle();
  await page.tick();
  assert.deepEqual(errors, ["offline"]);
  assert.equal(calls, 2);
  stop();
});
