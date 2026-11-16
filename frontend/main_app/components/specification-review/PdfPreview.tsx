"use client";

import { useId } from "react";
import { CircleAlert, Download, ExternalLink, FileText, TriangleAlert } from "lucide-react";
import type { PdfArtifact } from "@/lib/specification-review-api";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import PdfCanvasPreview from "./PdfCanvasPreview";

export interface PdfPreviewProps {
  pdf: PdfArtifact | null;
  publishedPdf?: PdfArtifact | null;
  url: string | null;
  loading?: boolean;
  readOnly?: boolean;
  error?: string | null;
  onRetry?: () => void;
  onRenderStateChange?: (url: string, ready: boolean) => void;
}

export default function PdfPreview({ pdf, publishedPdf, url, loading = false, readOnly = false, error, onRetry, onRenderStateChange }: PdfPreviewProps) {
  const id = useId();
  const privatePreview = pdf && !pdf.published;
  return (
    <section className="min-w-0" aria-labelledby={`${id}-title`}>
      <Card className="gap-0 py-0">
        <CardHeader className="border-b px-4 py-4 sm:px-5 [.border-b]:pb-4">
          <h2 id={`${id}-title`} className="flex items-center gap-2 text-lg font-semibold leading-tight">
            <FileText className="size-5 text-muted-foreground" aria-hidden="true" />Specification PDF
          </h2>
          {pdf && <p className="truncate text-sm text-muted-foreground" title={pdf.filename}>{pdf.filename}</p>}
          {pdf && <p className="truncate font-mono text-xs text-muted-foreground" title={pdf.revision}>Revision: {pdf.revision}</p>}
          {pdf && (
            <CardAction>
              <Badge variant="outline" className={privatePreview ? "border-warning/40 bg-warning/10 text-warning-foreground" : "border-success/30 bg-success/10 text-success-foreground"}>
                {privatePreview ? "Draft PDF · not published" : "Published PDF"}
              </Badge>
            </CardAction>
          )}
        </CardHeader>
        <CardContent className="space-y-4 px-4 py-4 sm:px-5">
          {privatePreview && (
            <Alert variant="warning" role="note" className="py-3 [&>svg]:top-3.5">
              <TriangleAlert className="size-4" aria-hidden="true" />
              <AlertDescription>{readOnly
                ? "This private preview has not been published to Monday. Completion is unavailable while this item is read-only."
                : "This preview has not been published to Monday. Completion becomes available after the matching PDF is published."}</AlertDescription>
            </Alert>
          )}
          {publishedPdf && pdf?.revision !== publishedPdf.revision && <p className="text-sm text-muted-foreground">A previous published PDF remains attached to the Monday item.</p>}
          {loading && (
            <div className="space-y-3">
              <p className="text-sm text-muted-foreground" role="status">Loading the protected PDF…</p>
              <Skeleton className="h-64 w-full bg-muted" />
            </div>
          )}
          {error && (
            <Alert variant="destructive" className="bg-card">
              <CircleAlert className="size-4" aria-hidden="true" />
              <AlertDescription className="space-y-2">
                <p>{error}</p>
                {onRetry && <Button type="button" variant="outline" size="sm" onClick={onRetry}>Try PDF again</Button>}
              </AlertDescription>
            </Alert>
          )}
          {url && pdf && (
            <>
              <div className="flex flex-wrap gap-2">
                <Button asChild variant="outline" size="sm">
                  <a href={url} target="_blank" rel="noopener noreferrer"><ExternalLink aria-hidden="true" />Open PDF in new tab</a>
                </Button>
                <Button asChild variant="outline" size="sm">
                  <a href={url} download={pdf.filename}><Download aria-hidden="true" />Download PDF</a>
                </Button>
              </div>
              <PdfCanvasPreview key={url} url={url} onRetry={onRetry} onRenderStateChange={onRenderStateChange} />
            </>
          )}
          {!pdf && !loading && (
            <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed px-4 py-10 text-center">
              <FileText className="size-8 text-muted-foreground/60" aria-hidden="true" />
              <p className="text-sm text-muted-foreground">{readOnly ? "No specification PDF is available for this item." : "The PDF will appear here when a specification has been generated."}</p>
            </div>
          )}
        </CardContent>
      </Card>
    </section>
  );
}
