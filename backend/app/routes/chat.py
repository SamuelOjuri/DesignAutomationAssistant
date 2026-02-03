import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from google import genai
from google.genai import types

from ..auth import CurrentUser, get_current_user
from ..config import settings
from ..db import get_db
from ..schemas import ChatRequest, ChatMessage
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
            "You are a helpful assistant. Use tools when needed to answer questions. "
            "When using retrieved sources, base your answer strictly on tool results."
        ),
    )


def _run_with_tools(
    db: Session,
    external_task_key: str,
    prompt: str,
    history: Optional[List[ChatMessage]],
    max_turns: int = 8,
) -> tuple[List[types.Content], List[Dict[str, Any]], bool]:
    client = genai.Client(api_key=settings.gemini_api_key)
    config = _build_tool_config()

    contents = _history_to_contents(history)
    contents.append(types.Content(role="user", parts=[types.Part(text=prompt)]))

    latest_citations: List[Dict[str, Any]] = []

    for _ in range(max_turns):
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=config,
        )

        function_calls = response.function_calls or []
        if not function_calls:
            # Do NOT append the model response; we want to stream it next
            return contents, latest_citations, True

        contents.append(response.candidates[0].content)

        tool_parts = []
        for fc in function_calls:
            name = getattr(fc, "name", None)
            if name is None and hasattr(fc, "function_call"):
                name = fc.function_call.name

            args = getattr(fc, "args", None)
            if args is None and hasattr(fc, "function_call"):
                args = fc.function_call.args

            if args is None:
                args = {}

            if name == "get_task_context":
                context = get_task_context(db, external_task_key)
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
                types.Part.from_function_response(
                    name=name,
                    response=tool_payload,
                )
            )
        
        if tool_parts:
            contents.append(types.Content(role="tool", parts=tool_parts))

    return contents, latest_citations, False


@router.post("/chat")
def chat(
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_task_access(payload.externalTaskKey, db, current_user)

    def event_stream():
        yield _sse({"type": "start", "ts": datetime.now(timezone.utc).isoformat()})
        try:
            contents, citations, ok = _run_with_tools(
                db=db,
                external_task_key=payload.externalTaskKey,
                prompt=payload.message,
                history=payload.history,
            )
            if not ok:
                yield _sse({"type": "message", "content": "Stopped because max_turns was reached."})
                yield _sse({"type": "done"})
                return

            final_config = types.GenerateContentConfig(
                # No tools in final call to prevent more tool requests
                tools=None,
                system_instruction=(
                    "You are a helpful assistant. Do not call tools. "
                    "Answer only using the provided tool results."
                ),
            )

            client = genai.Client(api_key=settings.gemini_api_key)

            stream = client.models.generate_content_stream(
                model=settings.gemini_model,
                contents=contents,
                config=final_config,
            )

            for chunk in stream:
                if chunk.text:
                    yield _sse({"type": "message", "content": chunk.text})

            if citations:
                yield _sse({"type": "citations", "citations": citations})

        except Exception as e:
            yield _sse({"type": "message", "content": f"Error: {e}"})
        finally:
            yield _sse({"type": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream")