/** The review view uses Monday identity directly, without chat handoffs or cookies. */
export interface MondayReviewContext {
  accountId: string;
  boardId: string;
  itemId: string;
}

export interface MondayReviewSdk {
  get(type: "context" | "sessionToken"): Promise<{ data: unknown }>;
  listen(type: "context", callback: (event: { data: unknown }) => void): () => void;
  execute(type: "openLinkInTab", options: { url: string }): Promise<unknown>;
}

declare global {
  interface Window {
    mondaySdk?: () => MondayReviewSdk;
  }
}

const SDK_URL = "https://cdn.jsdelivr.net/npm/monday-sdk-js/dist/main.js";
const SDK_SCRIPT_ID = "specification-review-monday-sdk";
const SDK_TIMEOUT_MS = 12000;

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : null;
}

function identifier(value: unknown): string | null {
  if (typeof value === "number" && (!Number.isSafeInteger(value) || value <= 0)) return null;
  if (typeof value !== "number" && typeof value !== "string") return null;
  const text = String(value);
  return /^[1-9][0-9]*$/.test(text) ? text : null;
}

/** Context comes only from the SDK. URL query parameters never establish identity. */
export function normalizeMondayReviewContext(value: unknown): MondayReviewContext | null {
  const context = record(value);
  if (!context) return null;
  const accountId = identifier(record(context.account)?.id ?? context.accountId);
  const boardId = identifier(context.boardId);
  const itemId = identifier(context.itemId ?? context.pulseId);
  return accountId && boardId && itemId ? { accountId, boardId, itemId } : null;
}

function abortError(): DOMException {
  return new DOMException("Review request cancelled", "AbortError");
}

/** SDK promises cannot be cancelled; ignore their late results after abort/timeout. */
function bounded<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(abortError()); return; }
    const cleanup = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    };
    const abort = () => { cleanup(); reject(abortError()); };
    const timer = setTimeout(() => {
      cleanup();
      reject(new Error("Monday did not respond. Open Specification Review from a Monday item and try again."));
    }, SDK_TIMEOUT_MS);
    signal?.addEventListener("abort", abort, { once: true });
    promise.then(value => { cleanup(); resolve(value); }, error => { cleanup(); reject(error); });
  });
}

/** Shares only script loading; each view creates its own SDK client/listeners. */
let sdkScript: Promise<void> | null = null;

export async function loadMondayReviewSdk(signal?: AbortSignal): Promise<MondayReviewSdk> {
  if (signal?.aborted) throw abortError();
  if (typeof window === "undefined") throw new Error("Monday review is available in the browser.");
  if (!window.mondaySdk) {
    if (!sdkScript) {
      sdkScript = new Promise<void>((resolve, reject) => {
        const previous = document.getElementById(SDK_SCRIPT_ID);
        previous?.remove();
        const script = document.createElement("script");
        script.id = SDK_SCRIPT_ID;
        script.src = SDK_URL;
        script.async = true;
        script.referrerPolicy = "no-referrer";
        const timer = setTimeout(() => finish(false), SDK_TIMEOUT_MS);
        const finish = (loaded: boolean) => {
          clearTimeout(timer);
          script.onload = null;
          script.onerror = null;
          if (loaded && window.mondaySdk) resolve();
          else {
            script.remove();
            reject(new Error("The Monday connection could not load. Check your connection and try again."));
          }
        };
        script.onload = () => finish(true);
        script.onerror = () => finish(false);
        document.head.append(script);
      }).catch(error => { sdkScript = null; throw error; });
    }
    await bounded(sdkScript, signal);
  }
  if (signal?.aborted) throw abortError();
  if (!window.mondaySdk) throw new Error("The Monday connection is unavailable.");
  return window.mondaySdk();
}

export async function getMondayReviewSession(sdk: MondayReviewSdk, signal?: AbortSignal) {
  if (signal?.aborted) throw abortError();
  const [contextResult, tokenResult] = await bounded(
    Promise.all([sdk.get("context"), sdk.get("sessionToken")]), signal,
  );
  const context = normalizeMondayReviewContext(contextResult.data);
  if (!context) throw new Error("Open Specification Review from an item in the Projects Holding Page.");
  const token = tokenResult.data;
  if (typeof token !== "string" || !token || token.length > 16384) {
    throw new Error("Your Monday session is unavailable. Reopen the item and try again.");
  }
  return { sessionToken: token, context };
}

export function listenMondayReviewContext(
  sdk: MondayReviewSdk,
  onContext: (context: MondayReviewContext) => void,
  onError?: (error: Error) => void,
): () => void {
  let active = true;
  const unsubscribe = sdk.listen("context", event => {
    if (!active) return;
    const context = normalizeMondayReviewContext(event.data);
    if (context) onContext(context);
    else onError?.(new Error("The Monday item context changed. Reopen Specification Review from the item."));
  });
  return () => { active = false; unsubscribe(); };
}

/** The SDK opens the top-level OAuth flow supported by Monday's sandboxed iframe. */
export async function openMondayReviewAuthorization(sdk: MondayReviewSdk, authorizationUrl: string) {
  let url: URL;
  try { url = new URL(authorizationUrl); }
  catch { throw new Error("The authorization link is invalid. Reconnect to Monday and try again."); }
  if (url.username || url.password || (url.protocol !== "https:" && !(url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname)))) {
    throw new Error("The authorization link is invalid. Reconnect to Monday and try again.");
  }
  await sdk.execute("openLinkInTab", { url: url.href });
}
