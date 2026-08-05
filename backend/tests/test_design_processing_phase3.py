from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
import uuid

import pytest

from backend.app import monday_client
from backend.app.models import DesignProcessingItem, DesignProcessingJob
from backend.app.services.design_processing_inputs import (
    DownloadedDesignEmailAsset,
    DesignProcessingInputError,
    canonical_revision_bytes,
    download_design_email_assets,
    parse_design_processing_target,
)
from backend.app.services.design_processing_state import ProcessingIdentity
from backend.app.services.design_processing_target import (
    DesignProcessingTargetMismatch,
    assert_current_execution_target,
    refresh_current_target,
)


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _item(
    *,
    files: list[dict[str, object]] | None = None,
    assets: list[dict[str, object]] | None = None,
    name: str = "Human enquiry name",
    raw_email_value: object = None,
) -> dict[str, object]:
    if raw_email_value is None:
        raw_email_value = json.dumps({"files": files or []}) if files else None
    return {
        "id": "2657106977",
        "name": name,
        "board": {"id": "1882196103"},
        "group": {"id": "group_mkpbd6vy"},
        "assets": assets or [],
        "column_values": [
            {"id": "file_mkpbm883", "type": "file", "value": raw_email_value}
        ],
    }


def _asset(
    asset_id: str,
    filename: str,
    *,
    size: object = 123456,
    created_at: object = "2026-07-29T10:45:00+01:00",
    url: str | None = None,
) -> dict[str, object]:
    return {
        "id": asset_id,
        "name": filename,
        "file_extension": filename.rsplit(".", 1)[-1],
        "file_size": size,
        "created_at": created_at,
        "url": url or f"https://monday.invalid/private/{asset_id}",
        "public_url": None,
    }


def test_revision_uses_sorted_supported_metadata_records_only():
    item = _item(
        files=[
            {"assetId": "00012", "name": "Later.eml"},
            {"assetId": "3", "name": "Earlier.msg"},
            {"assetId": "9", "name": "Ignored.pdf"},
        ],
        assets=[
            _asset("12", "Later.eml", size="20"),
            _asset("9", "Ignored.pdf"),
            _asset("3", "Earlier.msg", size=10),
        ],
    )

    target = parse_design_processing_target(item)

    assert [asset.asset_id for asset in target.email_assets] == ["3", "12"]
    assert [asset.created_at for asset in target.email_assets] == [
        "2026-07-29T09:45:00Z",
        "2026-07-29T09:45:00Z",
    ]
    expected_records = [
        {
            "assetId": "3",
            "createdAt": "2026-07-29T09:45:00Z",
            "filename": "Earlier.msg",
            "size": 10,
        },
        {
            "assetId": "12",
            "createdAt": "2026-07-29T09:45:00Z",
            "filename": "Later.eml",
            "size": 20,
        },
    ]
    canonical = json.dumps(
        expected_records,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert canonical_revision_bytes(target.email_assets) == canonical
    assert target.input_revision == hashlib.sha256(canonical).hexdigest()


def test_revision_serializes_preserved_filenames_as_utf8():
    target = parse_design_processing_target(
        _item(
            files=[{"assetId": "3", "name": "Café enquiry.msg"}],
            assets=[_asset("3", "Café enquiry.msg")],
        )
    )

    canonical = canonical_revision_bytes(target.email_assets)

    assert b"Caf\xc3\xa9 enquiry.msg" in canonical
    assert b"\\u00e9" not in canonical


def test_revision_ignores_membership_order_download_url_and_item_name():
    files = [
        {"assetId": "10", "name": "First.msg"},
        {"assetId": "20", "name": "Second.eml"},
    ]
    assets = [_asset("10", "First.msg"), _asset("20", "Second.eml")]
    original = parse_design_processing_target(_item(files=files, assets=assets))

    changed_assets = [dict(asset) for asset in reversed(assets)]
    changed_assets[0]["url"] = "https://monday.invalid/rotated-download-url"
    changed = parse_design_processing_target(
        _item(
            files=list(reversed(files)),
            assets=changed_assets,
            name="A changed human name",
        )
    )

    assert changed.input_revision == original.input_revision


def test_valid_empty_or_unsupported_email_column_is_readiness_not_error():
    empty = parse_design_processing_target(_item())
    unsupported = parse_design_processing_target(
        _item(
            files=[{"assetId": "9", "name": "drawing.pdf"}],
            assets=[_asset("9", "drawing.pdf")],
        )
    )

    assert empty.missing_email is True
    assert empty.input_revision is None
    assert unsupported.missing_email is True
    assert unsupported.input_revision is None


@pytest.mark.parametrize(
    "raw_value",
    ["not-json", "{}", '"files"', '{"files": {}}'],
)
def test_malformed_membership_never_becomes_empty_readiness(raw_value):
    with pytest.raises(DesignProcessingInputError):
        parse_design_processing_target(_item(raw_email_value=raw_value))


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("file_size", None),
        ("file_size", 12.5),
        ("created_at", None),
        ("created_at", "2026-07-29T09:45:00"),
        ("url", None),
    ],
)
def test_supported_asset_requires_complete_revision_and_download_metadata(
    field_name,
    value,
):
    asset = _asset("7", "enquiry.msg")
    if field_name == "url":
        asset["url"] = value
    else:
        asset[field_name] = value

    with pytest.raises(DesignProcessingInputError):
        parse_design_processing_target(
            _item(
                files=[{"assetId": "7", "name": "enquiry.msg"}],
                assets=[asset],
            )
        )


def test_missing_supported_metadata_fails_but_missing_unsupported_metadata_is_ignored():
    with pytest.raises(DesignProcessingInputError, match="missing item asset metadata"):
        parse_design_processing_target(
            _item(files=[{"assetId": "7", "name": "enquiry.msg"}])
        )

    target = parse_design_processing_target(
        _item(files=[{"assetId": "8", "name": "drawing.pdf"}])
    )
    assert target.missing_email is True


def test_intake_query_fetches_only_the_email_column_and_required_asset_metadata(
    monkeypatch,
):
    calls = []
    expected = _item()

    def request(access_token, query, variables=None, *, timeout=10, **kwargs):
        calls.append((query, variables, timeout))
        return {"data": {"items": [expected]}}

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)

    actual = monday_client.fetch_design_processing_intake_item("token", "2657106977")

    assert actual is expected
    assert calls[0][1] == {"itemIds": ["2657106977"]}
    assert 'column_values(ids: ["file_mkpbm883"])' in calls[0][0]
    assert "updated_at" not in calls[0][0]
    assert "updates" not in calls[0][0]


def test_file_column_inspection_returns_only_joined_output_assets(monkeypatch):
    payload_item = {
        "id": "2657106977",
        "assets": [
            {
                "id": "10",
                "name": "AI_Data.csv",
                "file_size": "120",
                "created_at": "2026-08-05T10:00:00Z",
            },
            {
                "id": "11",
                "name": "Matched_Projects.pdf",
                "file_size": 240,
                "created_at": "2026-08-05T10:01:00Z",
            },
        ],
        "column_values": [
            {
                "id": "file_mkza7y37",
                "value": json.dumps({"files": [{"assetId": "10"}]}),
            },
            {
                "id": "file_mm59rntf",
                "value": json.dumps({"files": [{"assetId": "11"}]}),
            },
        ],
    }
    monkeypatch.setattr(
        monday_client,
        "monday_graphql_request",
        lambda *args, **kwargs: {"data": {"items": [payload_item]}},
    )

    inspected = monday_client.inspect_design_processing_file_columns(
        "token",
        "2657106977",
    )

    assert inspected["file_mkza7y37"][0].asset_id == "10"
    assert inspected["file_mkza7y37"][0].size_bytes == 120
    assert inspected["file_mm59rntf"][0].filename == "Matched_Projects.pdf"


def test_project_word_search_returns_typed_legacy_matching_inputs(monkeypatch):
    queries = []

    def request(access_token, query, variables=None, *, timeout=10, **kwargs):
        queries.append(query)
        return {
            "data": {
                "boards": [
                    {
                        "items_page": {
                            "cursor": None,
                            "items": [
                                {
                                    "id": "123",
                                    "name": "16771",
                                    "state": "active",
                                    "column_values": [
                                        {"id": "text3__1", "text": "100 New Kings Road"},
                                        {"id": "date9__1", "text": "2025-02-10"},
                                    ],
                                }
                            ],
                        }
                    }
                ]
            }
        }

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)

    items = monday_client.fetch_project_items_matching_words(
        "token",
        "1825117125",
        ["Kings", "Road"],
    )

    assert items == (
        monday_client.MondayProjectBoardItem(
            item_id="123",
            project_reference="16771",
            project_title="100 New Kings Road",
            state="active",
            created_date="2025-02-10",
        ),
    )
    assert "operator: and" in queries[0]
    assert "text3__1" in queries[0]
    assert "date9__1" in queries[0]
    assert "mutation" not in queries[0].lower()


def test_project_search_failure_is_not_converted_to_no_matches(monkeypatch):
    monkeypatch.setattr(
        monday_client,
        "monday_graphql_request",
        lambda *args, **kwargs: {"data": {"boards": []}},
    )

    with pytest.raises(monday_client.MondayReadContractError):
        monday_client.fetch_project_items_matching_words(
            "token",
            "1825117125",
            ["Kings"],
        )


def test_project_full_text_fallback_preserves_legacy_query_shape(monkeypatch):
    queries = []

    def request(access_token, query, variables=None, *, timeout=10, **kwargs):
        queries.append(query)
        return {"data": {"boards": [{"items_page": {"items": []}}]}}

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)

    items = monday_client.fetch_project_items_matching_full_text(
        "token",
        "1825117125",
        "100 New Kings Road",
    )

    assert items == ()
    assert 'compare_value: ["100 New Kings Road"]' in queries[0]
    assert "operator: and" not in queries[0]


def test_project_word_counts_fail_closed_on_incomplete_alias(monkeypatch):
    monkeypatch.setattr(
        monday_client,
        "monday_graphql_request",
        lambda *args, **kwargs: {
            "data": {"w0": [{"items_page": {"items": [{"id": "1"}]}}]}
        },
    )

    with pytest.raises(monday_client.MondayReadContractError):
        monday_client.fetch_project_word_hit_counts(
            "token",
            "1825117125",
            ["Kings", "Road"],
        )


def test_active_project_fallback_paginates_without_reusing_query_params(monkeypatch):
    calls = []
    responses = [
        {
            "data": {
                "boards": [
                    {
                        "items_page": {
                            "cursor": "next-cursor",
                            "items": [
                                {
                                    "id": "1",
                                    "name": "10001",
                                    "state": "active",
                                    "column_values": [],
                                }
                            ],
                        }
                    }
                ]
            }
        },
        {
            "data": {
                "next_items_page": {
                    "cursor": None,
                    "items": [
                        {
                            "id": "2",
                            "name": "10002",
                            "state": "deleted",
                            "column_values": [],
                        },
                        {
                            "id": "3",
                            "name": "10003",
                            "state": "active",
                            "column_values": [],
                        },
                    ],
                }
            }
        },
    ]

    def request(access_token, query, variables=None, *, timeout=10, **kwargs):
        calls.append((query, variables))
        return responses.pop(0)

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)

    items = monday_client.fetch_active_project_items_since(
        "token",
        "1825117125",
    )

    assert [item.item_id for item in items] == ["1", "3"]
    assert "query_params" in calls[0][0]
    assert "next_items_page" in calls[1][0]
    assert "query_params" not in calls[1][0]
    assert calls[1][1] == {"cursor": "next-cursor", "limit": 500}


class FakeReadGateway:
    def __init__(self, target):
        self.target = target
        self.calls = 0

    def fetch_target(self, item_id):
        self.calls += 1
        return self.target

    def inspect_file_columns(self, item_id):
        raise AssertionError("not used")

    def fetch_project_word_hit_counts(self, words):
        raise AssertionError("not used")

    def fetch_project_items_matching_words(self, words, *, start_date="2021-01-01"):
        raise AssertionError("not used")

    def fetch_project_items_matching_full_text(
        self,
        project_name,
        *,
        start_date="2021-01-01",
    ):
        raise AssertionError("not used")

    def fetch_active_project_items_since(self, *, start_date="2021-01-01"):
        raise AssertionError("not used")


def _processing_item(
    *,
    desired: ProcessingIdentity | None = None,
    published: ProcessingIdentity | None = None,
    state: str = "scheduled",
) -> DesignProcessingItem:
    return DesignProcessingItem(
        id=uuid.uuid4(),
        board_id="1882196103",
        item_id="2657106977",
        latest_desired_input_revision=(desired.input_revision if desired else None),
        latest_desired_pipeline_version=(desired.pipeline_version if desired else None),
        latest_published_input_revision=(published.input_revision if published else None),
        latest_published_pipeline_version=(published.pipeline_version if published else None),
        state=state,
        warnings_json=[],
        created_at=NOW,
        updated_at=NOW,
    )


def _running_job(identity: ProcessingIdentity) -> DesignProcessingJob:
    return DesignProcessingJob(
        id=uuid.uuid4(),
        board_id="1882196103",
        item_id="2657106977",
        trigger_type="test",
        execution_kind="analysis",
        execution_input_revision=identity.input_revision,
        execution_pipeline_version=identity.pipeline_version,
        status="running",
        stage="extracting",
        scheduled_for=NOW,
        attempt_count=1,
        readiness_check_count=0,
        max_attempts=3,
        locked_by="worker-1",
        locked_at=NOW,
        heartbeat_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _target(*, name="Human name", files=None, assets=None, group_id="group_mkpbd6vy"):
    raw = _item(name=name, files=files, assets=assets)
    raw["group"] = {"id": group_id}
    return parse_design_processing_target(raw)


def test_refresh_handles_name_then_email_arrival_without_consuming_an_attempt():
    item = _processing_item(state="waiting_for_name")
    missing_both = FakeReadGateway(_target(name=""))

    first = refresh_current_target(
        item,
        gateway=missing_both,
        pipeline_version="pipeline-v1",
        expected_board_id="1882196103",
        expected_group_id="group_mkpbd6vy",
        now=NOW,
    )

    assert first.readiness == "waiting_for_name"
    assert item.latest_desired_input_revision is None

    name_only = FakeReadGateway(_target())
    second = refresh_current_target(
        item,
        gateway=name_only,
        pipeline_version="pipeline-v1",
        expected_board_id="1882196103",
        expected_group_id="group_mkpbd6vy",
        now=NOW,
    )
    assert second.readiness == "waiting_for_email"

    ready_target = _target(
        files=[{"assetId": "7", "name": "enquiry.msg"}],
        assets=[_asset("7", "enquiry.msg")],
    )
    ready = refresh_current_target(
        item,
        gateway=FakeReadGateway(ready_target),
        pipeline_version="pipeline-v1",
        expected_board_id="1882196103",
        expected_group_id="group_mkpbd6vy",
        now=NOW,
    )

    assert ready.readiness == "ready"
    assert item.latest_desired_input_revision == ready_target.input_revision
    assert item.latest_desired_pipeline_version == "pipeline-v1"
    assert item.state == "scheduled"


def test_refresh_email_before_name_keeps_name_precedence():
    email_without_name = _target(
        name="",
        files=[{"assetId": "7", "name": "enquiry.msg"}],
        assets=[_asset("7", "enquiry.msg")],
    )
    item = _processing_item()

    refreshed = refresh_current_target(
        item,
        gateway=FakeReadGateway(email_without_name),
        pipeline_version="pipeline-v1",
        expected_board_id="1882196103",
        expected_group_id="group_mkpbd6vy",
        now=NOW,
    )

    assert refreshed.readiness == "waiting_for_name"
    assert item.latest_desired_input_revision is None


def test_refresh_pipeline_only_change_invalidates_ready_identity():
    target = _target(
        files=[{"assetId": "7", "name": "enquiry.msg"}],
        assets=[_asset("7", "enquiry.msg")],
    )
    old_identity = ProcessingIdentity(target.input_revision, "pipeline-v1")
    item = _processing_item(
        desired=old_identity,
        published=old_identity,
        state="ready_for_review",
    )

    refreshed = refresh_current_target(
        item,
        gateway=FakeReadGateway(target),
        pipeline_version="pipeline-v2",
        expected_board_id="1882196103",
        expected_group_id="group_mkpbd6vy",
        now=NOW,
    )

    assert refreshed.identity == ProcessingIdentity(target.input_revision, "pipeline-v2")
    assert item.state == "scheduled"
    assert item.latest_published_pipeline_version == "pipeline-v1"


def test_restored_published_input_leaves_readiness_wait_for_safe_recheck():
    target = _target(
        files=[{"assetId": "7", "name": "enquiry.msg"}],
        assets=[_asset("7", "enquiry.msg")],
    )
    published = ProcessingIdentity(target.input_revision, "pipeline-v1")
    item = _processing_item(
        published=published,
        state="waiting_for_email",
    )

    refresh_current_target(
        item,
        gateway=FakeReadGateway(target),
        pipeline_version="pipeline-v1",
        expected_board_id="1882196103",
        expected_group_id="group_mkpbd6vy",
        now=NOW,
    )

    assert item.latest_desired_input_revision == published.input_revision
    assert item.state == "scheduled"


def test_refresh_leaving_landing_zone_clears_desired_and_marks_ineligible():
    existing = ProcessingIdentity("revision-a", "pipeline-v1")
    item = _processing_item(desired=existing, state="scheduled")

    refreshed = refresh_current_target(
        item,
        gateway=FakeReadGateway(_target(group_id="active_group")),
        pipeline_version="pipeline-v1",
        expected_board_id="1882196103",
        expected_group_id="group_mkpbd6vy",
        now=NOW,
    )

    assert refreshed.readiness == "ineligible"
    assert item.latest_desired_input_revision is None
    assert item.latest_desired_pipeline_version is None
    assert item.state == "ineligible"


def test_refresh_api_failure_does_not_mutate_stored_identity():
    identity = ProcessingIdentity("revision-a", "pipeline-v1")
    item = _processing_item(desired=identity, state="scheduled")

    class FailingGateway(FakeReadGateway):
        def fetch_target(self, item_id):
            raise monday_client.TransientMondayAPIError(detail="temporary failure")

    with pytest.raises(monday_client.TransientMondayAPIError):
        refresh_current_target(
            item,
            gateway=FailingGateway(None),
            pipeline_version="pipeline-v1",
            expected_board_id="1882196103",
            expected_group_id="group_mkpbd6vy",
            now=NOW,
        )

    assert item.latest_desired_input_revision == "revision-a"
    assert item.latest_desired_pipeline_version == "pipeline-v1"
    assert item.state == "scheduled"


def test_execution_gate_compares_remote_desired_and_execution_identity():
    target = _target(
        files=[{"assetId": "7", "name": "enquiry.msg"}],
        assets=[_asset("7", "enquiry.msg")],
    )
    identity = ProcessingIdentity(target.input_revision, "pipeline-v1")
    item = _processing_item(desired=identity, state="processing")
    job = _running_job(identity)
    gateway = FakeReadGateway(target)

    result = assert_current_execution_target(
        item,
        job,
        gateway=gateway,
        pipeline_version="pipeline-v1",
        expected_board_id="1882196103",
        expected_group_id="group_mkpbd6vy",
        worker_id="worker-1",
    )

    assert result is target
    assert gateway.calls == 1

    changed_target = _target(
        files=[{"assetId": "8", "name": "replacement.eml"}],
        assets=[_asset("8", "replacement.eml")],
    )
    with pytest.raises(DesignProcessingTargetMismatch) as exc_info:
        assert_current_execution_target(
            item,
            job,
            gateway=FakeReadGateway(changed_target),
            pipeline_version="pipeline-v1",
            expected_board_id="1882196103",
            expected_group_id="group_mkpbd6vy",
            worker_id="worker-1",
        )
    assert exc_info.value.reason == "input_changed"


def test_content_hashes_are_separate_and_each_asset_downloads_once():
    target = _target(
        files=[
            {"assetId": "8", "name": "second.eml"},
            {"assetId": "7", "name": "first.msg"},
        ],
        assets=[
            _asset("8", "second.eml", size=8),
            _asset("7", "first.msg", size=7),
        ],
    )
    revision_before_download = target.input_revision
    calls = []

    class Download:
        def __init__(self, asset_id, size):
            self.temp_path = f"C:/temp/{asset_id}"
            self.content_type = "application/octet-stream"
            self.sha256 = asset_id.zfill(64)
            self.size_bytes = size

    def downloader(asset, access_token):
        calls.append((asset["id"], access_token))
        return Download(asset["id"], int(asset["file_size"]))

    downloaded = download_design_email_assets(
        target.email_assets,
        "token",
        downloader=downloader,
    )

    assert all(isinstance(value, DownloadedDesignEmailAsset) for value in downloaded)
    assert calls == [("7", "token"), ("8", "token")]
    assert [value.content_sha256 for value in downloaded] == [
        "7".zfill(64),
        "8".zfill(64),
    ]
    assert target.input_revision == revision_before_download