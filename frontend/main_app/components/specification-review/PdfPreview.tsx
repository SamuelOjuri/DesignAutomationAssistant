"use client";

import { useId } from "react";
import type { PdfArtifact } from "@/lib/specification-review-api";

export interface PdfPreviewProps {
  pdf: PdfArtifact | null;
  publishedPdf?: PdfArtifact | null;
  url: string | null;
  loading?: boolean;
  readOnly?: boolean;
  error?: string | null;
  onRetry?: () => void;
}

export default function PdfPreview({ pdf, publishedPdf, url, loading = false, readOnly = false, error, onRetry }: PdfPreviewProps) {
  const id = useId();
  const privatePreview = pdf && !pdf.published;
  return (
    <section className="min-w-0 rounded-xl border bg-card p-4 sm:p-5" aria-labelledby={`${id}-title`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id={`${id}-title`} className="text-lg font-semibold">Specification PDF</h2>
        {pdf && <span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${privatePreview ? "bg-amber-50 text-amber-950" : "bg-secondary text-foreground"}`}>{privatePreview ? "Private preview" : "Published PDF"}</span>}
      </div>
      {pdf && <p className="mt-2 break-words text-sm text-muted-foreground">{pdf.filename}</p>}
      {pdf && <p className="mt-1 break-all text-xs text-muted-foreground">Revision: {pdf.revision}</p>}
      {privatePreview && <p className="mt-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">{readOnly
        ? "This private preview has not been published to Monday. Completion is unavailable while this item is read-only."
        : "This preview has not been published to Monday. Completion becomes available after the matching PDF is published."}</p>}
      {publishedPdf && pdf?.revision !== publishedPdf.revision && <p className="mt-2 text-sm text-muted-foreground">A previous published PDF remains attached to the Monday item.</p>}
      {loading && <p className="mt-4 text-sm text-muted-foreground" role="status">Loading the protected PDF…</p>}
      {error && <div className="mt-4 rounded-md border border-destructive/40 bg-accent p-3 text-sm" role="alert"><p>{error}</p>{onRetry && <button type="button" onClick={onRetry} className="mt-2 rounded-md border px-3 py-2 font-semibold hover:bg-secondary focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary">Try PDF again</button>}</div>}
      {url && pdf && (
        <>
          <div className="mt-4 flex flex-wrap gap-3 text-sm">
            <a href={url} target="_blank" rel="noopener noreferrer" className="rounded-md font-semibold text-primary underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary">Open PDF in new tab</a>
            <a href={url} download={pdf.filename} className="rounded-md font-semibold text-primary underline underline-offset-4 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary">Download PDF</a>
          </div>
          <iframe src={url} title="Current specification PDF preview" className="mt-4 h-[34rem] w-full min-w-0 rounded-lg border bg-secondary" />
          <p className="mt-2 text-xs text-muted-foreground">If the embedded preview is unavailable, use Open PDF in new tab or Download PDF.</p>
        </>
      )}
      {!pdf && !loading && <p className="mt-4 text-sm text-muted-foreground">{readOnly ? "No specification PDF is available for this item." : "The PDF will appear here when a specification has been generated."}</p>}
    </section>
  );
}
