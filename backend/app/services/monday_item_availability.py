"""Durable Monday processing availability shared by independent workers.

Observations suppress unnecessary reads; they never establish reviewer access or
replace live pre-publication checks. Empty reads are not deletion evidence.
Functions use the caller's transaction, perform no remote I/O and never commit.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import re
import uuid

from sqlalchemy import and_, exists, literal, or_, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import aliased

from ..config import settings
from ..models import AutoSyncReconciliationCheck, DesignProcessingReconciliationCheck, MondayItemAvailability
from .auto_sync_policy import RETIRED_SOURCE_OUTCOME


@dataclass(frozen=True)
class AvailabilityDecision:
    allowed: bool
    reason: str | None = None
    next_check_at: datetime | None = None
    state: str | None = None


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        raise ValueError("Availability observations require a datetime")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def utc_now():
    return _utc()


def _id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise ValueError("Monday identity must be a positive decimal ID")
    return str(value)


def processing_identity(access_token=None):
    token = settings.monday_ingestion_access_token if access_token is None else access_token
    if token is not None and not isinstance(token, str):
        raise ValueError("Monday processing access must be text")
    # An unconfigured identity supports offline callers; it grants no API access.
    value = b"configured\0" + token.encode() if token else b"unconfigured"
    return hashlib.sha256(value).hexdigest()


def _key(board_id, item_id, access_token):
    return _id(board_id), _id(item_id), processing_identity(access_token)


def _row(db, key, *, lock=False, create=False):
    if create:
        insert = postgres_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
        db.execute(insert(MondayItemAvailability).values(
            board_id=key[0], item_id=key[1], processing_identity=key[2],
        ).on_conflict_do_nothing(index_elements=["board_id", "item_id", "processing_identity"]))
    query = db.query(MondayItemAvailability).filter_by(board_id=key[0], item_id=key[1], processing_identity=key[2])
    if lock:
        query = query.with_for_update()
    return query.populate_existing().one_or_none()


def _legacy_retirement(db, board_id, item_id):
    stamps = []
    for model in (DesignProcessingReconciliationCheck, AutoSyncReconciliationCheck):
        query = db.query(model.last_attempted_at).filter_by(
            board_id=board_id, item_id=item_id, last_outcome=RETIRED_SOURCE_OUTCOME,
        )
        if model is AutoSyncReconciliationCheck:
            query = query.filter_by(scope="completed_transition")
        stamps.extend(_utc(row[0]) for row in query.all())
    return max(stamps, default=None)


def _positive_after(row, stamp):
    return bool(row is not None and row.state not in {"unknown", "unavailable", "retired"}
                and row.last_observed_at is not None and _utc(row.last_observed_at) >= stamp)


def availability_decision(db, board_id, item_id, access_token=None, now=None):
    now = _utc(now)
    key = _key(board_id, item_id, access_token)
    row = _row(db, key)
    retired = _legacy_retirement(db, key[0], key[1])
    if retired is not None and not _positive_after(row, retired):
        return AvailabilityDecision(False, "confirmed_deleted", None, "retired")
    if row is None:
        return AvailabilityDecision(True)
    if row.state == "retired":
        return AvailabilityDecision(False, row.reason or "confirmed_deleted", None, row.state)
    if row.probe_until is not None and _utc(row.probe_until) > now:
        return AvailabilityDecision(False, "probe_in_progress", _utc(row.probe_until), row.state)
    if row.next_check_at is not None and _utc(row.next_check_at) > now:
        return AvailabilityDecision(False, row.reason, _utc(row.next_check_at), row.state)
    return AvailabilityDecision(True, None, None, row.state)


def availability_read_predicate(board_id_expr, item_id_expr, access_token=None, now=None, *, rediscovered=False):
    """SQL gate before LIMIT so suppressed historical rows cannot starve work."""
    now = _utc(now)
    board = literal(board_id_expr) if isinstance(board_id_expr, str) else board_id_expr
    item = literal(item_id_expr) if isinstance(item_id_expr, str) else item_id_expr
    observed = aliased(MondayItemAvailability)
    same = and_(observed.board_id == board, observed.item_id == item,
                observed.processing_identity == processing_identity(access_token))
    suppressed = or_(observed.state == "retired", observed.next_check_at > now)
    if rediscovered:
        suppressed = and_(observed.state == "unavailable", observed.next_check_at > now)
    blocked = exists().where(same, or_(suppressed, observed.probe_until > now))
    legacy_blocks = []
    for model in (DesignProcessingReconciliationCheck, AutoSyncReconciliationCheck):
        legacy = aliased(model)
        positive = exists().where(
            observed.board_id == legacy.board_id, observed.item_id == legacy.item_id,
            observed.processing_identity == processing_identity(access_token),
            observed.state.notin_(("unknown", "unavailable", "retired")),
            observed.last_observed_at >= legacy.last_attempted_at,
        ).correlate(legacy)
        clauses = [legacy.board_id == board, legacy.item_id == item,
                   legacy.last_outcome == RETIRED_SOURCE_OUTCOME, ~positive]
        if model is AutoSyncReconciliationCheck:
            clauses.append(legacy.scope == "completed_transition")
        legacy_blocks.append(exists().where(*clauses))
    return and_(~blocked, *(~clause for clause in legacy_blocks)) if not rediscovered else ~blocked


def _watermark(row):
    return max((_utc(value) for value in (row.last_observed_at, row.last_event_at, row.last_attempted_at)
                if value is not None), default=None)


def _fresh(row, observed_at, event_at):
    stamp = _utc(event_at) if event_at is not None else observed_at
    if event_at is not None and stamp > observed_at + timedelta(minutes=5):
        raise ValueError("Monday event timestamp is in the future")
    watermark = _watermark(row)
    return watermark is None or stamp > watermark


def observe_item(db, board_id, item, access_token=None, observed_at=None, event_at=None):
    """Record a complete positive read; a newer live observation repairs misses."""
    if not isinstance(item, dict) or item.get("state") not in {"active", "archived", "deleted"}:
        raise ValueError("A complete Monday item lifecycle is required")
    item_id = _id(item.get("id"))
    actual_board = _id((item.get("board") or {}).get("id"))
    group = (item.get("group") or {}).get("id")
    if group is not None and (not isinstance(group, str) or not group.strip()):
        raise ValueError("Monday group identity is invalid")
    if item["state"] == "active" and group is None:
        raise ValueError("An active Monday item requires its group identity")
    now = _utc(observed_at)
    key = _key(board_id, item_id, access_token)
    row = _row(db, key, lock=True, create=True)
    if not _fresh(row, now, event_at):
        return row
    row.state = "out_of_board" if actual_board != key[0] else item["state"] if item["state"] != "active" else "available"
    row.reason = {"out_of_board": "out_of_board", "archived": "item_archived", "deleted": "item_deleted"}.get(row.state)
    row.actual_board_id, row.actual_group_id, row.actual_item_state = actual_board, group, item["state"]
    row.last_attempted_at = now
    row.last_observed_at = _utc(event_at) if event_at is not None else now
    if event_at is not None:
        row.last_event_at = _utc(event_at)
    row.consecutive_misses, row.retired_at = 0, None
    row.next_check_at = None if row.state == "available" else now + timedelta(seconds=settings.monday_item_availability_inactive_recheck_seconds)
    row.updated_at = now
    db.flush()
    return row


def observe_unavailable(db, board_id, item_id, access_token=None, observed_at=None):
    """Call only for a validated empty item lookup, never an API/auth failure."""
    now = _utc(observed_at)
    key = _key(board_id, item_id, access_token)
    row = _row(db, key, lock=True, create=True)
    retired = _legacy_retirement(db, key[0], key[1])
    if row.state == "retired" or (retired is not None and not _positive_after(row, retired)):
        return row
    if row.last_attempted_at is not None and now <= _utc(row.last_attempted_at):
        return row
    if _watermark(row) is not None and now <= _watermark(row):
        return row
    row.consecutive_misses += 1
    delays = settings.monday_item_availability_retry_seconds
    row.state, row.reason = "unavailable", "item_unavailable"
    row.last_attempted_at = now
    row.next_check_at = now + timedelta(seconds=delays[min(row.consecutive_misses - 1, len(delays) - 1)])
    row.updated_at = now
    db.flush()
    return row


def record_retired(db, board_id, item_id, access_token=None, observed_at=None, reason="confirmed_deleted"):
    """Explicit confirmed-deletion evidence, not inferred from missing reads."""
    now = _utc(observed_at)
    row = _row(db, _key(board_id, item_id, access_token), lock=True, create=True)
    if not _fresh(row, now, None):
        return row
    row.state, row.reason, row.actual_item_state = "retired", reason, "deleted"
    row.retired_at = row.last_attempted_at = row.last_observed_at = row.updated_at = now
    row.next_check_at = row.probe_token = row.probe_until = None
    db.flush()
    return row


def request_retry(db, board_id, item_id, access_token=None, observed_at=None, event_at=None):
    """Explicit nonterminal retry; confirmed retirement requires positive evidence."""
    now = _utc(observed_at)
    key = _key(board_id, item_id, access_token)
    row = _row(db, key, lock=True)
    retired = _legacy_retirement(db, key[0], key[1])
    if (row is not None and row.state == "retired") or (retired is not None and not _positive_after(row, retired)):
        return False
    if row is None:
        row = _row(db, key, lock=True, create=True)
    if event_at is not None:
        if not _fresh(row, now, event_at):
            return False
        row.last_event_at = _utc(event_at)
    row.next_check_at = None
    row.updated_at = now
    db.flush()
    return True


def lifecycle_probe_allowed(db, board_id, item_id, event_at, access_token=None, observed_at=None):
    """Whether a newer verified lifecycle event warrants a confirming read.

    This does not clear retirement, change availability or authorize a reviewer.
    Only the subsequent complete current item response can establish restoration.
    Callers restrict this to authenticated restore/create/move notifications.
    """
    if event_at is None:
        return False
    now = _utc(observed_at)
    stamp = _utc(event_at)
    if stamp > now + timedelta(minutes=5):
        return False
    key = _key(board_id, item_id, access_token)
    row = _row(db, key)
    watermark = _watermark(row) if row is not None else None
    retired = _legacy_retirement(db, key[0], key[1])
    boundary = max((value for value in (watermark, retired) if value is not None), default=None)
    return boundary is None or stamp > boundary


def claim_due_probe(db, board_id, item_id, access_token=None, now=None, *, rediscovered=False):
    """Claim a short probe lease; commit before remote work and release afterward."""
    now = _utc(now)
    key = _key(board_id, item_id, access_token)
    decision = availability_decision(db, board_id, item_id, access_token, now)
    if not decision.allowed:
        if not rediscovered or decision.reason == "probe_in_progress" or decision.state == "unavailable":
            return None
    _row(db, key, create=True)
    token = uuid.uuid4().hex
    result = db.execute(update(MondayItemAvailability).where(
        MondayItemAvailability.board_id == key[0], MondayItemAvailability.item_id == key[1],
        MondayItemAvailability.processing_identity == key[2],
        (MondayItemAvailability.state != "retired") if not rediscovered else True,
        or_(MondayItemAvailability.next_check_at.is_(None), MondayItemAvailability.next_check_at <= now,
            MondayItemAvailability.state != "unavailable") if rediscovered else
            or_(MondayItemAvailability.next_check_at.is_(None), MondayItemAvailability.next_check_at <= now),
        or_(MondayItemAvailability.probe_until.is_(None), MondayItemAvailability.probe_until <= now),
    ).values(probe_token=token, probe_until=now + timedelta(seconds=settings.monday_item_availability_probe_lease_seconds))
      .execution_options(synchronize_session=False))
    db.expire_all()
    return token if result.rowcount == 1 else None


def release_probe(db, board_id, item_id, lease_token, access_token=None):
    key = _key(board_id, item_id, access_token)
    result = db.execute(update(MondayItemAvailability).where(
        MondayItemAvailability.board_id == key[0], MondayItemAvailability.item_id == key[1],
        MondayItemAvailability.processing_identity == key[2], MondayItemAvailability.probe_token == lease_token,
    ).values(probe_token=None, probe_until=None).execution_options(synchronize_session=False))
    db.expire_all()
    return result.rowcount == 1


def complete_probe(db, board_id, item_id, lease_token, item, access_token=None, observed_at=None):
    """Promote a probe result only while its original lease/observation is current.

    The row stays locked through the observation, so a replacement lease or a
    newer lifecycle event cannot be overwritten between checking and writing.
    The caller releases the lease and commits after handling accepted results.
    """
    started = _utc(observed_at)
    key = _key(board_id, item_id, access_token)
    row = _row(db, key, lock=True)
    if (row is None or row.probe_token != lease_token or row.probe_until is None
            or _utc(row.probe_until) <= utc_now()):
        return False
    if _watermark(row) is not None and started <= _watermark(row):
        return False
    if item is None:
        retired = _legacy_retirement(db, key[0], key[1])
        if row.state == "retired" or (retired is not None and not _positive_after(row, retired)):
            return False
        observe_unavailable(db, board_id, item_id, access_token, observed_at=started)
    else:
        if not isinstance(item, dict) or str(item.get("id")) != key[1]:
            raise ValueError("Monday probe returned another item")
        observe_item(db, board_id, item, access_token, observed_at=started)
    return True
