import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.app.routes import chat
from backend.app.services.chat_streaming import AnswerJsonStream, event_stream


@pytest.mark.parametrize("size", [1, 2, 3, 7, 128])
@pytest.mark.parametrize("ascii_only", [True, False])
def test_answer_parser_handles_arbitrary_escapes_and_chunk_boundaries(size, ascii_only):
    answer = 'Roof "A": C:\\roof\\plan\n£100, café, 😀 [S1]\tend '
    document = json.dumps({"cited_chunk_ids": ["private-id"], "answer": answer}, ensure_ascii=ascii_only)
    parser = AnswerJsonStream()
    deltas = [parser.feed(document[index:index + size]) for index in range(0, len(document), size)]
    assert "".join(deltas) == answer
    assert parser.document == document
    assert "private-id" not in "".join(deltas)


def test_parser_waits_for_complete_surrogate_pair():
    parser = AnswerJsonStream()
    assert parser.feed('{"answer":"hello \\ud83d') == "hello "
    assert parser.feed('\\ude00') == "😀"
    assert parser.feed('","cited_chunk_ids":[]}') == ""


@pytest.mark.parametrize("fragment", ['{"answer":false}', '{"answer":"\\x"}', '{"answer":"\\udc00"}', '{"answer":"bad\n"}', '{"answer":"\\u+123"}'])
def test_parser_rejects_invalid_strings(fragment):
    with pytest.raises(ValueError):
        AnswerJsonStream().feed(fragment)


def test_final_validation_checks_citations_and_adds_coverage_note():
    evidence = [{"chunkId": "one", "filename": "roof.pdf", "section": "page:chunk:1"}]
    plan = chat._RetrievalPlan(corpus_wide_requested=True)
    answer, citations = chat._validated_stream_answer(
        json.dumps({"answer": "A roof detail. [S1]", "cited_chunk_ids": ["one"]}), plan, evidence,
    )
    assert answer.endswith(chat._PROJECT_COVERAGE_NOTE)
    assert citations[0]["sourceId"] == "S1"
    for content, ids in [("A detail [S2]", ["one"]), ("A detail [S1]", []), ("A detail", ["unknown"])]:
        with pytest.raises(ValueError):
            chat._validated_stream_answer(json.dumps({"answer": content, "cited_chunk_ids": ids}), plan, evidence)


def test_event_stream_heartbeats_do_not_restart_or_move_the_producer():
    async def run():
        owner = None
        closed = False
        async def events():
            nonlocal owner, closed
            owner = asyncio.current_task()
            try:
                yield "status", {"message": "Working"}
                await asyncio.sleep(0.04)
                assert asyncio.current_task() is owner
                yield "done", {"content": "Answer", "citations": [], "ok": True}
            finally:
                assert asyncio.current_task() is owner
                closed = True
        frames = [frame async for frame in event_stream(events(), timeout_seconds=1, heartbeat_seconds=0.01)]
        assert any(frame.startswith(b": keep-alive") for frame in frames)
        assert frames[-1].startswith(b"event: done")
        assert closed
    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_timeout_and_consumer_close_cancel_pending_work(cancel):
    async def run():
        closed = False
        async def events():
            nonlocal closed
            try:
                yield "status", {"message": "Working"}
                await asyncio.Event().wait()
            finally:
                closed = True
        stream = event_stream(events(), timeout_seconds=0.03, heartbeat_seconds=0.01)
        if cancel:
            assert (await anext(stream)).startswith(b"event: status")
            await stream.aclose()
        else:
            frames = [frame async for frame in stream]
            assert b'"code": "timeout"' in frames[-1]
        assert closed
    asyncio.run(run())


def test_eof_without_terminal_event_is_an_error():
    async def run():
        async def events():
            yield "delta", {"text": "Partial"}
        frames = [frame async for frame in event_stream(events(), timeout_seconds=1)]
        assert b'"code": "interrupted"' in frames[-1]
    asyncio.run(run())


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
def test_response_starts_before_work_and_closes_producer_on_disconnect(spec_version):
    from starlette.requests import ClientDisconnect, Request

    async def run():
        disconnected = asyncio.Event()
        closed = asyncio.Event()
        work_started = False
        sent = []
        async def events():
            nonlocal work_started
            work_started = True
            try:
                yield "status", {"message": "Working"}
                await asyncio.Event().wait()
            finally:
                closed.set()
        async def receive():
            await disconnected.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.start":
                assert not work_started
            else:
                assert message["body"].startswith(b"event: status")
                disconnected.set()
                if spec_version == "2.4":
                    raise OSError("Client disconnected during send")
                await asyncio.Event().wait()
        scope = {"type": "http", "asgi": {"spec_version": spec_version}}
        response = chat._stream_response(events(), Request(scope, receive), 1)
        if spec_version == "2.4":
            with pytest.raises(ClientDisconnect):
                await asyncio.wait_for(response(scope, receive, send), timeout=1)
        else:
            await asyncio.wait_for(response(scope, receive, send), timeout=1)
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 200
        assert closed.is_set(), "Cleanup must finish without waiting for garbage collection"
    asyncio.run(run())


def install_fake_pipeline(monkeypatch, *, document=None, finish=True, hang=False, planning_fails=False, retrieval_fails=False, search_queries=None):
    state = {"client_closed": False, "stream_closed": False, "retrieval": 0}
    evidence = [{"chunkId": "one", "filename": "roof.pdf", "section": "page:chunk:1", "snippet": "Roof evidence"}]

    class Models:
        async def generate_content(self, **kwargs):
            if planning_fails:
                raise ValueError("Invalid plan")
            return SimpleNamespace(parsed={"search_queries": ["roof"] if search_queries is None else search_queries})

        async def embed_content(self, **kwargs):
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 0.0])])

        async def generate_content_stream(self, **kwargs):
            async def chunks():
                try:
                    output = document if document is not None else json.dumps({"answer": "Roof £100. [S1]", "cited_chunk_ids": ["one"]})
                    for character in output:
                        yield SimpleNamespace(candidates=[SimpleNamespace(
                            finish_reason=None, content=SimpleNamespace(parts=[SimpleNamespace(text=character, thought=False)]),
                        )])
                    if hang:
                        await asyncio.Event().wait()
                    if finish:
                        yield SimpleNamespace(candidates=[SimpleNamespace(finish_reason=chat.types.FinishReason.STOP, content=None)])
                finally:
                    state["stream_closed"] = True
            return chunks()

    class AsyncClient:
        models = Models()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            state["client_closed"] = True

    async def read(operation, *args, **kwargs):
        if operation is chat.get_task_context:
            return {"status": "Design Needed"}
        state["retrieval"] += 1
        assert kwargs["query_embeddings"] == [[1.0, 0.0]]
        if retrieval_fails:
            raise RuntimeError("Database unavailable")
        return evidence

    monkeypatch.setattr(chat, "_read_for_stream", read)
    monkeypatch.setattr(chat, "create_gemini_client", lambda **kwargs: SimpleNamespace(aio=AsyncClient()))
    return state


def test_pipeline_streams_only_answer_and_finalises_public_citations(monkeypatch):
    state = install_fake_pipeline(monkeypatch, planning_fails=True)
    async def run():
        events = [event async for event in chat._chat_events(chat.ChatRequest(externalTaskKey="a:b:c", message="roof"))]
        assert [data["message"] for name, data in events if name == "status"] == [
            "Reading project details…",
            "Searching project documents…",
            "Generating response…",
        ]
        assert "".join(data["text"] for name, data in events if name == "delta") == "Roof £100. [S1]"
        name, result = events[-1]
        assert name == "done" and result["ok"] is True
        assert result["citations"][0]["sourceId"] == "S1"
        assert result["citations"][0]["section"] == "page"
        assert "chunkId" not in result["citations"][0]
    asyncio.run(run())
    assert state == {"client_closed": True, "stream_closed": True, "retrieval": 1}


@pytest.mark.parametrize("document,finish", [('{"answer":"partial', True), ('{"answer":"answer", "cited_chunk_ids":[]}', False), ('{"answer":"bad [S99]", "cited_chunk_ids":[]}', True)])
def test_invalid_or_unfinished_synthesis_replaces_provisional_text_with_grounded_fallback(monkeypatch, document, finish):
    state = install_fake_pipeline(monkeypatch, document=document, finish=finish)
    async def run():
        events = [event async for event in chat._chat_events(chat.ChatRequest(externalTaskKey="a:b:c", message="roof"))]
        name, result = events[-1]
        assert name == "done" and result["ok"] is False
        assert result["content"].startswith("The model did not produce a final synthesis.")
        assert "Roof evidence" in result["content"]
        assert '{"answer"' not in result["content"]
    asyncio.run(run())
    assert state["client_closed"] and state["stream_closed"]


def test_retrieval_failure_still_allows_context_only_answer(monkeypatch):
    install_fake_pipeline(monkeypatch, retrieval_fails=True, document='{"answer":"Design Needed", "cited_chunk_ids":[]}')
    async def run():
        events = [event async for event in chat._chat_events(chat.ChatRequest(externalTaskKey="a:b:c", message="status"))]
        assert events[-1] == ("done", {"content": "Design Needed", "citations": [], "ok": True})
    asyncio.run(run())


def test_context_only_answer_announces_generation_before_first_delta(monkeypatch):
    state = install_fake_pipeline(
        monkeypatch, search_queries=[],
        document='{"answer":"Design Needed", "cited_chunk_ids":[]}',
    )

    async def run():
        events = [event async for event in chat._chat_events(chat.ChatRequest(externalTaskKey="a:b:c", message="status"))]
        first_delta = next(index for index, (name, _) in enumerate(events) if name == "delta")
        assert events[:first_delta] == [
            ("status", {"message": "Reading project details…"}),
            ("status", {"message": "Generating response…"}),
        ]
        assert events[-1] == ("done", {"content": "Design Needed", "citations": [], "ok": True})

    asyncio.run(run())
    assert state["retrieval"] == 0


def test_cancellation_closes_provider_stream_and_client_without_fallback(monkeypatch):
    state = install_fake_pipeline(monkeypatch, hang=True)
    async def run():
        frames = [frame async for frame in event_stream(
            chat._chat_events(chat.ChatRequest(externalTaskKey="a:b:c", message="roof")), timeout_seconds=0.08,
        )]
        assert frames[-1].startswith(b"event: error")
        assert not any(frame.startswith(b"event: done") for frame in frames)
    asyncio.run(run())
    assert state["client_closed"] and state["stream_closed"]


def test_database_read_owns_and_closes_its_session(monkeypatch):
    closed = []
    class OwnedSession:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            closed.append(self)
    monkeypatch.setattr(chat, "SessionLocal", OwnedSession)
    def operation(db):
        assert isinstance(db, OwnedSession)
        raise ValueError("failed")
    with pytest.raises(ValueError):
        asyncio.run(chat._read_for_stream(operation))
    assert len(closed) == 1


def test_real_google_sdk_streaming_uses_async_transport_and_closes_response(monkeypatch):
    """Exercise installed SDK framing/configuration without any external API calls."""
    import httpx
    from google import genai

    requests = []
    closed = []
    class ProviderBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for text, reason in [('{"answer":"A roof', None), (' answer.","cited_chunk_ids":[]}', "STOP")]:
                candidate = {"content": {"role": "model", "parts": [{"text": text}]}}
                if reason:
                    candidate["finishReason"] = reason
                yield f"data: {json.dumps({'candidates': [candidate]})}\n\n".encode()
                await asyncio.sleep(0)
        async def aclose(self):
            closed.append(True)

    async def handle(request):
        requests.append(request.url.path)
        body = json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        if "streamGenerateContent" in request.url.path:
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=ProviderBody())
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"search_queries":[]}'}]}, "finishReason": "STOP"}]})

    def client(**kwargs):
        return genai.Client(api_key="test-only", http_options=chat.types.HttpOptions(
            async_client_args={"transport": httpx.MockTransport(handle)},
        ))

    async def read(*args, **kwargs):
        return {"status": "Design Needed"}
    monkeypatch.setattr(chat, "create_gemini_client", client)
    monkeypatch.setattr(chat, "_read_for_stream", read)
    async def run():
        frames = [frame async for frame in event_stream(
            chat._chat_events(chat.ChatRequest(externalTaskKey="a:b:c", message="roof")), timeout_seconds=2,
        )]
        assert any(b'"text": "A roof"' in frame for frame in frames)
        assert b'"content": "A roof answer."' in frames[-1]
        assert b'"ok": true' in frames[-1]
    asyncio.run(run())
    assert len(requests) == 2
    assert closed
