"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  FIELD_NAMES, bootstrapReviewSession, createReviewApi, ReviewApiError,
  type ReviewContext, type ReviewDraft, type ReviewWorkspace as Workspace,
  type ReviewSession, type FieldOperation,
} from "@/lib/specification-review-api";
import {
  emptyDraft, hasUnsavedChanges, saveCommand, approvalCommand, canComplete,
  mergeStatus, friendlyReviewError, sameReviewIdentity, updateSelection, updateField,
  draftValidationErrors,
  completionIssues, rebaseDraft,
} from "@/lib/specification-review-state";
import {
  loadMondayReviewSdk, getMondayReviewSession, listenMondayReviewContext,
  openMondayReviewAuthorization, type MondayReviewSdk,
} from "@/lib/specification-review-monday";
import FieldEditor from "./FieldEditor";
import CandidateSelector from "./CandidateSelector";
import PdfPreview from "./PdfPreview";
import ReviewStatus from "./ReviewStatus";
import { getReviewStatusDescription } from "@/lib/specification-review-status";
import "./review.css";

type Api = ReturnType<typeof createReviewApi>;
const apiBase = process.env.NEXT_PUBLIC_FASTAPI_BASE_URL || "";
const contextKey = (value: ReviewContext) => `${value.accountId}/${value.boardId}/${value.itemId}`;
const button = "review-button rounded-md border px-3 py-2 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50";

export default function ReviewWorkspace() {
  const [sdk, setSdk] = useState<MondayReviewSdk | null>(null);
  const [context, setContext] = useState<ReviewContext | null>(null);
  const [renewal, setRenewal] = useState(0);
  const [connection, setConnection] = useState("connecting");
  const [oauth, setOauth] = useState<string | null>(null);
  const [api, setApi] = useState<Api | null>(null);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [draft, setDraft] = useState<ReviewDraft>(emptyDraft);
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [pdfRevision, setPdfRevision] = useState<string | null>(null);
  const [pdfLoading, setPdfLoading] = useState(false);
  const [pdfError, setPdfError] = useState<string | null>(null);
  const [pdfRetry, setPdfRetry] = useState(0);
  const [checkedPdf, setCheckedPdf] = useState(false);
  const [sdkRetry, setSdkRetry] = useState(0);
  const active = useRef<{ key: string; controller: AbortController } | null>(null);
  const apiRef = useRef<Api | null>(null);
  const workspaceRef = useRef(workspace);
  const draftRef = useRef(draft);
  const busyRef = useRef(false);
  const receipt = useRef<{ digest: string; key: string } | null>(null);
  workspaceRef.current = workspace;
  draftRef.current = draft;

  const reportError = useCallback((failure: unknown) => {
    if (failure instanceof DOMException && failure.name === "AbortError") return;
    setError(friendlyReviewError(failure));
    if (failure instanceof ReviewApiError && failure.code === "authorization_expired") {
      apiRef.current?.destroy();
      setConnection("expired");
    }
    if (failure instanceof ReviewApiError && failure.code === "reviewer_forbidden") {
      apiRef.current?.destroy();
      setConnection("unavailable");
      setCheckedPdf(false);
    }
    if (failure instanceof ReviewApiError && ["stale_review", "source_changed", "scope_changed"].includes(failure.code)) {
      setConflict(true);
      setCheckedPdf(false);
    }
  }, []);

  const receiveWorkspace = useCallback((next: Workspace) => {
    const before = workspaceRef.current;
    if (before && hasUnsavedChanges(draftRef.current) && !sameReviewIdentity(before, next)) {
      setConflict(true);
      setCheckedPdf(false);
      // Revoked capabilities apply immediately, even while the old draft is kept.
      setWorkspace({ ...before, capabilities: next.capabilities, scope_reason: next.scope_reason,
        analysis: next.analysis, provisional: next.provisional,
        status: ["read_only", "analysis_pending", "source_waiting"].includes(next.status) ? next.status : before.status });
      return;
    }
    if (!before || !sameReviewIdentity(before, next)) setCheckedPdf(false);
    workspaceRef.current = next;
    setWorkspace(next);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    let stop: (() => void) | undefined;
    let observed = false;
    setConnection("connecting");
    setError(null);
    void (async () => {
      try {
        const nextSdk = await loadMondayReviewSdk(controller.signal);
        if (controller.signal.aborted) return;
        setSdk(nextSdk);
        const select = (next: ReviewContext) => {
          if (controller.signal.aborted) return;
          observed = true;
          if (active.current && active.current.key !== contextKey(next)) {
            active.current.controller.abort();
            apiRef.current?.destroy();
          }
          setContext((previous) => previous && contextKey(previous) === contextKey(next) ? previous : next);
        };
        stop = listenMondayReviewContext(nextSdk, select, (failure) => {
          observed = true;
          active.current?.controller.abort(); apiRef.current?.destroy(); apiRef.current = null;
          active.current = null; receipt.current = null; busyRef.current = false; setBusy(false);
          setContext(null); setApi(null); setWorkspace(null); workspaceRef.current = null;
          setDraft(emptyDraft()); draftRef.current = emptyDraft(); setConflict(false);
          setOauth(null); setNotice(null);
          setPdfUrl(null); setPdfRevision(null); setCheckedPdf(false); setConnection("unavailable");
          reportError(failure);
        });
        const initial = await getMondayReviewSession(nextSdk, controller.signal);
        if (!observed) select(initial.context);
      } catch (failure) {
        if (!controller.signal.aborted) { setConnection("unavailable"); reportError(failure); }
      }
    })();
    return () => { controller.abort(); stop?.(); };
  }, [sdkRetry, reportError]);

  useEffect(() => {
    if (!sdk || !context) return;
    const key = contextKey(context);
    const changedItem = active.current?.key !== key;
    active.current?.controller.abort();
    apiRef.current?.destroy();
    const controller = new AbortController();
    active.current = { key, controller };
    setApi(null);
    apiRef.current = null;
    setConnection("connecting");
    setOauth(null);
    setError(null);
    setCheckedPdf(false);
    if (changedItem) {
      workspaceRef.current = null;
      draftRef.current = emptyDraft();
      setWorkspace(null); setDraft(emptyDraft()); setConflict(false);
      setNotice(null); setPdfUrl(null); setPdfRevision(null); receipt.current = null;
      busyRef.current = false; setBusy(false);
    }
    void (async () => {
      try {
        const credentials = await getMondayReviewSession(sdk, controller.signal);
        if (controller.signal.aborted) return;
        if (contextKey(credentials.context) !== key) { setContext(credentials.context); return; }
        const session: ReviewSession = await bootstrapReviewSession(apiBase, credentials.sessionToken, context, controller.signal);
        if (controller.signal.aborted) return;
        if (session.status === "oauth_required") {
          setOauth(session.authorization_url); setConnection("oauth_required"); return;
        }
        const nextApi = createReviewApi(apiBase, session);
        if (contextKey(nextApi.context) !== key) { nextApi.destroy(); throw new Error("The review session belongs to another item. Reopen this Monday tab."); }
        apiRef.current = nextApi; setApi(nextApi); setConnection("ready");
        const next = await nextApi.workspace(controller.signal);
        if (!controller.signal.aborted) receiveWorkspace(next);
      } catch (failure) {
        if (!controller.signal.aborted) { setConnection("unavailable"); reportError(failure); }
      }
    })();
    return () => { controller.abort(); apiRef.current?.destroy(); };
  }, [sdk, context, renewal, reportError, receiveWorkspace]);

  useEffect(() => {
    if (!api || connection !== "ready") return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let reading = false;
    let lastFullRead = Date.now();
    const poll = async () => {
      if (controller.signal.aborted || reading) return;
      if (!document.hidden && !busyRef.current && !reading) {
        reading = true;
        try {
          const status = await api.status(controller.signal);
          if (controller.signal.aborted) return;
          const current = workspaceRef.current;
          if (busyRef.current) {
            // A command owns the next workspace read; keep the polling timer alive.
          } else if (!current || !sameReviewIdentity(current, status) || Date.now() - lastFullRead > 60000 || current.status === "source_waiting") {
            const next = await api.workspace(controller.signal);
            if (!controller.signal.aborted && !busyRef.current) receiveWorkspace(next);
            lastFullRead = Date.now();
          } else {
            const next = mergeStatus(current, status);
            workspaceRef.current = next; setWorkspace(next);
          }
        } catch (failure) { if (!controller.signal.aborted) reportError(failure); }
        finally { reading = false; }
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 5000);
    };
    timer = setTimeout(poll, 5000);
    const wake = () => { clearTimeout(timer); void poll(); };
    document.addEventListener("visibilitychange", wake);
    window.addEventListener("focus", wake);
    return () => { controller.abort(); clearTimeout(timer); document.removeEventListener("visibilitychange", wake); window.removeEventListener("focus", wake); };
  }, [api, connection, receiveWorkspace, reportError]);

  const revision = workspace?.pdf?.revision;
  useEffect(() => {
    const controller = new AbortController();
    let url: string | null = null;
    setPdfUrl(null); setPdfRevision(null); setPdfError(null); setCheckedPdf(false);
    if (!api || !revision || connection !== "ready") { setPdfLoading(false); return; }
    setPdfLoading(true);
    void api.pdf(revision, controller.signal).then((blob) => {
      if (controller.signal.aborted) return;
      url = URL.createObjectURL(blob); setPdfUrl(url); setPdfRevision(revision);
    }).catch((failure) => {
      if (!controller.signal.aborted) { setPdfError(friendlyReviewError(failure)); reportError(failure); }
    }).finally(() => { if (!controller.signal.aborted) setPdfLoading(false); });
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [api, revision, pdfRetry, connection, reportError]);

  const dirty = hasUnsavedChanges(draft);
  useEffect(() => {
    if (!dirty) return;
    const preventLoss = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", preventLoss);
    return () => window.removeEventListener("beforeunload", preventLoss);
  }, [dirty]);

  const commandKey = (kind: string, body: unknown) => {
    const digest = JSON.stringify([kind, body]);
    if (receipt.current?.digest !== digest) receipt.current = { digest, key: crypto.randomUUID() };
    return receipt.current.key;
  };
  const mutate = async (kind: "save" | "refresh" | "approve") => {
    const current = workspaceRef.current;
    const currentApi = apiRef.current;
    const operationScope = active.current;
    if (!current || !currentApi || !operationScope || operationScope.controller.signal.aborted || busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(null); setNotice(null);
    try {
      const signal = operationScope.controller.signal;
      if (kind === "save") {
        const base = saveCommand(current, draftRef.current, "pending");
        await currentApi.save({ ...base, idempotency_key: commandKey(kind, base) }, signal);
        if (signal.aborted) return;
        draftRef.current = emptyDraft(); setDraft(emptyDraft());
        setNotice("Corrections saved. Wait for the matching PDF, then check it before completing the review.");
      } else if (kind === "approve") {
        const base = approvalCommand(current, "pending");
        await currentApi.approve({ ...base, idempotency_key: commandKey(kind, base) }, signal);
        if (signal.aborted) return;
        setNotice("Specification complete. The item can now be moved manually in Monday.");
      } else {
        const base = { expected_review_version: current.review_version, expected_source_fingerprint: current.source_fingerprint!, idempotency_key: "pending" };
        await currentApi.refresh({ ...base, idempotency_key: commandKey(kind, base) }, signal);
        if (signal.aborted) return;
        setNotice("Recommendations refreshed. Saved decisions have been retained.");
      }
      receipt.current = null;
      const next = await currentApi.workspace(signal);
      if (!signal.aborted) { receiveWorkspace(next); setConflict(false); }
    } catch (failure) { if (!operationScope.controller.signal.aborted) reportError(failure); }
    finally { if (active.current === operationScope) { busyRef.current = false; setBusy(false); } }
  };

  const loadLatest = async () => {
    const scope = active.current;
    if (!api || !scope || busyRef.current) return;
    const signal = scope.controller.signal;
    busyRef.current = true; setBusy(true);
    try {
      const next = await api.workspace(signal);
      if (signal.aborted) return;
      // Acceptance is bound to a specific proposal. Keep manual input; ask the
      // reviewer to accept changed recommendations again in the new context.
      const kept = rebaseDraft(draftRef.current);
      draftRef.current = kept; setDraft(kept);
      workspaceRef.current = next; setWorkspace(next); setConflict(false);
      receipt.current = null; setCheckedPdf(false); setError(null);
      setNotice("Latest context loaded. Your entered values and selections are kept. Check them against the new evidence; accept recommendations and acknowledge source issues again where needed.");
    } catch (failure) { if (!signal.aborted) reportError(failure); }
    finally { if (active.current === scope) { busyRef.current = false; setBusy(false); } }
  };

  const usable = workspace && context && workspace.board_id === context.boardId && workspace.item_id === context.itemId;
  const readOnly = Boolean(usable && workspace.status === "read_only");
  const statusDescription = usable ? getReviewStatusDescription({ status: workspace.status,
    scopeReason: workspace.scope_reason, hasSource: Boolean(workspace.source), hasPdf: Boolean(workspace.pdf || workspace.published_pdf), analysis: workspace.analysis }) : null;
  const analysisPending = Boolean(usable && (workspace.provisional || (workspace.analysis && !workspace.analysis.ready)));
  const editable = Boolean(usable && !analysisPending && workspace.capabilities.edit && connection === "ready" && !busy && !conflict);
  const completeAllowed = Boolean(usable && !conflict && !busy && connection === "ready" && checkedPdf && canComplete(workspace, draft, pdfRevision || undefined));
  const unmappedFields = usable ? FIELD_NAMES.filter((label) => {
    const field = workspace.fields[label];
    return field?.effective.display_value && field.effective.cad_mapping.status === "unmapped";
  }) : [];
  const changeField = (label: (typeof FIELD_NAMES)[number], operation: FieldOperation) => {
    setDraft((value) => updateField(value, label, operation)); setCheckedPdf(false);
  };

  return (
    <main className="specification-review mx-auto max-w-[1600px] space-y-5 p-4 sm:p-6">
      <a className="review-skip" href="#review-fields">Skip to specification fields</a>
      <header className="flex flex-wrap items-start justify-between gap-3 border-b pb-4">
        <div>
          <p className="text-sm font-medium text-muted-foreground">Projects Holding Page</p>
          <h1 className="text-2xl font-semibold">Specification Review</h1>
          {context && <p className="mt-1 text-sm text-muted-foreground">Item {context.itemId} · Board {context.boardId}</p>}
          {usable && <p className="mt-1 text-sm text-muted-foreground">Saved review version {workspace.review_version} · {readOnly
            ? workspace.source ? "Saved source context retained" : "No source analysis available for review"
            : workspace.provisional ? "Current extraction available; matching and documents pending"
            : workspace.source ? "Current source context available" : "Waiting for current source context"}</p>}
        </div>
        {usable && <ReviewStatus status={workspace.status} scopeReason={workspace.scope_reason} hasSource={Boolean(workspace.source)} hasPdf={Boolean(workspace.pdf || workspace.published_pdf)} hasUnsavedChanges={dirty} analysis={workspace.analysis} />}
      </header>
      {error && <div role="alert" className="rounded-md border border-red-300 bg-red-50 p-4 text-red-900">{error}</div>}
      {notice && <p role="status" className="rounded-md border bg-white p-4">{notice}</p>}
      {connection === "connecting" && <p role="status">Connecting securely to this Monday item…</p>}
      {oauth && <section className="space-y-3 rounded-md border bg-white p-4" aria-label="Connect Monday">
        <p>Connect your Monday account to open this review. Return to this tab after authorizing.</p>
        <button className={button} onClick={() => { if (sdk) void openMondayReviewAuthorization(sdk, oauth).catch(reportError); }}>Connect Monday account</button>{" "}
        <button className={button} onClick={() => setRenewal((value) => value + 1)}>I’ve connected — resume review</button>
      </section>}
      {["expired", "unavailable"].includes(connection) && <section className="space-y-3 rounded-md border bg-white p-4" aria-label="Resume review">
        <p>{dirty ? "Your unsaved edits are kept in this tab. Reconnect to continue." : "Open this view inside a Monday item and reconnect to continue."}</p>
        <button className={button} onClick={() => sdk && context ? setRenewal((value) => value + 1) : setSdkRetry((value) => value + 1)}>Reconnect review</button>
      </section>}
      {usable && <>
        {readOnly && <p className="rounded-md border bg-white p-4">{statusDescription?.detail}</p>}
        {conflict && <section role="alert" className="space-y-3 rounded-md border border-amber-400 bg-amber-50 p-4">
          <p>The saved review, sources or item access have changed. Your draft is kept. Load the latest context and check your edits before saving.</p>
          <button className={button} disabled={busy || connection !== "ready"} onClick={() => void loadLatest()}>Load latest context and keep my edits</button>
        </section>}
        <CandidateSelector candidates={workspace.candidates} intent={workspace.source?.intent} selections={{ ...workspace.review.selections, ...draft.selections }} disabled={!editable} unavailableMessage={readOnly && !workspace.source ? "Project and drawing recommendations are unavailable in this read-only view." : undefined} onChange={(patch) => { setDraft((value) => updateSelection(value, patch)); setCheckedPdf(false); }} />
        {workspace.source && (workspace.source.selected_project || workspace.source.selected_drawing) && <p className="text-sm text-muted-foreground">Displayed recommendations use project {workspace.source.selected_project?.item_id || "unassigned"}, drawing {workspace.source.selected_drawing?.item_id || "unassigned"}. {readOnly ? "These saved recommendations are read-only." : "Save changed selections to resolve their recommendations."}</p>}
        <div className="grid min-w-0 items-start gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(320px,2fr)]">
          <section id="review-fields" aria-labelledby="review-fields-title" className="min-w-0 space-y-4">
            <h2 id="review-fields-title" className="text-xl font-semibold">Specification fields</h2>
            <p className="text-sm text-muted-foreground">{analysisPending ? "Validated source recommendations are available to read. Wait for analysis and matching to finish before making corrections." : readOnly ? "Any saved field values are shown for reference. Editing is unavailable in this view." : "Save corrections to regenerate the PDF. Entered values and intentional blanks display Source: Reviewer Specified."}</p>
            {FIELD_NAMES.map((label) => workspace.fields[label] && <FieldEditor key={label} label={label} field={workspace.fields[label]!} operation={draft.field_operations[label]} disabled={!editable} onChange={(operation) => changeField(label, operation)} />)}
            {!workspace.source && <p>{readOnly ? "No specification fields are available for this item." : "Fields will appear when current source analysis is available."}</p>}
            {workspace.source?.source_issues.map((issue) => {
              const id = `${issue.source_id}/${issue.code}`;
              const disposition = draft.source_dispositions.find((entry) => entry.source_id === issue.source_id && entry.issue_code === issue.code);
              const saved = workspace.review.source_dispositions.find((entry) => entry.source_id === issue.source_id && entry.issue_code === issue.code);
              return <section key={id} className="space-y-2 rounded-md border border-amber-300 bg-white p-4">
                <h3 className="font-medium">Source needs review</h3><p className="text-sm">{issue.detail}</p>
                {saved && <p className="text-sm">Previous disposition: {saved.explanation}</p>}
                <label className="block text-sm">Reviewed disposition for {issue.source_id}
                  <textarea className="mt-1 w-full rounded border p-2" rows={2} maxLength={4000} disabled={!editable} value={disposition?.explanation || ""} placeholder="Explain how you checked or addressed this source issue" onChange={(event) => {
                    const explanation = event.target.value;
                    setDraft((value) => ({ ...value, source_dispositions: [...value.source_dispositions.filter((entry) => !(entry.source_id === issue.source_id && entry.issue_code === issue.code)), ...(explanation ? [{ source_id: issue.source_id, issue_code: issue.code, explanation }] : [])] }));
                  }} />
                </label>
              </section>;
            })}
          </section>
          <aside className="min-w-0 space-y-4 xl:sticky xl:top-4" aria-label="PDF and completion">
            <PdfPreview pdf={workspace.pdf} publishedPdf={workspace.published_pdf} url={pdfUrl} loading={pdfLoading} readOnly={readOnly} error={pdfError} onRetry={() => setPdfRetry((value) => value + 1)} />
            <section className="space-y-3 rounded-lg border bg-white p-4" aria-labelledby="complete-title">
              <h2 id="complete-title" className="text-lg font-semibold">Final review</h2>
              {readOnly && <p className="text-sm">Completion is unavailable while this item is read-only.</p>}
              {!readOnly && completionIssues(workspace).length > 0 && <details className="text-sm"><summary className="cursor-pointer font-medium">Decisions still needed ({completionIssues(workspace).length})</summary><ul className="mt-2 list-disc space-y-1 pl-5">{completionIssues(workspace).map((issue) => <li key={issue}>{issue}</li>)}</ul></details>}
              {unmappedFields.length > 0 && <p id="review-mapping-limitations" className="rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">No confirmed CAD mapping is available for: {unmappedFields.join(", ")}. These values are preserved in the PDF; completion does not establish compatibility with CAD or TRICS.</p>}
              <label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={checkedPdf} aria-describedby={unmappedFields.length ? "review-mapping-limitations" : undefined} disabled={!pdfUrl || dirty || conflict || workspace.status !== "awaiting_final_review" || connection !== "ready" || busy} onChange={(event) => setCheckedPdf(event.target.checked)} />I have checked the current published PDF, its source labels{unmappedFields.length ? " and the mapping limitations above" : ""}.</label>
              <button className={`${button} w-full bg-primary text-primary-foreground`} disabled={!completeAllowed} onClick={() => void mutate("approve")}>Mark specification complete</button>
              {!readOnly && <p className="text-sm text-muted-foreground">Completion records this exact published PDF. Move the item manually in Monday after completion.</p>}
            </section>
          </aside>
        </div>
        <section className="flex flex-wrap items-center gap-3 rounded-lg border bg-white p-4" aria-label="Save review">
          <button className={`${button} bg-primary text-primary-foreground`} disabled={!editable || !dirty || !workspace.source_fingerprint || Object.keys(draftValidationErrors(draft)).length > 0 || draft.source_dispositions.some((entry) => !entry.explanation.trim())} onClick={() => void mutate("save")}>{busy ? "Working…" : "Save corrections and regenerate PDF"}</button>
          <button className={button} disabled={!editable || dirty || !workspace.source_fingerprint} onClick={() => void mutate("refresh")}>Refresh recommendations</button>
          {dirty && <p className="text-sm">Unsaved corrections · completion is unavailable</p>}
        </section>
        <section className="rounded-lg border bg-white p-4" aria-labelledby="history-title">
          <h2 id="history-title" className="text-lg font-semibold">Review history</h2>
          {!workspace.history.length && <p className="mt-2 text-sm text-muted-foreground">No review events yet.</p>}
          <ol className="mt-3 space-y-2 text-sm">{workspace.history.map((event) => <li key={event.event_id} className="border-b pb-2"><span className="font-medium">{event.command_type.replaceAll("_", " ")}</span> · version {event.review_version} · Monday user {event.actor_monday_user_id || "system"} · <time dateTime={event.created_at}>{new Date(event.created_at).toLocaleString("en-GB", { timeZone: "Europe/London" })}</time>{event.explanation && <p className="mt-1 text-muted-foreground">{event.explanation}</p>}</li>)}</ol>
        </section>
      </>}
    </main>
  );
}
