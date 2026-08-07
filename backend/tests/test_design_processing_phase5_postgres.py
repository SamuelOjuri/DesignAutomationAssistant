from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from queue import Queue
import threading
import uuid

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.models import DesignProcessingItem, DesignProcessingJob
from backend.app.services.design_processing_worker import (
    claim_due_analysis_jobs,
    recover_expired_analysis_leases,
)


BOARD_ID = "1882196103"
PIPELINE_VERSION = "phase5-postgres-test"


def _create_schema_engine(database_url: str, schema: str):
    engine = create_engine(database_url, pool_pre_ping=True)

    def configure_connection(
        dbapi_connection,
        _connection_record,
        _connection_proxy,
    ) -> None:
        previous_autocommit = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        try:
            with dbapi_connection.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema}", public')
                cursor.execute("SET statement_timeout TO 3000")
                cursor.execute("SET lock_timeout TO 1000")
        finally:
            dbapi_connection.autocommit = previous_autocommit

    event.listen(engine, "checkout", configure_connection)
    return engine


@pytest.fixture(scope="module")
def postgres_worker_database():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL worker tests")

    schema = f"design_processing_phase5_{uuid.uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    previous_database_url = os.environ.get("DATABASE_URL")
    test_engine = None
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))

        os.environ["DATABASE_URL"] = database_url
        test_engine = _create_schema_engine(database_url, schema)
        with test_engine.begin() as connection:
            assert (
                connection.execute(text("SELECT current_schema()")).scalar_one()
                == schema
            )
            alembic_config = Config("backend/alembic.ini")
            alembic_config.attributes["connection"] = connection
            alembic_config.attributes["version_table_schema"] = schema
            command.upgrade(
                alembic_config,
                "0010_design_processing_queue",
            )
        Session = sessionmaker(
            bind=test_engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
        with test_engine.connect() as connection:
            assert {
                "design_processing_items",
                "design_processing_jobs",
                "design_processing_artifacts",
            } <= set(inspect(connection).get_table_names(schema=schema))
        yield test_engine, Session
    finally:
        if test_engine is not None:
            test_engine.dispose()
        if previous_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_database_url
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


@pytest.fixture()
def postgres_worker_db(postgres_worker_database):
    engine, Session = postgres_worker_database
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE design_processing_artifacts, design_processing_jobs, "
                "design_processing_items CASCADE"
            )
        )
    db = Session()
    try:
        yield db, Session
    finally:
        db.close()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "TRUNCATE design_processing_artifacts, design_processing_jobs, "
                    "design_processing_items CASCADE"
                )
            )


def _add_item_and_job(
    db,
    *,
    item_id: str,
    now: datetime,
    desired_revision: str = "revision-a",
    execution_revision: str | None = None,
    status: str = "scheduled",
    locked_by: str | None = None,
    locked_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
    created_at: datetime | None = None,
) -> tuple[DesignProcessingItem, DesignProcessingJob]:
    item = DesignProcessingItem(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id=item_id,
        latest_desired_input_revision=desired_revision,
        latest_desired_pipeline_version=PIPELINE_VERSION,
        state="processing" if status == "running" else "scheduled",
        warnings_json=[],
        created_at=created_at or now,
        updated_at=now,
    )
    job = DesignProcessingJob(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id=item_id,
        trigger_type="phase5_postgres_test",
        execution_kind="analysis" if execution_revision is not None else None,
        execution_input_revision=execution_revision,
        execution_pipeline_version=(
            PIPELINE_VERSION if execution_revision is not None else None
        ),
        status=status,
        stage="extracting" if execution_revision is not None else None,
        scheduled_for=now - timedelta(minutes=5),
        attempt_count=1 if execution_revision is not None else 0,
        readiness_check_count=0,
        max_attempts=3,
        locked_by=locked_by,
        locked_at=locked_at,
        heartbeat_at=heartbeat_at,
        started_at=locked_at,
        created_at=created_at or now,
        updated_at=now,
    )
    db.add_all([item, job])
    db.commit()
    return item, job


def test_skip_locked_claims_next_due_job(postgres_worker_db):
    db, Session = postgres_worker_db
    now = datetime.now(timezone.utc)
    _, first_job = _add_item_and_job(
        db,
        item_id="skip-locked-first",
        now=now,
        created_at=now - timedelta(minutes=2),
    )
    _, second_job = _add_item_and_job(
        db,
        item_id="skip-locked-second",
        now=now,
        created_at=now - timedelta(minutes=1),
    )

    locker = Session()
    claimant = Session()
    try:
        locker.query(DesignProcessingJob).filter(
            DesignProcessingJob.id == first_job.id
        ).with_for_update().one()

        claimed = claim_due_analysis_jobs(
            claimant,
            worker_id="skip-locked-worker",
            limit=1,
            now=now,
        )

        assert [job.id for job in claimed] == [second_job.id]
    finally:
        locker.rollback()
        locker.close()
        claimant.close()

    db.expire_all()
    assert db.get(DesignProcessingJob, first_job.id).status == "scheduled"
    claimed_second = db.get(DesignProcessingJob, second_job.id)
    assert claimed_second.status == "running"
    assert claimed_second.locked_by == "skip-locked-worker"


def test_concurrent_workers_claim_one_job_once(postgres_worker_db):
    db, Session = postgres_worker_db
    now = datetime.now(timezone.utc)
    _, job = _add_item_and_job(db, item_id="single-claim", now=now)
    barrier = threading.Barrier(2)
    outcomes: Queue[BaseException | tuple[str, tuple[uuid.UUID, ...]]] = Queue()

    def claim(worker_id: str) -> None:
        worker_db = Session()
        try:
            barrier.wait(timeout=5)
            jobs = claim_due_analysis_jobs(
                worker_db,
                worker_id=worker_id,
                limit=1,
                now=now,
            )
            outcomes.put((worker_id, tuple(candidate.id for candidate in jobs)))
        except BaseException as exc:
            outcomes.put(exc)
        finally:
            worker_db.close()

    threads = [
        threading.Thread(target=claim, args=(worker_id,), daemon=True)
        for worker_id in ("worker-a", "worker-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    results = [outcomes.get(timeout=1), outcomes.get(timeout=1)]
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise errors[0]
    claimed_ids = [
        claimed_id
        for _, worker_claims in results
        for claimed_id in worker_claims
    ]
    assert claimed_ids == [job.id]

    db.expire_all()
    persisted = db.get(DesignProcessingJob, job.id)
    assert persisted.status == "running"
    assert persisted.locked_by in {"worker-a", "worker-b"}


def test_live_heartbeat_prevents_lease_recovery(postgres_worker_db):
    db, _ = postgres_worker_db
    now = datetime.now(timezone.utc)
    _, job = _add_item_and_job(
        db,
        item_id="live-heartbeat",
        now=now,
        execution_revision="revision-a",
        status="running",
        locked_by="live-worker",
        locked_at=now - timedelta(hours=2),
        heartbeat_at=now - timedelta(seconds=5),
    )

    recovered = recover_expired_analysis_leases(
        db,
        lease_timeout_seconds=60,
        now=now,
    )

    assert recovered == 0
    db.expire_all()
    persisted = db.get(DesignProcessingJob, job.id)
    assert persisted.status == "running"
    assert persisted.locked_by == "live-worker"


def test_expired_lease_is_recovered_to_retry_wait(postgres_worker_db):
    db, _ = postgres_worker_db
    now = datetime.now(timezone.utc)
    item, job = _add_item_and_job(
        db,
        item_id="expired-current",
        now=now,
        execution_revision="revision-a",
        status="running",
        locked_by="expired-worker",
        locked_at=now - timedelta(hours=2),
        heartbeat_at=now - timedelta(hours=2),
    )

    recovered = recover_expired_analysis_leases(
        db,
        lease_timeout_seconds=60,
        now=now,
    )

    assert recovered == 1
    db.expire_all()
    persisted_job = db.get(DesignProcessingJob, job.id)
    persisted_item = db.get(DesignProcessingItem, item.id)
    assert persisted_job.status == "retry_wait"
    assert persisted_job.scheduled_for == now
    assert persisted_job.next_retry_at == now
    assert persisted_job.locked_by is None
    assert persisted_job.locked_at is None
    assert persisted_job.heartbeat_at is None
    assert persisted_item.state == "processing"


def test_superseded_expired_lease_is_cancelled_and_replaced(postgres_worker_db):
    db, _ = postgres_worker_db
    now = datetime.now(timezone.utc)
    item, old_job = _add_item_and_job(
        db,
        item_id="expired-superseded",
        now=now,
        desired_revision="revision-b",
        execution_revision="revision-a",
        status="running",
        locked_by="expired-worker",
        locked_at=now - timedelta(hours=2),
        heartbeat_at=now - timedelta(hours=2),
    )

    recovered = recover_expired_analysis_leases(
        db,
        lease_timeout_seconds=60,
        now=now,
    )

    assert recovered == 1
    db.expire_all()
    jobs = (
        db.query(DesignProcessingJob)
        .filter(DesignProcessingJob.item_id == item.item_id)
        .order_by(DesignProcessingJob.created_at.asc(), DesignProcessingJob.id.asc())
        .all()
    )
    persisted_old = next(job for job in jobs if job.id == old_job.id)
    successor = next(job for job in jobs if job.id != old_job.id)
    assert persisted_old.status == "cancelled"
    assert persisted_old.superseded_by_revision == "revision-b"
    assert persisted_old.locked_by is None
    assert successor.status == "scheduled"
    assert successor.execution_kind is None
    assert len([job for job in jobs if job.status == "scheduled"]) == 1

    duplicate = DesignProcessingJob(
        id=uuid.uuid4(),
        board_id=BOARD_ID,
        item_id=item.item_id,
        trigger_type="duplicate_active_job",
        status="scheduled",
        scheduled_for=now,
        attempt_count=0,
        readiness_check_count=0,
        max_attempts=3,
        created_at=now,
        updated_at=now,
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()