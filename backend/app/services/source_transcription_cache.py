"""Scoped durable text cache; cached text never supplies request attribution."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import wraps
import hashlib
import json
import logging
from pathlib import Path
import threading

from ..config import DESIGN_PROCESSING_PIPELINE_REVISION, settings
from ..db import SessionLocal
from ..models import SourceTranscriptionCache
from .source_text import clean_source_text

logger = logging.getLogger(__name__)
TRANSCRIPTION_CACHE_VERSION = "source-transcription-v1"
_locks = {}
_locks_guard = threading.Lock()


def _key_lock(scope, key):
    # Extraction children live for one item. Distinct documents must never wait
    # behind an unrelated long OCR call merely because lock hashes collide.
    with _locks_guard:
        return _locks.setdefault((scope, key), threading.Lock())


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def cache_identity(client, content, filename, kind):
    from .specification_image_text import IMAGE_TRANSCRIPTION_PROMPT
    digest = _hash(content)
    scope = _hash(str(client.transcription_cache_scope).encode())
    contract = {"version": TRANSCRIPTION_CACHE_VERSION, "pipeline": DESIGN_PROCESSING_PIPELINE_REVISION, "kind": kind,
        "content": digest, "suffix": Path(filename).suffix.lower(),
        "model": client.model, "thinking": client.thinking_level,
        "native_pdf": getattr(client, "pdf_native_text_enabled", False),
        "image_prompt": IMAGE_TRANSCRIPTION_PROMPT if kind == "image" else None}
    return scope, _hash(json.dumps(contract, sort_keys=True).encode()), digest


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _cacheable_text(value):
    # Match the source reader's unsuccessful-transcription guard. A provider can
    # return HTTP success with an apology/error instead of document text.
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 500_000
            and not value.lstrip().lower().startswith((
                "error processing", "unable to process", "unsupported image format",
            )))


def read_text(scope, key, *, session_factory=SessionLocal, now=None):
    now = now or datetime.now(timezone.utc)
    with session_factory() as db:
        row = db.get(SourceTranscriptionCache, (scope, key))
        if row is None or _aware(row.expires_at) <= now:
            return None
        if not _cacheable_text(row.text) or _hash(row.text.encode()) != row.text_sha256:
            return None
        return row.text


def save_text(scope, key, digest, text, *, session_factory=SessionLocal, now=None):
    if not _cacheable_text(text):
        return
    now = now or datetime.now(timezone.utc)
    values = dict(scope_key=scope, cache_key=key, content_sha256=digest,
        text_sha256=_hash(text.encode()), text=text, created_at=now,
        expires_at=now + timedelta(days=settings.design_processing_transcription_cache_ttl_days))
    with session_factory() as db:
        # An upsert lets independent workers successfully read the same source.
        dialect = db.get_bind().dialect.name
        if dialect in {"postgresql", "sqlite"}:
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            statement = insert(SourceTranscriptionCache).values(**values)
            db.execute(statement.on_conflict_do_update(index_elements=["scope_key", "cache_key"], set_=values))
        else:
            db.merge(SourceTranscriptionCache(**values))
        # Expiry cleanup is bounded to this private scope and never touches
        # review/source artifacts. Expired rows are never returned by read_text.
        db.query(SourceTranscriptionCache).filter(
            SourceTranscriptionCache.scope_key == scope,
            SourceTranscriptionCache.expires_at <= now).delete(synchronize_session=False)
        db.commit()


def cached_transcription(kind):
    def decorate(function):
        @wraps(function)
        def wrapped(client, content, filename, *args, **kwargs):
            if not settings.design_processing_transcription_cache_enabled or not getattr(client, "transcription_cache_scope", None):
                return function(client, content, filename, *args, **kwargs)
            scope, key, digest = cache_identity(client, content, filename, kind)
            # Prevent identical inline logos launching duplicate calls in one
            # child. Separate workers may fill the same idempotent cache key.
            with _key_lock(scope, key):
                try:
                    text = read_text(scope, key)
                except Exception as exc:
                    logger.warning("Source transcription cache read unavailable error_type=%s", type(exc).__name__)
                    text = None
                if text is not None:
                    logger.info("Source transcription reused kind=%s digest=%s", kind, digest[:12])
                    return text
                # Failed, blocked and partial responses are never cached.
                text = function(client, content, filename, *args, **kwargs)
                text = clean_source_text(text) if isinstance(text, str) else text
                if _cacheable_text(text):
                    try:
                        save_text(scope, key, digest, text)
                    except Exception as exc:
                        logger.warning("Source transcription cache write unavailable error_type=%s", type(exc).__name__)
                return text
        return wrapped
    return decorate
