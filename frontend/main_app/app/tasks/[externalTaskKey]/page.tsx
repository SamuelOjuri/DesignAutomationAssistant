"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { getBrowserSupabase } from "@/lib/supabase/client";

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
};

// --- Extended types for summary, sources, citations, etc. ---
type ColumnValue = {
  column?: { title?: string | null };
  id?: string | null;
  type?: string | null;
  value?: any;
  text?: string | null;
  display_value?: string | null;
};

type CsvParamKeyValue = {
  assetId?: string | null;
  filename?: string | null;
  format: "key_value";
  documents?: { text: string; rowIndex: number }[];
  records?: { parameter: string; value: string; source: string; rowIndex: number }[];
};

type CsvParamTable = {
  assetId?: string | null;
  filename?: string | null;
  format: "table";
  rows?: Record<string, string>[];
};

type TaskContext = {
  column_values?: ColumnValue[];
  csv_params?: Array<CsvParamKeyValue | CsvParamTable>;
  // Keep any other fields from monday item JSON
  [key: string]: any;
};

type TaskSummaryResponse = {
  externalTaskKey: string;
  snapshotVersion?: string | null;
  taskContext?: TaskContext | null;
  status?: string | null;
  updatedAt?: string | null;
  syncStatus?: string | null;
};

type TaskSourceFile = {
  id: string;
  kind: string;
  originalFilename?: string | null;
  mimeType?: string | null;
  sizeBytes?: number | null;
  mondayAssetId?: string | null;
  createdAt?: string | null;
};

type TaskSourcesResponse = {
  snapshotVersion?: string | null;
  files: TaskSourceFile[];
};

// Extended Citation type
type Citation = {
  filename?: string | null;
  page?: number | null;
  section?: string | null;
  snippet?: string | null;
  score?: number | null;
  fileId?: string | null;
  mondayAssetId?: string | null;
};

type StreamEvent =
  | { type: "start"; ts: string }
  | { type: "message"; content: string }
  | { type: "citations"; citations: Citation[] }
  | { type: "done" };

async function getAccessToken(): Promise<string | null> {
  const supabase = getBrowserSupabase();
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

type SignedUrlResponse = { url: string; expiresAt: string };

// --- Summary helpers ---
const VALIDATED_COLUMN_TITLES = new Set([
  "Priority",
  "Designer",
  "Time tracking",
  "Status",
  "Date Received",
  "Hour Received",
  "New Enq / Amend",
  "TP Ref",
  "Project Name",
  "Zip Code",
  "Date Completed",
  "Hour Completed",
  "Turn Around (Hours)",
  "Date Sort",
]);

function formatColumnValue(col: ColumnValue): string {
  const raw = col.display_value ?? col.text ?? col.value;
  if (raw == null) return "";
  if (typeof raw === "string") return raw;
  if (typeof raw === "number" || typeof raw === "boolean") return String(raw);
  try {
    return JSON.stringify(raw);
  } catch {
    return String(raw);
  }
}

// --- Sources helpers ---
function formatBytes(bytes?: number | null): string {
  if (bytes == null) return "—";
  if (bytes === 0) return "0 B";
  const k = 1024;
  const sizes = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  const value = bytes / Math.pow(k, i);
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${sizes[i]}`;
}

function formatDate(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString();
}

export default function TaskPage() {
  // Use useParams hook to get the route parameter
  const params = useParams();
  const externalTaskKey = params.externalTaskKey as string;

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [citations, setCitations] = useState<Citation[]>([]);
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [syncStatus, setSyncStatus] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  // --- New state for summary and sources ---
  const [summary, setSummary] = useState<TaskSummaryResponse | null>(null);
  const [sources, setSources] = useState<TaskSourcesResponse | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [sourcesError, setSourcesError] = useState<string | null>(null);
  const [isLoadingSummary, setIsLoadingSummary] = useState(false);
  const [isLoadingSources, setIsLoadingSources] = useState(false);

  // --- Derived values for summary ---
  const validatedColumns = useMemo(() => {
    const cols = summary?.taskContext?.column_values ?? [];
    return cols
      .map((col) => {
        const title = col.column?.title?.trim() ?? "";
        const value = formatColumnValue(col);
        return { title, value };
      })
      .filter((c) => c.title && VALIDATED_COLUMN_TITLES.has(c.title) && c.value);
  }, [summary]);

  const csvParams = useMemo(() => {
    return summary?.taskContext?.csv_params ?? [];
  }, [summary]);

  // --- State/handlers for sources signed url ---
  const [signedUrls, setSignedUrls] = useState<Record<string, SignedUrlResponse>>({});
  const [signedUrlError, setSignedUrlError] = useState<string | null>(null);

  const baseUrl = useMemo(() => {
    return process.env.NEXT_PUBLIC_FASTAPI_BASE_URL?.replace(/\/$/, "") ?? "";
  }, []);

  const openSignedUrl = useCallback(
    async (fileId: string) => {
      setSignedUrlError(null);
      const cached = signedUrls[fileId];
      if (cached) {
        const expiresAt = new Date(cached.expiresAt).getTime();
        if (expiresAt - Date.now() > 60_000) {
          window.open(cached.url, "_blank", "noopener,noreferrer");
          return;
        }
      }
      try {
        const accessToken = await getAccessToken();
        if (!accessToken) {
          setSignedUrlError("Not authenticated.");
          return;
        }
        if (!baseUrl) {
          setSignedUrlError("FASTAPI base URL is not configured.");
          return;
        }
        const response = await fetch(
          `${baseUrl}/api/tasks/${externalTaskKey}/files/${fileId}/signed-url`,
          { headers: { Authorization: `Bearer ${accessToken}` } }
        );
        if (!response.ok) {
          setSignedUrlError(`Signed URL failed (${response.status})`);
          return;
        }
        const data = (await response.json()) as SignedUrlResponse;
        setSignedUrls((prev) => ({ ...prev, [fileId]: data }));
        window.open(data.url, "_blank", "noopener,noreferrer");
      } catch (e: any) {
        setSignedUrlError(`Signed URL error: ${String(e)}`);
      }
    },
    [baseUrl, externalTaskKey, signedUrls]
  );

  const appendAssistantChunk = useCallback((chunk: string) => {
    setMessages((prev) => {
      if (prev.length === 0 || prev[prev.length - 1].role !== "assistant") {
        return [...prev, { role: "assistant", content: chunk }];
      }
      const updated = [...prev];
      updated[updated.length - 1] = {
        ...updated[updated.length - 1],
        content: updated[updated.length - 1].content + chunk,
      };
      return updated;
    });
  }, []);

  const parseSse = useCallback(async (response: Response) => {
    const reader = response.body?.getReader();
    if (!reader) return;

    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 2);

        if (!rawEvent.startsWith("data:")) continue;
        const json = rawEvent.replace(/^data:\s*/, "");
        if (!json) continue;

        let evt: StreamEvent | null = null;
        try {
          evt = JSON.parse(json);
        } catch {
          continue;
        }

        if (!evt) continue;
        if (evt.type === "message" && evt.content) {
          appendAssistantChunk(evt.content);
        } else if (evt.type === "citations") {
          setCitations(evt.citations || []);
        }
      }
    }
  }, [appendAssistantChunk]);

  // --- Fetch summary and sources helpers ---
  const fetchSummary = useCallback(async (): Promise<TaskSummaryResponse | null> => {
    if (!externalTaskKey) return null;
    setIsLoadingSummary(true);
    setSummaryError(null);
    try {
      const accessToken = await getAccessToken();
      if (!accessToken) {
        setSummaryError("Not authenticated.");
        return null;
      }
      if (!baseUrl) {
        setSummaryError("FASTAPI base URL is not configured.");
        return null;
      }
      const response = await fetch(
        `${baseUrl}/api/tasks/${externalTaskKey}/summary`,
        {
          headers: { Authorization: `Bearer ${accessToken}` },
          cache: "no-store",
        }
      );
      if (!response.ok) {
        setSummaryError(`Summary failed (${response.status})`);
        return null;
      }
      const data = (await response.json()) as TaskSummaryResponse;
      setSummary(data);
      return data;
    } catch (e: any) {
      setSummaryError(`Summary error: ${String(e)}`);
      return null;
    } finally {
      setIsLoadingSummary(false);
    }
  }, [baseUrl, externalTaskKey]);

  const fetchSources = useCallback(async () => {
    if (!externalTaskKey) return;
    setIsLoadingSources(true);
    setSourcesError(null);
    try {
      const accessToken = await getAccessToken();
      if (!accessToken) {
        setSourcesError("Not authenticated.");
        return;
      }
      if (!baseUrl) {
        setSourcesError("FASTAPI base URL is not configured.");
        return;
      }
      const response = await fetch(
        `${baseUrl}/api/tasks/${externalTaskKey}/sources`,
        {
          headers: { Authorization: `Bearer ${accessToken}` },
          cache: "no-store",
        }
      );
      if (!response.ok) {
        setSourcesError(`Sources failed (${response.status})`);
        return;
      }
      const data = (await response.json()) as TaskSourcesResponse;
      setSources(data);
    } catch (e: any) {
      setSourcesError(`Sources error: ${String(e)}`);
    } finally {
      setIsLoadingSources(false);
    }
  }, [baseUrl, externalTaskKey]);

  const refreshTaskData = useCallback(async () => {
    await Promise.all([fetchSummary(), fetchSources()]);
  }, [fetchSummary, fetchSources]);

  useEffect(() => {
    if (externalTaskKey) {
      void refreshTaskData();
    }
  }, [externalTaskKey, refreshTaskData]);

  const sendMessage = useCallback(async () => {
    if (!input.trim() || isStreaming || !externalTaskKey) return;
    const prompt = input.trim();
    setInput("");
    setCitations([]);

    setMessages((prev) => [...prev, { role: "user", content: prompt }]);

    const accessToken = await getAccessToken();
    if (!accessToken) {
      appendAssistantChunk("Not authenticated. Please log in.");
      return;
    }
    if (!baseUrl) {
      appendAssistantChunk("FASTAPI base URL is not configured.");
      return;
    }

    setIsStreaming(true);
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const response = await fetch(`${baseUrl}/api/chat`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${accessToken}`,
        },
        body: JSON.stringify({
          externalTaskKey: decodeURIComponent(externalTaskKey),
          message: prompt,
          history: messages.map((m) => ({
            role: m.role,
            content: m.content,
          })),
        }),
        signal: controller.signal,
      });

      if (!response.ok || !response.body) {
        appendAssistantChunk(`Error: ${response.status}`);
        setIsStreaming(false);
        return;
      }

      await parseSse(response);
    } catch (e: any) {
      if (e?.name !== "AbortError") {
        appendAssistantChunk(`Error: ${String(e)}`);
      }
    } finally {
      setIsStreaming(false);
      abortRef.current = null;
    }
  }, [appendAssistantChunk, baseUrl, input, isStreaming, messages, externalTaskKey, parseSse]);

  const stopStreaming = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

  const pollTokenRef = useRef(0);

  const pollForSnapshotChange = useCallback(
    async (previousVersion: string | null) => {
      const token = ++pollTokenRef.current;
      const timeoutMs = 60_000;
      const intervalMs = 3_000;
      const start = Date.now();

      while (Date.now() - start < timeoutMs) {
        if (pollTokenRef.current !== token) {
          return false; // cancelled (new sync or unmount)
        }

        const data = await fetchSummary();
        const nextVersion = data?.snapshotVersion ?? null;
        const currentSyncStatus = data?.syncStatus;

        // If sync finished (success or failure), stop polling
        if (currentSyncStatus === "completed" || currentSyncStatus === "failed") {
          // Refresh sources to ensure we have the latest data
          await fetchSources();
          setSyncStatus(null); // Clear the ephemeral status message
          return true;
        }

        if (nextVersion && nextVersion !== previousVersion) {
          await fetchSources();
          return true;
        }

        await delay(intervalMs);
      }

      return false;
    },
    [fetchSummary, fetchSources]
  );

  // cancel pending poll on unmount
  useEffect(() => {
    return () => {
      pollTokenRef.current += 1;
    };
  }, []);

  const syncTask = useCallback(async () => {
    if (!externalTaskKey) return;

    const accessToken = await getAccessToken();
    if (!accessToken) {
      setSyncStatus("Not authenticated.");
      return;
    }
    if (!baseUrl) {
      setSyncStatus("FASTAPI base URL is not configured.");
      return;
    }

    setSyncStatus("Syncing...");
    try {
      const response = await fetch(
        `${baseUrl}/api/tasks/${externalTaskKey}/sync`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${accessToken}`,
          },
          body: JSON.stringify({ runAsync: true }),
        }
      );

      if (!response.ok) {
        setSyncStatus(`Sync failed (${response.status})`);
        return;
      }

      const previousVersion = summary?.snapshotVersion ?? null;

      setSyncStatus("Sync queued. Waiting for updates...");
      void pollForSnapshotChange(previousVersion);
    } catch (e: any) {
      setSyncStatus(`Sync error: ${String(e)}`);
    }
  }, [baseUrl, externalTaskKey, pollForSnapshotChange, summary?.snapshotVersion]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  // Show loading state if externalTaskKey is not yet available
  if (!externalTaskKey) {
    return (
      <main className="mx-auto mt-10 max-w-3xl px-4 pb-16">
        <p className="text-muted-foreground">Loading task...</p>
      </main>
    );
  }

  return (
    <main className="mx-auto mt-10 max-w-3xl px-4 pb-16">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Task</h1>
          <p className="text-muted-foreground mt-2">{externalTaskKey}</p>
        </div>
        <button
          onClick={syncTask}
          className="rounded border px-3 py-1 text-sm"
        >
          Sync task
        </button>
      </div>

      {syncStatus && (
        <p className="text-muted-foreground mt-2 text-sm">{syncStatus}</p>
      )}

      {/* --- Summary panel loading/errors/render --- */}
      {isLoadingSummary && <p className="text-sm text-muted-foreground">Loading summary…</p>}
      {summaryError && <p className="text-sm text-red-500">{summaryError}</p>}
      {summary && (
        <section className="mt-6 rounded border p-3">
          <div className="text-sm font-semibold">Summary</div>
          {validatedColumns.length === 0 ? (
            <p className="mt-2 text-sm text-muted-foreground">No validated columns found.</p>
          ) : (
            <dl className="mt-3 grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
              {validatedColumns.map((c) => (
                <div key={c.title}>
                  <dt className="text-muted-foreground">{c.title}</dt>
                  <dd className="font-medium text-foreground">{c.value}</dd>
                </div>
              ))}
            </dl>
          )}
          {csvParams.length > 0 && (
            <div className="mt-4">
              <div className="text-sm font-semibold">CSV Params</div>
              <div className="mt-2 space-y-3">
                {csvParams.map((csv, idx) => (
                  <div key={csv.assetId ?? `${csv.filename ?? "csv"}-${idx}`} className="rounded border p-2">
                    <div className="text-xs text-muted-foreground">
                      {csv.filename ?? "CSV"} • {csv.format}
                    </div>
                    {csv.format === "key_value" && csv.records?.length ? (
                      <table className="mt-2 w-full text-sm">
                        <thead>
                          <tr className="text-left text-xs text-muted-foreground">
                            <th className="pb-1">Parameter</th>
                            <th className="pb-1">Value</th>
                            <th className="pb-1">Source</th>
                          </tr>
                        </thead>
                        <tbody>
                          {csv.records.map((r) => (
                            <tr key={`${r.parameter}-${r.rowIndex}`}>
                              <td className="py-1 pr-2">{r.parameter}</td>
                              <td className="py-1 pr-2">{r.value}</td>
                              <td className="py-1">{r.source}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    ) : null}
                    {csv.format === "table" && (
                      <div className="mt-2 text-sm">
                        {csv.rows?.length ? (
                          <>
                            <div className="text-xs text-muted-foreground">
                              Showing {Math.min(5, csv.rows.length)} of {csv.rows.length} rows
                            </div>
                            <pre className="mt-2 whitespace-pre-wrap rounded bg-muted p-2 text-xs">
                              {JSON.stringify(csv.rows.slice(0, 5), null, 2)}
                            </pre>
                          </>
                        ) : (
                          <div className="text-xs text-muted-foreground">No rows found.</div>
                        )}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </section>
      )}

      {/* --- Sources panel loading/errors/render --- */}
      {isLoadingSources && <p className="text-sm text-muted-foreground">Loading sources…</p>}
      {sourcesError && <p className="text-sm text-red-500">{sourcesError}</p>}
      {sources && (
        <section className="mt-6 rounded border p-3">
          <div className="text-sm font-semibold">Sources</div>
          {signedUrlError && (
            <p className="mt-2 text-sm text-red-500">{signedUrlError}</p>
          )}
          {sources.files.length === 0 ? (
            <p className="mt-2 text-sm text-muted-foreground">No files found.</p>
          ) : (
            <ul className="mt-2 divide-y text-sm">
              {sources.files.map((file) => (
                <li key={file.id} className="py-2">
                  <div className="flex items-center justify-between gap-4">
                    <div>
                      <div className="font-medium text-foreground">
                        {file.originalFilename || "Untitled"}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {file.kind} • {formatBytes(file.sizeBytes)} • {formatDate(file.createdAt)}
                      </div>
                    </div>
                    <button
                      onClick={() => openSignedUrl(file.id)}
                      className="rounded border px-2 py-1 text-xs"
                    >
                      View / Download
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <div className="mt-8 space-y-4">
        {messages.map((m, i) => (
          <div
            key={i}
            className={`rounded border px-3 py-2 ${
              m.role === "user" ? "bg-background" : "bg-muted"
            }`}
          >
            <div className="text-xs uppercase text-muted-foreground">
              {m.role}
            </div>
            <div className="whitespace-pre-wrap">{m.content}</div>
          </div>
        ))}
      </div>

      {citations.length > 0 && (
        <div className="mt-6 rounded border p-3">
          <div className="text-sm font-semibold">Citations</div>
          <ul className="mt-2 space-y-2 text-sm">
            {citations.map((c, idx) => (
              <li key={idx} className="text-muted-foreground">
                <div className="font-medium text-foreground">
                  {c.filename || "Untitled"}
                </div>
                <div>
                  {c.page != null ? `Page ${c.page}` : "Page N/A"}
                  {c.section ? ` • ${c.section}` : ""}
                </div>
                {c.snippet && (
                  <div className="mt-1 whitespace-pre-wrap">{c.snippet}</div>
                )}
                {c.fileId ? (
                  <div className="mt-2">
                    <button
                      onClick={() => openSignedUrl(c.fileId as string)}
                      className="rounded border px-2 py-1 text-xs"
                    >
                      View source
                    </button>
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="mt-8 flex gap-2">
        <textarea
          className="min-h-[80px] w-full rounded border px-3 py-2"
          placeholder="Ask a question about this task..."
          value={input}
          onChange={(e) => setInput(e.target.value)}
        />
      </div>

      <div className="mt-2 flex items-center gap-2">
        <button
          onClick={sendMessage}
          disabled={isStreaming || !input.trim()}
          className="rounded bg-foreground px-3 py-1 text-sm text-background disabled:opacity-50"
        >
          Send
        </button>
        {isStreaming && (
          <button
            onClick={stopStreaming}
            className="rounded border px-3 py-1 text-sm"
          >
            Stop
          </button>
        )}
      </div>
    </main>
  );
}
