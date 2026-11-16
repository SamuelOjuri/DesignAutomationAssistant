"""A persistent source-review gate waits for changes rather than retrying."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.app.config import settings
from backend.app.models import PerformanceSpecificationReview
from backend.app.services import performance_specification as worker
from backend.app.services import specification_review as reviews
from backend.app.services import specification_review_workspace as workspace
from backend.app.services.performance_specification_sources import SpecificationReviewRequired
from backend.app.services.specification_review_contract import ReviewSourceSnapshot, SaveReviewDecisions
from backend.tests.test_performance_specification import BOARD, ITEM, case, current, run
from backend.tests.test_specification_phase4_worker import ACTOR, review_case, _save, _publish


def block_source(case):
    extraction = deepcopy(case.item.extracted_parameters_json)
    for fact in extraction["specificationEvidence"]:
        if fact["field"] == "Decking":
            fact["snippet"] = "An unsupported source excerpt"
    case.item.extracted_parameters_json = extraction
    case.db.commit()


def parked(case):
    block_source(case)
    assert run(case) == ["needs_review"]
    return current(case)


def test_review_required_parks_without_failure_or_automatic_retries(case, caplog):
    with caplog.at_level("INFO", logger=worker.__name__):
        row = parked(case)
    assert row.scheduled_for is row.lease_token is row.lease_until is row.last_error is None
    assert row.attempt_count == 0
    assert row.review_json["status"] == "needs_review"
    assert "Decking" in row.review_json["issues"]
    assert row.requested_generation > row.completed_generation
    assert not case.calls and not case.gateway.uploads
    assert worker.claim_specification(case.db) is None
    assert "Specification needs review item=" in caplog.text
    assert "Specification refresh failed" not in caplog.text
    assert "unsupported source excerpt" not in caplog.text


def test_unchanged_parked_item_is_not_rediscovered_or_reconciled(case):
    row = parked(case)
    generation = row.requested_generation
    row.checked_at = worker.utc_now() - timedelta(days=5)
    case.db.commit()
    case.gateway.fetch_target = lambda _: pytest.fail("Unchanged parked item read Monday")
    for _ in range(3):
        assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
        worker.enqueue_specification(case.db, BOARD, ITEM, reconcile=True)
        case.db.commit()
    row = current(case)
    assert row.requested_generation == generation and row.scheduled_for is None
    assert worker.specification_needs_reconciliation(row, item=case.item) is False


@pytest.mark.parametrize("field", [
    "desiredInputRevision", "analyzedInputRevision", "desiredPipelineVersion",
    "analyzedPipelineVersion", "configuredPipelineVersion", "scopePolicy",
    "sourcePolicyVersion", "referenceParserVersion", "publicationVersion",
])
def test_changed_parked_identity_is_recovered_once(case, field):
    row = parked(case)
    saved = deepcopy(row.review_json)
    saved["waitingIdentity"][field] = "obsolete"
    row.review_json = saved
    before = row.requested_generation
    case.db.commit()
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 1
    assert current(case).requested_generation == before + 1
    assert current(case).scheduled_for is not None
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0


def test_new_source_input_wakes_parked_item_before_extraction_completes(case):
    row = parked(case)
    before = row.requested_generation
    case.item.latest_desired_input_revision = "email-v2"
    case.gateway.target.input_revision = "email-v2"
    case.db.commit()
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 1
    assert current(case).requested_generation == before + 1
    assert worker.run_specifications_once(case.db, gateway=case.gateway,
        client=case.client, storage=case.storage) == ["waiting"]
    assert current(case).review_json["status"] == "source_waiting"


def test_missing_input_identity_recovers_instead_of_repeatedly_discovering_without_queueing(case):
    row = parked(case)
    case.item.latest_desired_input_revision = case.item.latest_analyzed_input_revision = None
    case.item.latest_desired_pipeline_version = case.item.latest_analyzed_pipeline_version = None
    saved = deepcopy(row.review_json)
    saved["waitingIdentity"] = worker._review_wait_identity(case.item)
    row.review_json = saved
    case.db.commit()
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 1
    assert current(case).scheduled_for is not None
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0


def test_relevant_dependency_notification_wakes_parked_item(case):
    row = parked(case)
    before = row.requested_generation
    assert worker.enqueue_specification_dependents(case.db, "1825117125", "901") == 1
    case.db.commit()
    row = current(case)
    assert row.requested_generation == before + 1 and row.scheduled_for is not None
    row.scheduled_for = worker.utc_now()
    case.db.commit()
    assert worker.run_specifications_once(case.db, gateway=case.gateway,
        client=case.client, storage=case.storage) == ["needs_review"]


def test_legacy_conflict_retry_is_parked_without_another_failed_attempt(case):
    row = parked(case)
    saved = deepcopy(row.review_json)
    saved["status"] = "conflict"
    saved.pop("waitingIdentity")
    row.review_json, row.attempt_count = saved, 9
    row.last_error = "SpecificationReviewRequired: historical retry"
    row.scheduled_for = worker.utc_now()
    case.db.commit()
    assert worker.run_specifications_once(case.db, gateway=case.gateway,
        client=case.client, storage=case.storage) == ["needs_review"]
    assert current(case).attempt_count == 0 and current(case).last_error is None
    assert current(case).scheduled_for is None


def test_reviewer_correction_wakes_parked_item_and_retains_prior_pdf(review_case):
    case = review_case
    original = current(case).current_revision
    block_source(case)
    assert run(case) == ["needs_review"]
    row = current(case)
    assert row.current_revision == original and row.scheduled_for is None
    snapshot = ReviewSourceSnapshot.model_validate(row.review_json["source"]["reviewSource"])
    review = case.db.get(PerformanceSpecificationReview, (BOARD, ITEM), populate_existing=True)
    command = SaveReviewDecisions(expected_review_version=review.review_version,
        expected_source_fingerprint=review.source_fingerprint, idempotency_key="resolve-review-wait",
        field_operations={"Decking": {"operation": "specify_value", "display_value": "Timber (18mm)"}})
    case.db.commit()
    reviews.save_review_decisions(case.db, board_id=BOARD, item_id=ITEM, actor=ACTOR,
                                 command=command, source=snapshot)
    assert current(case).scheduled_for is not None
    specification, artifact = _publish(case)
    assert specification.current_revision != original
    assert artifact.source_json["raw"]["Decking"] == "Timber (18mm)"


def test_lightweight_status_and_retained_workspace_show_review_wait(case):
    parked(case)
    status = workspace.get_status(case.db, board_id=BOARD, item_id=ITEM,
                                  account_id="200", editable=True, manual_publication=True)
    assert status["status"] == "needs_review" and status["queue"]["failed"] is False
    payload = workspace.get_workspace(case.db, board_id=BOARD, item_id=ITEM,
        account_id="200", gateway=None, editable=False)
    assert payload["source"] is not None and payload["fields"]["Decking"]["issues"]


def test_a_late_review_exception_cannot_park_a_newer_generation(case, monkeypatch):
    block_source(case)
    original = worker.resolve_specification_sources

    def newer_notification(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except SpecificationReviewRequired:
            worker.enqueue_specification(case.db, BOARD, ITEM, immediate=True)
            case.db.commit()
            raise

    monkeypatch.setattr(worker, "resolve_specification_sources", newer_notification)
    assert run(case) == ["superseded"]
    assert current(case).scheduled_for is not None
    assert current(case).review_json is None


@pytest.mark.parametrize("ready,review", [(False, None), (True, object())])
def test_workspace_shows_parked_review_instead_of_failed_or_regenerating(ready, review):
    spec = SimpleNamespace(review_json={"status": "needs_review"}, scheduled_for=None,
        lease_token=None, last_error=None, requested_generation=2, completed_generation=0)
    assert workspace._status(spec=spec, review=review, source_ready=ready,
        exact=False, approval=None, editable=True, manual_publication=True) == "needs_review"
