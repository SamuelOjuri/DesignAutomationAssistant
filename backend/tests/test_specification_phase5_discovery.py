"""Task-independent discovery, fair recovery and live publication boundaries."""

from copy import deepcopy
from datetime import timedelta
import uuid

import pytest

from backend.app.config import settings
from backend.app.models import (
    DesignProcessingItem, DesignProcessingReconciliationCheck, PerformanceSpecification,
    PerformanceSpecificationArtifact, PerformanceSpecificationApproval, PerformanceSpecificationReview, Task,
)
from backend.app.services import performance_specification as worker
from backend.app.services import specification_review as reviews
from backend.app.services.auto_sync_policy import RETIRED_SOURCE_OUTCOME
from backend.app.services.specification_review_contract import FIELD_NAMES, ReviewSourceSnapshot
from backend.tests.test_performance_specification import BOARD, ITEM, case, current, run
from backend.tests.test_specification_phase4_worker import ACTOR, review_case, _save, _publish


def _observation(case, *, group=None, outcome="ignored", when=None):
    when = when or worker.utc_now()
    row = DesignProcessingReconciliationCheck(
        board_id=BOARD, item_id=ITEM, last_attempted_at=when, last_checked_at=when,
        last_outcome=outcome, last_reason="ready", last_group_id=group,
    )
    case.db.add(row)
    case.db.commit()
    return row


def test_discovery_without_task_creates_work_and_repeated_checks_coalesce(case):
    assert case.db.query(Task).count() == 0
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 1
    row = current(case)
    generation, due = row.requested_generation, row.scheduled_for
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
    worker.enqueue_specification(case.db, BOARD, ITEM, reconcile=True)
    case.db.commit()
    assert current(case).requested_generation == generation
    assert current(case).scheduled_for == due


@pytest.mark.parametrize("lease", ["absent", "expired", "missing_deadline"])
def test_discovery_repairs_legacy_unscheduled_outstanding_generations(case, lease):
    row = worker.enqueue_specification(case.db, BOARD, ITEM, immediate=True)
    row.scheduled_for = None
    if lease != "absent":
        row.lease_token = "orphaned-lease"
        row.lease_until = worker.utc_now() - timedelta(minutes=1) if lease == "expired" else None
    case.db.commit()
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 1
    assert current(case).scheduled_for is not None
    assert worker.claim_specification(case.db) is not None


def test_discovery_revives_cancelled_item_after_new_landing_observation(case):
    assert run(case) == ["published"]
    revision = current(case).current_revision
    worker.cancel_specification(case.db, BOARD, ITEM)
    case.db.commit()
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
    _observation(case, group=settings.design_processing_landing_group_id,
                 when=worker.utc_now() + timedelta(seconds=1))
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 1
    assert worker.run_specifications_once(case.db, gateway=case.gateway,
        client=case.client, storage=case.storage) == ["unchanged"]
    assert current(case).current_revision == revision
    assert len(case.calls) == len(case.gateway.uploads) == 1


def test_dependency_changes_do_not_requeue_known_read_only_items(case):
    assert run(case) == ["published"]
    worker.cancel_specification(case.db, BOARD, ITEM, reason="outside_landing_zone")
    case.db.commit()
    generation = current(case).requested_generation
    assert worker.enqueue_specification_dependents(case.db, "1825117125", "901") == 0
    assert worker.enqueue_specification_dependents(case.db, "9000", "9101") == 0
    case.db.commit()
    assert current(case).requested_generation == generation
    assert current(case).scheduled_for is None


@pytest.mark.parametrize("mode", ["off", "allowlist"])
def test_dependency_notifications_respect_operating_mode_before_queueing(case, monkeypatch, mode):
    assert run(case) == ["published"]
    generation = current(case).requested_generation
    monkeypatch.setattr(settings, "performance_specification_mode", mode)
    monkeypatch.setattr(settings, "performance_specification_allowlist_item_ids", ["999"])
    assert worker.enqueue_specification_dependents(case.db, "1825117125", "901") == 0
    case.db.commit()
    assert current(case).requested_generation == generation


@pytest.mark.parametrize("field", ["scopePolicy", "sourcePolicyVersion", "referenceParserVersion",
                                  "extractionPipeline", "publicationVersion"])
def test_discovery_refreshes_obsolete_policy_once_without_retranslation(case, field):
    assert run(case) == ["published"]
    row = current(case)
    row.source_json = {**row.source_json, field: "obsolete"}
    before = row.requested_generation
    case.db.commit()
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 1
    assert current(case).requested_generation == before + 1
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
    assert worker.run_specifications_once(case.db, gateway=case.gateway,
        client=case.client, storage=case.storage) == ["unchanged"]
    assert len(case.calls) == len(case.gateway.uploads) == 1


@pytest.mark.parametrize("field", ["scopePolicy", "sourcePolicyVersion", "referenceParserVersion",
                                  "extractionPipeline", "publicationVersion"])
@pytest.mark.parametrize("missing", [True, False])
def test_discovery_recovers_missing_or_null_policy_identity(case, field, missing):
    assert run(case) == ["published"]
    row = current(case)
    source = deepcopy(row.source_json)
    if missing:
        source.pop(field)
    else:
        source[field] = None
    row.source_json = source
    before = row.requested_generation
    case.db.commit()
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 1
    assert current(case).requested_generation == before + 1


@pytest.mark.parametrize("wake", ["interval", "new_observation"])
def test_obsolete_policy_discovery_failure_respects_recheck_delay(case, wake):
    assert run(case) == ["published"]
    row = current(case)
    row.source_json = {**row.source_json, "scopePolicy": "obsolete"}
    before = row.requested_generation
    case.db.commit()
    fetch = case.gateway.fetch_target
    visits = []

    def unavailable(identity):
        visits.append(identity)
        raise TimeoutError("source unavailable")

    case.gateway.fetch_target = unavailable
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
    assert visits == [ITEM]
    row = current(case)
    assert row.requested_generation == before
    assert row.last_error == "Discovery: TimeoutError"
    if wake == "interval":
        row.checked_at = worker.utc_now() - timedelta(seconds=settings.performance_specification_recheck_seconds + 1)
        case.db.commit()
    else:
        _observation(case, group=settings.design_processing_landing_group_id,
            when=worker.utc_now() + timedelta(seconds=1))
    case.gateway.fetch_target = fetch
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 1
    assert current(case).requested_generation == before + 1


@pytest.mark.parametrize("group,state", [("topics", "active"), ("group_mkpbb3tx", "active"),
    ("group_mkpbd6vy", "archived"), ("group_mkpbd6vy", "deleted")])
def test_discovery_checks_live_scope_and_defers_excluded_rows(case, group, state):
    case.gateway.target.group_id, case.gateway.target.item_state = group, state
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
    row = current(case)
    assert row.scheduled_for is row.lease_token is None
    assert row.review_json["status"] == "read_only"
    case.gateway.fetch_target = lambda _: pytest.fail("Excluded discovery row was immediately revisited")
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
    assert not case.calls and not case.gateway.uploads


@pytest.mark.parametrize("group,outcome", [("topics", "ignored"), ("group_mkpbb3tx", "excluded"),
    ("group_mkpbd6vy", RETIRED_SOURCE_OUTCOME)])
def test_discovery_uses_durable_observations_to_skip_historical_scope(case, group, outcome):
    _observation(case, group=group, outcome=outcome)
    case.gateway.fetch_target = lambda _: pytest.fail("Historical/retired item read during discovery")
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
    assert current(case) is None


def test_bounded_discovery_rotates_past_unavailable_and_excluded_candidates(case):
    for identity in ("124", "125"):
        case.db.add(DesignProcessingItem(id=uuid.uuid4(), board_id=BOARD, item_id=identity,
            state="scheduled", warnings_json=[]))
    case.db.commit()
    visited = []

    def fetch(identity):
        visited.append(identity)
        if identity == ITEM:
            raise TimeoutError("source unavailable")
        target = deepcopy(case.gateway.target)
        target.item_id = identity
        if identity == "124":
            target.group_id = "topics"
        return target

    case.gateway.fetch_target = fetch
    assert worker.discover_specifications(case.db, limit=1, gateway=case.gateway) == 0
    assert worker.discover_specifications(case.db, limit=1, gateway=case.gateway) == 0
    assert worker.discover_specifications(case.db, limit=1, gateway=case.gateway) == 1
    assert visited == [ITEM, "124", "125"]
    assert current(case).last_error == "Discovery: TimeoutError"
    assert case.db.get(PerformanceSpecification, (BOARD, "125")).scheduled_for is not None


@pytest.mark.parametrize("mode,allowlist", [("off", []), ("allowlist", ["999"])])
def test_disabled_or_unlisted_work_does_not_mutate_queue_or_read_monday(case, monkeypatch, mode, allowlist):
    monkeypatch.setattr(settings, "performance_specification_mode", mode)
    monkeypatch.setattr(settings, "performance_specification_allowlist_item_ids", allowlist)
    case.gateway.fetch_target = lambda _: pytest.fail("Disabled/unlisted item read Monday")
    assert worker.enqueue_specification(case.db, BOARD, ITEM) is None
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
    assert worker.claim_specification(case.db) is None
    assert current(case) is None


@pytest.mark.parametrize("stage", ["translation", "upload"])
def test_leaving_landing_during_remote_work_retains_prior_pdf(case, stage):
    assert run(case) == ["published"]
    original = current(case).current_revision
    original_asset = case.gateway.uploads[0][0].asset_id
    # A relevant account recommendation change requires a replacement artifact.
    case.gateway.project["column_values"][1]["linked_items"][0]["name"] = "Changed Account"

    def move():
        case.gateway.target.group_id = "topics"
        case.gateway.holding["group"]["id"] = "topics"

    if stage == "translation":
        extraction = deepcopy(case.item.extracted_parameters_json)
        extraction["performanceParameters"]["Scale"] = "1 : 50"
        for fact in extraction["specificationEvidence"]:
            if fact["field"] == "Scale":
                fact.update(value="1 : 50", snippet="Scale: 1 : 50")
        extraction["sourceManifest"][0]["text"] = extraction["sourceManifest"][0]["text"].replace(
            "Scale: 1 : 100", "Scale: 1 : 50")
        case.item.extracted_parameters_json = extraction
        case.db.commit()
        case.on_translate.append(move)
    else:
        case.gateway.on_upload = move
    assert run(case) == ["excluded"]
    assert current(case).current_revision == original
    assert current(case).scheduled_for is None
    assert original_asset in {asset.asset_id for asset in case.gateway.files}
    assert case.db.get(PerformanceSpecificationArtifact, (BOARD, ITEM, original)).status == "published"
    assert not case.gateway.deletes


def test_failed_refresh_backoff_is_not_reset_by_periodic_discovery(case):
    worker.enqueue_specification(case.db, BOARD, ITEM, immediate=True)
    row = current(case)
    row.attempt_count = 3
    row.scheduled_for = worker.utc_now() + timedelta(minutes=15)
    before = row.requested_generation, worker._aware(row.scheduled_for)
    case.db.commit()
    case.gateway.fetch_target = lambda _: pytest.fail("Failed item discovery bypassed backoff")
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 0
    worker.enqueue_specification(case.db, BOARD, ITEM, reconcile=True)
    case.db.commit()
    assert (current(case).requested_generation, worker._aware(current(case).scheduled_for)) == before


def test_completed_pdf_and_approval_survive_human_move_and_unchanged_reentry(review_case):
    case = review_case
    _save(case, {label: {"operation": "specify_blank"} for label in FIELD_NAMES},
          selections={"selected_request_type": "new_enquiry"})
    _, artifact = _publish(case)
    review = case.db.get(PerformanceSpecificationReview, (BOARD, ITEM), populate_existing=True)
    source = ReviewSourceSnapshot.model_validate(artifact.source_json["reviewSource"])
    command = reviews.ApproveReview(
        expected_review_version=review.review_version, expected_source_fingerprint=review.source_fingerprint,
        idempotency_key=uuid.uuid4().hex, artifact_revision=artifact.revision,
        expected_decision_digest=review.decision_digest,
        expected_publication_identity=review.desired_publication_identity,
    )
    case.db.commit()
    result = reviews.approve_review(case.db, board_id=BOARD, item_id=ITEM,
                                   actor=ACTOR, command=command, source=source)
    worker.cancel_specification(case.db, BOARD, ITEM, reason="outside_landing_zone")
    case.db.commit()
    assert current(case).current_revision == artifact.revision
    assert current(case).review_json["left_before_completion"] is False
    assert case.db.get(PerformanceSpecificationApproval, uuid.UUID(result.approval_id)) is not None
    assert run(case) == ["unchanged"]
    review = case.db.get(PerformanceSpecificationReview, (BOARD, ITEM), populate_existing=True)
    assert str(review.current_approval_id) == result.approval_id
