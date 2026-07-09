from types import SimpleNamespace

from google.genai import types

from backend.app.routes import chat


class FakeResponse:
    def __init__(self, *, function_calls=None, candidates=None, text=""):
        self.function_calls = function_calls or []
        self.candidates = candidates or []
        self.text = text


def test_run_with_tools_returns_function_response_as_user_content(monkeypatch):
    generated_contents = []
    search_calls = []

    model_tool_content = types.Content(
        role="model",
        parts=[types.Part.from_function_call(name="search_task_docs", args={"query": "summary", "k": 1})],
    )

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            generated_contents.append(contents)
            if len(generated_contents) == 1:
                return FakeResponse(
                    function_calls=[SimpleNamespace(id="call-1", name="search_task_docs", args={"query": "summary", "k": 1})],
                    candidates=[SimpleNamespace(content=model_tool_content)],
                )
            return FakeResponse(text="The project needs a concise summary.")

    class FakeClient:
        def __init__(self, *, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(chat.genai, "Client", FakeClient)

    def fake_search_task_docs(db, external_task_key, query, k=8):
        search_calls.append({"query": query, "k": k})
        return [{"snippet": "Relevant project details."}]

    monkeypatch.setattr(chat, "search_task_docs", fake_search_task_docs)

    answer, citations, ok = chat._run_with_tools(
        db=None,
        external_task_key="acct:board:item",
        prompt="Provide a concise summary",
        history=None,
        max_turns=2,
    )

    assert ok is True
    assert answer == "The project needs a concise summary."
    assert citations == [{"snippet": "Relevant project details."}]
    assert search_calls == [{"query": "summary", "k": 1}]

    function_response_content = generated_contents[1][-1]
    assert function_response_content.role == "user"
    function_response = function_response_content.parts[0].function_response
    assert function_response is not None
    assert function_response.id == "call-1"
    assert "result" in function_response.response


def test_run_with_tools_synthesizes_answer_after_repeated_tool_calls(monkeypatch):
    generated_contents = []

    model_tool_content = types.Content(
        role="model",
        parts=[types.Part.from_function_call(name="search_task_docs", args={"query": "summary", "k": 1})],
    )

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            generated_contents.append(contents)
            if len(generated_contents) <= 2:
                return FakeResponse(
                    function_calls=[SimpleNamespace(id=f"call-{len(generated_contents)}", name="search_task_docs", args={"query": "summary", "k": 1})],
                    candidates=[SimpleNamespace(content=model_tool_content)],
                )
            return FakeResponse(text="This enquiry is low priority and currently needs design work.")

    class FakeClient:
        def __init__(self, *, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(chat.genai, "Client", FakeClient)
    monkeypatch.setattr(
        chat,
        "search_task_docs",
        lambda db, external_task_key, query, k=8: [
            {
                "filename": "monday_columns.txt",
                "snippet": "Priority: Low Priority. Status: Design Needed. New Enquiry.",
            }
        ],
    )

    answer, citations, ok = chat._run_with_tools(
        db=None,
        external_task_key="acct:board:item",
        prompt="Provide a concise summary",
        history=None,
        max_turns=2,
    )

    assert ok is True
    assert answer == "This enquiry is low priority and currently needs design work."
    assert citations[0]["filename"] == "monday_columns.txt"
    assert len(generated_contents) == 3


def test_run_with_tools_synthesizes_when_final_tool_response_has_no_text(monkeypatch):
    generated_contents = []

    model_tool_content = types.Content(
        role="model",
        parts=[types.Part.from_function_call(name="search_task_docs", args={"query": "summary", "k": 1})],
    )

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            generated_contents.append(contents)
            if len(generated_contents) == 1:
                return FakeResponse(
                    function_calls=[SimpleNamespace(id="call-1", name="search_task_docs", args={"query": "summary", "k": 1})],
                    candidates=[SimpleNamespace(content=model_tool_content)],
                )
            if len(generated_contents) == 2:
                return FakeResponse(text="")
            return FakeResponse(text="This enquiry is low priority and needs design work.")

    class FakeClient:
        def __init__(self, *, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(chat.genai, "Client", FakeClient)
    monkeypatch.setattr(
        chat,
        "search_task_docs",
        lambda db, external_task_key, query, k=8: [
            {
                "filename": "monday_columns.txt",
                "snippet": "Priority: Low Priority. Status: Design Needed.",
            }
        ],
    )

    answer, citations, ok = chat._run_with_tools(
        db=None,
        external_task_key="acct:board:item",
        prompt="Provide a concise summary",
        history=None,
        max_turns=2,
    )

    assert ok is True
    assert answer == "This enquiry is low priority and needs design work."
    assert citations[0]["filename"] == "monday_columns.txt"
    assert len(generated_contents) == 3


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
        "_run_with_tools",
        lambda **kwargs: (
            "This is the final project summary.",
            [{"filename": "monday_columns.txt", "section": "monday:columns"}],
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
        {"filename": "monday_columns.txt", "section": "monday:columns"}
    ]
    assert response.ok is True
    assert calls == {"access": 1, "commits": 1}