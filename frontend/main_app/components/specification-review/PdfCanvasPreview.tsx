"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, CircleAlert, Minus, Plus } from "lucide-react";
import type { PDFDocumentLoadingTask, PDFDocumentProxy, RenderTask } from "pdfjs-dist";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/utils";

interface PdfCanvasPreviewProps {
  url: string;
  onRetry?: () => void;
  onRenderStateChange?: (url: string, ready: boolean) => void;
}

/** Render the authenticated in-memory PDF without invoking a browser PDF plugin. */
export default function PdfCanvasPreview({ url, onRetry, onRenderStateChange }: PdfCanvasPreviewProps) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [pageNumber, setPageNumber] = useState(1);
  const [zoom, setZoom] = useState(1);
  const [width, setWidth] = useState(0);
  const [failed, setFailed] = useState(false);
  const [renderedKey, setRenderedKey] = useState<string | null>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const renderKey = `${pageNumber}/${zoom}/${width}`;
  const rendered = Boolean(pdf && !failed && renderedKey === renderKey);

  useEffect(() => {
    const controller = new AbortController();
    let disposed = false;
    let task: PDFDocumentLoadingTask | undefined;
    onRenderStateChange?.(url, false);
    void (async () => {
      // Keep PDF.js out of server rendering. All worker/font/decoder assets are
      // copied from the same pinned package by predev/prebuild, never a CDN.
      const pdfjs = await import("pdfjs-dist");
      if (disposed) return;
      const assets = `/pdfjs/${pdfjs.version}`;
      pdfjs.GlobalWorkerOptions.workerSrc = `${assets}/pdf.worker.min.mjs`;
      const response = await fetch(url, { signal: controller.signal, credentials: "omit" });
      if (!response.ok) throw new Error("PDF bytes unavailable");
      const data = new Uint8Array(await response.arrayBuffer());
      if (disposed) return;
      task = pdfjs.getDocument({
        data,
        cMapUrl: `${assets}/cmaps/`,
        cMapPacked: true,
        standardFontDataUrl: `${assets}/standard_fonts/`,
        wasmUrl: `${assets}/wasm/`,
      });
      const document = await task.promise;
      if (!disposed) setPdf(document);
    })().catch(() => {
      if (!disposed) {
        setFailed(true);
        onRenderStateChange?.(url, false);
      }
    });
    return () => {
      disposed = true;
      controller.abort();
      void task?.destroy().catch(() => { /* Context/revision changes cancel pending work. */ });
    };
  }, [url, onRenderStateChange]);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const measure = () => setWidth(Math.max(1, Math.floor(viewport.clientWidth - 24)));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(viewport);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!pdf || !canvas || !width || failed) return;
    let cancelled = false;
    let task: RenderTask | undefined;
    void (async () => {
      const page = await pdf.getPage(pageNumber);
      if (cancelled) return;
      const original = page.getViewport({ scale: 1 });
      const viewport = page.getViewport({ scale: (width / original.width) * zoom });
      // Bound bitmap memory on large displays while retaining HiDPI text.
      const outputScale = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(8_000_000 / (viewport.width * viewport.height)));
      canvas.width = Math.max(1, Math.floor(viewport.width * outputScale));
      canvas.height = Math.max(1, Math.floor(viewport.height * outputScale));
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      task = page.render({
        canvas,
        viewport,
        transform: [outputScale, 0, 0, outputScale, 0, 0],
      });
      await task.promise;
      if (!cancelled) {
        setRenderedKey(renderKey);
        onRenderStateChange?.(url, true);
      }
    })().catch(() => {
      if (!cancelled) {
        setFailed(true);
        onRenderStateChange?.(url, false);
      }
    });
    return () => { cancelled = true; task?.cancel(); };
  }, [pdf, pageNumber, zoom, width, failed, renderKey, url, onRenderStateChange]);

  return (
    <div className="min-w-0 space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border bg-muted/60 p-1.5" role="group" aria-label="PDF preview controls">
        <div className="flex items-center gap-1">
          <Button type="button" variant="outline" size="icon-sm" aria-label="Previous page" disabled={!pdf || failed || pageNumber <= 1} onClick={() => setPageNumber((page) => page - 1)}><ChevronLeft aria-hidden="true" /></Button>
          <span className="min-w-24 px-1 text-center text-sm font-medium tabular-nums" aria-live="polite">{pdf ? `Page ${pageNumber} of ${pdf.numPages}` : "Loading pages"}</span>
          <Button type="button" variant="outline" size="icon-sm" aria-label="Next page" disabled={!pdf || failed || pageNumber >= pdf.numPages} onClick={() => setPageNumber((page) => page + 1)}><ChevronRight aria-hidden="true" /></Button>
        </div>
        <div className="flex items-center gap-1">
          <Button type="button" variant="outline" size="icon-sm" aria-label="Zoom out" disabled={!pdf || failed || zoom <= 0.5} onClick={() => setZoom((value) => Math.max(0.5, value - 0.25))}><Minus aria-hidden="true" /></Button>
          <span className="w-12 text-center text-sm tabular-nums" aria-label="PDF zoom">{Math.round(zoom * 100)}%</span>
          <Button type="button" variant="outline" size="icon-sm" aria-label="Zoom in" disabled={!pdf || failed || zoom >= 2} onClick={() => setZoom((value) => Math.min(2, value + 0.25))}><Plus aria-hidden="true" /></Button>
          <Button type="button" variant="outline" size="sm" disabled={!pdf || failed || zoom === 1} onClick={() => setZoom(1)}>Fit width</Button>
        </div>
      </div>
      {failed ? (
        <Alert variant="destructive" className="bg-card" role="alert">
          <CircleAlert className="size-4" aria-hidden="true" />
          <AlertDescription className="space-y-2">
            <p>The PDF preview could not be rendered. You can still open or download the PDF.</p>
            {onRetry && <Button type="button" variant="outline" size="sm" onClick={onRetry}>Retry preview</Button>}
          </AlertDescription>
        </Alert>
      ) : (
        <p className={cn(rendered ? "sr-only" : "flex items-center gap-2 text-xs text-muted-foreground")} role="status">
          {!rendered && <Spinner role="presentation" aria-hidden="true" className="size-3.5" />}
          {rendered ? `Showing page ${pageNumber} of ${pdf?.numPages}.` : "Rendering PDF preview…"}
        </p>
      )}
      <div ref={viewportRef} className="max-h-[34rem] min-h-48 overflow-auto rounded-lg border bg-muted p-3 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50" style={{ scrollbarGutter: "stable" }} role="region" aria-label="Specification PDF pages" tabIndex={0} aria-busy={!rendered && !failed}>
        {!rendered && !failed && <Skeleton className="mx-auto aspect-[1/1.414] w-full max-w-md bg-card" />}
        {/* A new canvas per page/size prevents overlapping PDF.js render tasks. */}
        <canvas key={renderKey} ref={canvasRef} role="img" aria-label={`Specification PDF page ${pageNumber} of ${pdf?.numPages ?? "unknown"}`} data-testid="specification-pdf-canvas" data-rendered={rendered ? "true" : "false"} className={`mx-auto rounded-sm bg-white shadow-md ring-1 ring-black/5 ${rendered ? "" : "hidden"}`}>
          Use Open PDF in new tab or Download PDF to read this document.
        </canvas>
      </div>
      <p className="text-xs text-muted-foreground">Use the page controls to review the document. Open or download the PDF for selectable text.</p>
    </div>
  );
}
