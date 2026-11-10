"""Real PostgreSQL races at the Phase 6 review orchestration boundary.

Set TEST_DATABASE_URL to a disposable PostgreSQL database with pgvector
available. The shared fixtures migrate and remove only a generated schema.
Only remote source reads are stubbed; workspace transactions, audit receipts,
row locks, uniqueness constraints and generation updates use PostgreSQL.
"""

from queue import Queue
import threading

from backend.app.models import (
    PerformanceSpecification,
    PerformanceSpecificationReview,
    PerformanceSpecificationReviewEvent,
)
from backend.app.routes.specification_review import _error_status
from backend.app.services import specification_review as persistence
from backend.app.services import specification_review_workspace as workspace
from backend.tests.test_specification_review_persistence import (
    ACCOUNT, ACTOR, BOARD, save_command, source_snapshot,
)
from backend.tests.test_specification_review_postgres_concurrency import (
    postgres_review_database, postgres_review, race,
)


def database_state(Session, item_id):
    with Session() as db:
        review = db.get(PerformanceSpecificationReview, (BOARD, item_id))
        spec = db.get(PerformanceSpecification, (BOARD, item_id))
        events = db.query(PerformanceSpecificationReviewEvent).filter_by(
            board_id=BOARD, item_id=item_id).all()
        return {
            "review_version": review.review_version,
            "request_type": review.selected_request_type,
            "project": review.selected_project_item_id,
            "drawing": review.selected_drawing_item_id,
            "source_fingerprint": review.source_fingerprint,
            "decisions": review.field_decisions_json,
            "generation": spec.requested_generation,
            "save_events": sum(event.command_type == "save_decisions" for event in events),
            "refresh_events": sum(event.command_type == "refresh" for event in events),
            "source_events": sum(event.command_type == "source_changed" for event in events),
        }


def synchronized_source_read(monkeypatch, source):
    """Both requests observe the pre-race state before either can take locks."""
    ready = threading.Barrier(2)
    observed = set()
    guard = threading.Lock()

    def resolve(db, context, gateway):
        assert not db.in_transaction(), "Source reads must not hold database locks"
        identity = threading.get_ident()
        with guard:
            first_read = identity not in observed
            observed.add(identity)
        if first_read:
            ready.wait(timeout=5)
        return source.model_copy(deep=True)

    monkeypatch.setattr(workspace, "_resolve", resolve)


def test_workspace_competing_saves_keep_one_decision_and_return_one_http_409(postgres_review, monkeypatch):
    Session, item_id = postgres_review
    before = database_state(Session, item_id)
    synchronized_source_read(monkeypatch, source_snapshot())
    other_actor = ACTOR.model_copy(update={"app_user_id": "reviewer-2", "monday_user_id": "101"})
    commands = [save_command(field_operations={label: {"operation": "specify_blank"}})
                for label in ("Decking", "Contact")]
    outcomes = race(Session, [
        lambda db: workspace.save_decisions(db, board_id=BOARD, item_id=item_id,
            actor=ACTOR, command=commands[0], gateway=object()),
        lambda db: workspace.save_decisions(db, board_id=BOARD, item_id=item_id,
            actor=other_actor, command=commands[1], gateway=object()),
    ])
    successes = [result for result in outcomes if isinstance(result, persistence.ReviewCommandResult)]
    errors = [result for result in outcomes if isinstance(result, BaseException)]
    assert len(successes) == len(errors) == 1, outcomes
    assert isinstance(errors[0], persistence.ReviewConflict), outcomes
    assert errors[0].code == "stale_review"
    assert _error_status(errors[0].code) == 409
    after = database_state(Session, item_id)
    assert after["review_version"] == 1
    assert len(after["decisions"]) == 1
    assert after["save_events"] == 1
    assert after["generation"] == before["generation"] + 1


def test_explicit_refresh_fences_a_delayed_selection_save_and_its_sources(postgres_review, monkeypatch):
    Session, item_id = postgres_review
    before = database_state(Session, item_id)
    source_read = threading.Event()
    release_read = threading.Event()
    outcomes = Queue()
    delayed_gateway = object()
    fresh_gateway = object()

    def resolve(db, context, gateway):
        assert not db.in_transaction(), "Remote source reads must precede row locks"
        if gateway is delayed_gateway:
            assert context["review"].source_fingerprint == "sources-1"
            source_read.set()
            assert release_read.wait(timeout=10), "New source refresh did not finish"
            return source_snapshot("delayed-selection-sources")
        assert gateway is fresh_gateway
        return source_snapshot("newest-sources")

    monkeypatch.setattr(workspace, "_resolve", resolve)
    command = save_command(key="delayed-selection", selections={"selected_request_type": "new_enquiry"})

    def delayed_save():
        with Session() as db:
            try:
                outcomes.put(workspace.save_decisions(db, board_id=BOARD, item_id=item_id,
                    actor=ACTOR, command=command, gateway=delayed_gateway))
            except BaseException as exc:
                outcomes.put(exc)

    delayed = threading.Thread(target=delayed_save, daemon=True)
    delayed.start()
    try:
        assert source_read.wait(timeout=5), "Selection save did not reach its source read"
        with Session() as db:
            refreshed = workspace.refresh_recommendations(db, board_id=BOARD, item_id=item_id,
                actor=ACTOR, gateway=fresh_gateway, command=workspace.RefreshReview(
                    expected_review_version=0, expected_source_fingerprint="sources-1",
                    idempotency_key="explicit-newest-refresh"))
        assert refreshed.changed
        assert refreshed.source_fingerprint == "newest-sources"
    finally:
        release_read.set()
        delayed.join(timeout=15)
    assert not delayed.is_alive(), "The delayed source transaction did not finish"
    stale = outcomes.get(timeout=1)
    assert isinstance(stale, persistence.ReviewConflict), stale
    assert stale.code == "source_changed"
    assert _error_status(stale.code) == 409
    after = database_state(Session, item_id)
    assert after["source_fingerprint"] == "newest-sources"
    assert after["review_version"] == 0
    assert after["request_type"] is None
    assert after["project"] is after["drawing"] is None
    assert after["decisions"] == {}
    assert after["save_events"] == after["source_events"] == 0
    assert after["refresh_events"] == 1
    assert after["generation"] == before["generation"] + 1


def test_duplicate_refresh_is_one_atomic_receipt_and_one_queued_generation(postgres_review, monkeypatch):
    Session, item_id = postgres_review
    before = database_state(Session, item_id)
    synchronized_source_read(monkeypatch, source_snapshot("refreshed-sources"))
    command = workspace.RefreshReview(
        expected_review_version=0, expected_source_fingerprint="sources-1",
        idempotency_key="concurrent-identical-refresh",
    )

    def refresh(db):
        return workspace.refresh_recommendations(db, board_id=BOARD, item_id=item_id,
            actor=ACTOR, command=command, gateway=object())

    outcomes = race(Session, [refresh, refresh])
    assert all(isinstance(result, persistence.ReviewCommandResult) for result in outcomes), outcomes
    assert outcomes[0] == outcomes[1]
    assert outcomes[0].changed
    after = database_state(Session, item_id)
    assert after["source_fingerprint"] == "refreshed-sources"
    assert after["review_version"] == 0
    assert after["refresh_events"] == 1
    assert after["save_events"] == after["source_events"] == 0
    assert after["generation"] == before["generation"] + 1


def test_duplicate_save_returns_one_receipt_after_competing_source_reads(postgres_review, monkeypatch):
    Session, item_id = postgres_review
    before = database_state(Session, item_id)
    synchronized_source_read(monkeypatch, source_snapshot())
    command = save_command(key="concurrent-identical-save",
                           field_operations={"Decking": {"operation": "specify_blank"}})

    def save(db):
        return workspace.save_decisions(db, board_id=BOARD, item_id=item_id,
                                       actor=ACTOR, command=command, gateway=object())

    outcomes = race(Session, [save, save])
    assert all(isinstance(result, persistence.ReviewCommandResult) for result in outcomes), outcomes
    assert outcomes[0] == outcomes[1]
    after = database_state(Session, item_id)
    assert after["review_version"] == after["save_events"] == 1
    assert after["generation"] == before["generation"] + 1
