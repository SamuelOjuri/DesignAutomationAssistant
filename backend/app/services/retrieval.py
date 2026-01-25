from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types
from sqlalchemy.orm import Session

from ..config import settings
from ..models import TaskSnapshot, TaskFile, TaskChunk


def _normalize(vec: List[float]) -> List[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


def _latest_snapshot(db: Session, external_task_key: str) -> Optional[TaskSnapshot]:
    return (
        db.query(TaskSnapshot)
        .filter_by(external_task_key=external_task_key)
        .order_by(TaskSnapshot.created_at.desc())
        .first()
    )


def get_task_context(db: Session, external_task_key: str) -> Optional[Dict[str, Any]]:
    """
    Fetch latest task snapshot and return its task_context_json.
    """
    snapshot = _latest_snapshot(db, external_task_key)
    return snapshot.task_context_json if snapshot else None


def search_task_docs(
    db: Session,
    external_task_key: str,
    query: str,
    k: int = 8,
) -> List[Dict[str, Any]]:
    """
    Embed query and retrieve top-K similar chunks scoped to the latest snapshot.
    Returns citations with filename/page/section/snippet/score.
    """
    if not query or k <= 0:
        return []

    snapshot = _latest_snapshot(db, external_task_key)
    if snapshot is None:
        return []

    client = genai.Client(api_key=settings.gemini_api_key)
    result = client.models.embed_content(
        model="gemini-embedding-001",
        contents=[query],
        config=types.EmbedContentConfig(
            output_dimensionality=1536,
            task_type="RETRIEVAL_QUERY",
        ),
    )
    query_vec = _normalize(list(result.embeddings[0].values))

    distance = TaskChunk.embedding.cosine_distance(query_vec)
    rows = (
        db.query(TaskChunk, TaskFile, distance.label("score"))
        .join(TaskFile, TaskChunk.file_id == TaskFile.id)
        .filter(TaskFile.external_task_key == external_task_key)
        .filter(TaskFile.snapshot_id == snapshot.id)
        .order_by(distance.asc())
        .limit(k)
        .all()
    )

    citations: List[Dict[str, Any]] = []
    for chunk, file_rec, score in rows:
        citations.append(
            {
                "filename": file_rec.original_filename,
                "page": chunk.page,
                "section": chunk.section,
                "snippet": chunk.chunk_text,
                "score": float(score) if score is not None else None,
                "fileId": str(file_rec.id),
                "mondayAssetId": file_rec.monday_asset_id,
            }
        )
    return citations