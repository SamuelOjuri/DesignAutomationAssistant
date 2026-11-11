"""Database-coordinated admission for generation requests across worker children."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import time
import uuid

from sqlalchemy import func, text

from ..config import settings
from ..db import SessionLocal
from ..models import GeminiRequestLease


logger = logging.getLogger(__name__)


def budget_scope():
    # Stable across credential rotations. Every process using the same provider
    # project must use this same scope, including processes with different keys.
    return hashlib.sha256(settings.gemini_shared_budget_scope.encode()).hexdigest()


def try_acquire(scope, *, session_factory=SessionLocal, now=None, concurrent=None, rpm=None, lease_seconds=None):
    now = now or datetime.now(timezone.utc)
    concurrent = concurrent or settings.gemini_shared_max_concurrent
    rpm = rpm or settings.gemini_shared_requests_per_minute
    lease_seconds = lease_seconds or settings.gemini_shared_lease_seconds
    with session_factory() as db:
        if db.get_bind().dialect.name == "postgresql":
            # A short transaction serializes admission only. No database lock
            # or transaction is held while an external model request runs.
            lock_id = int.from_bytes(bytes.fromhex(scope[:16]), "big", signed=True)
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_id})
        cutoff = now - timedelta(seconds=60)
        db.query(GeminiRequestLease).filter(
            GeminiRequestLease.scope_key == scope,
            GeminiRequestLease.started_at < cutoff,
            GeminiRequestLease.expires_at <= now).delete(synchronize_session=False)
        active = db.query(func.count()).select_from(GeminiRequestLease).filter(
            GeminiRequestLease.scope_key == scope, GeminiRequestLease.released_at.is_(None),
            GeminiRequestLease.expires_at > now).scalar()
        recent = db.query(func.count()).select_from(GeminiRequestLease).filter(
            GeminiRequestLease.scope_key == scope, GeminiRequestLease.started_at > cutoff).scalar()
        if active >= concurrent or recent >= rpm:
            db.commit()
            return None
        token = uuid.uuid4().hex
        db.add(GeminiRequestLease(token=token, scope_key=scope, started_at=now,
                                 expires_at=now + timedelta(seconds=lease_seconds)))
        db.commit()
        return token


def release(token, *, session_factory=SessionLocal, now=None):
    with session_factory() as db:
        db.query(GeminiRequestLease).filter_by(token=token).update(
            {"released_at": now or datetime.now(timezone.utc)}, synchronize_session=False)
        db.commit()


@contextmanager
def shared_generation_slot():
    if not settings.gemini_shared_rate_limit_enabled:
        yield
        return
    if settings.gemini_shared_lease_seconds <= settings.gemini_request_timeout_seconds:
        raise RuntimeError("Shared Gemini lease must exceed the configured request timeout")
    deadline = time.monotonic() + settings.gemini_shared_wait_seconds
    token = None
    while token is None:
        # Missing migration/database failure is intentionally not a quota bypass.
        token = try_acquire(budget_scope())
        if token is None:
            if time.monotonic() >= deadline:
                raise TimeoutError("Shared Gemini request budget is busy")
            time.sleep(0.5)
    try:
        yield
    finally:
        try:
            release(token)
        except Exception as exc:
            # Admission remains fail closed. A failed release keeps its lease
            # occupied until expiry, without losing an already-paid response or
            # replacing the provider's original error with a database exception.
            logger.warning("Shared Gemini lease release unavailable error_type=%s", type(exc).__name__)
