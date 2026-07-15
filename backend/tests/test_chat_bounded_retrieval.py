import json
import logging
from types import SimpleNamespace

import pytest

from backend.app.routes import chat


class FakeResponse:
    def __init__(self, *, candidates=None, text="", parsed=None):
        self.candidates = candidates or []
        self.text = text
        self.parsed = parsed


def _is_retrieval_plan_config(config):
    return config.response_json_schema == chat._RetrievalPlan.model_json_schema()


def test_sanitize_retrieval_plan_normalizes_deduplicates_and_limits_queries():
    normal_plan = chat._RetrievalPlan(
        search_queries=["  Roof   U-value  ", "roof u-VALUE", "Roof falls"],
        third_search_justified=False,
        corpus_wide_requested=True,
    )

    sanitized = chat._sanitize_retrieval_plan(normal_plan, "original question")

    assert sanitized.search_queries == ["Roof U-value", "Roof falls"]
    assert sanitized.third_search_justified is False
    assert sanitized.corpus_wide_requested is True

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


def test_synthesis_labels_twelve_chunks_while_ui_keeps_six_public_citations():
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
        corpus_wide_requested=True,
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
    display_citations = chat._citations_for_display(citations)

    assert synthesis_payload["user_question"] == "List every roof requirement"
    assert synthesis_payload["recent_history"] == [
        {"role": "user", "content": "Review the roof"}
    ]
    assert synthesis_payload["task_context"] == {"status": "Design"}
    assert synthesis_payload["retrieval_plan"] == plan.model_dump()
    assert len(synthesis_payload["selected_evidence"]) == 12
    assert synthesis_payload["selected_evidence"][0]["sourceId"] == "S1"
    assert synthesis_payload["selected_evidence"][11]["sourceId"] == "S12"
    assert len(display_citations) == 6
    assert all("chunkId" not in citation for citation in display_citations)
    assert all("matchedQuery" not in citation for citation in display_citations)


def test_u_value_roof_fall_compound_question_batches_and_forces_synthesis(
    monkeypatch,
    caplog,
):
    caplog.set_level(logging.DEBUG, logger=chat.__name__)
    events = []
    generated = []
    context = {"status": "Design Needed", "priority": "Low"}
    plan = chat._RetrievalPlan(
        search_queries=["roof U-values", "roof falls"],
        corpus_wide_requested=False,
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
            if _is_retrieval_plan_config(config):
                events.append("planning")
                return FakeResponse(parsed=plan.model_dump())
            events.append("synthesis")
            return FakeResponse(
                parsed=chat._SynthesisResult(
                    answer="The roof requires the retrieved design values. [S1]",
                    cited_chunk_ids=["chunk-1"],
                )
            )

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
    assert answer == "The roof requires the retrieved design values. [S1]"
    assert citations == [{**evidence[0], "sourceId": "S1"}]
    assert events == ["context", "planning", "retrieval", "synthesis"]
    assert len(generated) == 2
    assert generated[0]["config"].temperature == 0.1
    assert generated[0]["config"].tools is None
    assert generated[1]["config"].tools is None
    assert generated[0]["config"].response_schema is None
    assert generated[1]["config"].response_schema is None
    assert generated[0]["config"].response_json_schema[
        "additionalProperties"
    ] is False
    assert generated[1]["config"].response_json_schema[
        "additionalProperties"
    ] is False

    planning_payload = json.loads(generated[0]["contents"])
    synthesis_payload = json.loads(generated[1]["contents"])
    assert planning_payload["task_context"] == context
    assert synthesis_payload["task_context"] == context
    assert synthesis_payload["retrieval_plan"] == plan.model_dump()
    assert synthesis_payload["selected_evidence"][0]["chunkId"] == "chunk-1"

    info_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == chat.__name__ and record.levelno == logging.INFO
    ]
    debug_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == chat.__name__ and record.levelno == logging.DEBUG
    ]
    for phase in ("planning", "retrieval", "synthesis", "total"):
        assert any(
            message.startswith(f"chat: {phase} duration_ms=")
            for message in info_messages
        )
    assert all("roof U-values" not in message for message in info_messages)
    assert any("roof U-values" in message for message in debug_messages)
    assert any("roof.pdf" in message for message in debug_messages)


def test_run_bounded_retrieval_skips_search_for_context_only_question(monkeypatch):
    generated_calls = []

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            generated_calls.append(config)
            if _is_retrieval_plan_config(config):
                return FakeResponse(parsed=chat._RetrievalPlan(search_queries=[]))
            return FakeResponse(
                parsed=chat._SynthesisResult(
                    answer="The task status is Design Needed."
                )
            )

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
            return FakeResponse(
                parsed=chat._SynthesisResult(
                    answer="The available roof evidence is summarized. [S1]",
                    cited_chunk_ids=["chunk-1"],
                )
            )

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
    assert answer == "The available roof evidence is summarized. [S1]"
    assert citations == [
        {"chunkId": "chunk-1", "snippet": "Roof evidence", "sourceId": "S1"}
    ]
    assert search_calls == [["Summarize the roof design"]]
    assert generation_count == 2


def test_run_bounded_retrieval_synthesizes_when_batch_retrieval_fails(monkeypatch):
    generation_count = 0

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            nonlocal generation_count
            generation_count += 1
            if _is_retrieval_plan_config(config):
                return FakeResponse(
                    parsed=chat._RetrievalPlan(search_queries=["roof details"])
                )
            assert json.loads(contents)["selected_evidence"] == []
            return FakeResponse(
                parsed=chat._SynthesisResult(
                    answer=(
                        "Document evidence was unavailable; the task context is "
                        "limited."
                    )
                )
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


def test_synthesis_api_failure_returns_grounded_fallback(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=chat.__name__)
    evidence = [
        {
            "chunkId": "chunk-1",
            "filename": "roof.pdf",
            "snippet": "The roof insulation requirement is 0.14 W/m2K.",
        }
    ]

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            if _is_retrieval_plan_config(config):
                return FakeResponse(
                    parsed={
                        "search_queries": ["roof insulation requirement"],
                        "third_search_justified": False,
                        "corpus_wide_requested": False,
                    }
                )
            raise RuntimeError("Gemini unavailable")

    class FakeClient:
        def __init__(self, *, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(chat.genai, "Client", FakeClient)
    monkeypatch.setattr(chat, "get_task_context", lambda db, key: None)
    monkeypatch.setattr(
        chat,
        "search_task_docs_batch",
        lambda *args, **kwargs: evidence,
    )

    answer, citations, ok = chat._run_bounded_retrieval(
        db=None,
        external_task_key="acct:board:item",
        prompt="What is the roof insulation requirement?",
        history=None,
    )

    assert ok is False
    assert answer.startswith("The model did not produce a final synthesis.")
    assert "0.14 W/m2K" in answer
    assert citations == evidence
    assert "synthesis failed; using grounded fallback (RuntimeError)" in caplog.text


def test_synthesis_requires_project_wide_coverage_qualification():
    generated = []

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            generated.append({"contents": contents, "config": config})
            return FakeResponse(
                parsed=chat._SynthesisResult(answer="A bounded answer.")
            )

    client = SimpleNamespace(models=FakeModels())
    plan = chat._RetrievalPlan(
        search_queries=["all roof requirements"],
        corpus_wide_requested=True,
    )

    answer, citations = chat._synthesize_answer(
        client,
        prompt="List every roof requirement",
        history=None,
        context=None,
        plan=plan,
        citations=[],
    )

    assert answer == (
        "A bounded answer.\n\n"
        "This is a partial project-wide review based on the available project "
        "evidence; other relevant records may not be represented."
    )
    assert citations == []
    assert "only when retrieval_plan.corpus_wide_requested is true" in str(
        generated[0]["config"].system_instruction
    )
    assert json.loads(generated[0]["contents"])["retrieval_plan"][
        "corpus_wide_requested"
    ] is True
    assert "partial project-wide review" in chat._fallback_answer_from_sources(
        None,
        [],
        corpus_wide_requested=True,
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
                    "sourceId": "S1",
                    "filename": "source.msg",
                    "fileId": "file-1",
                    "section": "email:body:chunk:1",
                    "snippet": "first body chunk",
                },
                {
                    "sourceId": "S3",
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
            "sourceId": "S1",
            "filename": "source.msg",
            "fileId": "file-1",
            "section": "email:body",
            "snippet": "first body chunk",
        },
        {
            "sourceId": "S3",
            "filename": "monday_columns.txt",
            "fileId": "file-2",
            "section": "monday:columns",
        },
    ]
    assert response.ok is True
    assert calls == {"access": 1, "commits": 1}


def test_cited_evidence_rejects_unknown_and_duplicate_chunk_ids():
    evidence = [
        {"chunkId": "chunk-1", "snippet": "First"},
        {"chunkId": "chunk-2", "snippet": "Second"},
    ]

    selected = chat._select_cited_evidence(
        evidence,
        ["chunk-2", "unknown", "chunk-2"],
    )

    assert selected == [
        {"chunkId": "chunk-2", "snippet": "Second", "sourceId": "S2"}
    ]


def test_email_disclaimer_is_removed_from_model_and_display_snippets():
    citation = {
        "chunkId": "chunk-1",
        "filename": "project.msg",
        "section": "email:body",
        "snippet": (
            "Site postcode: HP1 2AB.\n\nDisclaimer\n\n"
            "The information contained in this communication is confidential."
        ),
    }

    assert chat._clean_evidence_snippet(citation) == "Site postcode: HP1 2AB."
    assert chat._citations_for_display([citation])[0]["snippet"] == (
        "Site postcode: HP1 2AB."
    )
