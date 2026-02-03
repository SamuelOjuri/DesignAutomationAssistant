from __future__ import annotations

from dataclasses import dataclass
import csv
from typing import Any, Dict, List
import psutil  # Add this import for memory monitoring

from fastapi import HTTPException
from sqlalchemy.orm import Session

import os
import math
import gc  # Add this import

from google import genai
from google.genai import types
from ..config import settings
from ..db import SessionLocal

from .email_extraction import process_email_content, extract_email_sections, process_email_content_to_temp, cleanup_temp_files
from .pdf_extraction import process_pdf_batch
from .image_extraction import process_image_with_gemini
from .storage_ingest import ingest_derived_attachment_bytes, attachment_kind_for_filename
from ..models import Task, TaskSnapshot, TaskFile, TaskChunk
from ..monday_client import fetch_item_with_assets
from .storage_ingest import (
    compute_snapshot_version,
    extract_asset_kinds,
    ingest_asset,
    download_asset_to_temp,
)

import logging

logger = logging.getLogger(__name__)

def _log_memory(stage: str):
    """Log current memory usage."""
    try:
        process = psutil.Process()
        mem_info = process.memory_info()
        mem_mb = mem_info.rss / (1024 * 1024)
        logger.info(f"[MEMORY] {stage}: {mem_mb:.1f} MB")
    except Exception as e:
        logger.warning(f"[MEMORY] Could not get memory info: {e}")

@dataclass
class SyncResult:
    status: str
    snapshot_version: str | None

def _is_csv_asset(asset: Dict[str, Any], kind: str) -> bool:
    if kind == "csv":
        return True
    ext = (asset.get("file_extension") or "").lower()
    name = (asset.get("name") or "").lower()
    return ext == ".csv" or name.endswith(".csv")

def _normalize_header(name: str) -> str:
    return (name or "").strip().lower()

def _parse_key_value_csv(path: str) -> tuple[list[dict], list[dict]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        headers = reader.fieldnames or []
        header_map = { _normalize_header(h): h for h in headers }
        required = {"parameter", "value", "source"}
        if not required.issubset(set(header_map.keys())):
            raise ValueError("Not key_value CSV")
        documents: list[dict] = []
        records: list[dict] = []
        row_index = 1
        for row in reader:
            param = (row.get(header_map["parameter"]) or "").strip()
            value = (row.get(header_map["value"]) or "").strip()
            source = (row.get(header_map["source"]) or "").strip()
            if not (param or value or source):
                continue
            text = f"Parameter: {param} | Value: {value} | Source: {source}"
            documents.append({"text": text, "rowIndex": row_index})
            records.append(
                {"parameter": param, "value": value, "source": source, "rowIndex": row_index}
            )
            row_index += 1
        return documents, records

def _parse_generic_csv(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        return [row for row in reader if any((v or "").strip() for v in row.values())]

def _collect_asset_jobs(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    asset_kinds = extract_asset_kinds(item.get("column_values") or [])
    assets_by_id: Dict[str, Dict[str, Any]] = {}

    def add_asset(asset: Dict[str, Any], kind: str) -> None:
        asset_id = str(asset.get("id") or "")
        if not asset_id or asset_id in assets_by_id:
            return
        assets_by_id[asset_id] = {"asset": asset, "kind": kind}

    # Primary assets from item
    for asset in item.get("assets") or []:
        asset_id = str(asset.get("id") or "")
        kind = asset_kinds.get(asset_id, "attachment")
        add_asset(asset, kind)

    # Update assets (if any) – keep item kind if already present
    for update in item.get("updates") or []:
        for asset in update.get("assets") or []:
            add_asset(asset, "update_attachment")

    return list(assets_by_id.values())


def run_sync_pipeline(
    db: Session,
    external_task_key: str,
    access_token: str,
    force: bool = False,
) -> SyncResult:
    task = db.get(Task, external_task_key)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    item = fetch_item_with_assets(access_token, task.item_id)
    snapshot_version = compute_snapshot_version(item)

    snapshot = (
        db.query(TaskSnapshot)
        .filter_by(
            external_task_key=task.external_task_key,
            snapshot_version=snapshot_version,
        )
        .first()
    )

    if snapshot is not None and not force:
        return SyncResult(status="unchanged", snapshot_version=snapshot_version)

    if snapshot is None:
        snapshot = TaskSnapshot(
            external_task_key=task.external_task_key,
            snapshot_version=snapshot_version,
            task_context_json=item,
        )
        db.add(snapshot)
        db.flush()

    asset_jobs = _collect_asset_jobs(item)
    csv_params: List[Dict[str, Any]] = []
    doc_stats: Dict[str, Any] = {"total_docs": 0, "total_chunks": 0, "by_kind": {}}
    embed_buffer: list[dict] = []
    cleared_file_ids: set = set()
    embed_client = None

    # Memory-optimized limits to prevent OOM on 4GB instances
    MAX_SINGLE_PDF_SIZE = 30 * 1024 * 1024  # (reduced from 30MB)
    MAX_EMAIL_SIZE = 20 * 1024 * 1024  # (reduced from 20MB)
    MAX_IMAGE_SIZE = 10 * 1024 * 1024  # (reduced from 10MB)
    MAX_TEXT_CHARS = 400_000
    MAX_CHUNKS_PER_DOC = 400
    RSS_GUARD_MB = 2000  # Reduced to give more headroom before 4GB limit
    RSS_CRITICAL_MB = 3200  # Critical threshold - abort pipeline to prevent OOM kill
    SUPPORTED_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
    EMBED_BATCH_SIZE = 2  # Reduced from 4 to minimize memory pressure
    MAX_ATTACHMENTS_PER_EMAIL = 8  # Limit attachments to prevent memory accumulation

    def _rss_mb() -> float:
        try:
            return psutil.Process().memory_info().rss / (1024 * 1024)
        except Exception:
            return 0.0

    def _should_skip(reason: str) -> bool:
        rss = _rss_mb()
        if rss and rss > RSS_GUARD_MB:
            logger.warning(f"[OOM-GUARD] Skipping {reason}: RSS>{RSS_GUARD_MB}MB (current: {rss:.1f}MB)")
            return True
        return False

    def _should_abort() -> bool:
        """Check if memory is critical and pipeline should abort to prevent OOM kill."""
        rss = _rss_mb()
        if rss and rss > RSS_CRITICAL_MB:
            logger.error(f"[OOM-ABORT] Memory critical at {rss:.1f}MB (limit: {RSS_CRITICAL_MB}MB), aborting pipeline")
            return True
        return False

    def _chunk_text(text: str, size: int = 1000, overlap: int = 150) -> list[dict]:
        if not text:
            return []
        chunks = []
        start = 0
        while start < len(text):
            end = min(len(text), start + size)
            chunks.append({"text": text[start:end], "start": start, "end": end})

            # FIX: Stop if we have reached the end of the text
            if end >= len(text):
                break

            start = end - overlap
            if start < 0:
                start = 0
        return chunks

    def _normalize(vec: list[float]) -> list[float]:
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    def _ensure_chunks_cleared(file_id: Any) -> None:
        if file_id in cleared_file_ids:
            return
        db.query(TaskChunk).filter(TaskChunk.file_id == file_id).delete(
            synchronize_session=False
        )
        cleared_file_ids.add(file_id)

    def _flush_embed_buffer() -> None:
        nonlocal embed_client
        if not embed_buffer:
            return
        if _should_skip("embedding batch"):
            embed_buffer.clear()
            return
        if embed_client is None:
            embed_client = genai.Client(api_key=settings.gemini_api_key)
        logger.info(f"[EMBED] batch size={len(embed_buffer)}")
        _log_memory("Before embedding batch")
        
        try:
            contents = [c["chunk_text"] for c in embed_buffer]
            result = embed_client.models.embed_content(
                model="gemini-embedding-001",
                contents=contents,
                config=types.EmbedContentConfig(
                    output_dimensionality=1536,
                    task_type="RETRIEVAL_DOCUMENT",
                ),
            )
            embeddings = [_normalize(list(e.values)) for e in result.embeddings]
            for record, vector in zip(embed_buffer, embeddings):
                db.add(
                    TaskChunk(
                        file_id=record["file_id"],
                        chunk_text=record["chunk_text"],
                        embedding=vector,
                        page=record.get("page"),
                        section=record.get("section"),
                    )
                )
            
            # Explicit cleanup of large objects
            del result
            del contents
            del embeddings
            
        except Exception as e:
            logger.error(f"[EMBED] Failed to embed batch: {e}")
            # We clear the buffer anyway to avoid getting stuck
            
        embed_buffer.clear()
        gc.collect()  # Force GC after embedding
        _log_memory("After embedding batch")

    def _enqueue_chunk(
        file_id: Any,
        chunk_text: str,
        page: int | None,
        section: str | None,
    ) -> None:
        if not file_id or not chunk_text:
            return
        _ensure_chunks_cleared(file_id)
        embed_buffer.append(
            {
                "file_id": file_id,
                "chunk_text": chunk_text,
                "page": page,
                "section": section,
            }
        )
        if len(embed_buffer) >= EMBED_BATCH_SIZE:
            _flush_embed_buffer()

    def process_doc_for_embedding(
        file_id: Any,
        text: str | None,
        kind: str,
        section: str | None = None,
        page: int | None = None,
    ) -> None:
        if not file_id:
            return
        if _should_skip(f"{kind} embedding"):
            return
        text = (text or "").strip()
        if not text:
            return
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS]
        chunks = _chunk_text(text)
        if not chunks:
            return
        if len(chunks) > MAX_CHUNKS_PER_DOC:
            chunks = chunks[:MAX_CHUNKS_PER_DOC]
        doc_stats["total_docs"] += 1
        doc_stats["total_chunks"] += len(chunks)
        doc_stats["by_kind"][kind] = doc_stats["by_kind"].get(kind, 0) + 1
        multi = len(chunks) > 1
        for idx, chunk in enumerate(chunks, start=1):
            chunk_section = section
            if chunk_section:
                if multi:
                    chunk_section = f"{chunk_section}:chunk:{idx}"
            else:
                if kind == "pdf":
                    chunk_section = f"chunk:{idx}"
                else:
                    chunk_section = f"offset:{chunk['start']}-{chunk['end']}"
            _enqueue_chunk(file_id, chunk["text"], page, chunk_section)

    aborted = False
    for job in asset_jobs:
        # Check for critical memory pressure before processing each asset
        if _should_abort():
            logger.error("[OOM-ABORT] Stopping asset processing early to prevent crash")
            aborted = True
            break

        asset = job["asset"]
        kind = job["kind"]
        filename = (asset.get("name") or "").lower()

        logger.info(
            f"[ASSET] kind={kind} name={asset.get('name')} id={asset.get('id')}"
        )
        _log_memory("Before asset")

        # CSV handling (download once, parse, ingest once)
        if _is_csv_asset(asset, kind):
            downloaded = download_asset_to_temp(asset, access_token)
            documents: list[dict] | None = None
            try:
                documents, records = _parse_key_value_csv(downloaded.temp_path)
                csv_params.append(
                    {
                        "assetId": str(asset.get("id")),
                        "filename": asset.get("name"),
                        "format": "key_value",
                        "documents": documents,
                        "records": records,
                    }
                )
            except Exception:
                rows = _parse_generic_csv(downloaded.temp_path)
                csv_params.append(
                    {
                        "assetId": str(asset.get("id")),
                        "filename": asset.get("name"),
                        "format": "table",
                        "rows": rows,
                    }
                )

            file_record = ingest_asset(
                db,
                task,
                snapshot,
                asset,
                kind,
                access_token,
                downloaded=downloaded,
            )
            if documents:
                for doc in documents:
                    process_doc_for_embedding(
                        file_record.id,
                        doc["text"],
                        kind="csv",
                        section=f"row:{doc['rowIndex']}",
                        page=None,
                    )
            gc.collect()
            _log_memory("After CSV asset cleanup")
            continue

        # Email extraction

        is_email = filename.endswith(".eml") or filename.endswith(".msg") or kind == "email"
        if is_email:
            logger.info(f"[EMAIL] Processing email: {asset.get('name')}")
            _log_memory("Before email download")

            downloaded = download_asset_to_temp(asset, access_token)
            logger.info(f"[EMAIL] Downloaded to temp: {downloaded.temp_path}, size: {downloaded.size_bytes / (1024*1024):.2f} MB")
            _log_memory("After email download")
            if downloaded.size_bytes and downloaded.size_bytes > MAX_EMAIL_SIZE:
                logger.warning(
                    f"[EMAIL] Too large to extract: {downloaded.size_bytes} bytes"
                )
                ingest_asset(
                    db,
                    task,
                    snapshot,
                    asset,
                    kind,
                    access_token,
                    downloaded=downloaded,
                )
                continue
            if _should_skip("email extraction"):
                logger.warning("[EMAIL] Skipping extraction due to memory guard")
                ingest_asset(
                    db,
                    task,
                    snapshot,
                    asset,
                    kind,
                    access_token,
                    downloaded=downloaded,
                )
                continue

            with open(downloaded.temp_path, "rb") as f:
                email_bytes = f.read()
            logger.info(f"[EMAIL] Read email bytes: {len(email_bytes) / (1024*1024):.2f} MB")
            _log_memory("After reading email bytes")

            # Use memory-efficient extraction that writes attachments to temp files
            header, body, attachments, inline_images = process_email_content_to_temp(
                email_bytes, asset.get("name") or ""
            )
            logger.info(f"[EMAIL] Extracted: {len(attachments)} attachments, {len(inline_images or [])} inline images")
            _log_memory("After email extraction")

            del email_bytes  # Free email bytes immediately
            gc.collect()
            logger.info("[EMAIL] Freed email_bytes, ran gc.collect()")
            _log_memory("After freeing email_bytes")

            # Ingest the email file itself
            email_file = ingest_asset(
                db,
                task,
                snapshot,
                asset,
                kind,
                access_token,
                downloaded=downloaded,
            )
            logger.info("[EMAIL] Ingested email file to Supabase")
            _log_memory("After email file upload")

            # Extract text sections for RAG (header + body only, no attachment bytes)
            if header.strip():
                process_doc_for_embedding(
                    email_file.id,
                    header,
                    kind="email",
                    section="email:header",
                    page=None,
                )
            if body.strip():
                process_doc_for_embedding(
                    email_file.id,
                    body,
                    kind="email",
                    section="email:body",
                    page=None,
                )

            # Process PDF attachments ONE AT A TIME to minimize memory
            pdf_attachments = [att for att in attachments if att["filename"].lower().endswith(".pdf")]
            if len(pdf_attachments) > MAX_ATTACHMENTS_PER_EMAIL:
                logger.warning(f"[EMAIL] Limiting PDF attachments from {len(pdf_attachments)} to {MAX_ATTACHMENTS_PER_EMAIL}")
                pdf_attachments = pdf_attachments[:MAX_ATTACHMENTS_PER_EMAIL]
            logger.info(f"[EMAIL] Processing {len(pdf_attachments)} PDF attachments one at a time")

            for idx, att in enumerate(pdf_attachments, 1):
                # Check memory before each PDF
                if _should_abort():
                    logger.error(f"[OOM-ABORT] Stopping PDF processing at {idx}/{len(pdf_attachments)}")
                    break
                logger.info(f"[PDF {idx}/{len(pdf_attachments)}] Processing: {att['filename']}")
                _log_memory(f"Before PDF {idx}")

                try:
                    # Get file size before reading
                    file_size = os.path.getsize(att["temp_path"])
                    logger.info(f"[PDF {idx}] File size: {file_size / (1024*1024):.2f} MB")
                    if file_size > MAX_SINGLE_PDF_SIZE:
                        logger.warning(
                            f"[PDF {idx}] Too large for extraction: {file_size} bytes"
                        )
                        process_doc_for_embedding(
                            email_file.id,
                            f"PDF ATTACHMENT ({att['filename']}) [Too large for extraction]",
                            kind="pdf",
                            section=f"email:attachment:{att['filename']}",
                            page=None,
                        )
                        continue
                    if _should_skip(f"email pdf extraction {att['filename']}"):
                        logger.warning(
                            f"[PDF {idx}] Skipping extraction due to memory guard"
                        )
                        continue

                    with open(att["temp_path"], "rb") as f:
                        pdf_bytes = f.read()
                    logger.info(f"[PDF {idx}] Read into memory: {len(pdf_bytes) / (1024*1024):.2f} MB")
                    _log_memory(f"After reading PDF {idx}")

                    # Check size before processing
                    if len(pdf_bytes) <= MAX_SINGLE_PDF_SIZE:
                        logger.info(f"[PDF {idx}] Sending to Gemini for extraction...")
                        extracted = process_pdf_batch(
                            [{"filename": att["filename"], "content": pdf_bytes}]
                        )
                        logger.info(f"[PDF {idx}] Gemini extraction complete, text length: {len(extracted)}")
                        process_doc_for_embedding(
                            email_file.id,
                            f"PDF ATTACHMENT ({att['filename']}):\n{extracted}",
                            kind="pdf",
                            section=f"email:attachment:{att['filename']}",
                            page=None,
                        )
                    else:
                        logger.warning(f"[PDF {idx}] Too large for extraction: {len(pdf_bytes) / (1024*1024):.2f} MB")
                        process_doc_for_embedding(
                            email_file.id,
                            f"PDF ATTACHMENT ({att['filename']}) [Too large for extraction]",
                            kind="pdf",
                            section=f"email:attachment:{att['filename']}",
                            page=None,
                        )

                    # Ingest to Supabase
                    logger.info(f"[PDF {idx}] Uploading to Supabase...")
                    ingest_derived_attachment_bytes(
                        db,
                        task,
                        snapshot,
                        parent_asset_id=str(asset.get("id")),
                        filename=att["filename"],
                        content=pdf_bytes,
                        kind=attachment_kind_for_filename(att["filename"]),
                    )
                    logger.info(f"[PDF {idx}] Upload complete")

                    del pdf_bytes  # Free memory immediately
                    gc.collect()
                    logger.info(f"[PDF {idx}] Freed pdf_bytes, ran gc.collect()")
                    _log_memory(f"After freeing PDF {idx}")

                finally:
                    # Delete temp file immediately after processing
                    try:
                        os.unlink(att["temp_path"])
                        logger.info(f"[PDF {idx}] Deleted temp file: {att['temp_path']}")
                    except OSError as e:
                        logger.warning(f"[PDF {idx}] Failed to delete temp file: {e}")

            # Process image attachments ONE AT A TIME
            image_attachments = [
                att for att in attachments
                if any(att["filename"].lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp"))
            ]
            if len(image_attachments) > MAX_ATTACHMENTS_PER_EMAIL:
                logger.warning(f"[EMAIL] Limiting image attachments from {len(image_attachments)} to {MAX_ATTACHMENTS_PER_EMAIL}")
                image_attachments = image_attachments[:MAX_ATTACHMENTS_PER_EMAIL]
            logger.info(f"[EMAIL] Processing {len(image_attachments)} image attachments one at a time")

            for idx, att in enumerate(image_attachments, 1):
                # Check memory before each image
                if _should_abort():
                    logger.error(f"[OOM-ABORT] Stopping image processing at {idx}/{len(image_attachments)}")
                    break
                logger.info(f"[IMAGE {idx}/{len(image_attachments)}] Processing: {att['filename']}")
                _log_memory(f"Before image {idx}")

                try:
                    file_size = os.path.getsize(att["temp_path"])
                    logger.info(f"[IMAGE {idx}] File size: {file_size / (1024*1024):.2f} MB")
                    if file_size > MAX_IMAGE_SIZE:
                        logger.warning(
                            f"[IMAGE {idx}] Too large to extract: {file_size} bytes"
                        )
                        continue
                    if _should_skip(f"email image extraction {att['filename']}"):
                        logger.warning(
                            f"[IMAGE {idx}] Skipping extraction due to memory guard"
                        )
                        continue

                    with open(att["temp_path"], "rb") as f:
                        img_bytes = f.read()

                    _log_memory(f"After reading image {idx}")
                    logger.info(f"[IMAGE {idx}] Sending to Gemini...")
                    extracted = process_image_with_gemini(
                        img_bytes, att["filename"], "ATTACHMENT"
                    )
                    logger.info(f"[IMAGE {idx}] Gemini complete")

                    process_doc_for_embedding(
                        email_file.id,
                        f"IMAGE ATTACHMENT ({att['filename']}):\n{extracted}",
                        kind="image",
                        section=f"email:attachment:{att['filename']}",
                        page=None,
                    )
                    logger.info(f"[IMAGE {idx}] Uploading to Supabase...")
                    ingest_derived_attachment_bytes(
                        db,
                        task,
                        snapshot,
                        parent_asset_id=str(asset.get("id")),
                        filename=att["filename"],
                        content=img_bytes,
                        kind="attachment_image",
                    )
                    del img_bytes  # Free memory immediately
                    gc.collect()
                    _log_memory(f"After freeing image {idx}")

                finally:
                    try:
                        os.unlink(att["temp_path"])
                        logger.info(f"[IMAGE {idx}] Deleted temp file")
                    except OSError:
                        pass

            # Process inline images ONE AT A TIME
            logger.info(f"[EMAIL] Processing {len(inline_images or [])} inline images")

            for idx, img in enumerate(inline_images or [], 1):
                logger.info(f"[INLINE {idx}] Processing: {img['filename']}")
                try:
                    with open(img["temp_path"], "rb") as f:
                        img_bytes = f.read()
                    ingest_derived_attachment_bytes(
                        db,
                        task,
                        snapshot,
                        parent_asset_id=str(asset.get("id")),
                        filename=img["filename"],
                        content=img_bytes,
                        kind="attachment_image",
                        mime_type=img.get("mime_type"),
                    )
                    del img_bytes  # Free memory immediately
                    gc.collect()
                finally:
                    try:
                        os.unlink(img["temp_path"])
                    except OSError:
                        pass

            # Clean up any remaining non-visual attachments
            other_attachments = [
                att for att in attachments
                if not any(att["filename"].lower().endswith(ext) 
                          for ext in (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))
            ]
            logger.info(f"[EMAIL] Processing {len(other_attachments)} other attachments")

            for att in other_attachments:
                try:
                    with open(att["temp_path"], "rb") as f:
                        content = f.read()
                    ingest_derived_attachment_bytes(
                        db,
                        task,
                        snapshot,
                        parent_asset_id=str(asset.get("id")),
                        filename=att["filename"],
                        content=content,
                        kind=attachment_kind_for_filename(att["filename"]),
                    )
                    del content
                    gc.collect()
                finally:
                    try:
                        os.unlink(att["temp_path"])
                    except OSError:
                        pass

            logger.info(f"[EMAIL] Completed processing email: {asset.get('name')}")
            _log_memory("After complete email processing")
            continue

        # PDF extraction (non-email)
        if filename.endswith(".pdf"):
            downloaded = download_asset_to_temp(asset, access_token)
            logger.info(
                f"[PDF] Downloaded {asset.get('name')} size: "
                f"{(downloaded.size_bytes or 0) / (1024*1024):.2f} MB"
            )
            _log_memory("After PDF download")
            if _should_skip("pdf extraction"):
                logger.warning("[PDF] Skipping extraction due to memory guard")
                ingest_asset(
                    db,
                    task,
                    snapshot,
                    asset,
                    kind,
                    access_token,
                    downloaded=downloaded,
                )
                continue
            if downloaded.size_bytes > MAX_SINGLE_PDF_SIZE:
                file_record = ingest_asset(
                    db,
                    task,
                    snapshot,
                    asset,
                    kind,
                    access_token,
                    downloaded=downloaded,
                )
                process_doc_for_embedding(
                    file_record.id,
                    f"PDF too large for extraction ({downloaded.size_bytes} bytes).",
                    kind="pdf",
                    section=None,  # filled at chunk time as chunk:{n}
                    page=None,
                )
                continue
            with open(downloaded.temp_path, "rb") as f:
                pdf_bytes = f.read()
            logger.info(
                f"[PDF] Read into memory: {len(pdf_bytes) / (1024*1024):.2f} MB"
            )
            _log_memory("After reading PDF")
            extracted = process_pdf_batch(
                [{"filename": asset.get("name"), "content": pdf_bytes}]
            )
            logger.info(f"[PDF] Extracted text length: {len(extracted)}")
            _log_memory("After PDF extraction")
            file_record = ingest_asset(
                db,
                task,
                snapshot,
                asset,
                kind,
                access_token,
                downloaded=downloaded,
            )
            process_doc_for_embedding(
                file_record.id,
                extracted,
                kind="pdf",
                section=None,  # filled at chunk time as chunk:{n}
                page=None,
            )
            del pdf_bytes  # Free memory
            gc.collect()
            _log_memory("After non-email PDF cleanup")
            continue

        # Image extraction (non-email)
        if filename.endswith(SUPPORTED_IMAGE_EXTS):
            downloaded = download_asset_to_temp(asset, access_token)
            logger.info(
                f"[IMAGE] Downloaded {asset.get('name')} size: "
                f"{(downloaded.size_bytes or 0) / (1024*1024):.2f} MB"
            )
            _log_memory("After image download")
            if _should_skip("image extraction"):
                logger.warning("[IMAGE] Skipping extraction due to memory guard")
                ingest_asset(
                    db,
                    task,
                    snapshot,
                    asset,
                    kind,
                    access_token,
                    downloaded=downloaded,
                )
                continue
            if downloaded.size_bytes and downloaded.size_bytes > MAX_IMAGE_SIZE:
                logger.warning(
                    f"[IMAGE] Too large to extract: {downloaded.size_bytes} bytes"
                )
                file_record = ingest_asset(
                    db,
                    task,
                    snapshot,
                    asset,
                    kind,
                    access_token,
                    downloaded=downloaded,
                )
                process_doc_for_embedding(
                    file_record.id,
                    f"Image too large for extraction ({downloaded.size_bytes} bytes).",
                    kind="image",
                    section="image:description",
                    page=None,
                )
                continue
            with open(downloaded.temp_path, "rb") as f:
                img_bytes = f.read()
            logger.info(
                f"[IMAGE] Read into memory: {len(img_bytes) / (1024*1024):.2f} MB"
            )
            _log_memory("After reading image")
            extracted = process_image_with_gemini(
                img_bytes, asset.get("name"), "ATTACHMENT"
            )
            logger.info(f"[IMAGE] Extracted text length: {len(extracted)}")
            _log_memory("After image extraction")
            file_record = ingest_asset(
                db,
                task,
                snapshot,
                asset,
                kind,
                access_token,
                downloaded=downloaded,
            )
            process_doc_for_embedding(
                file_record.id,
                extracted,
                kind="image",
                section="image:description",
                page=None,
            )
            del img_bytes  # Free memory
            gc.collect()
            _log_memory("After non-email image cleanup")
            continue

        if filename.endswith((".gif", ".bmp")):
            file_record = ingest_asset(db, task, snapshot, asset, kind, access_token)
            process_doc_for_embedding(
                file_record.id,
                f"Unsupported image format: {filename.split('.')[-1].lower()}",
                kind="image",
                section="image:description",
                page=None,
            )
            gc.collect()
            _log_memory("After gif/bmp asset cleanup")
            continue

        # Default: just ingest once
        ingest_asset(db, task, snapshot, asset, kind, access_token)
        
        # Cleanup after each asset to prevent memory accumulation
        gc.collect()
        _log_memory("After default asset cleanup")

    # Log if we aborted early
    if aborted:
        logger.warning("[OOM-ABORT] Pipeline aborted early due to memory pressure - partial snapshot will be committed")
        gc.collect()
        _log_memory("After abort cleanup")

    # ---- Add column text docs for RAG ----
    def _build_column_text(item: Dict[str, Any]) -> str:
        # Pick only the columns you want for RAG
        ALLOWED_TITLES = {
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
        }
        lines = []
        for col in item.get("column_values") or []:
            title = (col.get("column") or {}).get("title")
            if not title or title not in ALLOWED_TITLES:
                continue
            value = col.get("display_value") or col.get("text") or col.get("value")
            if value is None or value == "":
                continue
            lines.append(f"Column: {title} | Value: {value}")
        return "\n".join(lines)

    column_text = _build_column_text(item)

    if column_text:
        col_file = ingest_derived_attachment_bytes(
            db,
            task,
            snapshot,
            parent_asset_id=f"columns:{task.item_id}",
            filename="monday_columns.txt",
            content=column_text.encode("utf-8"),
            kind="monday_columns",
            mime_type="text/plain",
        )
        process_doc_for_embedding(
            col_file.id,
            column_text,
            kind="monday_columns",
            section="monday:columns",
            page=None,
        )

    _flush_embed_buffer()

    task_context = dict(item)
    task_context["csv_params"] = csv_params
    task_context["extracted_docs_summary"] = doc_stats
    snapshot.task_context_json = task_context

    task.latest_snapshot_version = snapshot_version
    db.commit()

    return SyncResult(status="done", snapshot_version=snapshot_version)


def run_sync_pipeline_background(
    external_task_key: str,
    access_token: str,
    force: bool = False,
) -> None:
    from datetime import datetime, timezone
    
    db = SessionLocal()
    try:
        result = run_sync_pipeline(db, external_task_key, access_token, force=force)
        
        # Update sync status on success
        task = db.get(Task, external_task_key)
        if task:
            task.sync_status = "completed"
            task.sync_completed_at = datetime.now(timezone.utc)
            task.sync_error = None
            db.commit()
            logger.info(f"Sync completed for {external_task_key}: {result.status}")
    except Exception as e:
        db.rollback()
        logger.exception("Sync pipeline failed for %s", external_task_key)
        
        # Update sync status on failure
        try:
            task = db.get(Task, external_task_key)
            if task:
                task.sync_status = "failed"
                task.sync_completed_at = datetime.now(timezone.utc)
                task.sync_error = str(e)[:500]  # Truncate error message
                db.commit()
        except Exception:
            logger.exception("Failed to update sync status for %s", external_task_key)
    finally:
        db.close()