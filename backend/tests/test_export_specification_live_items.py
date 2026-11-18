import json
import sys

import pytest

from backend.app import monday_client
from backend.scripts import export_specification_live_items as exporter


def item(item_id, *, board_id="100", group_id="topics", state="active"):
    return {"id": item_id, "state": state, "board": {"id": board_id},
            "group": {"id": group_id, "title": "Group " + group_id}}


def first_response(items, *, cursor=None, board_id="100"):
    return {"data": {"boards": [{"id": board_id, "items_page": {"items": items, "cursor": cursor}}]}}


def test_reads_all_board_groups_and_skips_inactive_items(monkeypatch):
    def request(_token, query, variables, *, timeout):
        if "next_items_page" in query:
            assert variables["cursor"] == "page-2"
            return {"data": {"next_items_page": {
                "items": [item("3", group_id="completed")], "cursor": None,
            }}}
        assert variables["boardIds"] == ["100"]
        return first_response([item("1"), item("2", state="archived")], cursor="page-2")

    monkeypatch.setattr(monday_client, "monday_graphql_request", request)

    assert monday_client.fetch_live_board_group_items("test-token", "100") == [
        monday_client.MondayBoardGroupItem("1", "topics", "Group topics"),
        monday_client.MondayBoardGroupItem("3", "completed", "Group completed"),
    ]


@pytest.mark.parametrize("second_page, error", [
    ({}, "page is incomplete"),
    ({"items": [], "cursor": "page-2"}, "cursor is malformed or repeated"),
    ({"items": [item("1")], "cursor": None}, "returned a repeated item"),
    ({"items": [item("3", board_id="200")], "cursor": None}, "membership is malformed"),
])
def test_rejects_a_bad_continuation_instead_of_returning_partial_inventory(monkeypatch, second_page, error):
    responses = iter([
        first_response([item("1")], cursor="page-2"),
        {"data": {"next_items_page": second_page}},
    ])
    monkeypatch.setattr(monday_client, "monday_graphql_request", lambda *args, **kwargs: next(responses))

    with pytest.raises(monday_client.MondayReadContractError, match=error):
        monday_client.fetch_live_board_group_items("test-token", "100")


@pytest.mark.parametrize("as_sql", [False, True])
def test_cli_prints_one_sql_safe_snapshot(monkeypatch, capsys, as_sql):
    group_item = monday_client.MondayBoardGroupItem("5", "topics", "$monday_snapshot$")
    options = ["--sql"] if as_sql else []
    monkeypatch.setattr(sys, "argv", ["export_specification_live_items", *options])
    monkeypatch.setattr(exporter.settings, "monday_ingestion_access_token", "test-token")
    monkeypatch.setattr(exporter.settings, "design_processing_board_id", "100")
    monkeypatch.setattr(exporter, "fetch_live_board_group_items", lambda token, board: [group_item])

    assert exporter.main() == 0
    result = capsys.readouterr().out
    if as_sql:
        assert exporter.SNAPSHOT_PLACEHOLDER not in result
        assert "jsonb" in result
        result = result.rsplit(exporter.SNAPSHOT_QUOTE, 2)[1]
    snapshot = json.loads(result)
    assert snapshot["board_id"] == "100"
    assert snapshot["observed_at"].endswith("+00:00")
    assert snapshot["items"] == [{"item_id": "5", "group_id": "topics", "group_title": "$monday_snapshot$"}]
    assert "$" not in result
    assert "test-token" not in result
