from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.app.config import Settings, settings
from backend.app.services.legacy_enquiry import LEGACY_MANIFEST_DIGEST
from backend.scripts.generate_legacy_enquiry_fixtures import (
    DEFAULT_CSV_PATH,
    DEFAULT_EXPECTED_PATH,
    WORKSPACE_ROOT,
    generate_fixture,
)
from backend.scripts.verify_design_processing_storage import (
    PROBE_FILENAME,
    PROBE_ITEM_ID,
    build_design_processing_object_key,
)
from backend.scripts.verify_legacy_enquiry_manifest import (
    DEFAULT_LEGACY_ROOT,
    EXPECTED_MANIFEST_DIGEST,
    verify_legacy_manifest,
)


OFFLINE_FIXTURE_PREREQUISITES_AVAILABLE = (
    DEFAULT_LEGACY_ROOT.is_dir()
    and (
        WORKSPACE_ROOT / "data" / "FW_ Drawings Titley close_Walton House.msg"
    ).is_file()
)


def _settings(**overrides) -> Settings:
    values = settings.model_dump()
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.mark.skipif(
    not OFFLINE_FIXTURE_PREREQUISITES_AVAILABLE,
    reason="ignored legacy source and approved historical corpus are unavailable",
)
def test_pinned_legacy_manifest_and_source_snapshot_verify():
    verified_paths = verify_legacy_manifest()

    assert len(verified_paths) == 10
    assert LEGACY_MANIFEST_DIGEST == EXPECTED_MANIFEST_DIGEST


@pytest.mark.skipif(
    not OFFLINE_FIXTURE_PREREQUISITES_AVAILABLE,
    reason="ignored legacy source and approved historical corpus are unavailable",
)
def test_legacy_golden_fixtures_reproduce_byte_for_byte():
    expected_bytes, csv_bytes = generate_fixture()

    assert expected_bytes == DEFAULT_EXPECTED_PATH.read_bytes()
    assert csv_bytes == DEFAULT_CSV_PATH.read_bytes()
    assert csv_bytes.count(b"\r\n") == 17


def test_design_processing_defaults_pin_identity_and_remain_off():
    configured = _settings(
        design_processing_mode="off",
        design_processing_worker_enabled=False,
        design_processing_reconciliation_enabled=False,
        design_processing_allowlist_item_ids=[],
        design_processing_extraction_model="gemini-3.5-flash",
        design_processing_thinking_level="medium",
    )

    expected_version = (
        "legacy-files-"
        "82d5612a9efce97660c3a3fef36a731d45597cb3096e58365865727ba719e28e:"
        "model-gemini-3.5-flash:"
        "thinking-medium:"
        "output-v3"
    )
    assert configured.design_processing_mode == "off"
    assert configured.design_processing_worker_enabled is False
    assert configured.design_processing_reconciliation_enabled is False
    assert configured.design_processing_board_id == "1882196103"
    assert configured.design_processing_landing_group_id == "group_mkpbd6vy"
    assert configured.design_processing_project_board_id == "1825117125"
    assert configured.design_processing_thinking_level == "medium"
    assert configured.design_processing_pipeline_version == expected_version
    assert configured.design_processing_pipeline_digest == hashlib.sha256(
        expected_version.encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize("mode", ["invalid", "publish", "on"])
def test_design_processing_rejects_invalid_modes(mode):
    with pytest.raises(ValidationError):
        _settings(design_processing_mode=mode)


def test_design_processing_rejects_empty_allowlist_mode():
    with pytest.raises(ValidationError, match="must not be empty in allowlist mode"):
        _settings(
            design_processing_mode="allowlist",
            design_processing_allowlist_item_ids=[],
        )


def test_design_processing_normalizes_allowlist_and_activation_timestamp():
    configured = _settings(
        design_processing_mode="allowlist",
        design_processing_allowlist_item_ids="2657106977, 12345,2657106977",
        design_processing_activation_timestamp="2026-08-05T10:30:00+01:00",
    )

    assert configured.design_processing_allowlist_item_ids == ["2657106977", "12345"]
    assert configured.design_processing_activation_timestamp == datetime(
        2026,
        8,
        5,
        9,
        30,
        tzinfo=timezone.utc,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("design_processing_board_id", "not-an-id"),
        ("design_processing_project_board_id", "0"),
        ("design_processing_landing_group_id", ""),
        ("design_processing_extraction_model", "  "),
        ("design_processing_thinking_level", "deep"),
        ("design_processing_artifact_bucket", "bucket/path"),
        ("design_processing_allowlist_item_ids", "123,bad"),
        ("design_processing_activation_timestamp", "2026-08-05T10:30:00"),
    ],
)
def test_design_processing_rejects_invalid_target_settings(field, value):
    with pytest.raises(ValidationError):
        _settings(**{field: value})


def test_design_processing_rejects_invalid_readiness_intervals():
    with pytest.raises(ValidationError, match="max_interval_seconds"):
        _settings(
            design_processing_readiness_initial_interval_seconds=60,
            design_processing_readiness_max_interval_seconds=30,
        )


def test_design_processing_storage_key_uses_full_identity_namespace():
    input_revision = "a" * 64
    pipeline_digest = "b" * 64

    object_key = build_design_processing_object_key(
        board_id="1882196103",
        item_id=PROBE_ITEM_ID,
        input_revision=input_revision,
        pipeline_digest=pipeline_digest,
        filename=PROBE_FILENAME,
    )

    assert object_key == (
        "design-processing/1882196103/phase1-storage-probe/"
        f"{input_revision}/{pipeline_digest}/phase1_storage_probe.txt"
    )