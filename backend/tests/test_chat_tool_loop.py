import json
from types import SimpleNamespace

import pytest

from backend.app.routes import chat


class FakeResponse:
    def __init__(self, *, candidates=None, text="", parsed=None):
        self.candidates = candidates or []
        self.text = text
        self.parsed = parsed


def test_sanitize_retrieval_plan_normalizes_deduplicates_and_limits_queries():
    normal_plan = chat._RetrievalPlan(
        search_queries=["  Roof   U-value  ", "roof u-VALUE", "Roof falls"],
        third_search_justified=False,
        exhaustive=True,
    )

    sanitized = chat._sanitize_retrieval_plan(normal_plan, "original question")

    assert sanitized.search_queries == ["Roof U-value", "Roof falls"]
    assert sanitized.third_search_justified is False
    assert sanitized.exhaustive is True

    capped_normal = chat._sanitize_retrieval_plan(
        chat._RetrievalPlan(search_queries=["U-values", "roof falls", "drainage"]),
        "original question",
    )
    assert capped_normal.search_queries == ["U-values", "roof falls"]

    compound = chat._sanitize_retrieval_plan(
        chat._RetrievalPlan(
            search_queries=["U-values", "roof falls", "drainage"],
            third_search_justified=True,
        ),
        "original question",
    )
    assert compound.search_queries == ["U-values", "roof falls", "drainage"]


def test_retrieval_plan_rejects_more_than_three_queries():
    with pytest.raises(ValueError):
        chat._RetrievalPlan(search_queries=["one", "two", "three", "four"])


def test_sanitize_retrieval_plan_falls_back_only_for_failed_or_unusable_plans():
    failed = chat._sanitize_retrieval_plan(None, "  Original   question  ")
    unusable = chat._sanitize_retrieval_plan(
        chat._RetrievalPlan(search_queries=["  "]),
        "  Original   question  ",
    )
    context_only = chat._sanitize_retrieval_plan(
        chat._RetrievalPlan(search_queries=[]),
        "Original question",
    )

    assert failed.search_queries == ["Original question"]
    assert unusable.search_queries == ["Original question"]
    assert context_only.search_queries == []


def test_synthesis_keeps_twelve_chunks_while_ui_keeps_six_public_citations():
    citations = [
        {
            "chunkId": f"chunk-{index}",
            "fileId": f"file-{index}",
            "filename": f"source-{index}.pdf",
            "section": f"section-{index}",
            "snippet": f"evidence {index}",
            "matchedQuery": "roof design",
            "matchedQueryIndex": 0,
        }
        for index in range(15)
    ]
    plan = chat._RetrievalPlan(
        search_queries=["roof design"],
        exhaustive=True,
    )

    synthesis_payload = json.loads(
        chat._synthesis_payload(
            prompt="List every roof requirement",
            history=[chat.ChatMessage(role="user", content="Review the roof")],
            context={"status": "Design"},
            plan=plan,
            citations=citations,
        )
    )
    display_citations = chat._dedupe_citations_for_display(citations)

    assert synthesis_payload["user_question"] == "List every roof requirement"
    assert synthesis_payload["recent_history"] == [
        {"role": "user", "content": "Review the roof"}
    ]
    assert synthesis_payload["task_context"] == {"status": "Design"}
    assert synthesis_payload["retrieval_plan"] == plan.model_dump()
    assert len(synthesis_payload["selected_evidence"]) == 12
    assert len(display_citations) == 6
    assert all("chunkId" not in citation for citation in display_citations)
    assert all("matchedQuery" not in citation for citation in display_citations)


def test_run_bounded_retrieval_preloads_context_batches_and_forces_synthesis(
    monkeypatch,
):
    events = []
    generated = []
    context = {"status": "Design Needed", "priority": "Low"}
    plan = chat._RetrievalPlan(
        search_queries=["roof U-values", "roof falls"],
        exhaustive=False,
    )
    evidence = [
        {
            "chunkId": "chunk-1",
            "filename": "roof.pdf",
            "snippet": "Roof evidence",
            "matchedQueries": ["roof U-values"],
            "selectedByQuery": "roof U-values",
        }
    ]

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            generated.append({"contents": contents, "config": config})
            if config.response_schema is chat._RetrievalPlan:
                events.append("planning")
                return FakeResponse(parsed=plan)
            events.append("synthesis")
            return FakeResponse(text="The roof requires the retrieved design values.")

    class FakeClient:
        def __init__(self, *, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(chat.genai, "Client", FakeClient)

    def fake_get_task_context(db, external_task_key):
        events.append("context")
        return context

    def fake_search_task_docs_batch(db, external_task_key, queries, k):
        events.append("retrieval")
        assert queries == ["roof U-values", "roof falls"]
        assert k == 8
        return evidence

    monkeypatch.setattr(chat, "get_task_context", fake_get_task_context)
    monkeypatch.setattr(chat, "search_task_docs_batch", fake_search_task_docs_batch)

    answer, citations, ok = chat._run_bounded_retrieval(
        db=None,
        external_task_key="acct:board:item",
        prompt="What are the roof U-values and falls?",
        history=[chat.ChatMessage(role="user", content="Review the roof design")],
    )

    assert ok is True
    assert answer == "The roof requires the retrieved design values."
    assert citations == evidence
    assert events == ["context", "planning", "retrieval", "synthesis"]
    assert len(generated) == 2
    assert generated[0]["config"].temperature == 0.1
    assert generated[0]["config"].tools is None
    assert generated[1]["config"].tools is None

    planning_payload = json.loads(generated[0]["contents"])
    synthesis_payload = json.loads(generated[1]["contents"])
    assert planning_payload["task_context"] == context
    assert synthesis_payload["task_context"] == context
    assert synthesis_payload["retrieval_plan"] == plan.model_dump()
    assert synthesis_payload["selected_evidence"][0]["chunkId"] == "chunk-1"


def test_run_bounded_retrieval_skips_search_for_context_only_question(monkeypatch):
    generated_calls = []

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            generated_calls.append(config)
            if config.response_schema is chat._RetrievalPlan:
                return FakeResponse(parsed=chat._RetrievalPlan(search_queries=[]))
            return FakeResponse(text="The task status is Design Needed.")

    class FakeClient:
        def __init__(self, *, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(chat.genai, "Client", FakeClient)
    monkeypatch.setattr(
        chat, "get_task_context", lambda db, external_task_key: {"status": "Design Needed"}
    )
    monkeypatch.setattr(
        chat,
        "search_task_docs_batch",
        lambda *args, **kwargs: pytest.fail("Context-only plan must not search"),
    )

    answer, citations, ok = chat._run_bounded_retrieval(
        db=None,
        external_task_key="acct:board:item",
        prompt="What is the current status?",
        history=None,
    )

    assert ok is True
    assert answer == "The task status is Design Needed."
    assert citations == []
    assert len(generated_calls) == 2


def test_run_bounded_retrieval_uses_original_question_when_planning_fails(
    monkeypatch,
):
    generation_count = 0
    search_calls = []

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            nonlocal generation_count
            generation_count += 1
            if generation_count == 1:
                raise ValueError("malformed plan")
            synthesis_payload = json.loads(contents)
            assert synthesis_payload["retrieval_plan"]["search_queries"] == [
                "Summarize the roof design"
            ]
            return FakeResponse(text="The available roof evidence is summarized.")

    class FakeClient:
        def __init__(self, *, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(chat.genai, "Client", FakeClient)
    monkeypatch.setattr(chat, "get_task_context", lambda db, external_task_key: None)

    def fake_search_task_docs_batch(db, external_task_key, queries, k):
        search_calls.append(queries)
        return [{"chunkId": "chunk-1", "snippet": "Roof evidence"}]

    monkeypatch.setattr(
        chat,
        "search_task_docs_batch",
        fake_search_task_docs_batch,
    )

    answer, citations, ok = chat._run_bounded_retrieval(
        db=None,
        external_task_key="acct:board:item",
        prompt="  Summarize   the roof design  ",
        history=None,
    )

    assert ok is True
    assert answer == "The available roof evidence is summarized."
    assert citations == [{"chunkId": "chunk-1", "snippet": "Roof evidence"}]
    assert search_calls == [["Summarize the roof design"]]
    assert generation_count == 2


def test_run_bounded_retrieval_synthesizes_when_batch_retrieval_fails(monkeypatch):
    generation_count = 0

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            nonlocal generation_count
            generation_count += 1
            if config.response_schema is chat._RetrievalPlan:
                return FakeResponse(
                    parsed=chat._RetrievalPlan(search_queries=["roof details"])
                )
            assert json.loads(contents)["selected_evidence"] == []
            return FakeResponse(
                text="Document evidence was unavailable; the task context is limited."
            )

    class FakeClient:
        def __init__(self, *, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(chat.genai, "Client", FakeClient)
    monkeypatch.setattr(chat, "get_task_context", lambda db, key: {"status": "Design"})
    monkeypatch.setattr(
        chat,
        "search_task_docs_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )

    answer, citations, ok = chat._run_bounded_retrieval(
        db=None,
        external_task_key="acct:board:item",
        prompt="Summarize the roof details",
        history=None,
    )

    assert ok is True
    assert answer == "Document evidence was unavailable; the task context is limited."
    assert citations == []
    assert generation_count == 2


def test_synthesis_requires_non_exhaustive_qualification():
    generated = []

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            generated.append({"contents": contents, "config": config})
            return FakeResponse(text="A bounded answer.")

    client = SimpleNamespace(models=FakeModels())
    plan = chat._RetrievalPlan(
        search_queries=["all roof requirements"],
        exhaustive=True,
    )

    answer = chat._synthesize_answer(
        client,
        prompt="List every roof requirement",
        history=None,
        context=None,
        plan=plan,
        citations=[],
    )

    assert answer == "A bounded answer."
    assert "explicitly state that the answer is non-exhaustive" in str(
        generated[0]["config"].system_instruction
    )
    assert json.loads(generated[0]["contents"])["retrieval_plan"]["exhaustive"] is True
    assert "non-exhaustive" in chat._fallback_answer_from_sources(
        None,
        [],
        exhaustive=True,
    )


def test_chat_complete_returns_json_answer_and_citations(monkeypatch):
    calls = {"access": 0, "commits": 0}
    task = SimpleNamespace(external_task_key="acct:board:item")

    class FakeDb:
        def commit(self):
            calls["commits"] += 1

    def fake_require_task_access(external_task_key, db, current_user):
        assert external_task_key == "acct:board:item"
        assert current_user.id == "user-1"
        return task

    def fake_record_meaningful_access(db, task_arg):
        assert task_arg is task
        calls["access"] += 1

    monkeypatch.setattr(chat, "require_task_access", fake_require_task_access)
    monkeypatch.setattr(chat, "record_meaningful_access", fake_record_meaningful_access)
    monkeypatch.setattr(
        chat,
        "_run_bounded_retrieval",
        lambda **kwargs: (
            "This is the final project summary.",
            [
                {
                    "filename": "source.msg",
                    "fileId": "file-1",
                    "section": "email:body:chunk:1",
                    "snippet": "first body chunk",
                },
                {
                    "filename": "source.msg",
                    "fileId": "file-1",
                    "section": "email:body:chunk:2",
                    "snippet": "second body chunk",
                },
                {
                    "filename": "monday_columns.txt",
                    "fileId": "file-2",
                    "section": "monday:columns",
                },
            ],
            True,
        ),
    )

    response = chat.chat_complete(
        payload=chat.ChatRequest(
            externalTaskKey="acct:board:item",
            message="Provide a concise summary",
            history=None,
        ),
        db=FakeDb(),
        current_user=SimpleNamespace(id="user-1"),
        _csrf=None,
    )

    assert response.content == "This is the final project summary."
    assert response.citations == [
        {
            "filename": "source.msg",
            "fileId": "file-1",
            "section": "email:body",
            "snippet": "first body chunk",
        },
        {
            "filename": "monday_columns.txt",
            "fileId": "file-2",
            "section": "monday:columns",
        },
    ]
    assert response.ok is True
    assert calls == {"access": 1, "commits": 1}
