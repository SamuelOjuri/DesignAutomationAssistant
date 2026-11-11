"""Shared, leased lightweight lifecycle probes. No asset or CRM reads here."""

from __future__ import annotations

import logging

from ..monday_client import fetch_monday_item_statuses
from .monday_item_availability import (
    claim_due_probe, complete_probe, release_probe, request_retry, utc_now,
)

logger = logging.getLogger(__name__)


def probe_status_is_current(db, board_id, item_id, item, access_token=None):
    """Fence consumers of a completed batch against a newer lifecycle event.

    The caller keeps this short row lock through its local cancellation/queue
    transaction. Never relabel an earlier probe response with a newer timestamp.
    A missing result is current only while the shared state is unavailable.
    """
    from ..models import MondayItemAvailability
    from .monday_item_availability import processing_identity

    query = db.query(MondayItemAvailability).filter_by(
        board_id=str(board_id), item_id=str(item_id), processing_identity=processing_identity(access_token),
    ).populate_existing()
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update()
    observed = query.one_or_none()
    if observed is None:
        return False
    if item is None:
        return observed.state == "unavailable"
    if not isinstance(item, dict) or str(item.get("id")) != str(item_id):
        return False
    actual_board = str((item.get("board") or {}).get("id"))
    state = "out_of_board" if actual_board != str(board_id) else "available" if item.get("state") == "active" else item.get("state")
    return (observed.state == state and observed.actual_board_id == actual_board
        and observed.actual_group_id == (item.get("group") or {}).get("id")
        and observed.actual_item_state == item.get("state"))


def probe_items(db, board_id, item_ids, *, access_token=None, fetch_statuses=None,
                force=False, dry_run=False, rediscovered_ids=()):
    """Return only leased IDs: a validated item, or None for a validated miss.

    The caller must finish its existing transaction before invoking this helper:
    leases are committed before network I/O, observations afterwards. A failed
    batch records no missing items. Dry runs read remotely without writing leases
    or observations. A force request never overrides confirmed retirement.
    """
    identities = list(dict.fromkeys(str(value) for value in item_ids))
    if not identities:
        return {}
    if access_token is None:
        from .auto_sync import get_monday_ingestion_access_token
        access_token = get_monday_ingestion_access_token()
    fetch = fetch_statuses or (lambda ids: fetch_monday_item_statuses(access_token, ids))
    results = {}
    rediscovered = frozenset(str(value) for value in rediscovered_ids)
    for offset in range(0, len(identities), 100):
        leases = {}
        for item_id in identities[offset:offset + 100]:
            if dry_run:
                from .monday_item_availability import availability_decision
                if availability_decision(db, board_id, item_id, access_token=access_token).allowed:
                    leases[item_id] = None
                continue
            if force:
                request_retry(db, board_id, item_id, access_token=access_token)
            lease = claim_due_probe(db, board_id, item_id, access_token=access_token,
                                    rediscovered=item_id in rediscovered)
            if lease is not None:
                leases[item_id] = lease
        db.commit()
        if not leases:
            continue
        started = utc_now()
        try:
            visible = fetch(list(leases))
            if not isinstance(visible, dict) or set(visible) - set(leases):
                raise ValueError("Monday probe returned unexpected identities")
            # Validate the complete injected adapter result before any mutation.
            for identity, item in visible.items():
                if (not isinstance(item, dict) or str(item.get("id")) != identity
                        or item.get("state") not in {"active", "archived", "deleted"}
                        or not isinstance(item.get("board"), dict)
                        or not str(item["board"].get("id") or "").isascii()
                        or not str(item["board"].get("id") or "").isdecimal()
                        or int(str(item["board"]["id"])) <= 0
                        or (item["state"] == "active" and (
                            not isinstance(item.get("group"), dict) or not item["group"].get("id")))):
                    raise ValueError("Monday probe returned malformed lifecycle data")
            for identity in leases:
                item = visible.get(identity)
                if not dry_run:
                    accepted = complete_probe(db, board_id, identity, leases[identity], item,
                                              access_token=access_token, observed_at=started)
                    if not accepted:
                        continue
                results[identity] = item
            logger.info("Monday availability probe board=%s requested=%s visible=%s unavailable=%s dry_run=%s",
                        board_id, len(leases), len(visible), len(leases) - len(visible), dry_run)
        except Exception:
            db.rollback()
            raise
        finally:
            if not dry_run:
                for identity, lease in leases.items():
                    release_probe(db, board_id, identity, lease, access_token=access_token)
                db.commit()
    return results
