"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { ClipboardList, Files, MessageSquare, UserRound } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize from "rehype-sanitize";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { startTaskPolling } from "@/lib/task-polling";

type ChatMessage = {
  role: "user" | "assistant";
  content: string;
  citations?: Citation[];
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
  name?: string | null;
  column_values?: ColumnValue[];
  csv_params?: Array<CsvParamKeyValue | CsvParamTable>;
  monday_metadata?: {
    revision: string | null;
    checkedAt: string | null;
    changedAt: string | null;
    refreshPending: boolean;
    refreshError: string | null;
    fields: Array<{ columnId: string; title: string; displayValue: string; state: "set" | "empty" }>;
  };
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
  syncStartedAt?: string | null;
  syncCompletedAt?: string | null;
};

type TaskSourceFile = {
  id: string;
  kind: string;
  originalFilename?: string | null;
  mimeType?: string | null;
  sizeBytes?: number | null;
  mondayAssetId?: string | null;
  storageStatus: "stored" | "unsupported";
  storageErrorCode?: string | null;
  storageErrorDetail?: string | null;
  downloadAvailable: boolean;
  createdAt?: string | null;
};

type TaskSourcesResponse = {
  snapshotVersion?: string | null;
  files: TaskSourceFile[];
};

// Extended Citation type
type Citation = {
  sourceId?: string | null;
  filename?: string | null;
  page?: number | null;
  section?: string | null;
  snippet?: string | null;
  score?: number | null;
  fileId?: string | null;
  mondayAssetId?: string | null;
};

type ChatCompleteResponse = {
  content: string;
  citations?: Citation[];
  ok?: boolean;
};

type SignedUrlResponse = { url: string; expiresAt: string };

const CSRF_COOKIE_NAME = "daa_csrf";

// --- Summary helpers ---
const VALIDATED_COLUMN_TITLES = new Set([
  "Accounts",
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
  "Date Sort",
]);

const CRM_FIELDS = [
  ["board_relation_mm3c4g5x", "Accounts"],
  ["dropdown_mkpb98es", "New Enq / Amend"],
  ["board_relation_mkpbm5np", "TP Ref"],
  ["lookup_mkpb44am", "Project Name"],
  ["dropdown_mkpbafca", "Zip Code"],
] as const;

const Markdown = ({ children }: { children: string }) => (
  <div className="task-markdown max-w-none text-sm leading-6">
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeSanitize]}
      components={{
        a: ({ node, ...props }) => (
          <a {...props} className="break-all">
            {props.children}
          </a>
        ),
        pre: ({ node, ...props }) => (
          <pre {...props} className="whitespace-pre-wrap wrap-break-word overflow-hidden" />
        ),
        code: ({ node, ...props }) => (
          <code {...props} className="wrap-break-word" />
        ),
      }}
    >
      {children}
    </ReactMarkdown>
  </div>
);

function CitationExcerpt({ snippet }: { snippet: string }) {
  return (
    <details className="group/excerpt mt-3 min-w-0">
      <summary className="cursor-pointer list-none rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 [&::-webkit-details-marker]:hidden">
        <span className="line-clamp-3 whitespace-pre-line text-sm leading-6 text-muted-foreground [overflow-wrap:anywhere] group-open/excerpt:hidden">
          <ReactMarkdown allowedElements={[]} unwrapDisallowed skipHtml>
            {snippet}
          </ReactMarkdown>
        </span>
        <span className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-primary">
          <span className="group-open/excerpt:hidden">Show more</span>
          <span className="hidden group-open/excerpt:inline">Show less</span>
          <svg aria-hidden="true" viewBox="0 0 16 16" fill="none" className="h-3 w-3 transition-transform group-open/excerpt:rotate-180">
            <path d="m4 6 4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
      </summary>
      <div className="mt-2">
        <Markdown>{snippet}</Markdown>
      </div>
    </details>
  );
}

function getCookie(name: string): string | null {
  const value = document.cookie
    .split("; ")
    .find((part) => part.startsWith(`${name}=`));
  return value ? decodeURIComponent(value.split("=").slice(1).join("=")) : null;
}

function csrfHeaders(): HeadersInit {
  const token = getCookie(CSRF_COOKIE_NAME);
  return token ? { "X-CSRF-Token": token } : {};
}

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

function findColumnValue(cols: ColumnValue[], titles: string[]): string {
  const wanted = new Set(titles.map((title) => title.trim().toLowerCase()));
  for (const col of cols) {
    const title = col.column?.title?.trim().toLowerCase() ?? "";
    if (!wanted.has(title)) continue;
    const value = formatColumnValue(col).trim();
    if (value) return value;
  }
  return "";
}

function projectNumberFromFilename(filename?: string | null): string {
  const match = filename?.match(/\bTP[\s_-]*(\d{4,})(?=[^\d]|$)/i);
  return match?.[1] ?? "";
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
  const [input, setInput] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [syncStatus, setSyncStatus] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const [sessionExpired, setSessionExpired] = useState(false);
  const sessionExpiredRef = useRef(false);

  // --- New state for summary and sources ---
  const [summary, setSummary] = useState<TaskSummaryResponse | null>(null);
  const [sources, setSources] = useState<TaskSourcesResponse | null>(null);
  const [summaryError, setSummaryError] = useState<string | null>(null);
  const [sourcesError, setSourcesError] = useState<string | null>(null);
  const [isLoadingSummary, setIsLoadingSummary] = useState(false);
  const [isLoadingSources, setIsLoadingSources] = useState(false);
  const [refreshCount, setRefreshCount] = useState(0);
  const sourceVersionRef = useRef<string | null>(null);
  const mondayMetadata = summary?.taskContext?.monday_metadata;

  useEffect(() => {
    setSummary(null);
    setSources(null);
    sourceVersionRef.current = null;
  }, [externalTaskKey]);

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

  const mondayProject = useMemo(() => {
    if (summary?.taskContext?.monday_metadata?.revision) return "";
    const cols = summary?.taskContext?.column_values ?? [];
    const columnValue = findColumnValue(cols, [
      "Project",
      "Monday Project",
      "Project Number",
      "Project No",
      "Project Ref",
    ]);
    if (columnValue) return columnValue;

    for (const file of sources?.files ?? []) {
      const projectNumber = projectNumberFromFilename(file.originalFilename);
      if (projectNumber) return projectNumber;
    }

    return "";
  }, [sources, summary]);

  const summaryFields = useMemo(() => {
    const columns = summary?.taskContext?.column_values ?? [];
    const crm = CRM_FIELDS.map(([id, title]) => {
      const field = mondayMetadata?.fields.find((entry) => entry.columnId === id);
      const legacy = columns.find((col) => col.id === id);
      return { title, value: field
        ? field.displayValue || "Not set"
        : (legacy ? formatColumnValue(legacy) : "") || "Not yet checked" };
    });
    const other = validatedColumns.filter((col) => !CRM_FIELDS.some(([, title]) => title === col.title));
    return [...crm, ...(mondayProject ? [{ title: "Monday Project", value: mondayProject }] : []), ...other];
  }, [mondayProject, validatedColumns, mondayMetadata, summary]);

  const csvParams = useMemo(() => {
    return summary?.taskContext?.csv_params ?? [];
  }, [summary]);

  const hasSnapshot = Boolean(summary?.snapshotVersion);
  const hasSourcesSnapshot = Boolean(sources?.snapshotVersion);

  const isImageFile = (file: TaskSourceFile) => {
    if (file.kind === "attachment_image") return true;
    if (file.mimeType?.startsWith("image/")) return true;
    return /\.(png|jpe?g|gif|bmp|webp)$/i.test(file.originalFilename ?? "");
  };
  
  const visibleSources = sources?.files.filter((file) => !isImageFile(file)) ?? [];

  const isAwaitingFirstToken =
    isStreaming && (messages.length === 0 || messages[messages.length - 1].role !== "assistant");

  // --- State/handlers for sources signed url ---
  const [signedUrls, setSignedUrls] = useState<Record<string, SignedUrlResponse>>({});
  const [signedUrlError, setSignedUrlError] = useState<string | null>(null);

  const baseUrl = useMemo(() => {
    return process.env.NEXT_PUBLIC_FASTAPI_BASE_URL?.replace(/\/$/, "") ?? "";
  }, []);

  // Share one recovery message across every authenticated task request.
  const handleUnauthorized = useCallback((response: Response) => {
    if (response.status === 401 && !sessionExpiredRef.current) {
      sessionExpiredRef.current = true;
      setSessionExpired(true);
      setSummaryError(null);
      setSourcesError(null);
      setSignedUrlError(null);
      setSyncStatus(null);
      setIsLoadingSummary(false);
      setIsLoadingSources(false);
      setIsStreaming(false);
      abortRef.current?.abort();
    }
    // Ignore other in-flight responses once the session has expired, too.
    return sessionExpiredRef.current;
  }, []);

  const openSignedUrl = useCallback(
    async (fileId: string) => {
      if (sessionExpiredRef.current) return;
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
        if (!baseUrl) {
          setSignedUrlError("FASTAPI base URL is not configured.");
          return;
        }
        const response = await fetch(
          `${baseUrl}/api/tasks/${externalTaskKey}/files/${fileId}/signed-url`,
          { credentials: "include" }
        );
        if (handleUnauthorized(response)) return;
        if (!response.ok) {
          setSignedUrlError(`Signed URL failed (${response.status})`);
          return;
        }
        const data = (await response.json()) as SignedUrlResponse;
        if (sessionExpiredRef.current) return;
        setSignedUrls((prev) => ({ ...prev, [fileId]: data }));
        window.open(data.url, "_blank", "noopener,noreferrer");
      } catch (e: any) {
        if (sessionExpiredRef.current) return;
        setSignedUrlError(`Signed URL error: ${String(e)}`);
      }
    },
    [baseUrl, externalTaskKey, signedUrls, handleUnauthorized]
  );

  const appendAssistantChunk = useCallback((chunk: string, citations: Citation[] = []) => {
    setMessages((prev) => {
      if (prev.length === 0 || prev[prev.length - 1].role !== "assistant") {
        return [...prev, { role: "assistant", content: chunk, citations }];
      }
      const updated = [...prev];
      updated[updated.length - 1] = {
        ...updated[updated.length - 1],
        content: updated[updated.length - 1].content + chunk,
        citations:
          citations.length > 0
            ? citations
            : updated[updated.length - 1].citations,
      };
      return updated;
    });
  }, []);

  // --- Fetch summary and sources helpers ---
  const fetchSummary = useCallback(async (signal: AbortSignal, silent = false): Promise<TaskSummaryResponse | null> => {
    if (!externalTaskKey || sessionExpiredRef.current) return null;
    if (!silent) setIsLoadingSummary(true);
    try {
      if (!baseUrl) {
        setSummaryError("FASTAPI base URL is not configured.");
        return null;
      }
      const response = await fetch(
        `${baseUrl}/api/tasks/${externalTaskKey}/summary`,
        {
          credentials: "include",
          cache: "no-store",
          signal,
        }
      );
      if (signal.aborted || handleUnauthorized(response)) return null;
      if (!response.ok) {
        setSummaryError(`Summary failed (${response.status})`);
        return null;
      }
      const data = (await response.json()) as TaskSummaryResponse;
      if (signal.aborted || sessionExpiredRef.current) return null;
      setSummaryError(null);
      setSummary(data);
      return data;
    } catch (e: any) {
      if (signal.aborted || sessionExpiredRef.current) return null;
      setSummaryError(`Summary error: ${String(e)}`);
      return null;
    } finally {
      if (!signal.aborted) setIsLoadingSummary(false);
    }
  }, [baseUrl, externalTaskKey, handleUnauthorized]);

  const fetchSources = useCallback(async (signal: AbortSignal): Promise<boolean> => {
    if (!externalTaskKey || sessionExpiredRef.current) return false;
    setIsLoadingSources(true);
    setSourcesError(null);
    try {
      if (!baseUrl) {
        setSourcesError("FASTAPI base URL is not configured.");
        return false;
      }
      const response = await fetch(
        `${baseUrl}/api/tasks/${externalTaskKey}/sources`,
        {
          credentials: "include",
          cache: "no-store",
          signal,
        }
      );
      if (signal.aborted || handleUnauthorized(response)) return false;
      if (!response.ok) {
        setSourcesError(`Sources failed (${response.status})`);
        return false;
      }
      const data = (await response.json()) as TaskSourcesResponse;
      if (signal.aborted || sessionExpiredRef.current) return false;
      setSources(data);
      return true;
    } catch (e: any) {
      if (signal.aborted || sessionExpiredRef.current) return false;
      setSourcesError(`Sources error: ${String(e)}`);
      return false;
    } finally {
      if (!signal.aborted) setIsLoadingSources(false);
    }
  }, [baseUrl, externalTaskKey, handleUnauthorized]);

  useEffect(() => {
    if (!externalTaskKey || sessionExpired) return;
    return startTaskPolling(async (signal, first) => {
      const data = await fetchSummary(signal, !first);
      if (data && !signal.aborted) {
        if (data.syncStatus === "completed" || data.syncStatus === "failed") setSyncStatus(null);
        const sourceVersion = `${externalTaskKey}:${data.snapshotVersion}:${Boolean(data.taskContext?.monday_metadata?.revision)}`;
        if (sourceVersionRef.current !== sourceVersion && await fetchSources(signal)) {
          sourceVersionRef.current = sourceVersion;
        }
      }
    }, (error) => setSummaryError(`Refresh error: ${String(error)}`));
  }, [externalTaskKey, fetchSummary, fetchSources, refreshCount, sessionExpired]);

  const sendMessage = useCallback(async () => {
    if (!input.trim() || isStreaming || !externalTaskKey || sessionExpiredRef.current) return;
    const prompt = input.trim();
    setInput("");

    setMessages((prev) => [...prev, { role: "user", content: prompt }]);

    if (!baseUrl) {
      appendAssistantChunk("FASTAPI base URL is not configured.");
      return;
    }

    setIsStreaming(true);
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const response = await fetch(`${baseUrl}/api/chat/complete`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...csrfHeaders(),
        },
        body: JSON.stringify({
          externalTaskKey: decodeURIComponent(externalTaskKey),
          message: prompt,
          history: messages.map((m) => ({
            role: m.role,
            content: m.content,
          })),
        }),
        credentials: "include",
        signal: controller.signal,
      });

      if (handleUnauthorized(response)) {
        setInput((current) => current || prompt);
        return;
      }
      if (!response.ok) {
        appendAssistantChunk(`Error: ${response.status}`);
        setIsStreaming(false);
        return;
      }

      const data = (await response.json()) as ChatCompleteResponse;
      if (sessionExpiredRef.current) return;
      if (data.content) {
        appendAssistantChunk(data.content, data.citations || []);
      } else {
        appendAssistantChunk(
          "I found relevant sources, but no final answer was returned. Please try again."
        );
      }
    } catch (e: any) {
      if (sessionExpiredRef.current) {
        setInput((current) => current || prompt);
      } else if (e?.name !== "AbortError") {
        appendAssistantChunk(`Error: ${String(e)}`);
      }
    } finally {
      setIsStreaming(false);
      abortRef.current = null;
    }
  }, [appendAssistantChunk, baseUrl, input, isStreaming, messages, externalTaskKey, handleUnauthorized]);

  const stopStreaming = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const syncTask = useCallback(async () => {
    if (!externalTaskKey || sessionExpiredRef.current) return;

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
            ...csrfHeaders(),
          },
          body: JSON.stringify({ runAsync: true }),
          credentials: "include",
        }
      );

      if (handleUnauthorized(response)) return;
      if (!response.ok) {
        setSyncStatus(`Sync failed (${response.status})`);
        return;
      }

      setSyncStatus("Sync queued. Waiting for updates...");
      setRefreshCount((count) => count + 1);
    } catch (e: any) {
      if (sessionExpiredRef.current) return;
      setSyncStatus(`Sync error: ${String(e)}`);
    }
  }, [baseUrl, externalTaskKey, handleUnauthorized]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  // Show loading state if externalTaskKey is not yet available
  if (!externalTaskKey) {
    return (
      <main className="mx-auto min-h-screen max-w-5xl px-5 py-8">
        <p className="text-muted-foreground">Loading task...</p>
      </main>
    );
  }

  return (
    <main className="mx-auto min-h-screen max-w-5xl px-5 py-8">
      <div className="flex flex-col gap-4 border-b border-border pb-5 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-primary">
            Design Automation Assistant
          </p>
          <h1 className="mt-1 text-2xl font-semibold text-foreground">Task</h1>
          <p className="mt-2 break-all text-sm text-muted-foreground">{externalTaskKey}</p>
        </div>
        <button
          onClick={syncTask}
          disabled={sessionExpired}
          className="inline-flex items-center justify-center rounded-md bg-primary px-3 py-2 text-sm font-semibold text-primary-foreground shadow-sm shadow-primary/20 transition hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:cursor-not-allowed disabled:opacity-50"
        >
          Sync task
        </button>
      </div>

      {sessionExpired && (
        <Alert className="mt-5 border-primary/30 bg-card" aria-labelledby="session-expired-title">
          <AlertTitle id="session-expired-title">Your session has expired</AlertTitle>
          <AlertDescription>
            To continue, reopen this task from Monday CRM and authorise again if prompted.
            {input.trim() && <p className="mt-2">You can copy your unsent question below before leaving this tab.</p>}
          </AlertDescription>
        </Alert>
      )}

      {summary?.syncStatus && (
        <p className="mt-3 inline-flex rounded-full bg-accent px-3 py-1 text-xs font-medium text-accent-foreground">
          Server Sync Status: {summary.syncStatus}
          {summary.syncCompletedAt ? ` • Completed ${new Date(summary.syncCompletedAt).toLocaleString()}` : ""}
        </p>
      )}

      {/* --- Summary panel loading/errors/render --- */}
      {isLoadingSummary && <p className="text-sm text-muted-foreground">Loading summary…</p>}
      {summaryError && <p className="text-sm text-red-500">{summaryError}</p>}
      {summary && (
        <section className="mt-6 rounded-lg border border-border bg-card p-5 shadow-sm">
          <div className="flex items-center gap-3">
            <ClipboardList aria-hidden="true" className="h-4 w-4 text-primary" strokeWidth={1.75} />
            <div className="text-sm font-semibold text-foreground">Summary</div>
          </div>
          {mondayMetadata && (
            <div className="mt-3 space-y-1 text-xs text-muted-foreground" aria-live="polite">
              <p>{mondayMetadata.checkedAt
                ? `Monday details last checked ${new Date(mondayMetadata.checkedAt).toLocaleString()}`
                : "Monday details have not been checked yet."}
                {mondayMetadata.refreshPending && !mondayMetadata.refreshError ? " · Refresh pending" : ""}
              </p>
              {mondayMetadata.refreshError && <p role="status">{mondayMetadata.refreshError} Showing the last available values.</p>}
              {mondayMetadata.revision && !sessionExpired && <a className="underline" href={`${baseUrl}/api/tasks/${externalTaskKey}/monday-columns`} target="_blank" rel="noreferrer">View current Monday details</a>}
            </div>
          )}
          {!hasSnapshot && !mondayMetadata?.revision ? (
            <p className="mt-2 text-sm text-muted-foreground">Task data not yet synced.</p>
          ) : summaryFields.length === 0 ? (
            <p className="mt-2 text-sm text-muted-foreground">No validated columns found.</p>
          ) : (
            <dl className="mt-4 grid grid-cols-1 gap-x-8 gap-y-4 text-sm sm:grid-cols-2">
              {summaryFields.map((c) => (
                <div key={c.title} className="rounded-md bg-secondary/70 px-3 py-2">
                  <dt className="text-xs font-medium text-muted-foreground">{c.title}</dt>
                  <dd className="mt-1 font-semibold text-foreground">{c.value}</dd>
                </div>
              ))}
            </dl>
          )}
          {csvParams.length > 0 && (
            <div className="mt-4">
              <div className="text-sm font-semibold">Source Email Metadata</div>
              <div className="mt-2 space-y-3">
                {csvParams.map((csv, idx) => (
                  <div key={csv.assetId ?? `${csv.filename ?? "csv"}-${idx}`} className="rounded-md border border-border bg-background p-3">
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
                            <pre className="mt-2 whitespace-pre-wrap rounded-md bg-secondary p-3 text-xs">
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
        <section className="mt-6 rounded-lg border border-border bg-card p-5 shadow-sm">
          <div className="flex items-center gap-3">
            <Files aria-hidden="true" className="h-4 w-4 text-primary" strokeWidth={1.75} />
            <div className="text-sm font-semibold text-foreground">Sources</div>
          </div>
          {signedUrlError && (
            <p className="mt-2 text-sm text-red-500">{signedUrlError}</p>
          )}
          {!hasSourcesSnapshot ? (
            <p className="mt-2 text-sm text-muted-foreground">Task data not yet synced.</p>
          ) : visibleSources.length === 0 ? (
            <p className="mt-2 text-sm text-muted-foreground">No files found.</p>
          ) : (
            <ul className="mt-3 divide-y divide-border text-sm">
              {visibleSources.map((file) => (
                <li key={file.id} className="py-3 first:pt-0 last:pb-0">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0">
                      <div className="font-medium text-foreground">
                        {file.originalFilename || "Untitled"}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {file.kind} • {formatBytes(file.sizeBytes)} • {formatDate(file.createdAt)}
                      </div>
                    </div>
                    {file.downloadAvailable ? (
                      <button
                        onClick={() => openSignedUrl(file.id)}
                        disabled={sessionExpired}
                        className="inline-flex items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium text-foreground transition hover:border-primary/50 hover:text-primary disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        View / Download
                      </button>
                    ) : (
                      <div
                        className="text-xs font-medium text-amber-700"
                        title={file.storageErrorDetail ?? undefined}
                      >
                        {file.storageErrorCode === "object_too_large"
                          ? "Too large to store"
                          : "Unavailable"}
                      </div>
                    )}
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
            className={`min-w-0 rounded-lg border border-border bg-card shadow-sm ${
              m.role === "user" ? "px-4 py-3" : "p-5"
            }`}
          >
            {m.role === "assistant" ? (
              <div className="flex items-center gap-3">
                <MessageSquare aria-hidden="true" className="h-4 w-4 text-primary" strokeWidth={1.75} />
                <h3 className="text-sm font-semibold text-foreground">Assistant</h3>
              </div>
            ) : (
              <div className="flex items-center gap-3">
                <UserRound aria-hidden="true" className="h-4 w-4 text-primary" strokeWidth={1.75} />
                <h3 className="text-sm font-semibold text-foreground">User</h3>
              </div>
            )}
            {m.role === "assistant" ? (
              <div className="mt-4 min-w-0 rounded-md bg-secondary/70 p-4">
                <Markdown>{m.content}</Markdown>
              </div>
            ) : (
              <div className="whitespace-pre-wrap">{m.content}</div>
            )}
            {m.role === "assistant" && (m.citations?.length ?? 0) > 0 && (
              <section className="mt-6" aria-label="Citation sources">
                <div className="flex items-center gap-2">
                  <h4 className="text-sm font-semibold text-foreground">Sources</h4>
                  <span className="rounded-full bg-secondary px-2 py-0.5 text-xs font-medium text-muted-foreground">
                    {m.citations?.length}
                  </span>
                </div>
                <ul className="mt-3 space-y-3 text-sm">
                  {m.citations?.map((citation, citationIndex) => (
                    <li
                      key={`${citation.sourceId ?? "source"}-${citationIndex}`}
                      className="min-w-0 rounded-md border border-border bg-background p-3 sm:p-4"
                    >
                      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                        <div className="flex min-w-0 items-start gap-2">
                          {citation.sourceId && (
                            <span className="shrink-0 rounded bg-primary/10 px-2 py-1 text-xs font-semibold text-primary">
                              [{citation.sourceId}]
                            </span>
                          )}
                          <div className="min-w-0">
                            <div className="font-medium leading-6 text-foreground [overflow-wrap:anywhere]">
                              {citation.filename || "Untitled"}
                            </div>
                            {(citation.page != null || citation.section) && (
                              <div className="mt-1 text-xs text-muted-foreground [overflow-wrap:anywhere]">
                                {[citation.page != null ? `Page ${citation.page}` : null, citation.section]
                                  .filter(Boolean)
                                  .join(" • ")}
                              </div>
                            )}
                          </div>
                        </div>
                        {citation.fileId ? (
                          <button
                            type="button"
                            onClick={() => openSignedUrl(citation.fileId as string)}
                            disabled={sessionExpired}
                            aria-label={`View source ${citation.sourceId || citation.filename || citationIndex + 1}`}
                            className="inline-flex shrink-0 items-center justify-center self-start rounded-md border border-border bg-card px-3 py-1.5 text-xs font-medium text-foreground transition hover:border-primary/50 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            View source
                          </button>
                        ) : null}
                      </div>
                      {citation.snippet && <CitationExcerpt snippet={citation.snippet} />}
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </div>
        ))}
        {isAwaitingFirstToken && (
          <div className="rounded-lg border border-border bg-card p-5 shadow-sm">
            <div className="flex items-center gap-3">
              <MessageSquare aria-hidden="true" className="h-4 w-4 text-primary" strokeWidth={1.75} />
              <h3 className="text-sm font-semibold text-foreground">Assistant</h3>
            </div>
            <div className="mt-4 flex items-center gap-2 rounded-md bg-secondary/70 p-4 text-sm text-muted-foreground">
              Thinking
              <span className="inline-flex items-center gap-1">
                <span className="h-1 w-1 rounded-full bg-muted-foreground animate-bounce" />
                <span className="h-1 w-1 rounded-full bg-muted-foreground animate-bounce [animation-delay:100ms]" />
                <span className="h-1 w-1 rounded-full bg-muted-foreground animate-bounce [animation-delay:200ms]" />
              </span>
            </div>
          </div>
        )}
      </div>

      <div className="mt-8 flex gap-2">
        <textarea
          className="min-h-[96px] w-full rounded-lg border border-input bg-card px-4 py-3 text-sm shadow-sm outline-none transition placeholder:text-muted-foreground focus:border-primary focus:ring-2 focus:ring-primary/20"
          placeholder="Ask a question about this task..."
          value={input}
          readOnly={sessionExpired}
          onChange={(e) => setInput(e.target.value)}
        />
      </div>

      <div className="mt-2 flex items-center gap-2">
        <button
          onClick={sendMessage}
          disabled={sessionExpired || isStreaming || !input.trim()}
          aria-busy={isStreaming}
          className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground shadow-sm shadow-primary/20 transition hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isStreaming ? "Thinking..." : "Send"}
          {isStreaming && (
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-background border-t-transparent" />
          )}
        </button>
        {isStreaming && (
          <button
            onClick={stopStreaming}
            className="rounded-md border border-border bg-card px-4 py-2 text-sm font-medium text-foreground transition hover:border-primary/50 hover:text-primary"
          >
            Stop
          </button>
        )}
      </div>
    </main>
  );
}
