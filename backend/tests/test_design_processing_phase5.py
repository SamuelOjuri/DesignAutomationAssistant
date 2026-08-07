from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.config import settings
from backend.app.db import Base
from backend.app.models import (
    DesignProcessingArtifact,
    DesignProcessingItem,
    DesignProcessingJob,
)
from backend.app.monday_client import MondayProjectBoardItem
from backend.app.services.auto_sync import utc_now
from backend.app.services.design_processing_inputs import (
    DownloadedDesignEmailAsset,
    DesignEmailAsset,
    DesignProcessingTargetSnapshot,
    compute_design_input_revision,
)
from backend.app.services.design_processing_queue import queue_design_processing_snapshot
from backend.app.services.design_processing_state import ProcessingIdentity
from backend.app.services.legacy_enquiry.analysis import analyze_downloaded_email_assets
from backend.app.services.legacy_enquiry.matching import match_projects
from backend.app.services.design_processing_worker import (
    claim_due_analysis_jobs,
    recover_expired_analysis_leases,
    run_worker_once,
)


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "legacy_enquiry" / "v1"
BOARD_ID = "1882196103"
GROUP_ID = "group_mkpbd6vy"
ITEM_ID = "2657106977"


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def golden():
    return (
        json.loads((FIXTURE_ROOT / "input.json").read_text(encoding="utf-8")),
        json.loads((FIXTURE_ROOT / "expected.json").read_text(encoding="utf-8")),
    )


def _snapshot(golden_input, *, name: str = "Human enquiry name", revision=None):
    source_path = WORKSPACE_ROOT / golden_input["sourceEmail"]["path"]
    asset = DesignEmailAsset(
        asset_id="300",
        filename=source_path.name,
        file_extension="msg",
        size=source_path.stat().st_size,
        created_at="2026-01-30T15:45:07Z",
        download_url="https://monday.invalid/private/300",
        download_requires_auth=True,
    )
    return DesignProcessingTargetSnapshot(
        board_id=BOARD_ID,
        item_id=ITEM_ID,
        group_id=GROUP_ID,
        name=name,
        email_assets=(asset,),
        input_revision=revision or compute_design_input_revision((asset,)),
    )


def _project_items(golden_input):
    result = []
    for row in golden_input["matching"]["items"]:
        columns = {column["id"]: column.get("text") for column in row["column_values"]}
        result.append(
            MondayProjectBoardItem(
                item_id=str(row["id"]),
                project_reference=row["name"],
                project_title=columns.get("text3__1") or row["name"],
                state=row["state"],
                created_date=columns.get("date9__1"),
            )
        )
    return tuple(result)


class FakeReadGateway:
    def __init__(self, snapshot, golden_input):
        self.snapshot = snapshot
        self.items = _project_items(golden_input)
        self.word_counts = golden_input["matching"]["wordHitCounts"]
        self.fetch_target_calls = 0
        self.mutation_calls = []

    def fetch_target(self, item_id):
        assert item_id == ITEM_ID
        self.fetch_target_calls += 1
        return self.snapshot

    def inspect_file_columns(self, item_id):
        return {}

    def fetch_project_word_hit_counts(self, words):
        return {word: int(self.word_counts.get(word, 0)) for word in words}

    def fetch_project_items_matching_words(self, words, *, start_date="2021-01-01"):
        return self.items

    def fetch_project_items_matching_full_text(
        self,
        project_name,
        *,
        start_date="2021-01-01",
    ):
        return self.items

    def fetch_active_project_items_since(self, *, start_date="2021-01-01"):
        return tuple(item for item in self.items if item.state == "active")

    def update_design_owned_columns(self, *args, **kwargs):
        self.mutation_calls.append(("update", args, kwargs))
        raise AssertionError("shadow analysis attempted a Monday mutation")

    def upload_design_file(self, *args, **kwargs):
        self.mutation_calls.append(("upload", args, kwargs))
        raise AssertionError("shadow analysis attempted a Monday upload")

    def delete_design_file(self, *args, **kwargs):
        self.mutation_calls.append(("delete", args, kwargs))
        raise AssertionError("shadow analysis attempted a Monday delete")


class FakeLegacyClient:
    max_attachment_workers = 1

    def __init__(self, golden_input):
        self.responses = golden_input["recordedResponses"]
        self.query_count = 0
        self.attachment_count = 0

    def should_batch_pdfs(self, pdf_files):
        return True

    def process_pdf_batch(self, pdf_files):
        self.attachment_count += 1
        return self.responses["attachmentExtractionText"]

    def process_pdf(self, pdf_content, filename):
        self.attachment_count += 1
        return self.responses["attachmentExtractionText"]

    def process_image(self, image_content, filename, image_type="ATTACHMENT"):
        self.attachment_count += 1
        return self.responses["attachmentExtractionText"]

    def query_llm(self, context, query):
        self.query_count += 1
        return (
            self.responses["parameterExtraction"]
            if query
            else self.responses["projectTitle"]
        )


class MemoryArtifactStorage:
    def __init__(self, *, fail_writes=0):
        self.objects = {}
        self.write_count = 0
        self.fail_writes = fail_writes

    def write_private(self, bucket, object_key, content, content_type):
        self.write_count += 1
        if self.fail_writes:
            self.fail_writes -= 1
            raise RuntimeError("simulated private storage outage")
        self.objects[(bucket, object_key)] = bytes(content)

    def read_private(self, bucket, object_key):
        return self.objects[(bucket, object_key)]


class SupersedingArtifactStorage(MemoryArtifactStorage):
    def __init__(self, db_session, gateway, superseding_snapshot):
        super().__init__()
        self.db_session = db_session
        self.gateway = gateway
        self.superseding_snapshot = superseding_snapshot

    def write_private(self, bucket, object_key, content, content_type):
        super().write_private(bucket, object_key, content, content_type)
        if self.write_count == 2:
            item = self.db_session.query(DesignProcessingItem).one()
            item.latest_desired_input_revision = self.superseding_snapshot.input_revision
            item.latest_desired_pipeline_version = settings.design_processing_pipeline_version
            item.supersession_requested_at = utc_now()
            self.gateway.snapshot = self.superseding_snapshot


class FixtureDownloader:
    def __init__(self, source_path):
        self.source_path = source_path
        self.call_count = 0

    def __call__(self, asset, access_token):
        assert access_token == "test-token"
        self.call_count += 1
        content = self.source_path.read_bytes()
        temporary = tempfile.NamedTemporaryFile(delete=False, suffix=".msg")
        temporary.write(content)
        temporary.close()
        return SimpleNamespace(
            temp_path=temporary.name,
            content_type="application/vnd.ms-outlook",
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
        )


def _queue(db_session, snapshot):
    now = utc_now()
    result = queue_design_processing_snapshot(
        db_session,
        snapshot,
        trigger_type="phase5_test",
        mode="shadow",
        pipeline_version=settings.design_processing_pipeline_version,
        expected_board_id=BOARD_ID,
        expected_group_id=GROUP_ID,
        now=now,
    )
    db_session.commit()
    return result


def _run_shadow(db_session, golden_input, *, storage=None):
    snapshot = _snapshot(golden_input)
    gateway = FakeReadGateway(snapshot, golden_input)
    client = FakeLegacyClient(golden_input)
    source_path = WORKSPACE_ROOT / golden_input["sourceEmail"]["path"]
    downloader = FixtureDownloader(source_path)
    storage = storage or MemoryArtifactStorage()
    result = run_worker_once(
        db_session,
        worker_id="phase5-worker",
        access_token="test-token",
        gateway=gateway,
        analysis_client=client,
        artifact_storage=storage,
        downloader=downloader,
        mode="shadow",
        claim_limit=1,
        recover_leases=False,
        heartbeat_interval_seconds=0,
    )
    return result, gateway, client, downloader, storage


def test_shadow_analysis_matches_golden_and_issues_no_monday_writes(
    db_session,
    golden,
):
    golden_input, golden_expected = golden
    queued = _queue(db_session, _snapshot(golden_input))
    assert queued.job is not None

    result, gateway, client, downloader, storage = _run_shadow(
        db_session,
        golden_input,
    )

    item = db_session.query(DesignProcessingItem).one()
    job = db_session.query(DesignProcessingJob).one()
    artifacts = db_session.query(DesignProcessingArtifact).all()
    assert result.analyzed == 1
    assert item.state == "analyzed"
    assert item.latest_analyzed_input_revision == item.latest_desired_input_revision
    assert item.latest_analyzed_pipeline_version == item.latest_desired_pipeline_version
    assert item.latest_published_input_revision is None
    assert item.extracted_parameters_json["parameters"] == golden_expected["extraction"]["parameters"]
    assert item.match_result_json["legacyDiagnostics"] == golden_expected["matching"]
    assert job.status == "completed"
    assert job.attempt_count == 1
    assert {artifact.artifact_kind for artifact in artifacts} == {"ai_data", "match_report"}
    assert all(artifact.status == "rendered" for artifact in artifacts)
    assert all(artifact.monday_asset_id is None for artifact in artifacts)
    assert len(storage.objects) == 2
    csv_content = next(
        content
        for (_, object_key), content in storage.objects.items()
        if object_key.endswith(".csv")
    )
    pdf_content = next(
        content
        for (_, object_key), content in storage.objects.items()
        if object_key.endswith(".pdf")
    )
    assert csv_content == (FIXTURE_ROOT / "ai_data.csv").read_bytes()
    assert b"Extracted Company \\(context only" in pdf_content
    assert b"Example Roofing Limited" in pdf_content
    assert b"Candidate TP Ref: 16771" in pdf_content
    assert b"Candidate TP Ref: 20442" in pdf_content
    assert b"Match: 81.8%" in pdf_content
    assert b"Match: 62.7%" in pdf_content
    assert b"Accounts" in pdf_content
    assert b"New Enq / Amend" in pdf_content
    assert b"candidate TP Ref" in pdf_content
    assert downloader.call_count == 1
    assert client.query_count == 2
    assert gateway.mutation_calls == []


def test_retry_resumes_persisted_outputs_without_repeating_gemini(db_session, golden):
    golden_input, _ = golden
    _queue(db_session, _snapshot(golden_input))
    storage = MemoryArtifactStorage(fail_writes=1)

    first, _, client, downloader, storage = _run_shadow(
        db_session,
        golden_input,
        storage=storage,
    )
    assert first.retry_wait == 1
    assert client.query_count == 2
    assert downloader.call_count == 1

    job = db_session.query(DesignProcessingJob).one()
    job.scheduled_for = utc_now() - timedelta(seconds=1)
    job.next_retry_at = job.scheduled_for
    db_session.commit()

    snapshot = _snapshot(golden_input)
    gateway = FakeReadGateway(snapshot, golden_input)
    def unexpected_download(*args, **kwargs):
        raise AssertionError("retry repeated a completed extraction download")
    second = run_worker_once(
        db_session,
        worker_id="phase5-worker-2",
        access_token="test-token",
        gateway=gateway,
        analysis_client=client,
        artifact_storage=storage,
        downloader=unexpected_download,
        mode="shadow",
        claim_limit=1,
        recover_leases=False,
        heartbeat_interval_seconds=0,
    )

    assert second.analyzed == 1
    assert client.query_count == 2
    assert db_session.query(DesignProcessingJob).one().attempt_count == 2


def test_supersession_at_final_checkpoint_never_advances_analyzed_identity(
    db_session,
    golden,
):
    golden_input, _ = golden
    original_snapshot = _snapshot(golden_input, revision="revision-a")
    superseding_snapshot = _snapshot(golden_input, revision="revision-b")
    _queue(db_session, original_snapshot)
    gateway = FakeReadGateway(original_snapshot, golden_input)
    storage = SupersedingArtifactStorage(
        db_session,
        gateway,
        superseding_snapshot,
    )
    source_path = WORKSPACE_ROOT / golden_input["sourceEmail"]["path"]

    result = run_worker_once(
        db_session,
        worker_id="superseded-worker",
        access_token="test-token",
        gateway=gateway,
        analysis_client=FakeLegacyClient(golden_input),
        artifact_storage=storage,
        downloader=FixtureDownloader(source_path),
        mode="shadow",
        claim_limit=1,
        recover_leases=False,
        heartbeat_interval_seconds=0,
    )

    item = db_session.query(DesignProcessingItem).one()
    jobs = db_session.query(DesignProcessingJob).order_by(DesignProcessingJob.created_at).all()
    artifacts = db_session.query(DesignProcessingArtifact).all()
    assert result.cancelled == 1
    assert item.latest_desired_input_revision == "revision-b"
    assert item.latest_analyzed_input_revision is None
    assert jobs[0].status == "cancelled"
    assert jobs[0].superseded_by_revision == "revision-b"
    assert jobs[1].status == "scheduled"
    assert len(
        [job for job in jobs if job.status in {"scheduled", "running", "retry_wait"}]
    ) == 1
    assert len(artifacts) == 2
    assert {artifact.input_revision for artifact in artifacts} == {"revision-a"}
    assert all(artifact.status == "rendered" for artifact in artifacts)


def test_readiness_wait_does_not_consume_normal_attempt(db_session, golden):
    golden_input, _ = golden
    snapshot = _snapshot(golden_input, name="")
    _queue(db_session, snapshot)
    gateway = FakeReadGateway(snapshot, golden_input)

    result = run_worker_once(
        db_session,
        worker_id="readiness-worker",
        access_token="test-token",
        gateway=gateway,
        analysis_client=FakeLegacyClient(golden_input),
        artifact_storage=MemoryArtifactStorage(),
        mode="shadow",
        claim_limit=1,
        recover_leases=False,
        heartbeat_interval_seconds=0,
    )

    job = db_session.query(DesignProcessingJob).one()
    assert result.readiness_wait == 1
    assert job.status == "retry_wait"
    assert job.stage == "waiting_for_name"
    assert job.readiness_check_count == 1
    assert job.attempt_count == 0


def test_analysis_worker_does_not_claim_publication_only_job(db_session):
    identity = ProcessingIdentity("revision-a", settings.design_processing_pipeline_version)
    item = DesignProcessingItem(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id=ITEM_ID,
        latest_desired_input_revision=identity.input_revision,
        latest_desired_pipeline_version=identity.pipeline_version,
        latest_analyzed_input_revision=identity.input_revision,
        latest_analyzed_pipeline_version=identity.pipeline_version,
        state="analyzed",
        warnings_json=[],
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    job = DesignProcessingJob(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id=ITEM_ID,
        trigger_type="publication_pending",
        status="scheduled",
        scheduled_for=utc_now(),
        attempt_count=0,
        readiness_check_count=0,
        max_attempts=3,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    db_session.add_all([item, job])
    db_session.commit()

    assert claim_due_analysis_jobs(
        db_session,
        worker_id="analysis-only",
        limit=1,
    ) == []
    assert db_session.get(DesignProcessingJob, job.id).status == "scheduled"


def test_project_search_failure_is_not_reported_as_no_matches(golden):
    golden_input, _ = golden

    class FailingGateway:
        def fetch_project_word_hit_counts(self, words):
            raise RuntimeError("simulated Monday search failure")

    with pytest.raises(RuntimeError, match="simulated Monday search failure"):
        match_projects(golden_input["matching"]["projectName"], FailingGateway())


def test_empty_project_title_is_retryable_instead_of_becoming_no_matches(
    tmp_path,
    golden,
):
    golden_input, _ = golden
    email_content = (
        b"From: sender@example.com\nTo: sales@example.com\n"
        b"Subject: Enquiry\nDate: Wed, 05 Aug 2026 12:00:00 +0000\n\nBody"
    )
    email_path = tmp_path / "empty-title.eml"
    email_path.write_bytes(email_content)
    source = DesignEmailAsset(
        asset_id="1",
        filename=email_path.name,
        file_extension="eml",
        size=len(email_content),
        created_at="2026-08-05T12:00:00Z",
        download_url="unused",
        download_requires_auth=False,
    )
    downloaded = DownloadedDesignEmailAsset(
        source=source,
        temp_path=str(email_path),
        content_type="message/rfc822",
        content_sha256=hashlib.sha256(email_content).hexdigest(),
        size_bytes=len(email_content),
    )
    client = FakeLegacyClient(golden_input)
    original_query_llm = client.query_llm
    client.query_llm = lambda context, query: (
        original_query_llm(context, query) if query else ""
    )

    with pytest.raises(ValueError, match="returned no title"):
        analyze_downloaded_email_assets((downloaded,), client=client)


def test_multi_email_analysis_sorts_by_numeric_asset_id(tmp_path, golden):
    golden_input, _ = golden
    downloaded_assets = []
    for asset_id, filename, body in (
        ("20", "later.eml", "LATER EMAIL BODY"),
        ("3", "earlier.eml", "EARLIER EMAIL BODY"),
    ):
        content = (
            "From: sender@example.com\nTo: sales@example.com\n"
            f"Subject: {filename}\nDate: Wed, 05 Aug 2026 12:00:00 +0000\n\n{body}"
        ).encode("utf-8")
        path = tmp_path / filename
        path.write_bytes(content)
        source = DesignEmailAsset(
            asset_id=asset_id,
            filename=filename,
            file_extension="eml",
            size=len(content),
            created_at="2026-08-05T12:00:00Z",
            download_url="unused",
            download_requires_auth=False,
        )
        downloaded_assets.append(
            DownloadedDesignEmailAsset(
                source=source,
                temp_path=str(path),
                content_type="message/rfc822",
                content_sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
            )
        )

    client = FakeLegacyClient(golden_input)
    contexts = []
    original_query_llm = client.query_llm

    def recording_query(context, query):
        contexts.append(context)
        return original_query_llm(context, query)

    client.query_llm = recording_query
    result = analyze_downloaded_email_assets(
        tuple(downloaded_assets),
        client=client,
    )

    assert [audit.asset_id for audit in result.email_content_audit] == ["3", "20"]
    assert contexts[0].index("EARLIER EMAIL BODY") < contexts[0].index(
        "LATER EMAIL BODY"
    )


def test_expired_superseded_lease_is_cancelled_and_replaced_in_sqlite(db_session):
    now = utc_now()
    identity_a = ProcessingIdentity("revision-a", settings.design_processing_pipeline_version)
    identity_b = ProcessingIdentity("revision-b", settings.design_processing_pipeline_version)
    item = DesignProcessingItem(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id=ITEM_ID,
        latest_desired_input_revision=identity_b.input_revision,
        latest_desired_pipeline_version=identity_b.pipeline_version,
        state="processing",
        warnings_json=[],
        created_at=now - timedelta(hours=2),
        updated_at=now,
    )
    job = DesignProcessingJob(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id=ITEM_ID,
        trigger_type="test",
        execution_kind="analysis",
        execution_input_revision=identity_a.input_revision,
        execution_pipeline_version=identity_a.pipeline_version,
        status="running",
        stage="extracting",
        scheduled_for=now - timedelta(hours=2),
        attempt_count=1,
        readiness_check_count=0,
        max_attempts=3,
        locked_by="dead-worker",
        locked_at=now - timedelta(hours=2),
        heartbeat_at=now - timedelta(hours=2),
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=2),
    )
    db_session.add_all([item, job])
    db_session.commit()

    recovered = recover_expired_analysis_leases(
        db_session,
        lease_timeout_seconds=60,
        now=now,
    )

    jobs = db_session.query(DesignProcessingJob).order_by(DesignProcessingJob.created_at).all()
    assert recovered == 1
    assert jobs[0].status == "cancelled"
    assert jobs[0].superseded_by_revision == "revision-b"
    assert jobs[1].status == "scheduled"
    assert len([candidate for candidate in jobs if candidate.status in {"scheduled", "running", "retry_wait"}]) == 1
