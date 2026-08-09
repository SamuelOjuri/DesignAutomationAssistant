from __future__ import annotations

import pytest

from backend.app.services import llm_interface


class FakeRateLimiter:
    def __init__(self):
        self.releases = 0

    def wait_for_availability(self):
        return True

    def release(self):
        self.releases += 1


class FakeEmbeddingModels:
    def __init__(self):
        self.calls = 0

    def embed_content(self, **kwargs):
        self.calls += 1
        return {"ok": True, "kwargs": kwargs}


class FakeEmbeddingClient:
    def __init__(self):
        self.models = FakeEmbeddingModels()


class FakeGenerationModels:
    def __init__(self):
        self.kwargs = None

    def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return {"ok": True}


class FakeGenerationClient:
    def __init__(self):
        self.models = FakeGenerationModels()


def test_create_gemini_client_configures_transient_retries(monkeypatch):
    captured = {}

    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(llm_interface.genai, "Client", fake_client)

    client = llm_interface.create_gemini_client(max_retries=5, initial_backoff=2)

    assert client is not None
    retry_options = captured["http_options"].retry_options
    assert retry_options.attempts == 6
    assert retry_options.initial_delay == 2
    assert 429 in retry_options.http_status_codes
    assert 503 in retry_options.http_status_codes


def test_gemini_generate_content_forwards_config_and_releases_slot(monkeypatch):
    limiter = FakeRateLimiter()
    client = FakeGenerationClient()
    config = {"thinking_config": {"thinking_level": "medium"}}
    monkeypatch.setattr(llm_interface, "create_gemini_client", lambda **kwargs: client)
    monkeypatch.setattr(llm_interface, "get_rate_limiter", lambda: limiter)

    response = llm_interface.gemini_api_with_retry(
        model="gemini-3.5-flash",
        contents="Extract parameters",
        config=config,
    )

    assert response == {"ok": True}
    assert client.models.kwargs == {
        "model": "gemini-3.5-flash",
        "contents": "Extract parameters",
        "config": config,
    }
    assert limiter.releases == 1


def test_gemini_embed_content_uses_configured_client_once(monkeypatch):
    limiter = FakeRateLimiter()
    client = FakeEmbeddingClient()
    monkeypatch.setattr(llm_interface, "get_rate_limiter", lambda: limiter)

    response = llm_interface.gemini_embed_content_with_retry(
        client,
        model="gemini-embedding-001",
        contents=["chunk"],
        config={"task_type": "RETRIEVAL_DOCUMENT"},
    )

    assert response["ok"] is True
    assert client.models.calls == 1
    assert limiter.releases == 1


def test_gemini_embed_content_releases_slot_after_sdk_failure(monkeypatch):
    class FailingModels:
        def __init__(self):
            self.calls = 0

        def embed_content(self, **kwargs):
            self.calls += 1
            raise RuntimeError("503 UNAVAILABLE")

    class FailingClient:
        def __init__(self):
            self.models = FailingModels()

    limiter = FakeRateLimiter()
    client = FailingClient()
    monkeypatch.setattr(llm_interface, "get_rate_limiter", lambda: limiter)

    with pytest.raises(RuntimeError, match="UNAVAILABLE"):
        llm_interface.gemini_embed_content_with_retry(
            client,
            model="gemini-embedding-001",
            contents=["chunk"],
            config={"task_type": "RETRIEVAL_DOCUMENT"},
        )

    assert client.models.calls == 1
    assert limiter.releases == 1