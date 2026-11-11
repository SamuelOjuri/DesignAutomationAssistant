"""Remove bounded batches of expired optimization data; never review artifacts."""
import argparse
from datetime import datetime, timedelta, timezone
import json

from ..app.db import SessionLocal
from ..app.models import GeminiRequestLease, SourceTranscriptionCache


def purge_expired(db, *, limit=1000, dry_run=False, now=None):
    if not 1 <= limit <= 10000:
        raise ValueError("Cleanup limit must be between 1 and 10000")
    now = now or datetime.now(timezone.utc)
    cache_rows = db.query(SourceTranscriptionCache.scope_key, SourceTranscriptionCache.cache_key).filter(
        SourceTranscriptionCache.expires_at <= now).order_by(SourceTranscriptionCache.expires_at).limit(limit).all()
    lease_rows = db.query(GeminiRequestLease.token).filter(
        GeminiRequestLease.expires_at <= now,
        GeminiRequestLease.started_at < now - timedelta(seconds=60)).limit(limit).all()
    result = {"dry_run": dry_run, "expired_cache_rows": len(cache_rows), "expired_request_leases": len(lease_rows)}
    if not dry_run:
        for scope, key in cache_rows:
            db.query(SourceTranscriptionCache).filter_by(scope_key=scope, cache_key=key).filter(
                SourceTranscriptionCache.expires_at <= now).delete(synchronize_session=False)
        for (token,) in lease_rows:
            db.query(GeminiRequestLease).filter_by(token=token).filter(
                GeminiRequestLease.expires_at <= now).delete(synchronize_session=False)
        db.commit()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    with SessionLocal() as db:
        print(json.dumps(purge_expired(db, limit=args.limit, dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
