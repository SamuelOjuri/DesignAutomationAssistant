from __future__ import annotations

import math
import logging
from time import perf_counter
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types
from sqlalchemy.orm import Session

from ..config import settings
from ..models import TaskSnapshot, TaskFile, TaskChunk

logger = logging.getLogger(__name__)


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


def _search_snapshot_for_embedding(
    db: Session,
    external_task_key: str,
    snapshot_id: Any,
    query: str,
    query_index: int,
    query_vec: List[float],
    k: int,
) -> List[Dict[str, Any]]:
    distance = TaskChunk.embedding.cosine_distance(query_vec)
    rows = (
        db.query(TaskChunk, TaskFile, distance.label("score"))
        .join(TaskFile, TaskChunk.file_id == TaskFile.id)
        .filter(TaskFile.external_task_key == external_task_key)
        .filter(TaskFile.snapshot_id == snapshot_id)
        .order_by(distance.asc())
        .limit(k)
        .all()
    )

    citations: List[Dict[str, Any]] = []
    for chunk, file_rec, score in rows:
        citations.append(
            {
                "chunkId": str(chunk.id),
                "filename": file_rec.original_filename,
                "page": chunk.page,
                "section": chunk.section,
                "snippet": chunk.chunk_text,
                "score": float(score) if score is not None else None,
                "fileId": str(file_rec.id),
                "mondayAssetId": file_rec.monday_asset_id,
                "matchedQuery": query,
                "matchedQueryIndex": query_index,
            }
        )
    return citations


def _score_value(result: Dict[str, Any]) -> float:
    score = result.get("score")
    return float(score) if score is not None else math.inf


def select_diverse_evidence(
    candidates: List[Dict[str, Any]],
    *,
    max_evidence_chunks: Optional[int] = None,
    max_chunks_per_file: Optional[int] = None,
) -> List[Dict[str, Any]]:
    configured_evidence_limit = settings.chat_retrieval_max_evidence_chunks
    evidence_limit = (
        configured_evidence_limit
        if max_evidence_chunks is None
        else min(max_evidence_chunks, configured_evidence_limit)
    )
    configured_file_limit = settings.chat_retrieval_max_chunks_per_file
    file_limit = (
        configured_file_limit
        if max_chunks_per_file is None
        else min(max_chunks_per_file, configured_file_limit)
    )
    if evidence_limit <= 0 or file_limit <= 0:
        return []

    best_by_chunk: Dict[str, Dict[str, Any]] = {}
    query_buckets: Dict[int, List[str]] = {}
    query_bucket_members: Dict[int, set[str]] = {}
    query_text: Dict[int, str] = {}

    for candidate in candidates:
        chunk_id = str(candidate.get("chunkId") or "")
        query_index = candidate.get("matchedQueryIndex")
        matched_query = candidate.get("matchedQuery")
        if not chunk_id or not isinstance(query_index, int):
            continue

        query_text[query_index] = str(matched_query or "")
        bucket = query_buckets.setdefault(query_index, [])
        bucket_members = query_bucket_members.setdefault(query_index, set())
        if chunk_id not in bucket_members:
            bucket.append(chunk_id)
            bucket_members.add(chunk_id)

        existing = best_by_chunk.get(chunk_id)
        if existing is None:
            merged = dict(candidate)
            merged["matchedQueries"] = [matched_query]
            merged["matchedQueryIndexes"] = [query_index]
            best_by_chunk[chunk_id] = merged
            continue

        matched_queries = list(existing["matchedQueries"])
        matched_query_indexes = list(existing["matchedQueryIndexes"])
        if query_index not in matched_query_indexes:
            matched_queries.append(matched_query)
            matched_query_indexes.append(query_index)

        if _score_value(candidate) < _score_value(existing):
            merged = dict(candidate)
            merged["matchedQueries"] = matched_queries
            merged["matchedQueryIndexes"] = matched_query_indexes
            best_by_chunk[chunk_id] = merged
        else:
            existing["matchedQueries"] = matched_queries
            existing["matchedQueryIndexes"] = matched_query_indexes

    selected: List[Dict[str, Any]] = []
    selected_chunk_ids = set()
    chunks_per_file: Dict[Any, int] = {}
    bucket_positions = {query_index: 0 for query_index in query_buckets}

    while len(selected) < evidence_limit:
        selected_this_round = False
        candidates_remaining = False

        for query_index in sorted(query_buckets):
            bucket = query_buckets[query_index]
            while bucket_positions[query_index] < len(bucket):
                candidates_remaining = True
                chunk_id = bucket[bucket_positions[query_index]]
                bucket_positions[query_index] += 1
                if chunk_id in selected_chunk_ids:
                    continue

                candidate = best_by_chunk[chunk_id]
                file_key = (
                    candidate.get("fileId")
                    or candidate.get("filename")
                    or f"chunk:{chunk_id}"
                )
                file_chunk_count = chunks_per_file.get(file_key, 0)
                if file_chunk_count >= file_limit:
                    continue

                selected_candidate = dict(candidate)
                selected_candidate["selectedByQuery"] = query_text[query_index]
                selected_candidate["selectedByQueryIndex"] = query_index
                selected.append(selected_candidate)
                selected_chunk_ids.add(chunk_id)
                chunks_per_file[file_key] = file_chunk_count + 1
                selected_this_round = True
                break

            if len(selected) >= evidence_limit:
                break

        if not candidates_remaining or not selected_this_round:
            break

    return selected


def search_task_docs_batch(
    db: Session,
    external_task_key: str,
    queries: List[str],
    k: int = 8,
    *,
    max_evidence_chunks: Optional[int] = None,
    max_chunks_per_file: Optional[int] = None,
) -> List[Dict[str, Any]]:
    normalized_queries = []
    for query in queries:
        normalized_query = query.strip() if isinstance(query, str) else ""
        if not normalized_query:
            continue
        normalized_queries.append(normalized_query)
        if len(normalized_queries) >= settings.chat_retrieval_max_compound_queries:
            break
    if not normalized_queries or k <= 0:
        return []

    retrieval_started = perf_counter()
    snapshot = _latest_snapshot(db, external_task_key)
    if snapshot is None:
        logger.info(
            "retrieval: candidates=0 queries=%s duration_ms=%.1f no_snapshot=true",
            len(normalized_queries),
            (perf_counter() - retrieval_started) * 1000,
        )
        return []

    candidate_limit = min(k, settings.chat_retrieval_candidates_per_query)
    client = genai.Client(api_key=settings.gemini_api_key)
    result = client.models.embed_content(
        model="gemini-embedding-001",
        contents=normalized_queries,
        config=types.EmbedContentConfig(
            output_dimensionality=1536,
            task_type="RETRIEVAL_QUERY",
        ),
    )

    candidates: List[Dict[str, Any]] = []
    for query_index, (query, embedding) in enumerate(
        zip(normalized_queries, result.embeddings)
    ):
        query_vec = _normalize(list(embedding.values))
        candidates.extend(
            _search_snapshot_for_embedding(
                db,
                external_task_key,
                snapshot.id,
                query,
                query_index,
                query_vec,
                candidate_limit,
            )
        )

    logger.info(
        "retrieval: candidates=%s queries=%s duration_ms=%.1f",
        len(candidates),
        len(normalized_queries),
        (perf_counter() - retrieval_started) * 1000,
    )
    logger.debug("retrieval: queries=%r", normalized_queries)

    selection_started = perf_counter()
    selected = select_diverse_evidence(
        candidates,
        max_evidence_chunks=max_evidence_chunks,
        max_chunks_per_file=max_chunks_per_file,
    )
    logger.info(
        "retrieval: selected=%s duration_ms=%.1f",
        len(selected),
        (perf_counter() - selection_started) * 1000,
    )
    logger.debug(
        "retrieval: selected_chunks=%s",
        [
            {
                "chunkId": result.get("chunkId"),
                "fileId": result.get("fileId"),
                "score": result.get("score"),
                "selectedByQueryIndex": result.get("selectedByQueryIndex"),
            }
            for result in selected
        ],
    )
    return selected
