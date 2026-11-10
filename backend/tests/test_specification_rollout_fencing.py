"""Rollout changes fence specification side effects independently of design exports."""

import pytest

from backend.app.config import settings
from backend.app.models import PerformanceSpecificationArtifact
from backend.app.services import performance_specification as worker
from backend.tests.test_performance_specification import BOARD, ITEM, case, current, run


@pytest.mark.parametrize("mode", ["off", "shadow", "allowlist"])
def test_specification_rollout_change_during_translation_prevents_monday_upload(case, monkeypatch, mode):
    def restrict():
        monkeypatch.setattr(settings, "performance_specification_mode", mode)
        monkeypatch.setattr(settings, "performance_specification_allowlist_item_ids", ["999"])

    case.on_translate.append(restrict)
    assert run(case) == (["rendered"] if mode == "shadow" else ["superseded"])
    assert settings.design_processing_mode == "enabled"
    assert case.gateway.uploads == case.gateway.deletes == []
    assert current(case).current_revision is None


def test_specification_allowlist_removal_during_upload_never_promotes_the_asset(case, monkeypatch):
    monkeypatch.setattr(settings, "performance_specification_mode", "allowlist")
    monkeypatch.setattr(settings, "performance_specification_allowlist_item_ids", [ITEM])
    case.gateway.on_upload = lambda: monkeypatch.setattr(settings, "performance_specification_allowlist_item_ids", ["999"])
    assert run(case) == ["superseded"]
    assert current(case).current_revision is None
    artifact = case.db.query(PerformanceSpecificationArtifact).one()
    assert artifact.monday_asset_id is not None and artifact.status == "uploaded"
    assert not case.gateway.deletes


def test_rollout_change_after_cleanup_inspection_prevents_owned_file_deletion(case, monkeypatch):
    assert run(case) == ["published"]
    old_asset = case.gateway.uploads[0][0].asset_id
    case.gateway.project["column_values"][1]["linked_items"][0]["name"] = "Updated Account"
    assert run(case) == ["published"]
    assert old_asset in case.gateway.deletes

    # Recreate a tracked prior attachment so cleanup has real ledger-owned work.
    previous = case.db.query(PerformanceSpecificationArtifact).filter(
        PerformanceSpecificationArtifact.revision != current(case).current_revision,
    ).one()
    previous.status = "published"
    case.gateway.files.append(case.gateway.uploads[0][0])
    case.gateway.deletes.clear()
    case.db.commit()
    source = current(case).source_json
    inspect = case.gateway.inspect_files

    def restrict_after_inspection(item_id):
        result = inspect(item_id)
        monkeypatch.setattr(settings, "performance_specification_mode", "shadow")
        return result

    monkeypatch.setattr(case.gateway, "inspect_files", restrict_after_inspection)
    worker._cleanup_specifications(case.db, BOARD, ITEM, current(case).current_revision,
                                   case.gateway, source)
    assert case.gateway.deletes == []
    assert previous.status == "published"
    assert settings.design_processing_mode == "enabled"
