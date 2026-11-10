"""Landing Zone scope and review source dependency lifecycle regressions."""

from copy import deepcopy

import pytest

from backend.app.config import settings
from backend.app.services.performance_specification_sources import (
    SpecificationIneligible, SpecificationSourceError, _read_column, resolve_specification_sources,
)
from backend.app.services.specification_policy import (
    eligible_specification_group_ids, specification_scope_exclusion,
)
from backend.app.services.specification_review_contract import ReviewDecisionState
from backend.tests.test_performance_specification import case, relation


@pytest.fixture()
def landing_case(case):
    case.gateway.target.group_id = settings.design_processing_landing_group_id
    case.gateway.holding["group"]["id"] = settings.design_processing_landing_group_id
    return case


@pytest.mark.parametrize("group,state,expected", [
    ("landing", "active", None), ("topics", "active", "outside_landing_zone"),
    ("completed", "active", "completed_folder"), ("landing", "archived", "item_archived"),
    ("landing", "deleted", "item_deleted"), ("landing", None, "item_unknown"),
])
def test_specification_policy_uses_live_landing_zone_only(monkeypatch, group, state, expected):
    monkeypatch.setattr(settings, "design_processing_board_id", "123")
    monkeypatch.setattr(settings, "design_processing_landing_group_id", "landing")
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "completed")
    assert eligible_specification_group_ids() == frozenset({"landing"})
    assert specification_scope_exclusion(board_id="123", group_id=group, item_state=state) == expected
    assert specification_scope_exclusion(board_id="456", group_id="landing", item_state="active") == "board_not_managed"


def test_completed_exclusion_wins_over_misconfigured_landing_group(monkeypatch):
    monkeypatch.setattr(settings, "design_processing_landing_group_id", "completed")
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "completed")
    assert not eligible_specification_group_ids()
    assert specification_scope_exclusion(board_id=settings.design_processing_board_id,
        group_id="completed", item_state="active") == "completed_folder"


@pytest.mark.parametrize("surface", ["target", "holding"])
@pytest.mark.parametrize("lifecycle", ["group", "archived", "deleted", "board"])
def test_every_live_source_scope_read_fails_closed(landing_case, surface, lifecycle):
    case = landing_case
    if surface == "target":
        attribute, value = (("group_id", "topics") if lifecycle == "group" else
            ("board_id", "999") if lifecycle == "board" else ("item_state", lifecycle))
        setattr(case.gateway.target, attribute, value)
    elif lifecycle == "group":
        case.gateway.holding["group"]["id"] = "topics"
    elif lifecycle == "board":
        case.gateway.holding["board"]["id"] = "999"
    else:
        case.gateway.holding["state"] = lifecycle
    with pytest.raises(SpecificationIneligible):
        resolve_specification_sources(case.gateway, case.item)


def test_empty_holding_relation_retains_all_candidate_dependencies_until_selection(landing_case):
    case = landing_case
    case.gateway.holding["column_values"] = []
    project_board = case.gateway.project["board"]["id"]
    case.item.match_result_json = {"result": {"specificationCandidates": {
        "lookup": {"status": "available", "projects": [
            {"record": {"board_id": project_board, "item_id": "901"}, "rationale": "Detected reference"},
            {"record": {"board_id": project_board, "item_id": "902"}, "rationale": "Plausible site"},
        ], "drawings": [{"record": {"board_id": "9000", "item_id": "9201"},
            "project": {"board_id": project_board, "item_id": "902"},
            "literal_reference": "TP00902_26.01 - A", "lifecycle": "active",
            "match_kind": "other", "rationale": "Other plausible roof"}]},
        "dependencies": [[project_board, "901"], [project_board, "902"], ["9000", "9201"],
            ["1654217230", "802"], ["444", "445"]],
    }}}
    dependencies = resolve_specification_sources(case.gateway, case.item)["dependencies"]
    assert all(identity in dependencies for identity in [
        [project_board, "901"], [project_board, "902"], ["9000", "9201"],
        ["1654217230", "802"], ["444", "445"],
    ])
    state = ReviewDecisionState(selections={"selected_request_type": "amendment",
        "selected_project": {"board_id": project_board, "item_id": "901"},
        "selected_drawing": {"board_id": "9000", "item_id": "9101"}})
    dependencies = resolve_specification_sources(case.gateway, case.item, review_state=state)["dependencies"]
    assert [project_board, "901"] in dependencies
    assert ["9000", "9101"] in dependencies
    assert ["1654217230", "801"] in dependencies
    assert all(identity not in dependencies for identity in [
        [project_board, "902"], ["9000", "9201"], ["1654217230", "802"], ["444", "445"],
    ])


def test_explicit_drawing_narrows_dependencies_and_keeps_relevant_contact_mirror(landing_case):
    case = landing_case
    extraction = deepcopy(case.item.extracted_parameters_json)
    extraction["performanceParameters"]["Contact"] = None
    extraction["specificationEvidence"] = [entry for entry in extraction["specificationEvidence"]
        if entry["field"] != "Contact"]
    case.item.extracted_parameters_json = extraction
    selected = case.gateway.project["subitems"][0]
    selected["column_values"].append({"id": "mirror_11__1", "type": "mirror",
        "column": {"title": "Contact"}, "mirrored_items": [{
            "linked_item": {"id": "777", "board": {"id": "888"}},
            "mirrored_value": {"__typename": "BoardRelationValue", **relation("contact", [
                {"id": "555", "name": "Selected Contact", "board": {"id": "666"}},
            ])},
        }]})
    other = deepcopy(selected)
    other.update(id="9102", name="TP00901_26.02 - A")
    case.gateway.project["subitems"].append(other)
    state = ReviewDecisionState(selections={"selected_request_type": "amendment",
        "selected_project": {"board_id": case.gateway.project["board"]["id"], "item_id": "901"},
        "selected_drawing": {"board_id": "9000", "item_id": "9101"}})
    dependencies = resolve_specification_sources(case.gateway, case.item, review_state=state)["dependencies"]
    assert ["9000", "9101"] in dependencies
    assert ["9000", "9102"] not in dependencies
    assert ["888", "777"] in dependencies
    assert ["666", "555"] in dependencies


def test_unreadable_optional_mirror_keeps_all_known_source_dependencies():
    dependencies = set()
    mirror = {"id": "mirror", "type": "mirror", "mirrored_items": [
        {"linked_item": {"id": "1", "board": {"id": "2"}}, "mirrored_value": {}},
        {"linked_item": {"id": "3", "board": {"id": "4"}}, "mirrored_value": {"text": "value"}},
    ]}
    with pytest.raises(SpecificationSourceError):
        _read_column(mirror, dependencies)
    assert dependencies == {("2", "1"), ("4", "3")}


def test_partially_unreadable_relation_keeps_known_source_dependency():
    dependencies = set()
    linked = relation("account", [{"id": "1", "board": {"id": "2"}, "name": "Account"}])
    linked["linked_item_ids"].append("unavailable")
    with pytest.raises(SpecificationSourceError):
        _read_column(linked, dependencies)
    assert dependencies == {("2", "1")}
