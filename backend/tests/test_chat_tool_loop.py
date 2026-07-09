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

    model_tool_content = types.Content(
        role="model",
        parts=[types.Part.from_function_call(name="search_task_docs", args={"query": "summary", "k": 1})],
    )

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            generated_contents.append(contents)
            if len(generated_contents) == 1:
                return FakeResponse(
                    function_calls=[SimpleNamespace(name="search_task_docs", args={"query": "summary", "k": 1})],
                    candidates=[SimpleNamespace(content=model_tool_content)],
                )
            return FakeResponse(text="The project needs a concise summary.")

    class FakeClient:
        def __init__(self, *, api_key):
            self.models = FakeModels()

    monkeypatch.setattr(chat.genai, "Client", FakeClient)
    monkeypatch.setattr(
        chat,
        "search_task_docs",
        lambda db, external_task_key, query, k=8: [{"snippet": "Relevant project details."}],
    )

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

    function_response_content = generated_contents[1][-1]
    assert function_response_content.role == "user"
    assert function_response_content.parts[0].function_response is not None


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
                    function_calls=[SimpleNamespace(name="search_task_docs", args={"query": "summary", "k": 1})],
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