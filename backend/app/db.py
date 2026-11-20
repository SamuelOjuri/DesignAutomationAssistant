import json
import logging
import multiprocessing
import os
import random
import time

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings
from .services.database_diagnostics import database_error_details

logger = logging.getLogger(__name__)


def _engine_options(database_url: str, *, extraction: bool = False) -> dict:
    options = {"pool_pre_ping": True}
    if database_url.startswith("postgresql"):
        options.update(
            pool_size=(settings.database_extraction_pool_size if extraction else settings.database_pool_size),
            max_overflow=(settings.database_extraction_max_overflow if extraction else settings.database_max_overflow),
            pool_recycle=settings.database_pool_recycle_seconds,
            pool_timeout=settings.database_pool_timeout_seconds,
            connect_args={
                "connect_timeout": settings.database_connect_timeout_seconds,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 5,
            },
        )
    return options


_keep_idle_connections = False


def _release_idle_connection(dbapi_connection, connection_record) -> None:
    # A session-mode pooler counts every open client connection against its
    # limit, used or not. Workers sleep through poll intervals and model calls,
    # so they return the slot between transactions and reconnect on demand. The
    # pool still bounds how many connections one process opens at a time.
    if not _keep_idle_connections:
        connection_record.close()


def _connect_when_slot_available(dialect, connection_record, cargs, cparams):
    """Wait briefly for a pooler slot; every other connection failure raises at once."""
    started = time.monotonic()
    deadline = started + settings.database_connect_retry_seconds
    delay = 0.25
    while True:
        try:
            connection = dialect.connect(*cargs, **cparams)
        except Exception as exc:
            details = database_error_details(exc)
            remaining = deadline - time.monotonic()
            if not details or details["database_error"] != "connection_limit" or remaining <= 0:
                raise
            time.sleep(min(random.uniform(delay / 2, delay), remaining))
            delay = min(delay * 2, 2.0)
            continue
        if delay > 0.25:
            logger.warning("Database connection limit reached; waited %.1fs for a pooler slot",
                           time.monotonic() - started)
        return connection


def _create_engine(options: dict):
    created = create_engine(settings.database_url, **options)
    if created.dialect.name == "postgresql":
        event.listen(created, "checkin", _release_idle_connection)
        event.listen(created, "do_connect", _connect_when_slot_available)
    return created


def keep_idle_connections() -> None:
    """Let the API keep its bounded pool open; a new connection per request is slow."""
    global _keep_idle_connections
    _keep_idle_connections = True


_pool_role = "application"
_active_engine_options = _engine_options(settings.database_url)
engine = _create_engine(_active_engine_options)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)

Base = declarative_base()


def database_pool_diagnostics() -> dict:
    """Report local connection bounds without DSNs, usernames or credentials."""
    size = _active_engine_options.get("pool_size")
    overflow = _active_engine_options.get("max_overflow")
    return {
        "database_pool_role": _pool_role,
        "database_pool_size": size,
        "database_max_overflow": overflow,
        "database_pool_max_connections": size + overflow if size is not None and overflow is not None else None,
        "database_idle_connections": (
            "kept" if _keep_idle_connections or engine.dialect.name != "postgresql" else "released"),
    }


def configure_extraction_database() -> bool:
    """Bound the actual spawned extraction child's pool before starting threads.

    Keep the existing sessionmaker object: cache and admission helpers capture it
    as a default argument at import. Replacing the factory would leave those
    helpers attached to the child's original application-sized pool.
    """
    global engine, _pool_role, _active_engine_options
    if (multiprocessing.current_process().name != "design-extraction"
            or engine.dialect.name != "postgresql" or _pool_role == "extraction"):
        return False
    options = _engine_options(settings.database_url, extraction=True)
    replacement = _create_engine(options)
    previous = engine
    SessionLocal.configure(bind=replacement)
    engine, _pool_role, _active_engine_options = replacement, "extraction", options
    # Production extraction uses spawn, so this engine belongs only to the
    # child. Parent worker/API sessions and connections are never rebound.
    previous.dispose()
    print(json.dumps({"event": "database_pool_runtime", "pid": os.getpid(), **database_pool_diagnostics()}), flush=True)
    return True


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
