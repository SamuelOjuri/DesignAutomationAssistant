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