from __future__ import annotations

from dataclasses import dataclass
import csv
from typing import Any, Dict, List

from fastapi import HTTPException
from sqlalchemy.orm import Session

import math
from google import genai
from google.genai import types
from ..config import settings
from ..db import SessionLocal

from .email_extraction import process_email_content, extract_email_sections
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
        .one_or_none()
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
    extracted_docs: List[Dict[str, Any]] = []

    MAX_SINGLE_PDF_SIZE = 100 * 1024 * 1024  # 100MB
    SUPPORTED_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")

    for job in asset_jobs:

        asset = job["asset"]
        kind = job["kind"]
        filename = (asset.get("name") or "").lower()

        # CSV handling (download once, parse, ingest once)
        if _is_csv_asset(asset, kind):
            downloaded = download_asset_to_temp(asset, access_token)
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

            # For RAG: push each CSV row document into extracted_docs
            if "documents" in csv_params[-1]:
                for doc in csv_params[-1]["documents"]:
                    extracted_docs.append(
                        {
                            "assetId": str(asset.get("id")),  # map to CSV TaskFile
                            "filename": asset.get("name"),
                            "kind": "csv",
                            "text": doc["text"],
                            "section": f"row:{doc['rowIndex']}",
                            "page": None,
                        }
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

        # Email extraction
        is_email = filename.endswith(".eml") or filename.endswith(".msg") or kind == "email"
        if is_email:
            downloaded = download_asset_to_temp(asset, access_token)
            with open(downloaded.temp_path, "rb") as f:
                email_bytes = f.read()
            header, body, attachments, inline_images = process_email_content(
                email_bytes, asset.get("name") or ""
            )
            sections = extract_email_sections(header, body, attachments, inline_images)
            for section in sections:
                extracted_docs.append(
                    {
                        "assetId": str(asset.get("id")),
                        "filename": asset.get("name"),
                        "kind": "email",
                        "text": section["text"],
                        "section": section["section"],
                        "page": None,
                    }
                )
            ingest_asset(db, task, snapshot, asset, kind, access_token, downloaded=downloaded)
            for att in attachments:
                ingest_derived_attachment_bytes(
                    db,
                    task,
                    snapshot,
                    parent_asset_id=str(asset.get("id")),
                    filename=att["filename"],
                    content=att["content"],
                    kind=attachment_kind_for_filename(att["filename"]),
                )
            for img in inline_images or []:
                ingest_derived_attachment_bytes(
                    db,
                    task,
                    snapshot,
                    parent_asset_id=str(asset.get("id")),
                    filename=img["filename"],
                    content=img["content"],
                    kind="attachment_image",
                    mime_type=img.get("mime_type"),
                )
            continue

        # PDF extraction (non-email)
        if filename.endswith(".pdf"):
            downloaded = download_asset_to_temp(asset, access_token)
            if downloaded.size_bytes > MAX_SINGLE_PDF_SIZE:
                extracted_docs.append(
                    {
                        "assetId": str(asset.get("id")),
                        "filename": asset.get("name"),
                        "kind": "pdf",
                        "text": f"PDF too large for extraction ({downloaded.size_bytes} bytes).",
                        "section": None,  # filled at chunk time as chunk:{n}
                        "page": None,
                    }
                )
                ingest_asset(db, task, snapshot, asset, kind, access_token, downloaded=downloaded)
                continue
            with open(downloaded.temp_path, "rb") as f:
                pdf_bytes = f.read()
            extracted = process_pdf_batch(
                [{"filename": asset.get("name"), "content": pdf_bytes}]
            )
            extracted_docs.append(
                {
                    "assetId": str(asset.get("id")),
                    "filename": asset.get("name"),
                    "kind": "pdf",
                    "text": extracted,
                    "section": None,  # filled at chunk time as chunk:{n}
                    "page": None,
                }
            )
            ingest_asset(db, task, snapshot, asset, kind, access_token, downloaded=downloaded)
            continue

        # Image extraction (non-email)
        if filename.endswith(SUPPORTED_IMAGE_EXTS):
            downloaded = download_asset_to_temp(asset, access_token)
            with open(downloaded.temp_path, "rb") as f:
                img_bytes = f.read()
            extracted = process_image_with_gemini(
                img_bytes, asset.get("name"), "ATTACHMENT"
            )
            extracted_docs.append(
                {
                    "assetId": str(asset.get("id")),
                    "filename": asset.get("name"),
                    "kind": "image",
                    "text": extracted,
                    "section": "image:description",
                    "page": None,
                }
            )
            ingest_asset(db, task, snapshot, asset, kind, access_token, downloaded=downloaded)
            continue

        if filename.endswith((".gif", ".bmp")):
            extracted_docs.append(
                {
                    "assetId": str(asset.get("id")),
                    "filename": asset.get("name"),
                    "kind": "image",
                    "text": f"Unsupported image format: {filename.split('.')[-1].lower()}",
                    "section": "image:description",
                    "page": None,
                }
            )
            ingest_asset(db, task, snapshot, asset, kind, access_token)
            continue

        # Default: just ingest once
        ingest_asset(db, task, snapshot, asset, kind, access_token)

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
        extracted_docs.append(
            {
                "assetId": col_file.monday_asset_id,
                "filename": col_file.original_filename,
                "kind": "monday_columns",
                "text": column_text,
                "section": "monday:columns",
                "page": None,
            }
        )

    # --- chunk + embed extracted_docs ---
    def _chunk_text(text: str, size: int = 1000, overlap: int = 150) -> list[dict]:
        if not text:
            return []
        chunks = []
        start = 0
        while start < len(text):
            end = min(len(text), start + size)
            chunks.append({"text": text[start:end], "start": start, "end": end})
            start = end - overlap
            if start < 0:
                start = 0
        return chunks

    def _normalize(vec: list[float]) -> list[float]:
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    # Build file map for this snapshot
    files = db.query(TaskFile).filter_by(snapshot_id=snapshot.id).all()
    file_id_by_asset = {
        f.monday_asset_id: f.id for f in files if f.monday_asset_id
    }

    # delete existing chunks for this snapshot
    if files:
        db.query(TaskChunk).filter(
            TaskChunk.file_id.in_([f.id for f in files])
        ).delete(synchronize_session=False)

    # Build chunk list
    chunk_records: list[dict] = []
    for doc in extracted_docs:
        asset_id = doc.get("assetId")
        text = (doc.get("text") or "").strip()
        file_id = file_id_by_asset.get(asset_id)
        if not file_id or not text:
            continue

        chunks = _chunk_text(text)
        if not chunks:
            continue
        multi = len(chunks) > 1

        for idx, chunk in enumerate(chunks, start=1):
            section = doc.get("section")
            if section:
                if multi:
                    section = f"{section}:chunk:{idx}"
            else:
                if doc.get("kind") == "pdf":
                    section = f"chunk:{idx}"
                else:
                    section = f"offset:{chunk['start']}-{chunk['end']}"

            chunk_records.append(
                {
                    "file_id": file_id,
                    "chunk_text": chunk["text"],
                    "page": doc.get("page"),
                    "section": section,
                }
            )

    # Embed + insert
    if chunk_records:
        client = genai.Client(api_key=settings.gemini_api_key)
        BATCH_SIZE = 16
        for i in range(0, len(chunk_records), BATCH_SIZE):
            batch = chunk_records[i:i + BATCH_SIZE]
            contents = [c["chunk_text"] for c in batch]
            result = client.models.embed_content(
                model="gemini-embedding-001",
                contents=contents,
                config=types.EmbedContentConfig(
                    output_dimensionality=1536,
                    task_type="RETRIEVAL_DOCUMENT",
                ),
            )
            embeddings = [_normalize(list(e.values)) for e in result.embeddings]
            for record, vector in zip(batch, embeddings):
                db.add(
                    TaskChunk(
                        file_id=record["file_id"],
                        chunk_text=record["chunk_text"],
                        embedding=vector,
                        page=record.get("page"),
                        section=record.get("section"),
                    )
                )

    task_context = dict(item)
    task_context["csv_params"] = csv_params
    task_context["extracted_docs"] = extracted_docs
    snapshot.task_context_json = task_context

    task.latest_snapshot_version = snapshot_version
    db.commit()

    return SyncResult(status="done", snapshot_version=snapshot_version)


def run_sync_pipeline_background(
    external_task_key: str,
    access_token: str,
    force: bool = False,
) -> None:
    db = SessionLocal()
    try:
        run_sync_pipeline(db, external_task_key, access_token, force=force)
    except Exception:
        db.rollback()
        logger.exception("Sync pipeline failed for %s", external_task_key)
    finally:
        db.close()