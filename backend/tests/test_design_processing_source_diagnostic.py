import json
from dataclasses import replace

from backend.scripts import check_design_processing_sources as diagnostic


def test_source_probe_checks_real_readers_without_external_services(monkeypatch, capsys):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "a" * 40)
    assert diagnostic.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["commit"] == "a" * 12
    assert payload["pipeline_revision"] == "output-v18"
    assert payload["pid"] > 0
    assert payload["dwg_status"] == "skipped:dwg"
    assert payload["encrypted_pdf_status"] == "skipped:encrypted_pdf"
    assert payload["zip_status"] == "expanded:archive"
    assert all(check["passed"] for check in payload["checks"])
    intake = payload["checks"][-1]
    assert intake["email_assets"] == ["request.eml"]
    assert intake["supplementary_assets"] == ["roof-plans.zip"]
    assert intake["zip_add_changes_revision"] is True
    assert intake["zip_change_changes_revision"] is True


def test_source_probe_returns_failure_when_deployed_reader_does_not_skip_dwg(monkeypatch, capsys):
    actual = diagnostic.extract_source_records
    def outdated_reader(content, filename, *args, **kwargs):
        if filename.endswith(".dwg"):
            return [{"status": "failed:ValueError", "text": ""}]
        return actual(content, filename, *args, **kwargs)
    monkeypatch.setattr(diagnostic, "extract_source_records", outdated_reader)
    assert diagnostic.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed" and not payload["checks"][0]["passed"]


def test_source_probe_detects_worker_without_encrypted_pdf_skip(monkeypatch, capsys):
    actual = diagnostic.extract_source_records

    def outdated_reader(content, filename, *args, **kwargs):
        if filename.endswith(".pdf"):
            return [{"status": "read", "text": ""}]
        return actual(content, filename, *args, **kwargs)

    monkeypatch.setattr(diagnostic, "extract_source_records", outdated_reader)
    assert diagnostic.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert not next(check for check in payload["checks"] if check["source"] == "protected.pdf")["passed"]


def test_source_probe_detects_legacy_intake_ignoring_separate_email_column_zip(monkeypatch, capsys):
    actual = diagnostic.parse_design_processing_target

    def outdated_parser(item):
        return replace(actual(item), supplementary_assets=(), input_revision="email-only-revision")

    monkeypatch.setattr(diagnostic, "parse_design_processing_target", outdated_parser)
    assert diagnostic.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert all(check["passed"] for check in payload["checks"][:-1])
    intake = payload["checks"][-1]
    assert intake["passed"] is False
    assert intake["supplementary_assets"] == []
    assert intake["zip_add_changes_revision"] is False
    assert intake["zip_change_changes_revision"] is False


def test_source_probe_supports_configured_extra_columns_without_changing_settings(monkeypatch):
    monkeypatch.setattr(diagnostic.settings, "design_processing_source_file_column_ids", ["source_docs"])
    assert diagnostic.check_email_column_zip_intake()["passed"] is True
    assert diagnostic.settings.design_processing_source_file_column_ids == ["source_docs"]
