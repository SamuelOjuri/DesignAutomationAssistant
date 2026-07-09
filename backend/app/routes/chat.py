import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from google import genai
from google.genai import types

from ..auth import CurrentUser, get_current_user, require_csrf_token
from ..config import settings
from ..db import get_db
from ..schemas import ChatRequest, ChatMessage
from ..services.auto_sync_purge import record_meaningful_access
from ..services.retrieval import get_task_context, search_task_docs
from .tasks import require_task_access

router = APIRouter(prefix="/api", tags=["chat"])


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _history_to_contents(history: Optional[List[ChatMessage]]) -> List[types.Content]:
    contents: List[types.Content] = []
    if not history:
        return contents

    for msg in history:
        # Map app roles -> GenAI roles
        role = "user" if msg.role == "user" else "model"
        text = (msg.content or "").strip()
        if not text:
            continue
        contents.append(
            types.Content(role=role, parts=[types.Part(text=text)])
        )
    return contents


def _build_tool_config() -> types.GenerateContentConfig:
    tool_decls = [
        {
            "name": "get_task_context",
            "description": "Get structured task context for the current task.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "search_task_docs",
            "description": "Search task documents for relevant passages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query text.",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Max number of results (1-20).",
                    },
                },
                "required": ["query"],
            },
        },
    ]

    tools = types.Tool(function_declarations=tool_decls)

    # #if uncondition tool call is needed
    # tool_config = types.ToolConfig(
    #     function_calling_config=types.FunctionCallingConfig(
    #         mode="ANY",
    #         allowed_function_names=["search_task_docs"],  # or include get_task_context too
    #     )
    # )

    return types.GenerateContentConfig(
        tools=[tools],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        system_instruction=(
            "You are a helpful technical design assistant. Use tools when needed "
            "to answer questions about the current task. After tool results are "
            "provided, produce a concise plain-text answer. When using retrieved "
            "sources, base your answer strictly on tool results."
        ),
    )


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


def _sources_payload(context: Any, citations: List[Dict[str, Any]]) -> str:
    return json.dumps(
        {
            "task_context": context,
            "citations": [
                {
                    "filename": citation.get("filename"),
                    "page": citation.get("page"),
                    "section": citation.get("section"),
                    "snippet": _limited_text(citation.get("snippet"), 1800),
                }
                for citation in citations[:6]
            ],
        },
        default=str,
    )


def _fallback_answer_from_sources(context: Any, citations: List[Dict[str, Any]]) -> str:
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

    lines = ["I found relevant task context and source material, but the model did not produce a final synthesis."]
    if details:
        lines.append("Task details: " + "; ".join(details))
    if snippets:
        lines.append("Relevant source excerpts: " + " | ".join(snippets))
    return "\n\n".join(lines)


def _synthesize_without_tools(
    client: genai.Client,
    *,
    prompt: str,
    context: Any,
    citations: List[Dict[str, Any]],
) -> str:
    if context is None and not citations:
        return ""

    synthesis_config = types.GenerateContentConfig(
        system_instruction=(
            "You are a helpful technical design assistant. Produce a concise "
            "plain-text answer using only the supplied task context and source "
            "excerpts. Do not call tools. If the supplied material is incomplete, "
            "say what is missing."
        )
    )
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        text=(
                            f"User question:\n{prompt}\n\n"
                            f"Task context and source excerpts:\n{_sources_payload(context, citations)}"
                        )
                    )
                ],
            )
        ],
        config=synthesis_config,
    )
    return _response_text(response)


def _function_response_part(
    *,
    name: str | None,
    response: dict[str, Any],
    call_id: str | None,
) -> types.Part:
    function_response_kwargs = {
        "name": name or "unknown_tool",
        "response": {"result": response},
    }
    if call_id:
        function_response_kwargs["id"] = call_id

    return types.Part(
        function_response=types.FunctionResponse(**function_response_kwargs)
    )


def _run_with_tools(
    db: Session,
    external_task_key: str,
    prompt: str,
    history: Optional[List[ChatMessage]],
    max_turns: int = 8,
) -> tuple[str, List[Dict[str, Any]], bool]:
    client = genai.Client(api_key=settings.gemini_api_key)
    config = _build_tool_config()

    contents = _history_to_contents(history)
    contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))

    latest_citations: List[Dict[str, Any]] = []
    latest_context: Any = None

    for _ in range(max_turns):
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=config,
        )

        function_calls = response.function_calls or []
        if not function_calls:
            answer = _response_text(response)
            if answer:
                return answer, latest_citations, True

            synthesis = _synthesize_without_tools(
                client,
                prompt=prompt,
                context=latest_context,
                citations=latest_citations,
            )
            if synthesis:
                return synthesis, latest_citations, True

            if latest_context is not None or latest_citations:
                fallback = _fallback_answer_from_sources(latest_context, latest_citations)
                return fallback, latest_citations, False

            return "", latest_citations, False

        contents.append(response.candidates[0].content)

        tool_parts = []
        for fc in function_calls:
            name = getattr(fc, "name", None)
            if name is None and hasattr(fc, "function_call"):
                name = fc.function_call.name

            args = getattr(fc, "args", None)
            if args is None and hasattr(fc, "function_call"):
                args = fc.function_call.args

            call_id = getattr(fc, "id", None)
            if call_id is None and hasattr(fc, "function_call"):
                call_id = fc.function_call.id

            if args is None:
                args = {}

            if name == "get_task_context":
                context = get_task_context(db, external_task_key)
                latest_context = context
                tool_payload = {"task_context": context}

            elif name == "search_task_docs":
                query = (args.get("query") or "").strip()
                k = args.get("k", 8)
                try:
                    k = max(1, min(20, int(k)))
                except Exception:
                    k = 8
                results = search_task_docs(db, external_task_key, query, k=k)
                latest_citations = results
                tool_payload = {"citations": results}

            else:
                tool_payload = {"error": f"Unknown tool: {name}"}

            tool_parts.append(
                _function_response_part(
                    name=name,
                    response=tool_payload,
                    call_id=call_id,
                )
            )
        
        if tool_parts:
            contents.append(types.Content(role="user", parts=tool_parts))

    synthesis = _synthesize_without_tools(
        client,
        prompt=prompt,
        context=latest_context,
        citations=latest_citations,
    )
    if synthesis:
        return synthesis, latest_citations, True

    fallback = _fallback_answer_from_sources(latest_context, latest_citations)
    return fallback, latest_citations, False


@router.post("/chat")
def chat(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf_token),
):
    task = require_task_access(payload.externalTaskKey, db, current_user)
    if payload.message.strip():
        record_meaningful_access(db, task)
        db.commit()

    def event_stream():
        yield _sse({"type": "start", "ts": datetime.now(timezone.utc).isoformat()})
        try:
            answer, citations, ok = _run_with_tools(
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

            if answer:
                yield _sse({"type": "message", "content": answer})
            else:
                yield _sse(
                    {
                        "type": "message",
                        "content": (
                            "I found relevant sources, but no final answer was "
                            "returned. Please try again."
                        ),
                    }
                )

            if citations:
                yield _sse({"type": "citations", "citations": citations})

        except Exception as e:
            yield _sse({"type": "message", "content": f"Error: {e}"})
        finally:
            yield _sse({"type": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")