import json
from types import SimpleNamespace

import pytest
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from backend.app.services.gemini_requests import (
    GeminiJSONRequester, GeminiRequestFailure, request_gemini, runtime_diagnostics,
)
from backend.app.services.gemini_schema import gemini_response_schema
from backend.app.services.legacy_enquiry.email_extraction import extract_text_from_email
from backend.app.services.legacy_enquiry.source_documents import IncompleteSourceExtraction
from backend.tests.pdf_helpers import pdf_bytes


class Result(BaseModel):
    value: str
    evidence: list[str] = Field(max_length=2)


class ProviderError(Exception):
    status = "INVALID_ARGUMENT"
    def __init__(self, code=400):
        self.code = code
        super().__init__("private source text https://example.invalid/?key=DO_NOT_LOG")


def config():
    return types.GenerateContentConfig(response_mime_type="application/json", response_json_schema=gemini_response_schema(Result))


def test_schema_400_has_one_json_retry_and_recovery_reuses_that_mode(caplog):
    calls = []
    def generate(model, prompt, request_config):
        calls.append((prompt, request_config))
        if request_config.response_json_schema:
            raise ProviderError()
        return SimpleNamespace(text='{"value":"original","evidence":["source"]}', parsed=None)
    requester = GeminiJSONRequester(generate, "offline", config(), Result)
    response = requester("Original facts", stage="parameter_extraction")
    assert Result.model_validate_json(response.text).value == "original"
    assert len(calls) == 2 and requester.json_mode
    assert calls[1][1].response_mime_type == "application/json"
    assert calls[1][1].response_json_schema is None and calls[1][1].response_schema is None
    assert "Original facts" in calls[1][0] and "JSON schema:" in calls[1][0]
    requester("Recovery facts", stage="parameter_recovery")
    assert len(calls) == 3 and calls[2][1].response_json_schema is None
    assert "DO_NOT_LOG" not in caplog.text and "private source text" not in caplog.text


@pytest.mark.parametrize("status,expected_calls", [(400, 2), (401, 1), (403, 1), (429, 1), (500, 1)])
def test_persistent_errors_are_bounded_and_keep_only_safe_diagnostics(status, expected_calls):
    calls = []
    def fail(*args):
        calls.append(args)
        raise ProviderError(status)
    requester = GeminiJSONRequester(fail, "offline", config(), Result)
    with pytest.raises(GeminiRequestFailure) as captured:
        requester("PRIVATE ENQUIRY", stage="cad_translation")
    assert len(calls) == expected_calls
    error = captured.value
    assert error.details["stage"] == "cad_translation" and error.http_status == status
    assert error.details["mode"] == ("json" if status == 400 else "structured")
    assert "PRIVATE ENQUIRY" not in str(error) and "DO_NOT_LOG" not in str(error)
    assert error.__suppress_context__ is True


def test_fallback_output_still_requires_the_original_model_validation():
    def generate(model, prompt, request_config):
        if request_config.response_json_schema:
            raise ProviderError()
        return SimpleNamespace(text='{"value":"original","evidence":["a","b","c"]}', parsed=None)
    response = GeminiJSONRequester(generate, "offline", config(), Result)("Facts", stage="parameter_extraction")
    with pytest.raises(ValidationError):
        Result.model_validate_json(response.text)


def test_pdf_error_has_no_json_retry_and_manifest_retains_stage_and_status():
    pdf = pdf_bytes()
    calls = []
    def fail(*args):
        calls.append(args)
        raise ProviderError()
    def process_pdf(content, filename):
        return request_gemini(fail, "offline", [], types.GenerateContentConfig(), stage="pdf_text", input_bytes=len(content))
    manifest = []
    with pytest.raises(IncompleteSourceExtraction) as captured:
        extract_text_from_email("Request", [{"filename": "drawing.pdf", "content": pdf}],
                                extractor=SimpleNamespace(max_attachment_workers=1, process_pdf=process_pdf), manifest=manifest)
    assert len(calls) == 1
    details = manifest[1]["error"]
    assert details["stage"] == "pdf_text" and details["http_status"] == 400 and details["input_bytes"] == len(pdf)
    assert "stage=pdf_text" in str(captured.value) and "http_status=400" in str(captured.value)
    assert "DO_NOT_LOG" not in json.dumps(manifest) and "private source text" not in str(captured.value)


def test_runtime_metadata_identifies_deployment_without_exposing_environment(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "a" * 40)
    monkeypatch.setenv("GEMINI_API_KEY", "DO_NOT_LOG")
    result = runtime_diagnostics("offline", "medium", config().response_json_schema)
    assert result["commit"] == "a" * 12 and result["array_bounds"] == 0
    assert result["python"] and result["google_genai"] and result["schema_sha256"]
    assert "DO_NOT_LOG" not in json.dumps(result)


@pytest.mark.parametrize("reason,code,text", [
    ("RECITATION", "blocked_response", None),
    ("SAFETY", "blocked_response", "Private partial source"),
    ("MAX_TOKENS", "incomplete_response", "Private partial source"),
    ("STOP", "empty_response", None),
])
def test_http_success_with_blocked_partial_or_empty_output_is_not_a_read_source(reason, code, text):
    response = SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=reason,
        finish_message="Private provider explanation")])
    calls = []
    def generate(*args):
        calls.append(args)
        return response
    with pytest.raises(GeminiRequestFailure) as error:
        GeminiJSONRequester(generate, "offline", config(), Result)("Private source", stage="parameter_extraction")
    assert len(calls) == 1  # No schema fallback or repeated blocked requests.
    assert error.value.details["code"] == code
    assert error.value.details["finish_reason"] == (reason if reason != "STOP" else None)
    assert "Private" not in str(error.value)


def test_prompt_block_is_reported_even_without_candidates():
    response = SimpleNamespace(prompt_feedback=SimpleNamespace(block_reason="OTHER"), candidates=[])
    with pytest.raises(GeminiRequestFailure) as error:
        request_gemini(lambda *_: response, "offline", [], types.GenerateContentConfig(), stage="image_text")
    assert error.value.details["block_reason"] == "OTHER"


def test_complete_text_and_parsed_only_test_responses_remain_usable():
    for response in (SimpleNamespace(text="Complete", candidates=[SimpleNamespace(finish_reason=types.FinishReason.STOP)]),
                     SimpleNamespace(parsed={"value": "Original"})):
        assert request_gemini(lambda *_: response, "offline", [], types.GenerateContentConfig(), stage="image_text") is response
