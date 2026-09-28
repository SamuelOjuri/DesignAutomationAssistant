export type ChatCitation = {
  sourceId?: string | null;
  filename?: string | null;
  page?: number | null;
  section?: string | null;
  snippet?: string | null;
  score?: number | null;
  fileId?: string | null;
  mondayAssetId?: string | null;
};

export type ChatCompletion = { content: string; citations: ChatCitation[]; ok: boolean };
export type ChatStreamEvent =
  | { type: "status"; message: string }
  | { type: "delta"; text: string }
  | ({ type: "done" } & ChatCompletion);

export class ChatStreamError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ChatStreamError";
  }
}

function parseEvent(name: string, data: string): ChatStreamEvent {
  let value;
  try { value = JSON.parse(data); } catch { throw new ChatStreamError("The server sent an unreadable response. Please try again."); }
  if (value && typeof value === "object") {
    if (name === "error" && typeof value.message === "string") throw new ChatStreamError(value.message);
    if (name === "status" && typeof value.message === "string") return { type: name, message: value.message };
    if (name === "delta" && typeof value.text === "string") return { type: name, text: value.text };
    if (name === "done" && typeof value.content === "string" && typeof value.ok === "boolean"
      && Array.isArray(value.citations) && value.citations.every(validCitation)) {
      return { type: name, content: value.content, citations: value.citations, ok: value.ok };
    }
  }
  throw new ChatStreamError("The server sent an invalid response. Please try again.");
}

function validCitation(value: unknown): value is ChatCitation {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const citation = value as Record<string, unknown>;
  return ["sourceId", "filename", "section", "snippet", "fileId", "mondayAssetId"]
    .every((key) => citation[key] == null || typeof citation[key] === "string")
    && ["page", "score"].every((key) => citation[key] == null || (typeof citation[key] === "number" && Number.isFinite(citation[key])));
}

/** Parse SSE framing independently from fetch chunk boundaries and UTF-8 bytes. */
export async function readChatStream(
  response: Response,
  onEvent: (event: ChatStreamEvent) => void,
  signal?: AbortSignal,
): Promise<ChatCompletion> {
  if (!response.headers.get("Content-Type")?.toLowerCase().startsWith("text/event-stream") || !response.body) {
    await response.body?.cancel();
    throw new ChatStreamError("The server did not start a streaming response. Please try again.");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  let name = "message";
  let data: string[] = [];
  let eventSize = 0;
  const maxEventSize = 4 * 1024 * 1024;
  const abort = () => { void reader.cancel().catch(() => {}); };
  signal?.addEventListener("abort", abort, { once: true });
  const checkAbort = () => {
    if (signal?.aborted) throw new DOMException("Response stopped", "AbortError");
  };

  function consume(eof: boolean): ChatCompletion | undefined {
    while (true) {
      const boundary = buffer.search(/[\r\n]/);
      if (boundary < 0) break;
      // A CR and LF may arrive in separate network reads.
      if (!eof && buffer[boundary] === "\r" && boundary === buffer.length - 1) break;
      const line = buffer.slice(0, boundary);
      const separatorLength = buffer[boundary] === "\r" && buffer[boundary + 1] === "\n" ? 2 : 1;
      buffer = buffer.slice(boundary + separatorLength);
      if (!line) {
        if (data.length) {
          const event = parseEvent(name, data.join("\n"));
          onEvent(event);
          if (event.type === "done") return event;
        }
        name = "message";
        data = [];
        eventSize = 0;
      } else if (!line.startsWith(":")) {
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        let value = colon < 0 ? "" : line.slice(colon + 1);
        if (value.startsWith(" ")) value = value.slice(1);
        if (field === "event") name = value;
        if (field === "data") {
          data.push(value);
          eventSize += value.length;
        }
      }
      if (eventSize > maxEventSize) throw new ChatStreamError("The response is too large to display.");
    }
    if (buffer.length + eventSize > maxEventSize) throw new ChatStreamError("The response is too large to display.");
  }

  try {
    while (true) {
      checkAbort();
      const { done, value } = await reader.read();
      checkAbort();
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
      const completion = consume(done);
      if (completion) return completion;
      if (done) throw new ChatStreamError("The connection ended before the answer was complete. Please try again.");
    }
  } finally {
    signal?.removeEventListener("abort", abort);
    try { await reader.cancel(); } catch { /* The upstream may already be disconnected. */ }
    reader.releaseLock();
  }
}
