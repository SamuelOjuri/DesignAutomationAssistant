"""Observable speed changes preserve ordering, evidence and execution fences."""
from datetime import datetime, timedelta, timezone
import hashlib
from threading import Barrier
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.models import GeminiRequestLease, SourceTranscriptionCache
from backend.app.services.legacy_enquiry.analysis import analyze_downloaded_email_assets
from backend.app.services.specification_semantics import PDF_FIELD_NAMES, SpecificationEvidence
from backend.app.services.source_progress import SourceProgress
from backend.scripts.purge_processing_cache import purge_expired
from backend.app.services import design_processing_execution as execution
from backend.tests.pdf_helpers import pdf_bytes
from backend.tests.test_source_archives import downloaded, parameters


def _send_progress_then_result(connection):
    connection.send(("progress", {"phase": "reading_sources", "source_counts": {"total": 1, "read": 0}}))
    connection.send(("progress", {"phase": "reading_sources", "source_counts": {"total": 1, "read": 1}}))
    connection.send(("progress", {"phase": "extracting_fields", "source_counts": {"total": 1, "read": 1}}))
    connection.send((True, {"done": True}))
    connection.close()


def test_child_progress_is_coalesced_and_final_result_still_checked():
    progress, checks = [], []
    result = execution._run_in_process(_send_progress_then_result, (),
        check_current=lambda: checks.append(True), on_progress=progress.append, poll_seconds=0.05)
    assert result == {"done": True} and checks
    assert [event["phase"] for event in progress] == ["reading_sources", "extracting_fields"]
    assert progress[-1]["source_counts"]["read"] == 1


def test_independent_supplementary_pdfs_overlap_and_progress_does_not_expose_source_text(tmp_path):
    barrier = Barrier(2)
    events, contexts = [], []
    def pdf_reader(content, filename):
        barrier.wait(timeout=5)
        return "Private drawing: " + filename
    assets = [downloaded(tmp_path, "request.eml", b"From: client@example.com\nSubject: Roof\n\nContact: Alex", "1"),
              downloaded(tmp_path, "a.pdf", pdf_bytes(), "2"), downloaded(tmp_path, "b.pdf", pdf_bytes(), "3")]
    client = SimpleNamespace(max_attachment_workers=4, process_pdf=pdf_reader,
        extract_design_parameters=lambda text: contexts.append(text) or parameters(), query_llm=lambda *args: "Roof")
    result = analyze_downloaded_email_assets(assets, client=client, on_progress=events.append)
    assert [entry["sourceId"] for entry in result.source_manifest] == ["asset:2", "asset:3", "asset:1:body"]
    assert contexts[0].index("a.pdf") < contexts[0].index("b.pdf")
    assert events[-1] == {"phase": "extracting_fields", "source_counts": {
        "total": 3, "read": 3, "skipped": 0, "review": 0, "failed": 0}}
    assert all(set(event) == {"phase", "source_counts"} for event in events)
    assert "Private drawing" not in str(events) and "Alex" not in str(events)
    assert set(result.stage_timings) == {"reading_sources_seconds", "extracting_fields_seconds", "project_title_seconds", "analysis_seconds"}


def test_verified_drawing_title_avoids_a_second_project_name_model_call(tmp_path):
    mail = b"From: client@example.com\nSubject: Roof\n\nDrawing Title: Roof A"
    extraction = parameters()
    extraction.drawing_title = "Roof A"
    extraction.specification_assessed_fields = list(PDF_FIELD_NAMES)
    extraction.specification_evidence = [SpecificationEvidence(field="Drawing Title", value="Roof A",
        source_id="asset:1:body", location="body", snippet="Drawing Title: Roof A", scope=None,
        issuer=None, role="requested", request_role="direct_request", message_date=None,
        rationale="Literal supplied project title")]
    def unexpected(*args):
        pytest.fail("A verified title must not require another project-name request")
    result = analyze_downloaded_email_assets([downloaded(tmp_path, "request.eml", mail, "1")],
        client=SimpleNamespace(extract_design_parameters=lambda _: extraction, query_llm=unexpected))
    assert result.project_name == "Roof A"


def test_progress_counts_distinct_sources_and_excludes_archive_containers():
    events = []
    progress = SourceProgress(events.append)
    progress.record({"sourceId": "archive", "status": "expanded:archive", "text": "PRIVATE"})
    progress.record({"sourceId": "child", "status": "pending"})
    progress.record({"sourceId": "child", "status": "skipped:unsupported_format"})
    assert events[-1]["source_counts"] == {"total": 1, "read": 0, "skipped": 1, "review": 0, "failed": 0}
    assert "PRIVATE" not in str(events)


def test_expiry_cleanup_is_bounded_and_dry_run_preserves_live_rows(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "cleanup.sqlite"))
    for table in (SourceTranscriptionCache.__table__, GeminiRequestLease.__table__):
        table.create(engine)
    factory = sessionmaker(bind=engine)
    now = datetime.now(timezone.utc)
    with factory() as db:
        for index in range(3):
            db.add(SourceTranscriptionCache(scope_key="scope", cache_key=str(index), content_sha256="x", text_sha256="x",
                text="private", expires_at=now + timedelta(days=1) if index == 2 else now - timedelta(days=1)))
        db.add(GeminiRequestLease(token="live", scope_key="scope", started_at=now, expires_at=now + timedelta(minutes=5)))
        db.commit()
        assert purge_expired(db, limit=1, dry_run=True, now=now)["expired_cache_rows"] == 1
        assert db.query(SourceTranscriptionCache).count() == 3
        assert purge_expired(db, limit=1, now=now)["expired_cache_rows"] == 1
        assert db.query(SourceTranscriptionCache).count() == 2
        assert db.query(GeminiRequestLease).count() == 1
    engine.dispose()
