"""Active-group preparation never grants permission to publish a Monday file."""
from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy.orm import Session

from backend.app.config import settings
from backend.app.models import (PerformanceSpecificationReviewEvent, DesignProcessingReconciliationCheck,
    PerformanceSpecificationReview, PerformanceSpecificationApproval)
from backend.app.services import performance_specification as worker
from backend.app.services import specification_review as reviews
from backend.app.services import specification_review_workspace as workspace
from backend.app.services.specification_policy import eligible_specification_group_ids
from backend.tests.test_performance_specification import BOARD, ITEM, case, current, run
from backend.app.services.specification_review_contract import FIELD_NAMES, ReviewSourceSnapshot
from backend.tests.test_specification_phase4_worker import ACTOR, review_case, _save, _publish


def move(case, group):
    case.gateway.target.group_id = group
    case.gateway.holding["group"]["id"] = group


def open_review(case):
    return workspace.get_workspace(case.db, board_id=BOARD, item_id=ITEM,
        account_id=ACTOR.monday_account_id, gateway=case.gateway, editable=True, manual_publication=True)


def execute(case):
    return worker.run_specifications_once(case.db, gateway=case.gateway, client=case.client, storage=case.storage)


def prepare(case):
    open_review(case)
    assert execute(case) == ["rendered"]
    payload = open_review(case)
    assert payload["status"] == "ready_to_publish"
    return payload


def command(payload):
    return reviews.PublishReview(expected_review_version=payload["review_version"],
        expected_source_fingerprint=payload["source_fingerprint"],
        expected_decision_digest=payload["decision_digest"],
        expected_publication_identity=payload["desired_publication_identity"],
        artifact_revision=payload["pdf"]["revision"], idempotency_key=uuid.uuid4().hex)


def publish(case, request):
    return workspace.publish_specification(case.db, board_id=BOARD, item_id=ITEM,
        actor=ACTOR, command=request, gateway=case.gateway, storage=case.storage)


def change_source(case):
    case.gateway.project["column_values"][1]["linked_items"][0]["name"] += " revised"


def draft_progress(case):
    with Session(case.db.get_bind()) as db:
        identity = {"board_id": BOARD, "item_id": ITEM, "account_id": ACTOR.monday_account_id,
                    "editable": True, "manual_publication": True}
        status = workspace.get_status(db, **identity)
        payload = workspace.get_workspace(db, gateway=case.gateway, **identity)
    return [(result["status"], result["queue"]["running"], result["queue"]["phase"])
            for result in (status, payload)]


def test_draft_rendering_phase_covers_pdf_generation_and_storage(case, monkeypatch):
    move(case, "topics")
    open_review(case)
    assert draft_progress(case) == [("preparing_draft", False, None)] * 2
    render_pdf = worker.render_performance_specification_pdf
    write_pdf = case.storage.write_private
    publication_permitted = worker._publication_permitted
    during_render = []
    after_lease_expiry = []
    during_storage = []
    after_storage = []

    def render(*args, **kwargs):
        during_render.extend(draft_progress(case))
        after_expiry = worker.utc_now() + timedelta(seconds=worker.LEASE_SECONDS + 1)
        with monkeypatch.context() as clock:
            clock.setattr(workspace, "datetime", SimpleNamespace(now=lambda _: after_expiry))
            after_lease_expiry.extend(draft_progress(case))
        return render_pdf(*args, **kwargs)

    def store(*args, **kwargs):
        during_storage.extend(draft_progress(case))
        return write_pdf(*args, **kwargs)

    def check_publication(*args, **kwargs):
        after_storage.extend(draft_progress(case))
        return publication_permitted(*args, **kwargs)

    monkeypatch.setattr(worker, "render_performance_specification_pdf", render)
    monkeypatch.setattr(case.storage, "write_private", store)
    monkeypatch.setattr(worker, "_publication_permitted", check_publication)

    assert execute(case) == ["rendered"]
    assert during_render == during_storage == [("preparing_draft", True, "rendering")] * 2
    assert after_lease_expiry == [("preparing_draft", False, None)] * 2
    assert after_storage == [("preparing_draft", True, None)] * 2
    assert draft_progress(case) == [("ready_to_publish", False, None)] * 2


def test_scheduled_recheck_reuses_existing_draft_without_rendering_phase(case, monkeypatch):
    move(case, "topics")
    revision = prepare(case)["pdf"]["revision"]
    record = current(case)
    assert record.requested_generation == record.completed_generation
    record.scheduled_for = worker.utc_now()
    case.db.commit()
    read_pdf = case.storage.read_private
    during_recheck = []

    def read(*args, **kwargs):
        during_recheck.extend(draft_progress(case))
        return read_pdf(*args, **kwargs)

    monkeypatch.setattr(case.storage, "read_private", read)

    assert execute(case) == ["rendered"]
    assert during_recheck == [("preparing_draft", True, None)] * 2
    assert open_review(case)["pdf"]["revision"] == revision
    assert draft_progress(case) == [("ready_to_publish", False, None)] * 2


def test_scheduled_draft_recheck_reads_the_item_target_once(case, monkeypatch):
    move(case, "topics")
    prepare(case)
    record = current(case)
    record.scheduled_for = worker.utc_now()
    case.db.commit()
    reads = []
    fetch_target = case.gateway.fetch_target
    monkeypatch.setattr(case.gateway, "fetch_target", lambda item_id: reads.append(item_id) or fetch_target(item_id))

    assert execute(case) == ["rendered"]
    # The draft decision reuses the source read's target; upload boundaries still read again.
    assert reads == [ITEM]


def test_active_first_publication_without_corrections_is_explicit_and_idempotent(case):
    move(case, "topics")
    assert run(case) == ["rendered"]
    assert current(case).current_revision is None
    payload = prepare(case)
    assert not case.gateway.uploads
    request = command(payload)
    first = publish(case, request)
    assert publish(case, request) == first
    assert execute(case) == ["published"]
    assert len(case.gateway.uploads) == 1
    assert current(case).requested_publication_revision is None
    # Replaying a completed command cannot authorize a second upload.
    assert publish(case, request) == first
    assert run(case) == ["rendered"]
    assert len(case.gateway.uploads) == 1
    events = case.db.query(PerformanceSpecificationReviewEvent).filter_by(command_type="publish").all()
    assert len(events) == 1 and events[0].actor_monday_user_id == ACTOR.monday_user_id
    assert request.artifact_revision in events[0].explanation


def test_sources_refresh_and_saved_edits_prepare_drafts_without_replacing_pdf(case):
    assert run(case) == ["published"]
    previous = current(case).current_revision
    move(case, "topics")
    open_review(case)
    assert execute(case) == ["rendered"]
    change_source(case)
    assert run(case) == ["rendered"]
    payload = open_review(case)
    assert payload["status"] == "ready_to_publish"
    assert payload["published_pdf"]["revision"] == previous
    assert payload["publication_changes"]
    assert workspace.get_status(case.db, board_id=BOARD, item_id=ITEM, account_id=ACTOR.monday_account_id,
        editable=True, manual_publication=True)["status"] == "ready_to_publish"
    workspace.refresh_recommendations(case.db, board_id=BOARD, item_id=ITEM, actor=ACTOR,
        command=workspace.RefreshReview(expected_review_version=payload["review_version"],
            expected_source_fingerprint=payload["source_fingerprint"], idempotency_key=uuid.uuid4().hex), gateway=case.gateway)
    workspace.save_decisions(case.db, board_id=BOARD, item_id=ITEM, actor=ACTOR,
        command=reviews.SaveReviewDecisions(expected_review_version=payload["review_version"],
            expected_source_fingerprint=payload["source_fingerprint"], idempotency_key=uuid.uuid4().hex,
            field_operations={"Decking": {"operation": "specify_value", "display_value": "Reviewer deck"}}), gateway=case.gateway)
    assert execute(case) == ["rendered"]
    assert len(case.gateway.uploads) == 1 and not case.gateway.deletes
    payload = open_review(case)
    publish(case, command(payload))
    assert execute(case) == ["published"]
    assert current(case).current_revision != previous
    assert len(case.gateway.uploads) == 2 and case.gateway.deletes


@pytest.mark.parametrize("when", ["before_request", "after_request", "during_upload"])
def test_changed_sources_cannot_use_previous_publication_permission(case, when):
    move(case, "topics")
    payload = prepare(case)
    request = command(payload)
    if when == "before_request":
        change_source(case)
        with pytest.raises(reviews.ReviewConflict):
            publish(case, request)
    else:
        publish(case, request)
        if when == "after_request":
            change_source(case)
        else:
            case.gateway.on_upload = lambda: change_source(case)
        assert execute(case) == (["rendered"] if when == "after_request" else ["superseded"])
    assert current(case).current_revision is None
    assert current(case).requested_publication_revision is None
    case.gateway.on_upload = None
    assert run(case) == ["rendered"]
    assert current(case).current_revision is None


def test_saved_edits_revoke_queued_publication(case):
    move(case, "topics")
    payload = prepare(case)
    publish(case, command(payload))
    workspace.save_decisions(case.db, board_id=BOARD, item_id=ITEM, actor=ACTOR,
        command=reviews.SaveReviewDecisions(expected_review_version=payload["review_version"],
            expected_source_fingerprint=payload["source_fingerprint"], idempotency_key=uuid.uuid4().hex,
            field_operations={"Decking": {"operation": "specify_blank"}}), gateway=case.gateway)
    assert current(case).requested_publication_revision is None
    assert execute(case) == ["rendered"] and not case.gateway.uploads


def test_landing_move_during_analysis_does_not_publish_in_active_group(case):
    case.on_translate.append(lambda: move(case, "topics"))
    assert run(case) == ["rendered"]
    assert not case.gateway.uploads


@pytest.mark.parametrize("group", ["completed", "unconfigured"])
def test_excluded_group_cancels_publication_and_keeps_prior_pdf(case, monkeypatch, group):
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "completed")
    monkeypatch.setattr(settings, "auto_sync_active_group_ids", "topics,completed")
    assert "completed" not in eligible_specification_group_ids()
    assert run(case) == ["published"]
    previous = current(case).current_revision
    move(case, "topics")
    change_source(case)
    payload = prepare(case)
    publish(case, command(payload))
    move(case, group)
    assert execute(case) == ["excluded"]
    assert current(case).current_revision == previous
    assert current(case).requested_publication_revision is None
    assert len(case.gateway.uploads) == 1 and not case.gateway.deletes


def test_cancelled_active_record_is_discovered_but_not_published(case):
    move(case, "topics")
    worker.enqueue_specification(case.db, BOARD, ITEM, immediate=True)
    worker.cancel_specification(case.db, BOARD, ITEM)
    now = worker.utc_now()
    current(case).checked_at = now - timedelta(hours=2)
    case.db.add(DesignProcessingReconciliationCheck(board_id=BOARD, item_id=ITEM,
        last_group_id="topics", last_attempted_at=now, last_checked_at=now, last_outcome="ignored", last_reason="ready"))
    case.db.commit()
    assert worker.discover_specifications(case.db, gateway=case.gateway) == 1
    assert execute(case) == ["rendered"]
    assert not case.gateway.uploads


def test_failed_upload_retains_request_and_retries_same_pdf(case):
    move(case, "topics")
    payload = prepare(case)
    publish(case, command(payload))
    upload = case.gateway.upload
    def fail(*args):
        raise TimeoutError("upload temporarily unavailable")
    case.gateway.upload = fail
    assert execute(case) == ["failed"]
    assert current(case).requested_publication_revision == payload["pdf"]["revision"]
    case.gateway.upload = upload
    assert run(case) == ["published"]
    assert len(case.gateway.uploads) == 1


def test_model_change_revokes_exact_publication_request(case):
    move(case, "topics")
    payload = prepare(case)
    publish(case, command(payload))
    case.client = replace(case.client, model=case.client.model + "-updated")
    assert execute(case) == ["rendered"]
    assert current(case).prepared_revision != payload["pdf"]["revision"]
    assert current(case).requested_publication_revision is None
    assert not case.gateway.uploads


@pytest.mark.parametrize("active", [True, False])
def test_new_model_pdf_cannot_inherit_previous_pdf_approval(review_case, active):
    case = review_case
    _save(case, {label: {"operation": "specify_blank"} for label in FIELD_NAMES},
        selections={"selected_request_type": "new_enquiry"})
    _, artifact = _publish(case)
    payload = open_review(case)
    source = ReviewSourceSnapshot.model_validate(artifact.source_json["reviewSource"])
    case.db.commit()
    approved = reviews.approve_review(case.db, board_id=BOARD, item_id=ITEM, actor=ACTOR,
        command=reviews.ApproveReview(**command(payload).model_dump()),
        source=source)
    uploads = len(case.gateway.uploads)
    if active:
        move(case, "topics")
    case.client = replace(case.client, model=case.client.model + "-updated")
    assert run(case) == (["rendered"] if active else ["published"])
    review = case.db.get(PerformanceSpecificationReview, (BOARD, ITEM), populate_existing=True)
    assert review.current_approval_id is None
    assert case.db.get(PerformanceSpecificationApproval, uuid.UUID(approved.approval_id)) is not None
    if active:
        assert len(case.gateway.uploads) == uploads
        payload = open_review(case)
        assert payload["status"] == "ready_to_publish"
        publish(case, command(payload))
        assert execute(case) == ["published"]
    assert open_review(case)["status"] == "awaiting_final_review"
