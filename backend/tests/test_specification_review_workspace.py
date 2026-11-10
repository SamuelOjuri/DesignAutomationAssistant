"""Phase 6 source, publication and concurrency boundaries with remote reads stubbed."""

from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest

from backend.app.config import settings
from backend.app.models import (
    PerformanceSpecification, PerformanceSpecificationArtifact,
    PerformanceSpecificationReview, PerformanceSpecificationReviewEvent, PerformanceSpecificationApproval,
)
from backend.app.services import specification_review as persistence
from backend.app.services import specification_review_workspace as workspace
from backend.app.services.specification_review_artifacts import build_review_artifact_metadata
from backend.app.services.specification_review_contract import FIELD_NAMES
from backend.app.services.performance_specification_sources import SpecificationSourceWaiting
from backend.tests.test_specification_review_persistence import (
    ACCOUNT, ACTOR, BOARD, ITEM, review_db, save_command, source_snapshot,
)


@pytest.fixture(autouse=True)
def specification_rollout(monkeypatch):
    monkeypatch.setattr(settings, "performance_specification_enabled", True)
    monkeypatch.setattr(settings, "performance_specification_mode", "enabled")
    monkeypatch.setattr(settings, "performance_specification_allowlist_item_ids", [ITEM])


@pytest.fixture()
def source_reader(review_db, monkeypatch):
    current = SimpleNamespace(source=source_snapshot(), calls=0, callback=None)

    def resolve(*args, **kwargs):
        assert not review_db.in_transaction(), "Remote sources held a DB transaction"
        current.calls += 1
        if current.callback:
            current.callback()
        return {"reviewSource": current.source.model_dump(mode="json")}

    monkeypatch.setattr(workspace, "resolve_specification_sources", resolve)
    return current


def open_workspace(db, *, editable=True, item_id=ITEM):
    return workspace.get_workspace(db, board_id=BOARD, item_id=item_id,
        account_id=ACCOUNT, gateway=object() if editable else None, editable=editable)


def save(db, command):
    return workspace.save_decisions(db, board_id=BOARD, item_id=ITEM,
                                   actor=ACTOR, command=command, gateway=object())


def refresh(db, *, fingerprint="sources-1", key="refresh-key", version=0):
    return workspace.refresh_recommendations(db, board_id=BOARD, item_id=ITEM,
        actor=ACTOR, command=workspace.RefreshReview(expected_review_version=version,
            expected_source_fingerprint=fingerprint, idempotency_key=key), gateway=object())


def test_workspace_returns_all_fields_evidence_and_versions_without_tasks(review_db, source_reader):
    payload = open_workspace(review_db)
    assert set(payload["fields"]) == set(FIELD_NAMES)
    assert payload["source_fingerprint"] == "sources-1"
    assert payload["review_version"] == 0
    assert payload["fields"]["Decking"]["effective"]["source_label"] == "Email Context"
    assert payload["candidates"]["status"] == "available"
    assert payload["status"] == "regenerating_pdf"
    assert source_reader.calls == 1
    assert not review_db.in_transaction()


def test_unknown_item_waits_without_registration_or_remote_lookup(review_db, source_reader):
    payload = open_workspace(review_db, item_id="456")
    assert payload["status"] == "source_waiting"
    assert payload["fields"] == {}
    assert source_reader.calls == 0
    assert review_db.get(PerformanceSpecification, (BOARD, "456")) is None


def test_read_only_and_polling_use_retained_sources_without_remote_reads(review_db, source_reader):
    source_reader.callback = lambda: pytest.fail("Read-only/polling called remote sources")
    payload = open_workspace(review_db, editable=False)
    assert payload["status"] == "read_only"
    assert len(payload["fields"]) == 18
    status = workspace.get_status(review_db, board_id=BOARD, item_id=ITEM,
                                  account_id=ACCOUNT, editable=True)
    assert status["source_fingerprint"] == "sources-1"
    assert "fields" not in status and "source" not in status


def test_save_refreshes_changed_sources_and_rejects_old_command(review_db, source_reader):
    source_reader.source = source_snapshot("sources-2")
    with pytest.raises(persistence.ReviewConflict, match="Recommendations changed") as caught:
        save(review_db, save_command(field_operations={"Decking": {"operation": "specify_blank"}}))
    assert caught.value.code == "source_changed"
    review = review_db.get(PerformanceSpecificationReview, (BOARD, ITEM))
    assert review.source_fingerprint == "sources-2"
    assert review.review_version == 0 and review.field_decisions_json == {}


def test_save_retry_returns_original_receipt_after_later_edits_without_another_read(review_db, source_reader):
    command = save_command(key="save-key", field_operations={"Decking": {"operation": "specify_blank"}})
    original = save(review_db, command)
    save(review_db, save_command(version=1, field_operations={"Company": {"operation": "specify_blank"}}))
    source_reader.callback = lambda: pytest.fail("Identical retry read sources again")
    assert save(review_db, command) == original
    assert review_db.get(PerformanceSpecificationReview, (BOARD, ITEM)).review_version == 2


def test_delayed_source_read_cannot_replace_concurrent_reviewer_selection(review_db, source_reader):
    def concurrent_save():
        source_reader.callback = None
        persistence.save_review_decisions(review_db, board_id=BOARD, item_id=ITEM,
            actor=ACTOR, command=save_command(selections={"selected_request_type": "new_enquiry"}),
            source=source_snapshot())

    source_reader.callback = concurrent_save
    with pytest.raises(persistence.ReviewConflict) as caught:
        open_workspace(review_db)
    assert caught.value.code == "stale_review"
    review = review_db.get(PerformanceSpecificationReview, (BOARD, ITEM))
    assert review.selected_request_type == "new_enquiry" and review.review_version == 1


def test_identical_in_flight_save_returns_committed_receipt_after_source_cas_conflict(review_db, source_reader):
    command = save_command(key="same-in-flight-command", field_operations={"Decking": {"operation": "specify_blank"}})
    receipt = []
    def concurrent_retry():
        source_reader.callback = None
        receipt.append(persistence.save_review_decisions(review_db, board_id=BOARD, item_id=ITEM,
            actor=ACTOR, command=command, source=source_snapshot()))
    source_reader.callback = concurrent_retry
    assert save(review_db, command) == receipt[0]
    assert review_db.query(PerformanceSpecificationReviewEvent).filter_by(command_type="save_decisions").count() == 1


def test_refresh_queues_changed_source_once_and_records_immutable_idempotent_receipt(review_db, source_reader):
    source_reader.source = source_snapshot("sources-2")
    first = refresh(review_db)
    generation = review_db.get(PerformanceSpecification, (BOARD, ITEM)).requested_generation
    review_db.rollback()
    source_reader.source = source_snapshot("sources-3")
    refresh(review_db, fingerprint="sources-2", key="another-refresh")
    later_generation = review_db.get(PerformanceSpecification, (BOARD, ITEM)).requested_generation
    review_db.rollback()
    source_reader.callback = lambda: pytest.fail("Retried refresh read sources")
    assert refresh(review_db) == first
    assert later_generation == generation + 1
    assert review_db.get(PerformanceSpecification, (BOARD, ITEM)).requested_generation == later_generation
    events = review_db.query(PerformanceSpecificationReviewEvent).filter_by(command_type="refresh").all()
    assert len(events) == 2 and all(event.actor_monday_user_id == ACTOR.monday_user_id for event in events)
    review_db.rollback()
    with pytest.raises(persistence.ReviewConflict) as caught:
        refresh(review_db, fingerprint="sources-3")
    assert caught.value.code == "idempotency_conflict"


def test_unchanged_refresh_records_receipt_without_an_extra_generation(review_db, source_reader):
    before = review_db.get(PerformanceSpecification, (BOARD, ITEM)).requested_generation
    review_db.rollback()
    result = refresh(review_db)
    assert not result.changed
    assert review_db.get(PerformanceSpecification, (BOARD, ITEM)).requested_generation == before


@pytest.mark.parametrize("fingerprint", ["sources-1", "sources-2"])
def test_successful_refresh_clears_source_error_observation_and_preserves_worker_diagnostics(review_db, source_reader, fingerprint):
    spec = review_db.get(PerformanceSpecification, (BOARD, ITEM))
    spec.review_json = {"reviewApiSourceState": "source_unavailable", "issues": {"Decking": "Keep this diagnostic"}}
    review_db.commit()
    source_reader.source = source_snapshot(fingerprint)
    result = refresh(review_db)
    assert result.changed == (fingerprint == "sources-2")
    spec = review_db.get(PerformanceSpecification, (BOARD, ITEM))
    assert spec.review_json == {"issues": {"Decking": "Keep this diagnostic"}}
    review_db.rollback()
    assert workspace.get_status(review_db, board_id=BOARD, item_id=ITEM,
        account_id=ACCOUNT, editable=True)["status"] != "source_unavailable"


@pytest.fixture()
def published(review_db, source_reader):
    save(review_db, save_command(selections={"selected_request_type": "new_enquiry"},
        field_operations={label: {"operation": "specify_blank"} for label in FIELD_NAMES}))
    review = review_db.get(PerformanceSpecificationReview, (BOARD, ITEM))
    metadata = build_review_artifact_metadata(persistence.review_state(review), source_reader.source)
    content = b"%PDF-1.7\nreviewed document"
    artifact = PerformanceSpecificationArtifact(board_id=BOARD, item_id=ITEM, revision="revision-current",
        source_json={"review": metadata, "reviewSource": source_reader.source.model_dump(mode="json")},
        translation_json={}, model_version="test", filename="reviewed.pdf", storage_bucket="private",
        storage_object_key="reviewed.pdf", content_sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content), monday_asset_id="91", status="published")
    review_db.add(artifact)
    spec = review_db.get(PerformanceSpecification, (BOARD, ITEM))
    spec.completed_generation = spec.requested_generation
    spec.current_revision = artifact.revision
    spec.lease_token = spec.lease_until = None
    review_db.commit()

    class Storage:
        callback = None
        def read_private(self, bucket, key):
            assert not review_db.in_transaction()
            assert (bucket, key) == ("private", "reviewed.pdf")
            if self.callback:
                self.callback()
            return self.content

    class Gateway:
        callback = None
        def inspect_files(self, item_id):
            assert not review_db.in_transaction()
            if self.callback:
                self.callback()
            return self.assets

    storage = Storage()
    storage.content = content
    gateway = Gateway()
    gateway.assets = [SimpleNamespace(asset_id="91", filename="reviewed.pdf", size_bytes=len(content))]
    command = persistence.ApproveReview(expected_review_version=1, expected_source_fingerprint="sources-1",
        expected_decision_digest=metadata["decision_digest"], expected_publication_identity=metadata["publication_identity"],
        artifact_revision="revision-current", idempotency_key="approve-key")
    return SimpleNamespace(db=review_db, storage=storage, gateway=gateway, command=command, content=content)


def approve(case):
    return workspace.approve_specification(case.db, board_id=BOARD, item_id=ITEM,
        actor=ACTOR, command=case.command, gateway=case.gateway, storage=case.storage)


def test_approval_verifies_sources_asset_and_storage_then_completes_exact_revision(published):
    result = approve(published)
    assert result.approval_id
    status = workspace.get_status(published.db, board_id=BOARD, item_id=ITEM, account_id=ACCOUNT, editable=True)
    assert status["status"] == "complete"
    assert status["approval"]["artifact_revision"] == "revision-current"


@pytest.mark.parametrize("failure", ["missing", "duplicate", "wrong_id", "wrong_size", "unknown_size", "corrupt_bytes"])
def test_approval_fails_closed_on_unverified_asset_or_private_pdf(published, failure):
    asset = published.gateway.assets[0]
    if failure == "missing":
        published.gateway.assets = []
    elif failure == "duplicate":
        published.gateway.assets.append(deepcopy(asset))
    elif failure == "wrong_id":
        asset.asset_id = "999"
    elif failure == "wrong_size":
        asset.size_bytes += 1
    elif failure == "unknown_size":
        asset.size_bytes = None
    else:
        published.storage.content = b"corrupted"
    with pytest.raises(persistence.ReviewConflict) as caught:
        approve(published)
    assert caught.value.code == "publication_failed"
    assert published.db.get(PerformanceSpecificationReview, (BOARD, ITEM)).current_approval_id is None


def test_sources_changing_during_pdf_verification_prevent_approval(published, source_reader):
    published.storage.callback = lambda: setattr(source_reader, "source", source_snapshot("sources-2"))
    with pytest.raises(persistence.ReviewConflict) as caught:
        approve(published)
    assert caught.value.code == "source_changed"
    assert published.db.get(PerformanceSpecificationReview, (BOARD, ITEM)).current_approval_id is None


@pytest.mark.parametrize("change", ["master_pause", "off", "shadow", "allowlist_removed"])
def test_approval_rollout_gate_runs_before_remote_verification(published, source_reader, monkeypatch, change):
    if change == "master_pause":
        monkeypatch.setattr(settings, "performance_specification_enabled", False)
    elif change == "allowlist_removed":
        monkeypatch.setattr(settings, "performance_specification_mode", "allowlist")
        monkeypatch.setattr(settings, "performance_specification_allowlist_item_ids", ["999"])
    else:
        monkeypatch.setattr(settings, "performance_specification_mode", change)
    source_reader.callback = lambda: pytest.fail("Disabled completion must not read sources")
    published.storage.callback = lambda: pytest.fail("Disabled completion must not read storage")
    published.gateway.callback = lambda: pytest.fail("Disabled completion must not inspect Monday assets")
    with pytest.raises(persistence.ReviewConflict) as caught:
        approve(published)
    assert caught.value.code == "reviewer_forbidden"
    assert published.db.query(PerformanceSpecificationApproval).count() == 0


@pytest.mark.parametrize("phase", ["first_source", "storage", "assets", "final_source"])
@pytest.mark.parametrize("change", ["master_pause", "shadow", "allowlist_removed"])
def test_rollout_changes_during_remote_verification_cannot_complete(published, source_reader, monkeypatch, phase, change):
    monkeypatch.setattr(settings, "performance_specification_mode", "allowlist")
    calls = {"sources": 0, "changed": False}

    def change_rollout():
        calls["changed"] = True
        if change == "master_pause":
            monkeypatch.setattr(settings, "performance_specification_enabled", False)
        elif change == "shadow":
            monkeypatch.setattr(settings, "performance_specification_mode", "shadow")
        else:
            monkeypatch.setattr(settings, "performance_specification_allowlist_item_ids", ["999"])

    def sources_callback():
        calls["sources"] += 1
        if phase == "first_source" and calls["sources"] == 1:
            change_rollout()
        elif phase == "final_source" and calls["sources"] == 2:
            change_rollout()

    source_reader.callback = sources_callback
    if phase == "storage":
        published.storage.callback = change_rollout
    elif phase == "assets":
        published.gateway.callback = change_rollout
    with pytest.raises(persistence.ReviewConflict) as caught:
        approve(published)
    assert calls["changed"]
    assert caught.value.code == "reviewer_forbidden"
    assert published.db.query(PerformanceSpecificationApproval).count() == 0
    assert published.db.get(PerformanceSpecificationReview, (BOARD, ITEM)).current_approval_id is None
    assert published.db.query(PerformanceSpecificationReviewEvent).filter_by(command_type="approve").count() == 0


def test_paused_publisher_still_saves_pending_reviewer_decisions(review_db, source_reader, monkeypatch):
    monkeypatch.setattr(settings, "performance_specification_enabled", False)
    result = save(review_db, save_command(field_operations={"Decking": {"operation": "specify_blank"}}))
    assert result.changed and result.review_version == 1
    spec = review_db.get(PerformanceSpecification, (BOARD, ITEM))
    assert spec.requested_generation > spec.completed_generation
    assert review_db.get(PerformanceSpecificationReview, (BOARD, ITEM)).field_decisions_json["Decking"]["decision"]["operation"] == "specify_blank"


def test_new_queued_generation_prevents_approval(published):
    spec = published.db.get(PerformanceSpecification, (BOARD, ITEM))
    spec.requested_generation += 1
    published.db.commit()
    with pytest.raises(persistence.ReviewConflict) as caught:
        approve(published)
    assert caught.value.code == "stale_review"


def test_current_extraction_waiting_invalidates_completion_and_retains_previous_pdf(published, monkeypatch):
    approve(published)
    def waiting(*args, **kwargs):
        raise SpecificationSourceWaiting("Current email has not been analyzed")
    monkeypatch.setattr(workspace, "resolve_specification_sources", waiting)
    payload = open_workspace(published.db)
    assert payload["status"] == "source_waiting" and payload["approval"] is None
    assert payload["published_pdf"]["revision"] == "revision-current"
    assert workspace.get_status(published.db, board_id=BOARD, item_id=ITEM,
        account_id=ACCOUNT, editable=True)["status"] == "source_waiting"


@pytest.mark.parametrize("operation", ["save", "approve", "refresh"])
def test_waiting_sources_on_a_mutation_immediately_invalidate_completion(published, monkeypatch, operation):
    approve(published)
    def waiting(*args, **kwargs):
        raise SpecificationSourceWaiting("A new client message needs analysis")
    monkeypatch.setattr(workspace, "resolve_specification_sources", waiting)
    with pytest.raises(persistence.ReviewConflict) as caught:
        if operation == "save":
            save(published.db, save_command(version=1, key="new-save",
                field_operations={"Decking": {"operation": "specify_value", "display_value": "Timber"}}))
        elif operation == "approve":
            command = published.command.model_copy(update={"idempotency_key": "new-approval"})
            workspace.approve_specification(published.db, board_id=BOARD, item_id=ITEM,
                actor=ACTOR, command=command, gateway=published.gateway, storage=published.storage)
        else:
            refresh(published.db, version=1)
    assert caught.value.code == "source_waiting"
    status = workspace.get_status(published.db, board_id=BOARD, item_id=ITEM,
        account_id=ACCOUNT, editable=True)
    assert status["approval"] is None and status["status"] == "source_waiting"
    assert status["published_pdf"]["revision"] == "revision-current"


def test_retained_pdf_is_scoped_and_integrity_verified(published):
    filename, content = workspace.read_pdf(published.db, board_id=BOARD, item_id=ITEM,
        account_id=ACCOUNT, revision="revision-current", storage=published.storage)
    assert filename == "reviewed.pdf" and content == published.content
    with pytest.raises(persistence.ReviewConflict) as caught:
        workspace.read_pdf(published.db, board_id=BOARD, item_id=ITEM,
            account_id="999", revision="revision-current", storage=published.storage)
    assert caught.value.code == "reviewer_forbidden"
    with pytest.raises(persistence.ReviewConflict):
        workspace.read_pdf(published.db, board_id=BOARD, item_id="456",
            account_id=ACCOUNT, revision="revision-current", storage=published.storage)
