import json

from backend.scripts import check_design_processing_sources as diagnostic


def test_source_probe_checks_real_readers_without_external_services(monkeypatch, capsys):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "a" * 40)
    assert diagnostic.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["commit"] == "a" * 12
    assert payload["pipeline_revision"] == "output-v15"
    assert payload["pid"] > 0
    assert payload["dwg_status"] == "skipped:dwg"
    assert payload["encrypted_pdf_status"] == "skipped:encrypted_pdf"
    assert payload["zip_status"] == "expanded:archive"
    assert all(check["passed"] for check in payload["checks"])


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
