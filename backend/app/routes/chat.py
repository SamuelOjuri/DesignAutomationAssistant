import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from google import genai
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

from ..auth import CurrentUser, get_current_user, require_csrf_token
from ..config import settings
from ..db import get_db
from ..schemas import ChatRequest, ChatMessage, ChatCompleteResponse
from ..services.auto_sync_purge import record_meaningful_access
from ..services.retrieval import get_task_context, search_task_docs_batch
from .tasks import require_task_access

router = APIRouter(prefix="/api", tags=["chat"])
logger = logging.getLogger(__name__)


class _RetrievalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_queries: List[str] = Field(
        default_factory=list,
        max_length=3,
        description="Distinct searches needed to answer the user's question.",
    )
    third_search_justified: bool = Field(
        default=False,
        description="True only when the question contains three independent subquestions.",
    )
    exhaustive: bool = Field(
        default=False,
        description="Whether the question asks for an exhaustive or absence-sensitive answer.",
    )


def _normalize_search_query(query: Any) -> str:
    if not isinstance(query, str):
        return ""
    return " ".join(query.split())


def _sanitize_retrieval_plan(
    plan: Optional[_RetrievalPlan],
    original_question: str,
) -> _RetrievalPlan:
    fallback_query = _normalize_search_query(original_question)
    if plan is None:
        return _RetrievalPlan(
            search_queries=[fallback_query] if fallback_query else [],
        )

    query_limit = (
        settings.chat_retrieval_max_compound_queries
        if plan.third_search_justified
        else settings.chat_retrieval_max_queries
    )
    normalized_queries: List[str] = []
    seen_queries = set()

    for query in plan.search_queries:
        normalized_query = _normalize_search_query(query)
        dedupe_key = normalized_query.casefold()
        if not normalized_query or dedupe_key in seen_queries:
            continue
        seen_queries.add(dedupe_key)
        normalized_queries.append(normalized_query)
        if len(normalized_queries) >= query_limit:
            break

    if plan.search_queries and not normalized_queries and fallback_query:
        normalized_queries.append(fallback_query)

    return plan.model_copy(update={"search_queries": normalized_queries})


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text).strip()

    parts = []
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                parts.append(str(part_text))
    return "".join(parts).strip()


def _limited_text(value: Any, limit: int = 2000) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else f"{text[:limit]}..."


def _history_payload(
    history: Optional[List[ChatMessage]],
    limit: int = 8,
) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = []
    for message in (history or [])[-limit:]:
        content = _limited_text(message.content, 2000)
        if content:
            messages.append({"role": message.role, "content": content})
    return messages


def _plan_retrieval(
    client: genai.Client,
    *,
    prompt: str,
    history: Optional[List[ChatMessage]],
    context: Any,
) -> _RetrievalPlan:
    planning_config = types.GenerateContentConfig(
        temperature=0.1,
        response_mime_type="application/json",
        response_schema=_RetrievalPlan,
        system_instruction=(
            "Plan bounded document retrieval for a technical design assistant. "
            "Return no search queries when the supplied task context is enough to "
            "answer the question. Otherwise return one or two distinct, focused "
            "queries. Return three only when the question has three independent "
            "subquestions, and then set third_search_justified to true. Mark "
            "exhaustive true for inventories, audits, chronologies, contradiction "
            "checks, completeness requests, or claims that something is absent. "
            "Do not answer the question."
        ),
    )
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=json.dumps(
            {
                "user_question": prompt,
                "recent_history": _history_payload(history),
                "task_context": context,
            },
            default=str,
        ),
        config=planning_config,
    )

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, _RetrievalPlan):
        return parsed
    if isinstance(parsed, dict):
        return _RetrievalPlan.model_validate(parsed)

    response_text = _response_text(response)
    if not response_text:
        raise ValueError("Retrieval planner returned no structured plan")
    return _RetrievalPlan.model_validate_json(response_text)


def _synthesis_payload(
    *,
    prompt: str,
    history: Optional[List[ChatMessage]],
    context: Any,
    plan: _RetrievalPlan,
    citations: List[Dict[str, Any]],
) -> str:
    return json.dumps(
        {
            "user_question": prompt,
            "recent_history": _history_payload(history),
            "task_context": context,
            "retrieval_plan": plan.model_dump(),
            "selected_evidence": [
                {
                    "chunkId": citation.get("chunkId"),
                    "filename": citation.get("filename"),
                    "page": citation.get("page"),
                    "section": citation.get("section"),
                    "snippet": _limited_text(citation.get("snippet"), 1800),
                    "score": citation.get("score"),
                    "matchedQueries": citation.get("matchedQueries"),
                    "selectedByQuery": citation.get("selectedByQuery"),
                }
                for citation in citations[
                    : settings.chat_retrieval_max_evidence_chunks
                ]
            ],
        },
        default=str,
    )


def _citation_debug_payload(citations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "filename": citation.get("filename"),
            "section": citation.get("section"),
            "score": citation.get("score"),
        }
        for citation in citations[:6]
    ]


def _display_section(section: Any) -> Any:
    if not isinstance(section, str):
        return section
    parts = section.split(":")
    if len(parts) >= 3 and parts[-2] == "chunk" and parts[-1].isdigit():
        return ":".join(parts[:-2])
    return section


def _citation_display_key(citation: Dict[str, Any]) -> tuple[Any, Any, Any]:
    return (
        citation.get("fileId") or citation.get("filename"),
        citation.get("page"),
        _display_section(citation.get("section")),
    )


def _dedupe_citations_for_display(
    citations: List[Dict[str, Any]],
    limit: int = 6,
) -> List[Dict[str, Any]]:
    seen = set()
    deduped: List[Dict[str, Any]] = []

    for citation in citations:
        key = _citation_display_key(citation)
        if key in seen:
            continue
        seen.add(key)

        display_citation = {
            key: citation[key]
            for key in (
                "filename",
                "page",
                "section",
                "snippet",
                "score",
                "fileId",
                "mondayAssetId",
            )
            if key in citation
        }
        display_citation["section"] = _display_section(citation.get("section"))
        deduped.append(display_citation)
        if len(deduped) >= limit:
            break

    return deduped


def _fallback_answer_from_sources(
    context: Any,
    citations: List[Dict[str, Any]],
    *,
    exhaustive: bool,
) -> str:
    details: List[str] = []
    if isinstance(context, dict):
        for key, value in context.items():
            if value in (None, "", [], {}):
                continue
            details.append(f"{key}: {_limited_text(value, 220)}")
            if len(details) >= 5:
                break

    snippets = [
        _limited_text(citation.get("snippet"), 260)
        for citation in citations[:3]
        if citation.get("snippet")
    ]

    lines = ["The model did not produce a final synthesis."]
    if exhaustive:
        lines.append(
            "This answer is non-exhaustive because only bounded evidence was reviewed."
        )
    if details:
        lines.append("Task details: " + "; ".join(details))
    if snippets:
        lines.append("Relevant source excerpts: " + " | ".join(snippets))
    if not details and not snippets:
        lines.append("No usable task context or document evidence was available.")
    return "\n\n".join(lines)


def _synthesize_answer(
    client: genai.Client,
    *,
    prompt: str,
    history: Optional[List[ChatMessage]],
    context: Any,
    plan: _RetrievalPlan,
    citations: List[Dict[str, Any]],
) -> str:
    synthesis_config = types.GenerateContentConfig(
        temperature=0.2,
        system_instruction=(
            "You are a helpful technical design assistant. Produce a concise "
            "plain-text answer using only the supplied task context and selected "
            "evidence. Do not call tools and do not invent facts. If the supplied "
            "material is incomplete, identify the missing evidence needed for a "
            "firmer answer. When retrieval_plan.exhaustive is true, explicitly "
            "state that the answer is non-exhaustive because retrieval was bounded; "
            "apply the same qualification to any broad inventory, audit, chronology, "
            "contradiction, completeness, or absence request even if that flag is "
            "false. Never claim corpus-wide completeness or prove absence from these "
            "results."
        )
    )
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=_synthesis_payload(
            prompt=prompt,
            history=history,
            context=context,
            plan=plan,
            citations=citations,
        ),
        config=synthesis_config,
    )
    return _response_text(response)


def _run_bounded_retrieval(
    db: Session,
    external_task_key: str,
    prompt: str,
    history: Optional[List[ChatMessage]],
) -> tuple[str, List[Dict[str, Any]], bool]:
    client = genai.Client(api_key=settings.gemini_api_key)
    context = get_task_context(db, external_task_key)

    try:
        proposed_plan = _plan_retrieval(
            client,
            prompt=prompt,
            history=history,
            context=context,
        )
    except Exception as exc:
        logger.warning(
            "chat: retrieval planning failed; using original question (%s)",
            type(exc).__name__,
        )
        proposed_plan = None

    plan = _sanitize_retrieval_plan(proposed_plan, prompt)
    logger.info(
        "chat: retrieval plan searches=%s exhaustive=%s",
        len(plan.search_queries),
        plan.exhaustive,
    )
    logger.debug("chat: planned search queries=%r", plan.search_queries)

    citations: List[Dict[str, Any]] = []
    if plan.search_queries:
        try:
            citations = search_task_docs_batch(
                db,
                external_task_key,
                plan.search_queries,
                k=settings.chat_retrieval_candidates_per_query,
            )
        except Exception as exc:
            logger.warning(
                "chat: batch retrieval failed; synthesizing without document "
                "evidence (%s)",
                type(exc).__name__,
            )
    logger.info("chat: selected evidence chunks=%s", len(citations))
    logger.debug(
        "chat: selected evidence=%s",
        _citation_debug_payload(citations),
    )

    answer = _synthesize_answer(
        client,
        prompt=prompt,
        history=history,
        context=context,
        plan=plan,
        citations=citations,
    )
    if answer:
        logger.info("chat: final synthesis (%s chars)", len(answer))
        return answer, citations, True

    logger.warning(
        "chat: synthesis empty; grounded fallback (context=%s, citations=%s)",
        context is not None,
        len(citations),
    )
    fallback = _fallback_answer_from_sources(
        context,
        citations,
        exhaustive=plan.exhaustive,
    )
    return fallback, citations, False


@router.post("/chat/complete", response_model=ChatCompleteResponse)
def chat_complete(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf_token),
) -> ChatCompleteResponse:
    task = require_task_access(payload.externalTaskKey, db, current_user)
    if payload.message.strip():
        record_meaningful_access(db, task)
        db.commit()

    answer, citations, ok = _run_bounded_retrieval(
        db=db,
        external_task_key=payload.externalTaskKey,
        prompt=payload.message,
        history=payload.history,
    )
    if not ok and not answer:
        answer = (
            "I found relevant sources, but the model did not finish a "
            "plain-text answer. Please try rephrasing the question."
        )
    if not answer:
        answer = (
            "I found relevant sources, but no final answer was "
            "returned. Please try again."
        )

    return ChatCompleteResponse(
        content=answer,
        citations=_dedupe_citations_for_display(citations),
        ok=ok,
    )
