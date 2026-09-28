import assert from "node:assert/strict";
import { test } from "node:test";
import { readChatStream, ChatStreamError } from "../lib/chat-stream.ts";

const encoder = new TextEncoder();
const frame = (event, value, newline = "\n") => `event: ${event}${newline}data: ${JSON.stringify(value)}${newline}${newline}`;

function responseFrom(text, size = 1) {
  const bytes = encoder.encode(text);
  let offset = 0;
  return new Response(new ReadableStream({
    pull(controller) {
      if (offset >= bytes.length) { controller.close(); return; }
      controller.enqueue(bytes.slice(offset, offset + size));
      offset += size;
    },
  }), { headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
}

for (const size of [1, 2, 3, 7, 4096]) {
  test(`decodes UTF-8 and SSE across ${size}-byte boundaries and accepts authoritative final text`, async () => {
    const final = { content: 'Roof £100 😀.\nFinal note. [S1]', citations: [{ sourceId: "S1", filename: "roof.pdf" }], ok: true };
    const wire = ": keep-alive\r\n\r\n" + frame("status", { message: "Searching…" }, "\r\n")
      + frame("delta", { text: 'Roof £100 😀.\n' }, "\r\n") + frame("done", final, "\r\n");
    const events = [];
    const result = await readChatStream(responseFrom(wire, size), (event) => events.push(event));
    assert.equal(result.content, final.content);
    assert.deepEqual(result.citations, final.citations);
    assert.deepEqual(events.map((event) => event.type), ["status", "delta", "done"]);
    assert.equal(events[1].text, 'Roof £100 😀.\n');
  });
}

test("handles multiple data lines and bare CR event separators", async () => {
  const wire = 'event: done\rdata: {"content":"answer",\rdata: "citations":[],"ok":true}\r\r';
  assert.equal((await readChatStream(responseFrom(wire), () => {})).content, "answer");
});

test("EOF without done, including an unterminated done frame, remains interrupted", async () => {
  for (const tail of ["", 'event: done\ndata: {"content":"answer","citations":[],"ok":true}']) {
    const events = [];
    await assert.rejects(readChatStream(responseFrom(frame("delta", { text: "Partial" }) + tail), (event) => events.push(event)), /before the answer was complete/);
    assert.deepEqual(events.map((event) => event.type), ["delta"]);
  }
});

test("explicit failures do not mark provisional text complete", async () => {
  const events = [];
  await assert.rejects(readChatStream(responseFrom(frame("delta", { text: "Partial" }) + frame("error", { message: "Generation failed", code: "failure" })), (event) => events.push(event)), /Generation failed/);
  assert.equal(events.length, 1);
});

test("malformed events, invalid citations, and nonstreaming responses fail clearly", async () => {
  for (const wire of ['event: delta\ndata: {invalid}\n\n', frame("done", { content: "answer", citations: [null], ok: true }), frame("done", { content: "answer", citations: [{ filename: 123 }], ok: true })]) {
    await assert.rejects(readChatStream(responseFrom(wire), () => {}), ChatStreamError);
  }
  await assert.rejects(readChatStream(Response.json({ content: "answer" }), () => {}), /did not start a streaming response/);
});

test("done completes immediately and cancels the remaining connection", async () => {
  let cancelled = false;
  const response = new Response(new ReadableStream({
    start(controller) { controller.enqueue(encoder.encode(frame("done", { content: "Final", citations: [], ok: false }))); },
    cancel() { cancelled = true; },
  }), { headers: { "Content-Type": "text/event-stream" } });
  const result = await readChatStream(response, () => {});
  assert.equal(result.ok, false);
  assert.equal(cancelled, true);
});

test("abort interrupts a pending read and releases the stream", async () => {
  const controller = new AbortController();
  let cancelled = false;
  const response = new Response(new ReadableStream({ cancel() { cancelled = true; } }), { headers: { "Content-Type": "text/event-stream" } });
  const reading = readChatStream(response, () => assert.fail("No events expected"), controller.signal);
  controller.abort();
  await assert.rejects(reading, { name: "AbortError" });
  assert.equal(cancelled, true);
  assert.equal(response.body.locked, false);
});
