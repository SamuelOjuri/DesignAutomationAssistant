"""Specification rollout is independent of legacy design extraction/output."""

from copy import deepcopy

from pydantic import ValidationError
import pytest

from backend.app.config import Settings, settings
from backend.app.services import specification_policy as policy
from backend.app.services.specification_review_auth import review_capabilities


def configured(**overrides):
    values = settings.model_dump()
    values.update(overrides)
    return Settings.model_validate(values)


@pytest.fixture
def isolated_rollout(monkeypatch):
    monkeypatch.setattr(settings, "performance_specification_enabled", True)
    monkeypatch.setattr(settings, "performance_specification_review_enabled", True)
    monkeypatch.setattr(settings, "performance_specification_mode", "enabled")
    monkeypatch.setattr(settings, "performance_specification_allowlist_item_ids", ["30"])
    monkeypatch.setattr(settings, "design_processing_mode", "off")
    monkeypatch.setattr(settings, "design_processing_allowlist_item_ids", ["99"])
    monkeypatch.setattr(settings, "design_processing_board_id", "20")
    monkeypatch.setattr(settings, "design_processing_landing_group_id", "landing")
    monkeypatch.setattr(settings, "auto_sync_completed_group_id", "completed")
    return {"id": "30", "state": "active", "board": {"id": "20"}, "group": {"id": "landing"}}


def test_defaults_require_explicit_specification_rollout_even_when_legacy_design_is_enabled(monkeypatch):
    required_values = {name: getattr(settings, name) for name, field in Settings.model_fields.items() if field.is_required()}
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    instance = Settings(_env_file=None, design_processing_mode="enabled", performance_specification_enabled=True, **required_values)
    assert instance.performance_specification_mode == "off"
    assert instance.performance_specification_allowlist_item_ids == []
    assert instance.design_processing_mode == "enabled"


@pytest.mark.parametrize("raw,expected", [
    ("30, 40,30", ["30", "40"]),
    ('["30", "40", "30"]', ["30", "40"]),
    ("[30, 40]", ["30", "40"]),
    (["30", 40, " 30 "], ["30", "40"]),
    ("  ", []),
    (None, []),
])
def test_specification_allowlist_normalizes_csv_json_and_explicit_settings(raw, expected):
    instance = configured(performance_specification_mode="off", performance_specification_allowlist_item_ids=raw)
    assert instance.performance_specification_allowlist_item_ids == expected


@pytest.mark.parametrize("raw", ["0", "-1", "30,", "30,no", "[30,", "[true]", "[3.1]", "[{}]", "٣٠", "01", True, 30, {"id": "30"}])
def test_invalid_specification_allowlists_are_rejected(raw):
    with pytest.raises(ValidationError, match="performance_specification_allowlist_item_ids"):
        configured(performance_specification_mode="off", performance_specification_allowlist_item_ids=raw)


@pytest.mark.parametrize("master", [False, True])
def test_specification_allowlist_mode_requires_its_own_nonempty_list(master):
    with pytest.raises(ValidationError, match="performance_specification_allowlist_item_ids must not be empty"):
        configured(performance_specification_enabled=master, performance_specification_mode="allowlist",
            performance_specification_allowlist_item_ids=[], design_processing_mode="allowlist",
            design_processing_allowlist_item_ids=["30"])


def test_specification_environment_parsing_does_not_reuse_design_allowlist(monkeypatch):
    required_values = {name: getattr(settings, name) for name, field in Settings.model_fields.items() if field.is_required()}
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("PERFORMANCE_SPECIFICATION_MODE", " ALLOWLIST ")
    monkeypatch.setenv("PERFORMANCE_SPECIFICATION_ALLOWLIST_ITEM_IDS", ' ["30", "40"] ')
    monkeypatch.setenv("DESIGN_PROCESSING_MODE", "allowlist")
    monkeypatch.setenv("DESIGN_PROCESSING_ALLOWLIST_ITEM_IDS", "99,100")
    instance = Settings(_env_file=None, **required_values)
    assert instance.performance_specification_mode == "allowlist"
    assert instance.performance_specification_allowlist_item_ids == ["30", "40"]
    assert instance.design_processing_allowlist_item_ids == ["99", "100"]


@pytest.mark.parametrize("mode", ["on", "publish", "unknown", ""])
def test_invalid_specification_modes_are_rejected(mode):
    with pytest.raises(ValidationError, match="performance_specification_mode"):
        configured(performance_specification_mode=mode)


@pytest.mark.parametrize("design_mode", ["off", "shadow", "allowlist", "enabled"])
@pytest.mark.parametrize("specification_mode", ["off", "shadow", "allowlist", "enabled"])
@pytest.mark.parametrize("master", [False, True])
@pytest.mark.parametrize("item_id", ["30", "99"])
def test_work_and_publication_use_only_specification_rollout(isolated_rollout, monkeypatch, design_mode, specification_mode, master, item_id):
    monkeypatch.setattr(settings, "design_processing_mode", design_mode)
    monkeypatch.setattr(settings, "performance_specification_mode", specification_mode)
    monkeypatch.setattr(settings, "performance_specification_enabled", master)
    in_scope = specification_mode != "off" and (specification_mode != "allowlist" or item_id == "30")
    assert policy.specification_processing_enabled() is (master and specification_mode != "off")
    assert policy.specification_processing_allowed(item_id) is (master and in_scope)
    assert policy.specification_publication_allowed(item_id) is (master and in_scope and specification_mode != "shadow")
    assert policy.specification_rollout_exclusion(item_id) == (
        "processing_off" if specification_mode == "off" else "item_not_allowlisted" if not in_scope else None
    )


@pytest.mark.parametrize("design_mode", ["off", "shadow", "allowlist", "enabled"])
@pytest.mark.parametrize("specification_mode", ["off", "shadow", "allowlist", "enabled"])
@pytest.mark.parametrize("master", [False, True])
@pytest.mark.parametrize("item_id", ["30", "99"])
def test_review_capabilities_follow_specification_controls_and_preserve_pending_saves(isolated_rollout, monkeypatch, design_mode, specification_mode, master, item_id):
    monkeypatch.setattr(settings, "design_processing_mode", design_mode)
    monkeypatch.setattr(settings, "performance_specification_mode", specification_mode)
    monkeypatch.setattr(settings, "performance_specification_enabled", master)
    snapshot = {**isolated_rollout, "id": item_id}
    editable = specification_mode != "off" and (specification_mode != "allowlist" or item_id == "30")
    capabilities, reason = review_capabilities(snapshot)
    assert capabilities == {"read": True, "edit": editable,
        "approve": editable and master and specification_mode != "shadow"}
    assert reason == policy.specification_rollout_exclusion(item_id)


@pytest.mark.parametrize("change,reason", [
    ({"board": {"id": "21"}}, "board_not_managed"),
    ({"group": {"id": "active"}}, "outside_landing_zone"),
    ({"group": {"id": "completed"}}, "completed_folder"),
    ({"state": "archived"}, "item_archived"),
    ({"state": "deleted"}, "item_deleted"),
])
def test_specification_rollout_cannot_bypass_live_landing_scope(isolated_rollout, change, reason):
    snapshot = {**deepcopy(isolated_rollout), **change}
    capabilities, exclusion = review_capabilities(snapshot)
    assert capabilities == {"read": True, "edit": False, "approve": False}
    assert exclusion == reason


def test_review_feature_disable_still_retains_read_access(isolated_rollout, monkeypatch):
    monkeypatch.setattr(settings, "performance_specification_review_enabled", False)
    assert review_capabilities(isolated_rollout) == ({"read": True, "edit": False, "approve": False}, "review_disabled")


def test_invalid_runtime_mode_fails_closed(isolated_rollout, monkeypatch):
    # Settings validation normally prevents this; runtime/old-process drift must
    # never turn an unknown value into implicit publication permission.
    monkeypatch.setattr(settings, "performance_specification_mode", "unknown")
    assert policy.specification_processing_enabled() is False
    assert policy.specification_rollout_exclusion("30") == "processing_off"
    assert policy.specification_publication_allowed("30") is False
