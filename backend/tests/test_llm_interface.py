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
        if self.calls == 1:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return {"ok": True, "kwargs": kwargs}


class FakeEmbeddingClient:
    def __init__(self):
        self.models = FakeEmbeddingModels()


def test_gemini_embed_content_retries_rate_limit(monkeypatch):
    limiter = FakeRateLimiter()
    client = FakeEmbeddingClient()
    monkeypatch.setattr(llm_interface, "get_rate_limiter", lambda: limiter)
    monkeypatch.setattr(llm_interface.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm_interface.random, "uniform", lambda start, end: 0)

    response = llm_interface.gemini_embed_content_with_retry(
        client,
        model="gemini-embedding-001",
        contents=["chunk"],
        config={"task_type": "RETRIEVAL_DOCUMENT"},
        max_retries=1,
        initial_backoff=1,
    )

    assert response["ok"] is True
    assert client.models.calls == 2
    assert limiter.releases == 2


def test_gemini_embed_content_raises_after_retry_exhaustion(monkeypatch):
    class AlwaysRateLimitedModels:
        def __init__(self):
            self.calls = 0

        def embed_content(self, **kwargs):
            self.calls += 1
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    class AlwaysRateLimitedClient:
        def __init__(self):
            self.models = AlwaysRateLimitedModels()

    limiter = FakeRateLimiter()
    client = AlwaysRateLimitedClient()
    monkeypatch.setattr(llm_interface, "get_rate_limiter", lambda: limiter)
    monkeypatch.setattr(llm_interface.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm_interface.random, "uniform", lambda start, end: 0)

    with pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED"):
        llm_interface.gemini_embed_content_with_retry(
            client,
            model="gemini-embedding-001",
            contents=["chunk"],
            config={"task_type": "RETRIEVAL_DOCUMENT"},
            max_retries=1,
            initial_backoff=1,
        )

    assert client.models.calls == 2
    assert limiter.releases == 2