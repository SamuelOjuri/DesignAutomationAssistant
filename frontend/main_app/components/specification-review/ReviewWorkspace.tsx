"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  FIELD_NAMES, bootstrapReviewSession, createReviewApi, ReviewApiError,
  type ReviewContext, type ReviewDraft, type ReviewWorkspace as Workspace,
  type ReviewSession, type FieldOperation,
} from "@/lib/specification-review-api";
import {
  emptyDraft, hasUnsavedChanges, saveCommand, approvalCommand, canComplete, canPublish, publicationCommand,
  mergeStatus, friendlyReviewError, sameReviewIdentity, updateSelection, updateField,
  draftValidationErrors, fieldOverviewTone, fieldReviewState,
  completionIssues, rebaseDraft, sourceIssueAddressed, updateSourceDisposition,
} from "@/lib/specification-review-state";
import {
  loadMondayReviewSdk, getMondayReviewSession, listenMondayReviewContext,
  openMondayReviewAuthorization, type MondayReviewSdk,
} from "@/lib/specification-review-monday";
import {
  ChevronRight, CircleAlert, CircleCheck, Clock, Info, Lock, PlugZap, RefreshCw, Save, Send, ShieldCheck, TriangleAlert, Unplug,
} from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import FieldEditor from "./FieldEditor";
import FieldOverview, { type FieldOverviewEntry } from "./FieldOverview";
import CandidateSelector from "./CandidateSelector";
import PdfPreview from "./PdfPreview";
import ReviewStatus from "./ReviewStatus";
import { getReviewStatusDescription } from "@/lib/specification-review-status";
import "./review.css";

type Api = ReturnType<typeof createReviewApi>;
const apiBase = process.env.NEXT_PUBLIC_FASTAPI_BASE_URL || "";
const contextKey = (value: ReviewContext) => `${value.accountId}/${value.boardId}/${value.itemId}`;
const panelHeader = "border-b px-4 py-4 sm:px-5 [.border-b]:pb-4";
const panelTitle = "flex items-center gap-2 text-lg font-semibold leading-tight";
const panelIcon = "size-5 text-primary";
const checkRow = "flex cursor-pointer items-start gap-3 rounded-lg border bg-muted/40 p-3 text-sm has-[:disabled]:cursor-not-allowed has-[:disabled]:text-muted-foreground";
const checkbox = "mt-0.5 size-4 shrink-0 accent-primary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring";
const fieldAnchor = (label: string) => `review-field-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;

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
  const [renderedPdfUrl, setRenderedPdfUrl] = useState<string | null>(null);
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

  const receivePdfRenderState = useCallback((url: string, ready: boolean) => {
    setRenderedPdfUrl((previous) => ready ? url : previous === url ? null : previous);
    if (!ready) setCheckedPdf(false);
  }, []);

  const receiveWorkspace = useCallback((next: Workspace) => {
    const before = workspaceRef.current;
    if (before && hasUnsavedChanges(draftRef.current) && !sameReviewIdentity(before, next)) {
      setConflict(true);
      setCheckedPdf(false);
      // Revoked capabilities apply immediately, even while the old draft is kept.
      setWorkspace({ ...before, capabilities: next.capabilities, scope_reason: next.scope_reason,
        analysis: next.analysis, provisional: next.provisional,
        publication_mode: next.publication_mode, requested_publication_revision: next.requested_publication_revision,
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
          } else if (!current || !sameReviewIdentity(current, status)
            || current.published_pdf?.revision !== status.published_pdf?.revision
            || Date.now() - lastFullRead > 60000 || current.status === "source_waiting") {
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

  const [showPublished, setShowPublished] = useState(false);
  useEffect(() => { setShowPublished(false); }, [workspace?.pdf?.revision, workspace?.item_id]);
  const visiblePdf = showPublished ? workspace?.published_pdf : workspace?.pdf;
  const revision = visiblePdf?.revision;
  useEffect(() => {
    const controller = new AbortController();
    let url: string | null = null;
    setPdfUrl(null); setPdfRevision(null); setRenderedPdfUrl(null); setPdfError(null); setCheckedPdf(false);
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
  const mutate = async (kind: "save" | "refresh" | "approve" | "publish") => {
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
        setNotice(current.publication_mode === "manual"
          ? "Corrections saved. Inspect the updated draft, then publish when ready. The published PDF is unchanged."
          : "Corrections saved. Wait for the matching PDF, then check it before completing the review.");
      } else if (kind === "publish") {
        if (!canPublish(current, draftRef.current, pdfRevision)) return;
        const base = publicationCommand(current, "pending");
        await currentApi.publish({ ...base, idempotency_key: commandKey(kind, base) }, signal);
        if (signal.aborted) return;
        setNotice("Publication requested for this draft. Wait for publication, then check the published PDF before completing the review.");
      } else if (kind === "approve") {
        const base = approvalCommand(current, "pending");
        await currentApi.approve({ ...base, idempotency_key: commandKey(kind, base) }, signal);
        if (signal.aborted) return;
        setNotice("The current published specification has been marked complete.");
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
  const currentPdfUrl = pdfRevision === revision ? pdfUrl : null;
  const previewReady = Boolean(currentPdfUrl && renderedPdfUrl === currentPdfUrl);
  const completeAllowed = Boolean(usable && !conflict && !busy && connection === "ready" && previewReady && checkedPdf && canComplete(workspace, draft, pdfRevision || undefined));
  const manualPublication = Boolean(usable && workspace.publication_mode === "manual");
  const publishAllowed = Boolean(usable && editable && !showPublished && canPublish(workspace, draft, pdfRevision));
  const unmappedFields = usable ? FIELD_NAMES.filter((label) => {
    const field = workspace.fields[label];
    return field?.effective.display_value && field.effective.cad_mapping.status === "unmapped";
  }) : [];
  const changeField = (label: (typeof FIELD_NAMES)[number], operation: FieldOperation) => {
    setDraft((value) => updateField(value, label, operation)); setCheckedPdf(false);
  };
  const decisionsNeeded = usable && !readOnly ? completionIssues(workspace) : [];
  const showFieldStatus = Boolean(usable && !readOnly && !analysisPending);
  const fieldEntries: (FieldOverviewEntry & { label: (typeof FIELD_NAMES)[number]; state?: ReturnType<typeof fieldReviewState> })[] = usable
    ? FIELD_NAMES.flatMap((label) => {
      const field = workspace.fields[label];
      if (!field) return [];
      const state = showFieldStatus ? fieldReviewState(field) : undefined;
      return [{ label, anchorId: fieldAnchor(label), state, tone: fieldOverviewTone(state, Boolean(draft.field_operations[label])) }];
    }) : [];
  // Selection edits can set several keys at once, so they count as one change.
  const unsavedCount = Object.keys(draft.field_operations).length + (Object.keys(draft.selections).length ? 1 : 0) + draft.source_dispositions.length;
  const stillNeeded = showFieldStatus && decisionsNeeded.length ? plural(decisionsNeeded.length, "decision still needed", "decisions still needed") : null;
  const saveSummary = dirty
    ? { dot: "bg-primary", title: plural(unsavedCount, "unsaved change", "unsaved changes"), detail: [stillNeeded, "completion unavailable until saved"] }
    : readOnly ? { dot: "border border-muted-foreground/60", title: "Editing is unavailable in this view", detail: [] }
      : analysisPending ? { dot: "bg-info", title: "Waiting for analysis and matching to finish", detail: [] }
        : { dot: "bg-success", title: "All changes saved", detail: [stillNeeded ?? "no outstanding decisions"] };
  const sourceContext = !usable ? null : readOnly
    ? workspace.source ? "Saved source context retained" : "No source analysis available for review"
    : workspace.provisional ? "Current extraction available; matching and documents pending"
    : workspace.source ? "Current source context available" : "Waiting for current source context";

  return (
    <div className="specification-review min-h-screen bg-background text-foreground">
      <main className="mx-auto max-w-[1600px] space-y-6 p-4 sm:p-6">
        <a className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-card focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:shadow-md focus:outline-none focus:ring-[3px] focus:ring-ring/50" href="#review-fields">Skip to specification fields</a>
        <header className="flex flex-col gap-4 border-b pb-5 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0 space-y-2">
            <p className="flex items-center gap-2 text-sm font-medium text-muted-foreground"><span className="size-2 rounded-full bg-brand" aria-hidden="true" />Projects Holding Page</p>
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Specification Review</h1>
            {context && (
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline" className="bg-card font-normal text-muted-foreground">Item {context.itemId}</Badge>
                <Badge variant="outline" className="bg-card font-normal text-muted-foreground">Board {context.boardId}</Badge>
                {usable && <Badge variant="outline" className="bg-card font-normal text-muted-foreground">Saved review version {workspace.review_version}</Badge>}
              </div>
            )}
            {sourceContext && <p className="text-sm text-muted-foreground">{sourceContext}</p>}
          </div>
          {usable && <ReviewStatus className="lg:max-w-xl" status={workspace.status} scopeReason={workspace.scope_reason} hasSource={Boolean(workspace.source)} hasPdf={Boolean(workspace.pdf || workspace.published_pdf)} hasUnsavedChanges={dirty} analysis={workspace.analysis} />}
        </header>
        {error && <Alert variant="destructive" className="bg-card"><CircleAlert className="size-4" aria-hidden="true" /><AlertDescription>{error}</AlertDescription></Alert>}
        {notice && <Alert variant="success" role="status"><CircleCheck className="size-4" aria-hidden="true" /><AlertDescription>{notice}</AlertDescription></Alert>}
        {connection === "connecting" && <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground"><Spinner role="presentation" aria-hidden="true" />Connecting securely to this Monday item…</p>}
        {oauth && (
          <section aria-label="Connect Monday">
            <Card className="gap-4 py-5">
              <CardHeader className="px-5">
                <CardTitle className="flex items-center gap-2"><PlugZap className="size-5 text-muted-foreground" aria-hidden="true" />Connect your Monday account</CardTitle>
                <CardDescription>Connect your Monday account to open this review. Return to this tab after authorizing.</CardDescription>
              </CardHeader>
              <CardContent className="flex flex-wrap gap-2 px-5">
                <Button onClick={() => { if (sdk) void openMondayReviewAuthorization(sdk, oauth).catch(reportError); }}>Connect Monday account</Button>
                <Button variant="outline" onClick={() => setRenewal((value) => value + 1)}>I’ve connected — resume review</Button>
              </CardContent>
            </Card>
          </section>
        )}
        {["expired", "unavailable"].includes(connection) && (
          <section aria-label="Resume review">
            <Card className="gap-4 py-5">
              <CardHeader className="px-5">
                <CardTitle className="flex items-center gap-2"><Unplug className="size-5 text-muted-foreground" aria-hidden="true" />Reconnect to continue</CardTitle>
                <CardDescription>{dirty ? "Your unsaved edits are kept in this tab. Reconnect to continue." : "Open this view inside a Monday item and reconnect to continue."}</CardDescription>
              </CardHeader>
              <CardContent className="px-5">
                <Button onClick={() => sdk && context ? setRenewal((value) => value + 1) : setSdkRetry((value) => value + 1)}>Reconnect review</Button>
              </CardContent>
            </Card>
          </section>
        )}
        {usable && <>
          {readOnly && <Alert className="bg-card" role="note"><Lock className="size-4" aria-hidden="true" /><AlertDescription>{statusDescription?.detail}</AlertDescription></Alert>}
          {conflict && (
            <Alert variant="warning" role="alert">
              <TriangleAlert className="size-4" aria-hidden="true" />
              <AlertTitle>Review context changed</AlertTitle>
              <AlertDescription className="space-y-3">
                <p>The saved review, sources or item access have changed. Your draft is kept. Load the latest context and check your edits before saving.</p>
                <Button variant="outline" disabled={busy || connection !== "ready"} onClick={() => void loadLatest()}>Load latest context and keep my edits</Button>
              </AlertDescription>
            </Alert>
          )}
          <CandidateSelector candidates={workspace.candidates} extractedProjectTitle={workspace.extracted_project_title} intent={workspace.source?.intent} selections={{ ...workspace.review.selections, ...draft.selections }} disabled={!editable} unavailableMessage={readOnly && !workspace.source ? "Project and drawing recommendations are unavailable in this read-only view." : undefined} onChange={(patch) => { setDraft((value) => updateSelection(value, patch)); setCheckedPdf(false); }} />
          {workspace.source && (workspace.source.selected_project || workspace.source.selected_drawing) && (
            <p className="flex items-start gap-2 text-sm text-muted-foreground"><Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" /><span>Displayed recommendations use project {workspace.source.selected_project?.item_id || "unassigned"}, drawing {workspace.source.selected_drawing?.item_id || "unassigned"}. {readOnly ? "These saved recommendations are read-only." : "Save changed selections to resolve their recommendations."}</span></p>
          )}
          <div className="grid min-w-0 items-start gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(340px,2fr)]">
            <section id="review-fields" aria-labelledby="review-fields-title" className="min-w-0 scroll-mt-4 space-y-4">
              <div className="space-y-1">
                <h2 id="review-fields-title" className="text-xl font-semibold tracking-tight">Specification fields</h2>
                <p className="text-sm text-muted-foreground">{analysisPending ? "Validated source recommendations are available to read. Wait for analysis and matching to finish before making corrections." : readOnly ? "Any saved field values are shown for reference. Editing is unavailable in this view." : manualPublication ? "Save corrections to prepare a draft. Publish separately when ready. Entered values and intentional blanks display Source: Reviewer Specified." : "Save corrections to regenerate the PDF. Entered values and intentional blanks display Source: Reviewer Specified."}</p>
              </div>
              {fieldEntries.length > 0 && <FieldOverview entries={fieldEntries} showStatus={showFieldStatus} />}
              {fieldEntries.map(({ label, anchorId, state }) => <FieldEditor key={label} anchorId={anchorId} label={label} field={workspace.fields[label]!} operation={draft.field_operations[label]} reviewState={state} disabled={!editable} onChange={(operation) => changeField(label, operation)} />)}
              {!workspace.source && <p className="rounded-xl border border-dashed bg-card p-6 text-center text-sm text-muted-foreground">{readOnly ? "No specification fields are available for this item." : "Fields will appear when current source analysis is available."}</p>}
              {workspace.source?.source_issues.map((issue) => {
                const id = `${issue.source_id}/${issue.code}`;
                const disposition = draft.source_dispositions.find((entry) => entry.source_id === issue.source_id && entry.issue_code === issue.code);
                const saved = workspace.review.source_dispositions.find((entry) => entry.source_id === issue.source_id && entry.issue_code === issue.code);
                const addressed = sourceIssueAddressed(workspace, issue);
                return <section key={id} className="space-y-3 rounded-xl border border-warning/50 bg-card p-4 shadow-xs sm:p-5">
                  <h3 className="flex items-center gap-2 font-semibold"><TriangleAlert className="size-4 text-warning" aria-hidden="true" />Source needs review</h3>
                  <p className="text-sm [overflow-wrap:anywhere]">{issue.detail}</p>
                  <label className={checkRow}>
                    <input type="checkbox" className={checkbox} disabled={!editable} checked={disposition?.addressed ?? addressed} onChange={(event) => {
                      const checked = event.target.checked;
                      setDraft((value) => updateSourceDisposition(value, workspace, issue, checked));
                      setCheckedPdf(false);
                    }} />
                    I confirm this source issue has been addressed.
                  </label>
                  {saved && (saved.addressed ?? Boolean(saved.explanation?.trim())) && !addressed && <p className="text-sm text-warning-foreground">The sources have changed. Review this issue again before confirming it.</p>}
                </section>;
              })}
            </section>
            {/* Capped above the sticky save bar; the column scrolls independently when taller. */}
            <aside className="min-w-0 space-y-4 xl:sticky xl:top-4 xl:-mx-1 xl:max-h-[calc(100dvh-7rem)] xl:overflow-y-auto xl:px-1 xl:pb-1" aria-label="PDF and completion">
              {manualPublication && workspace.pdf && workspace.published_pdf && workspace.pdf.revision !== workspace.published_pdf.revision && (
                <div className="flex gap-1 rounded-lg border bg-muted p-1" role="group" aria-label="Choose specification version">
                  <Button variant="ghost" size="sm" className="flex-1 aria-pressed:bg-card aria-pressed:shadow-xs" aria-pressed={!showPublished} onClick={() => setShowPublished(false)}>View draft PDF</Button>
                  <Button variant="ghost" size="sm" className="flex-1 aria-pressed:bg-card aria-pressed:shadow-xs" aria-pressed={showPublished} onClick={() => setShowPublished(true)}>View published PDF</Button>
                </div>
              )}
              <PdfPreview pdf={visiblePdf || null} publishedPdf={workspace.published_pdf} url={currentPdfUrl} loading={pdfLoading} readOnly={readOnly} error={pdfError} onRetry={() => setPdfRetry((value) => value + 1)} onRenderStateChange={receivePdfRenderState} />
              {manualPublication && workspace.published_pdf && workspace.status !== "complete" && workspace.status !== "awaiting_final_review" && (
                <section aria-label="Recommended publication changes">
                  <Card className="gap-0 py-0">
                    <CardHeader className={panelHeader}><h2 className={panelTitle}>Recommended publication changes</h2></CardHeader>
                    <CardContent className="space-y-3 px-4 py-4 sm:px-5">
                      {workspace.publication_changes?.length ? <div className="overflow-x-auto rounded-lg border"><table className="w-full text-left text-sm">
                        <thead className="bg-muted/60 text-xs uppercase tracking-wide text-muted-foreground"><tr><th className="px-3 py-2 font-medium">Field</th><th className="px-3 py-2 font-medium">Published</th><th className="px-3 py-2 font-medium">Proposed</th></tr></thead>
                        <tbody className="divide-y">{workspace.publication_changes.map((change) => <tr key={change.field}>
                          <th className="px-3 py-2 align-top font-medium">{change.field}</th><td className="px-3 py-2 align-top text-muted-foreground">{change.published_value ?? "Not provided"}<span className="block text-xs">{change.published_source}</span></td>
                          <td className="px-3 py-2 align-top">{change.proposed_value ?? "Not provided"}<span className="block text-xs text-muted-foreground">{change.proposed_source}</span></td>
                        </tr>)}</tbody></table></div> : <p className="text-sm">Review current evidence, source labels and saved decisions. Source or review changes can require a new PDF even when displayed field values are unchanged.</p>}
                      <p className="text-sm text-muted-foreground">The published PDF remains attached until you publish an update.</p>
                    </CardContent>
                  </Card>
                </section>
              )}
              <section aria-labelledby="complete-title">
                <Card className="gap-0 py-0">
                  <CardHeader className={panelHeader}><h2 id="complete-title" className={panelTitle}><ShieldCheck className={panelIcon} aria-hidden="true" />Final review</h2></CardHeader>
                  <CardContent className="space-y-4 px-4 py-4 sm:px-5">
                    {readOnly && <p className="text-sm text-muted-foreground">Completion is unavailable while this item is read-only.</p>}
                    {decisionsNeeded.length > 0 && (
                      <details className="group rounded-lg border border-warning/40 bg-warning/10 text-sm text-warning-foreground">
                        <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-3 py-2.5 font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring [&::-webkit-details-marker]:hidden">
                          <ChevronRight className="size-4 shrink-0 transition-transform group-open:rotate-90" aria-hidden="true" />Decisions still needed
                          <Badge variant="outline" className="ml-auto border-warning/40 bg-card tabular-nums text-warning-foreground">{decisionsNeeded.length}</Badge>
                        </summary>
                        <ul className="list-disc space-y-1 border-t border-warning/30 py-2.5 pl-8 pr-3">{decisionsNeeded.map((issue) => <li key={issue}>{issue}</li>)}</ul>
                      </details>
                    )}
                    {unmappedFields.length > 0 && <Alert variant="warning" role="note" id="review-mapping-limitations" className="py-3 [&>svg]:top-3.5"><Info className="size-4" aria-hidden="true" /><AlertDescription>No confirmed CAD mapping is available for: {unmappedFields.join(", ")}. These values are preserved in the PDF; completion does not establish compatibility with CAD or TRICS.</AlertDescription></Alert>}
                    <label className={checkRow}><input type="checkbox" className={checkbox} checked={checkedPdf} aria-describedby={unmappedFields.length ? "review-mapping-limitations" : undefined} disabled={!previewReady || dirty || conflict || workspace.status !== "awaiting_final_review" || connection !== "ready" || busy} onChange={(event) => setCheckedPdf(event.target.checked)} />I have checked the current published PDF, its source labels{unmappedFields.length ? " and the mapping limitations above" : ""}.</label>
                    <Button size="lg" className="w-full" disabled={!completeAllowed} onClick={() => void mutate("approve")}><CircleCheck aria-hidden="true" />Mark specification complete</Button>
                    {!readOnly && <p className="text-xs text-muted-foreground">Completion records this exact published PDF. Relevant source or decision changes reopen review.</p>}
                  </CardContent>
                </Card>
              </section>
            </aside>
          </div>
          <section aria-label="Save review" className="sticky bottom-0 z-30 -mx-1.5 bg-linear-to-t from-background from-70% to-background/0 px-1.5 pb-3 pt-2.5">
            <Card className="flex-row flex-wrap items-center gap-3 border-input px-4 py-3 shadow-[0_10px_30px_rgb(74_21_18/0.14),0_2px_6px_rgb(74_21_18/0.06)] sm:px-5">
              <p className="mr-auto flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm" role="status">
                <span className={`size-2 shrink-0 rounded-full ${saveSummary.dot}`} aria-hidden="true" />
                <span className="font-semibold">{saveSummary.title}</span>
                {saveSummary.detail.filter(Boolean).map((detail) => <span key={detail} className="text-muted-foreground">{" "}· {detail}</span>)}
              </p>
              <Button disabled={!editable || !dirty || !workspace.source_fingerprint || Object.keys(draftValidationErrors(draft)).length > 0} onClick={() => void mutate("save")}>
                {busy ? <Spinner role="presentation" aria-hidden="true" /> : <Save aria-hidden="true" />}{busy ? "Working…" : manualPublication ? "Save corrections" : "Save corrections and regenerate PDF"}
              </Button>
              {manualPublication && <Button disabled={!publishAllowed} onClick={() => void mutate("publish")}><Send aria-hidden="true" />{workspace.published_pdf ? "Update Published PDF" : "Publish Specification PDF"}</Button>}
              <Button variant="outline" disabled={!editable || dirty || !workspace.source_fingerprint} onClick={() => void mutate("refresh")}><RefreshCw aria-hidden="true" />Refresh recommendations</Button>
            </Card>
          </section>
          <section aria-labelledby="history-title">
            <Card className="gap-0 py-0">
              <CardHeader className={panelHeader}><h2 id="history-title" className={panelTitle}><Clock className={panelIcon} aria-hidden="true" />Review history</h2></CardHeader>
              <CardContent className="px-4 py-4 sm:px-5">
                {!workspace.history.length && <p className="text-sm text-muted-foreground">No review events yet.</p>}
                {workspace.history.length > 0 && <ol className="ml-1.5 space-y-4 border-l border-primary/25 pl-5 text-sm">{workspace.history.map((event) => <li key={event.event_id} className="relative">
                  <span className="absolute -left-[1.6rem] top-1.5 size-2.5 rounded-full border-2 border-card bg-primary" aria-hidden="true" />
                  <p className="font-medium first-letter:uppercase">{event.command_type.replaceAll("_", " ")}</p>
                  <p className="text-muted-foreground">Version {event.review_version} · Monday user {event.actor_monday_user_id || "system"} · <time dateTime={event.created_at}>{new Date(event.created_at).toLocaleString("en-GB", { timeZone: "Europe/London" })}</time></p>
                  {event.explanation && <p className="mt-1 text-muted-foreground">{event.explanation}</p>}
                </li>)}</ol>}
              </CardContent>
            </Card>
          </section>
        </>}
      </main>
    </div>
  );
}
