"""Local durability/admission checks; PostgreSQL races use disposable schemas only."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import os
from threading import Barrier
from types import SimpleNamespace
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from backend.app.config import settings
from backend.app.models import GeminiRequestLease, SourceTranscriptionCache
from backend.app.services import llm_interface
from backend.app.services import shared_gemini_budget as budget
from backend.app.services import source_transcription_cache as cache
from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient


NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
SCOPE = hashlib.sha256(b"offline-provider-project").hexdigest()
OTHER_SCOPE = hashlib.sha256(b"other-provider-project").hexdigest()
DIGEST = hashlib.sha256(b"original document").hexdigest()
MIGRATION = importlib.import_module("backend.migrations.versions.0021_processing_acceleration")


@pytest.fixture
def sqlite_store(tmp_path):
    engine = create_engine("sqlite+pysqlite:///" + str(tmp_path / "acceleration.sqlite"))
    for model in (SourceTranscriptionCache, GeminiRequestLease):
        model.__table__.create(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def cached_store(sqlite_store, monkeypatch):
    read, save = cache.read_text, cache.save_text
    monkeypatch.setattr(settings, "design_processing_transcription_cache_enabled", True)
    monkeypatch.setattr(cache, "read_text", lambda scope, key: read(
        scope, key, session_factory=sqlite_store, now=NOW))
    monkeypatch.setattr(cache, "save_text", lambda scope, key, digest, value: save(
        scope, key, digest, value, session_factory=sqlite_store, now=NOW))
    return sqlite_store


def test_cache_upsert_integrity_expiry_and_scope_cleanup(sqlite_store, monkeypatch):
    monkeypatch.setattr(settings, "design_processing_transcription_cache_ttl_days", 1)
    save = lambda scope, key, value, now=NOW: cache.save_text(
        scope, key, DIGEST, value, session_factory=sqlite_store, now=now)
    read = lambda scope, key, now=NOW: cache.read_text(
        scope, key, session_factory=sqlite_store, now=now)
    save(SCOPE, "same-key", "First text")
    save(SCOPE, "same-key", "Replacement text")
    save(OTHER_SCOPE, "same-key", "Other account text")
    assert read(SCOPE, "same-key") == "Replacement text"
    assert read(OTHER_SCOPE, "same-key") == "Other account text"
    with sqlite_store() as db:
        assert db.query(SourceTranscriptionCache).count() == 2
        row = db.get(SourceTranscriptionCache, (SCOPE, "same-key"))
        assert row.content_sha256 == DIGEST
        row.text = "Corrupted text with original checksum"
        db.commit()
    assert read(SCOPE, "same-key") is None
    save(SCOPE, "same-key", "Repaired text")
    expired_at = NOW + timedelta(days=1)
    assert read(SCOPE, "same-key", expired_at) is None
    save(SCOPE, "new-key", "Current text", expired_at)
    with sqlite_store() as db:
        assert db.get(SourceTranscriptionCache, (SCOPE, "same-key")) is None
        assert db.get(SourceTranscriptionCache, (OTHER_SCOPE, "same-key")) is not None
        assert db.get(SourceTranscriptionCache, (SCOPE, "new-key")) is not None


def test_cache_identity_tracks_contract_but_not_source_filename_or_raw_scope(monkeypatch):
    client = SimpleNamespace(model="offline-model", thinking_level="medium",
                             pdf_native_text_enabled=True, transcription_cache_scope="account-private")
    original = cache.cache_identity(client, b"source", "logo.png", "image")
    assert original == cache.cache_identity(client, b"source", "forwarded-logo.PNG", "image")
    assert all(len(part) == 64 for part in original)
    assert "account-private" not in original
    for key, value in (("model", "other-model"), ("thinking_level", "low"), ("pdf_native_text_enabled", False)):
        changed = SimpleNamespace(**{**vars(client), key: value})
        assert cache.cache_identity(changed, b"source", "logo.png", "image")[1] != original[1]
    different_scope = SimpleNamespace(**{**vars(client), "transcription_cache_scope": "other-account"})
    assert cache.cache_identity(different_scope, b"source", "logo.png", "image")[0] != original[0]
    assert cache.cache_identity(client, b"different", "logo.png", "image")[1] != original[1]
    assert cache.cache_identity(client, b"source", "logo.jpg", "image")[1] != original[1]
    assert cache.cache_identity(client, b"source", "logo.png", "pdf")[1] != original[1]
    monkeypatch.setattr(cache, "TRANSCRIPTION_CACHE_VERSION", "next-prompt-version")
    assert cache.cache_identity(client, b"source", "logo.png", "image")[1] != original[1]


def test_real_client_decorator_reuses_text_across_client_instances_in_same_scope(cached_store):
    calls = []

    def generate(*args):
        calls.append(args)
        return SimpleNamespace(text="[TEXT REGION 1]\nLiteral logo\n[/TEXT REGION 1]")

    def client(scope):
        return LegacyGeminiClient("offline", generate_content=generate, transcription_cache_scope=scope)

    first = client("account-A").process_image(b"same-image", "logo.png")
    assert client("account-A").process_image(b"same-image", "forwarded-logo.png") == first
    assert len(calls) == 1
    client("account-B").process_image(b"same-image", "logo.png")
    assert len(calls) == 2
    with cached_store() as db:
        assert db.query(SourceTranscriptionCache).count() == 2


@pytest.mark.parametrize("bad_result", [None, "", " \n", "Error processing PDF", " Unable to process source", "Unsupported image format: bad", "x" * 500_001],
                         ids=["null", "empty", "whitespace", "error-prefix", "unable-prefix", "unsupported-prefix", "text-limit"])
def test_invalid_or_unsuccessful_transcriptions_are_not_cached(cached_store, bad_result):
    replies = iter([bad_result, "Valid transcription"])
    calls = []

    @cache.cached_transcription("image")
    def transcribe(client, content, filename):
        calls.append(filename)
        return next(replies)

    client = SimpleNamespace(model="offline", thinking_level="medium", transcription_cache_scope="account-A")
    assert transcribe(client, b"bytes", "logo.png") == bad_result
    with cached_store() as db:
        assert db.query(SourceTranscriptionCache).count() == 0
    assert transcribe(client, b"bytes", "logo.png") == "Valid transcription"
    assert len(calls) == 2


def test_loader_failure_is_not_cached_and_later_success_is_reused(cached_store):
    calls = []

    @cache.cached_transcription("pdf")
    def transcribe(client, content, filename):
        calls.append(filename)
        if len(calls) == 1:
            raise TimeoutError("temporary transcription failure")
        return "Scale: 1:100"

    client = SimpleNamespace(model="offline", thinking_level="medium", transcription_cache_scope="account-A")
    with pytest.raises(TimeoutError):
        transcribe(client, b"document", "plan.pdf")
    with cached_store() as db:
        assert db.query(SourceTranscriptionCache).count() == 0
    assert transcribe(client, b"document", "plan.pdf") == "Scale: 1:100"
    assert transcribe(client, b"document", "plan.pdf") == "Scale: 1:100"
    assert len(calls) == 2


def test_cache_unavailable_does_not_turn_valid_transcription_into_job_failure(monkeypatch):
    monkeypatch.setattr(settings, "design_processing_transcription_cache_enabled", True)
    def unavailable(*args):
        raise RuntimeError("cache unavailable")
    monkeypatch.setattr(cache, "read_text", unavailable)
    monkeypatch.setattr(cache, "save_text", unavailable)

    @cache.cached_transcription("image")
    def transcribe(client, content, filename):
        return "Valid transcription"

    client = SimpleNamespace(model="offline", thinking_level="medium", transcription_cache_scope="account-A")
    assert transcribe(client, b"image", "logo.png") == "Valid transcription"


def test_budget_limits_concurrent_requests_but_released_requests_still_count_for_rpm(sqlite_store):
    acquire = lambda scope=SCOPE, now=NOW: budget.try_acquire(
        scope, session_factory=sqlite_store, now=now, concurrent=1, rpm=2, lease_seconds=120)
    first = acquire()
    assert first and acquire() is None
    assert acquire(OTHER_SCOPE)
    budget.release(first, session_factory=sqlite_store, now=NOW)
    second = acquire()
    assert second
    budget.release(second, session_factory=sqlite_store, now=NOW)
    assert acquire() is None
    assert acquire(now=NOW + timedelta(seconds=60))


def test_expired_crashed_lease_is_reclaimed_without_overlooking_still_active_old_request(sqlite_store):
    first = budget.try_acquire(SCOPE, session_factory=sqlite_store, now=NOW, concurrent=1, rpm=10, lease_seconds=120)
    assert first
    assert budget.try_acquire(SCOPE, session_factory=sqlite_store, now=NOW + timedelta(seconds=61), concurrent=1, rpm=10) is None
    later = budget.try_acquire(SCOPE, session_factory=sqlite_store, now=NOW + timedelta(seconds=120), concurrent=1, rpm=10)
    assert later
    with sqlite_store() as db:
        assert db.get(GeminiRequestLease, first) is None
        assert db.get(GeminiRequestLease, later).released_at is None


def test_shared_budget_scope_survives_api_key_rotation_but_separates_provider_projects(monkeypatch):
    monkeypatch.setattr(settings, "gemini_shared_budget_scope", "offline-project-A")
    original = budget.budget_scope()
    monkeypatch.setattr(settings, "gemini_api_key", "offline-rotated-key")
    assert budget.budget_scope() == original
    monkeypatch.setattr(settings, "gemini_shared_budget_scope", "offline-project-B")
    assert budget.budget_scope() != original


@pytest.fixture
def local_shared_budget(sqlite_store, monkeypatch):
    acquire, release = budget.try_acquire, budget.release
    monkeypatch.setattr(settings, "gemini_shared_rate_limit_enabled", True)
    monkeypatch.setattr(settings, "gemini_shared_max_concurrent", 1)
    monkeypatch.setattr(settings, "gemini_shared_requests_per_minute", 20)
    monkeypatch.setattr(settings, "gemini_shared_lease_seconds", 900)
    monkeypatch.setattr(settings, "gemini_request_timeout_seconds", 240)
    monkeypatch.setattr(budget, "try_acquire", lambda scope: acquire(scope, session_factory=sqlite_store, now=NOW))
    monkeypatch.setattr(budget, "release", lambda token: release(token, session_factory=sqlite_store, now=NOW))
    return sqlite_store


def test_shared_slot_releases_after_provider_failure(local_shared_budget):
    with pytest.raises(RuntimeError, match="provider failed"):
        with budget.shared_generation_slot():
            raise RuntimeError("provider failed")
    with budget.shared_generation_slot():
        pass
    with local_shared_budget() as db:
        rows = db.query(GeminiRequestLease).all()
        assert len(rows) == 2 and all(row.released_at is not None for row in rows)


@pytest.mark.parametrize("provider_fails", [False, True], ids=["successful-response", "original-provider-error"])
def test_release_database_failure_preserves_response_or_original_provider_error(local_shared_budget, monkeypatch, caplog, provider_fails):
    original_error = ValueError("original provider error")
    releases, provider_calls = [], []

    def generate(**kwargs):
        provider_calls.append(kwargs)
        if provider_fails:
            raise original_error
        return "successful provider response"

    def failed_release(token):
        raise RuntimeError("secret-db-url private-lease-token")

    monkeypatch.setattr(budget, "release", failed_release)
    monkeypatch.setattr(llm_interface, "create_gemini_client", lambda **kwargs: SimpleNamespace(
        models=SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr(llm_interface, "get_rate_limiter", lambda: SimpleNamespace(
        wait_for_availability=lambda: True, release=lambda: releases.append(True)))
    if provider_fails:
        with pytest.raises(ValueError) as caught:
            llm_interface.gemini_api_with_retry("offline", "context", max_retries=0)
        assert caught.value is original_error
    else:
        assert llm_interface.gemini_api_with_retry("offline", "context", max_retries=0) == "successful provider response"
    assert len(provider_calls) == 1 and releases == [True]
    assert "lease release unavailable error_type=RuntimeError" in caplog.text
    assert "secret-db-url" not in caplog.text and "private-lease-token" not in caplog.text
    with local_shared_budget() as db:
        row = db.query(GeminiRequestLease).one()
        assert row.released_at is None


def test_each_http_retry_gets_its_own_shared_admission_and_releases_it(local_shared_budget, monkeypatch):
    class TransientError(RuntimeError):
        code = 503
    provider_calls, client_options, backoffs, local_releases = [], [], [], []

    def generate(**kwargs):
        with local_shared_budget() as db:
            assert db.query(GeminiRequestLease).filter_by(released_at=None).count() == 1
            assert db.query(GeminiRequestLease).count() == len(provider_calls) + 1
        provider_calls.append(kwargs)
        if len(provider_calls) < 3:
            raise TransientError("try again")
        return "success"

    def create_client(**kwargs):
        client_options.append(kwargs)
        return SimpleNamespace(models=SimpleNamespace(generate_content=generate))

    monkeypatch.setattr(llm_interface, "create_gemini_client", create_client)
    monkeypatch.setattr(llm_interface, "get_rate_limiter", lambda: SimpleNamespace(
        wait_for_availability=lambda: True, release=lambda: local_releases.append(True)))
    monkeypatch.setattr(llm_interface.time, "sleep", backoffs.append)
    assert llm_interface.gemini_api_with_retry("offline", "context", max_retries=2, initial_backoff=1) == "success"
    assert client_options == [{"max_retries": 0, "initial_backoff": 1}]
    assert backoffs == [1, 2] and local_releases == [True]
    with local_shared_budget() as db:
        rows = db.query(GeminiRequestLease).all()
        assert len(rows) == 3 and all(row.released_at is not None for row in rows)


def test_budget_database_failure_never_calls_provider(monkeypatch):
    monkeypatch.setattr(settings, "gemini_shared_rate_limit_enabled", True)
    monkeypatch.setattr(settings, "gemini_shared_lease_seconds", 900)
    monkeypatch.setattr(settings, "gemini_request_timeout_seconds", 240)
    def unavailable(scope):
        raise RuntimeError("budget database unavailable")
    monkeypatch.setattr(budget, "try_acquire", unavailable)
    with pytest.raises(RuntimeError, match="budget database unavailable"):
        with budget.shared_generation_slot():
            pytest.fail("A failed admission must not reach a provider request")


def test_acceleration_migration_round_trip_matches_model_tables(tmp_path):
    engine = create_engine("sqlite+pysqlite:///" + str(tmp_path / "migration.sqlite"))
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION.upgrade()
            inspector = inspect(connection)
            assert set(inspector.get_table_names()) == {"source_transcription_cache", "gemini_request_leases"}
            for model in (SourceTranscriptionCache, GeminiRequestLease):
                assert set(model.__table__.columns.keys()) == {column["name"] for column in inspector.get_columns(model.__tablename__)}
            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION.downgrade()
            assert inspect(connection).get_table_names() == []
    finally:
        engine.dispose()


@pytest.fixture
def postgres_store():
    # This is deliberately never inferred from settings.database_url.
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for isolated PostgreSQL admission races")
    schema = "processing_accel_" + uuid.uuid4().hex
    admin, engine = create_engine(url), create_engine(url, pool_pre_ping=True)

    @event.listens_for(engine, "checkout")
    def isolate(connection, _record, _proxy):
        prior = connection.autocommit
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}", public')
                cursor.execute("SET statement_timeout TO 10000")
                cursor.execute("SET lock_timeout TO 5000")
        finally:
            connection.autocommit = prior

    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT current_schema()")) == schema
            with Operations.context(MigrationContext.configure(connection)):
                MIGRATION.upgrade()
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def test_postgres_simultaneous_workers_respect_shared_limit_and_cache_upsert(postgres_store):
    with postgres_store() as db:
        assert db.execute(text("SELECT relrowsecurity FROM pg_class WHERE oid='source_transcription_cache'::regclass")).scalar() is True
        assert db.execute(text("SELECT relrowsecurity FROM pg_class WHERE oid='gemini_request_leases'::regclass")).scalar() is True
    start = Barrier(4)

    def acquire(_):
        start.wait(timeout=5)
        return budget.try_acquire(SCOPE, session_factory=postgres_store, now=NOW, concurrent=2, rpm=10)

    with ThreadPoolExecutor(max_workers=4) as pool:
        tokens = list(pool.map(acquire, range(4)))
    assert len([token for token in tokens if token]) == 2
    for token in tokens:
        if token:
            budget.release(token, session_factory=postgres_store, now=NOW)

    cache_start = Barrier(4)

    def save(index):
        cache_start.wait(timeout=5)
        cache.save_text(SCOPE, DIGEST, DIGEST, f"Transcription {index}", session_factory=postgres_store, now=NOW)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(save, range(4)))
    with postgres_store() as db:
        assert db.query(SourceTranscriptionCache).count() == 1
    assert cache.read_text(SCOPE, DIGEST, session_factory=postgres_store, now=NOW).startswith("Transcription ")
