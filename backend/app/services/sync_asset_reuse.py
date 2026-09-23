from __future__ import annotations

from copy import deepcopy
import json
from typing import Any
import uuid

from sqlalchemy import func, insert, literal, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import TaskChunk, TaskFile, TaskSnapshot


PROCESSING_VERSION = "asset-results-v1"
MANIFEST_KEY = "_asset_results"


def public_task_context(context: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in context.items() if key != MANIFEST_KEY}


def processing_version() -> str:
    return json.dumps({
        "pipeline": PROCESSING_VERSION,
        "extraction_model": settings.gemini_model,
        "embedding": "gemini-embedding-001:1536:RETRIEVAL_DOCUMENT",
    }, sort_keys=True)


class AssetResultReuse:
    def __init__(
        self,
        db: Session,
        snapshot: TaskSnapshot,
        *,
        force: bool,
        doc_stats: dict[str, Any],
        csv_params: list[dict[str, Any]],
    ) -> None:
        self.db = db
        self.snapshot = snapshot
        self.doc_stats = doc_stats
        self.csv_params = csv_params
        self.version = processing_version()
        self.results: dict[str, Any] = {}
        self.current: dict[str, Any] | None = None
        self.disabled = False
        self.previous = None if force else (
            db.query(TaskSnapshot)
            .filter(
                TaskSnapshot.external_task_key == snapshot.external_task_key,
                TaskSnapshot.ingestion_status == "complete",
                TaskSnapshot.id != snapshot.id,
            )
            .order_by(TaskSnapshot.created_at.desc(), TaskSnapshot.id.desc())
            .first()
        )
        previous_context = self.previous.task_context_json if self.previous is not None else {}
        manifest = previous_context.get(MANIFEST_KEY) or {}
        self.previous_results = (
            manifest.get("assets", {}) if manifest.get("version") == self.version else {}
        )

    def begin(self, asset_id: str, *, filename: str, kind: str, extension: str = "") -> None:
        self.finish()
        self.current = {
            "asset_id": asset_id,
            "identity": {"filename": filename, "kind": kind, "extension": extension},
            "files": {},
            "stats_before": deepcopy(self.doc_stats),
            "csv_start": len(self.csv_params),
            "complete": True,
        }

    def set_hash(self, sha256: str | None) -> None:
        if self.current is not None:
            self.current["identity"]["sha256"] = sha256

    def record_file(self, file_record: TaskFile) -> None:
        if self.current is None:
            return
        if not getattr(file_record, "id", None) or getattr(file_record, "storage_status", None) != "stored":
            self.disallow()
            return
        self.current["files"][str(file_record.id)] = file_record

    def disallow(self) -> None:
        if self.current is not None:
            self.current["complete"] = False

    def disallow_all(self) -> None:
        self.disabled = True

    def finish(self) -> None:
        current = self.current
        if current is None:
            return
        digest = current["identity"].get("sha256")
        if current["complete"] and current["files"] and digest and len(digest) == 64:
            before = current["stats_before"]
            self.results[current["asset_id"]] = {
                "complete": True,
                "identity": current["identity"],
                "file_ids": list(current["files"]),
                "csv_params": deepcopy(self.csv_params[current["csv_start"]:]),
                "doc_stats": {
                    "total_docs": self.doc_stats["total_docs"] - before["total_docs"],
                    "total_chunks": self.doc_stats["total_chunks"] - before["total_chunks"],
                    "by_kind": {
                        kind: count - before["by_kind"].get(kind, 0)
                        for kind, count in self.doc_stats["by_kind"].items()
                        if count != before["by_kind"].get(kind, 0)
                    },
                },
            }
        self.current = None

    def manifest(self) -> dict[str, Any]:
        self.finish()
        return {"version": self.version, "assets": {} if self.disabled else self.results}

    def try_reuse(self) -> bool:
        current = self.current
        if current is None or self.previous is None or self.disabled:
            return False
        previous = self.previous_results.get(current["asset_id"])
        if not previous or not previous.get("complete") or previous.get("identity") != current["identity"]:
            return False
        file_ids = [uuid.UUID(value) for value in previous["file_ids"]]
        files = self.db.query(TaskFile).filter(
            TaskFile.id.in_(file_ids),
            TaskFile.snapshot_id == self.previous.id,
            TaskFile.external_task_key == self.snapshot.external_task_key,
            TaskFile.storage_status == "stored",
            TaskFile.deleted_at.is_(None),
        ).all()
        if not files or len(files) != len(file_ids):
            return False
        chunk_count = self.db.query(func.count(TaskChunk.id)).filter(
            TaskChunk.file_id.in_(file_ids),
        ).scalar()
        if chunk_count != previous["doc_stats"]["total_chunks"]:
            return False
        for source in files:
            target = TaskFile(
                id=uuid.uuid4(),
                external_task_key=self.snapshot.external_task_key,
                snapshot_id=self.snapshot.id,
                **{name: getattr(source, name) for name in (
                    "kind", "monday_asset_id", "original_filename", "mime_type", "size_bytes",
                    "bucket", "object_path", "sha256", "storage_status",
                )},
            )
            self.db.add(target)
            self.db.flush()
            self.db.execute(insert(TaskChunk).from_select(
                ["file_id", "page", "section", "chunk_text", "embedding"],
                select(
                    literal(target.id, type_=TaskChunk.file_id.type),
                    TaskChunk.page, TaskChunk.section, TaskChunk.chunk_text, TaskChunk.embedding,
                ).where(TaskChunk.file_id == source.id),
            ))
            self.record_file(target)
        self.csv_params.extend(deepcopy(previous["csv_params"]))
        stats = previous["doc_stats"]
        for name in ("total_docs", "total_chunks"):
            self.doc_stats[name] += stats[name]
        for kind, count in stats["by_kind"].items():
            self.doc_stats["by_kind"][kind] = self.doc_stats["by_kind"].get(kind, 0) + count
        return True