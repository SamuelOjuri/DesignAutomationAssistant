import json
import multiprocessing
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings


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


_pool_role = "application"
_active_engine_options = _engine_options(settings.database_url)
engine = create_engine(settings.database_url, **_active_engine_options)
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
    replacement = create_engine(settings.database_url, **options)
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
