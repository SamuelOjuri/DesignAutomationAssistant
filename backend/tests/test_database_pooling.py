"""Connection budgets apply per process and survive imported session factories."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from typing import Annotated

from pydantic import TypeAdapter, ValidationError
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import QueuePool, StaticPool

from backend.app import db
from backend.app.config import Settings, settings
from backend.app.services import design_processing_execution as execution
from backend.app.services import shared_gemini_budget, source_transcription_cache


def test_postgres_pools_have_bounded_parent_and_extraction_defaults(monkeypatch):
    for name, value in (("database_pool_size", 2), ("database_max_overflow", 0),
                        ("database_extraction_pool_size", 1), ("database_extraction_max_overflow", 0)):
        monkeypatch.setattr(settings, name, value)
    application = db._engine_options("postgresql://example.invalid/app")
    child = db._engine_options("postgresql://example.invalid/app", extraction=True)
    assert (application["pool_size"], application["max_overflow"]) == (2, 0)
    assert (child["pool_size"], child["max_overflow"]) == (1, 0)
    assert application["connect_args"]["keepalives"] == 1
    assert child["connect_args"]["connect_timeout"] == settings.database_connect_timeout_seconds
    assert db._engine_options("sqlite://") == {"pool_pre_ping": True}


@pytest.mark.parametrize("field,value", [
    ("database_pool_size", 0), ("database_pool_size", 51), ("database_max_overflow", -1),
    ("database_extraction_pool_size", 0), ("database_extraction_max_overflow", -1),
    ("database_pool_timeout_seconds", 0), ("database_pool_recycle_seconds", -1),
    ("database_connect_timeout_seconds", 0),
])
def test_pool_settings_reject_unbounded_or_nonpositive_budgets(field, value):
    definition = Settings.model_fields[field]
    adapter = TypeAdapter(Annotated[definition.annotation, *definition.metadata])
    with pytest.raises(ValidationError):
        adapter.validate_python(value)


@pytest.mark.parametrize("extraction,expected", [(False, 2), (True, 1)])
def test_real_queue_pool_never_exceeds_configured_parallel_connection_limit(tmp_path, monkeypatch, extraction, expected):
    monkeypatch.setattr(settings, "database_pool_size", 2)
    monkeypatch.setattr(settings, "database_max_overflow", 0)
    monkeypatch.setattr(settings, "database_extraction_pool_size", 1)
    monkeypatch.setattr(settings, "database_extraction_max_overflow", 0)
    options = db._engine_options("postgresql://example.invalid/app", extraction=extraction)
    options.pop("connect_args")
    # Exercise a real QueuePool without requiring external database access.
    engine = create_engine("sqlite:///" + str(tmp_path / "pool.sqlite"), poolclass=QueuePool,
                           connect_args={"check_same_thread": False}, **options)
    counters, guard = {"active": 0, "maximum": 0, "created": 0}, Lock()
    start, release, occupied = Barrier(5), Event(), Event()

    @event.listens_for(engine, "connect")
    def connected(*args):
        with guard:
            counters["created"] += 1

    @event.listens_for(engine, "checkout")
    def checked_out(*args):
        with guard:
            counters["active"] += 1
            counters["maximum"] = max(counters["maximum"], counters["active"])
            if counters["active"] == expected:
                occupied.set()

    @event.listens_for(engine, "checkin")
    def checked_in(*args):
        with guard:
            counters["active"] -= 1

    def query():
        start.wait(timeout=5)
        with engine.connect() as connection:
            assert release.wait(timeout=5)
            return connection.scalar(text("SELECT 1"))

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(query) for _ in range(4)]
            try:
                start.wait(timeout=5)
                assert occupied.wait(timeout=5)
                with guard:
                    assert counters["active"] == expected
            finally:
                release.set()
            assert [future.result(timeout=5) for future in futures] == [1, 1, 1, 1]
        assert counters == {"active": 0, "maximum": expected, "created": expected}
    finally:
        engine.dispose()


def test_only_the_actual_extraction_child_rebinds_the_same_captured_factory(tmp_path, monkeypatch, capsys):
    original_factory, original_bind = db.SessionLocal, db.SessionLocal.kw["bind"]
    child_engine = create_engine("sqlite:///" + str(tmp_path / "child.sqlite"), poolclass=QueuePool,
                                 pool_size=1, max_overflow=0)
    disposed, creations = [], []
    previous_engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"),
                                      dispose=lambda: disposed.append(True))
    monkeypatch.setattr(db, "engine", previous_engine)
    monkeypatch.setattr(db, "_pool_role", "application")
    monkeypatch.setattr(db, "_active_engine_options", {"pool_size": 2, "max_overflow": 0})
    monkeypatch.setattr(db.multiprocessing, "current_process", lambda: SimpleNamespace(name="design-extraction"))

    def create_child(url, **options):
        creations.append(options)
        return child_engine

    monkeypatch.setattr(db, "create_engine", create_child)
    try:
        assert db.configure_extraction_database() is True
        assert db.SessionLocal is original_factory
        assert db.SessionLocal.kw["bind"] is child_engine
        for function in (source_transcription_cache.read_text, source_transcription_cache.save_text,
                         shared_gemini_budget.try_acquire, shared_gemini_budget.release):
            assert function.__kwdefaults__["session_factory"] is original_factory
        with source_transcription_cache.read_text.__kwdefaults__["session_factory"]() as session:
            assert session.scalar(text("SELECT 1")) == 1
        assert disposed == [True] and len(creations) == 1
        assert (creations[0]["pool_size"], creations[0]["max_overflow"]) == (1, 0)
        payload = json.loads(capsys.readouterr().out)
        assert payload["event"] == "database_pool_runtime"
        assert payload["database_pool_role"] == "extraction"
        assert payload["database_pool_max_connections"] == 1
        assert not any(value in str(payload) for value in ("postgresql://", "password", "user@"))
        assert db.configure_extraction_database() is False
        assert len(creations) == 1
    finally:
        original_factory.configure(bind=original_bind)
        child_engine.dispose()


def test_direct_child_helper_calls_do_not_rebind_the_parent_engine(monkeypatch):
    original_factory, original_engine, original_bind = db.SessionLocal, db.engine, db.SessionLocal.kw["bind"]
    monkeypatch.setattr(db.multiprocessing, "current_process", lambda: SimpleNamespace(name="MainProcess"))
    monkeypatch.setattr(execution, "analyze_downloaded_email_assets", lambda *args, **kwargs: "complete")
    messages = []
    execution._analyze_in_child(SimpleNamespace(send=messages.append, close=lambda: None), (), None)
    assert messages == [(True, "complete")]
    assert db.SessionLocal is original_factory and db.SessionLocal.kw["bind"] is original_bind
    assert db.engine is original_engine


def test_sqlite_static_pool_is_unchanged_in_an_extraction_named_test_process(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db.multiprocessing, "current_process", lambda: SimpleNamespace(name="design-extraction"))
    original_bind = db.SessionLocal.kw["bind"]
    try:
        assert db.configure_extraction_database() is False
        assert isinstance(db.engine.pool, StaticPool)
        assert db.SessionLocal.kw["bind"] is original_bind
    finally:
        engine.dispose()
