"""File-only analysis exposes missing client context without inventing a request."""
import hashlib
import io
from email.message import EmailMessage
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from backend.app.services.legacy_enquiry.analysis import analyze_downloaded_email_assets
from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.specification_semantics import MISSING_CLIENT_EMAIL_REVIEW_STATUS
from backend.tests.test_specification_evidence import candidate
from backend.tests.test_specification_publication_hardening import parameters
from backend.tests.test_zip_only_review_flow import missing_email


SUMMARY = "Project: Example Campus\nTarget U-value: 0.15\nCompany: Survey Firm\nContact: Survey Contact\nDate of visit: 12 Nov 2026"


def download(tmp_path, name, content, identity):
    path = tmp_path / name
    path.write_bytes(content)
    return SimpleNamespace(temp_path=str(path), content_sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content),
        source=SimpleNamespace(asset_id=identity, filename=name, file_extension="zip" if name.endswith(".zip") else "eml"))


def zip_download(tmp_path):
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("summary.txt", SUMMARY)
    return download(tmp_path, "survey.zip", output.getvalue(), "1")


@pytest.mark.parametrize("typed_response", [True, False])
def test_llm_clears_survey_client_claims_in_initial_and_recovery_responses(typed_response):
    source = {"sourceId": "asset:1:member:0", "filename": "survey.zip!/summary.txt", "sourceType": "attachment",
              "status": "read", "text": SUMMARY, "contentSha256": "abc", "observedAt": None}
    response = parameters(company="Survey Firm", contact="Survey Contact", date_received="12 Nov 2026", hour_received="10:00",
        specification_evidence=[candidate("Company", "Survey Firm", "Company: Survey Firm", source_id=source["sourceId"]),
                                candidate("Contact", "Survey Contact", "Contact: Survey Contact", source_id=source["sourceId"])])
    calls = []

    def generate(model, prompt, config):
        calls.append(prompt)
        return SimpleNamespace(parsed=response.model_copy(deep=True) if typed_response else response.model_dump())

    result = LegacyGeminiClient("offline", generate_content=generate).extract_design_parameters_with_manifest(
        SUMMARY, [source, missing_email()])
    assert len(calls) == 2  # Missing technical facts still receive normal bounded recovery.
    assert "No client email was provided" in calls[0]
    assert result.company is result.contact is result.date_received is result.hour_received is None
    assert not result.specification_evidence


def test_zip_only_analysis_retains_technical_evidence_and_marks_missing_email(tmp_path):
    callbacks, contexts = [], []

    def extract(context, manifest):
        contexts.append(context)
        summary, = [record for record in manifest if record["status"] == "read"]
        return parameters(drawing_title="Example Campus", target_u_value="0.15",
            company="Survey Firm", contact="Survey Contact", date_received="12 Nov 2026", hour_received="10:00",
            specification_evidence=[
                candidate("Drawing Title", "Example Campus", "Project: Example Campus", source_id=summary["sourceId"]),
                candidate("Target U Value", "0.15", "Target U-value: 0.15", source_id=summary["sourceId"]),
                candidate("Company", "Survey Firm", "Company: Survey Firm", source_id=summary["sourceId"]),
            ])

    result = analyze_downloaded_email_assets([zip_download(tmp_path)], client=SimpleNamespace(
        extract_design_parameters_with_manifest=extract, query_llm=lambda *args: pytest.fail("Verified title should be reused")),
        on_progress=callbacks.append)
    assert result.project_name == "Example Campus" and result.performance_parameters["Target U-Value"] == "0.15"
    assert result.performance_parameters["Company"] is result.performance_parameters["Contact"] is None
    assert result.performance_parameters["Date Received"] is None
    assert result.parameters["Date Received"] == result.parameters["Hour Received"] == "Not provided"
    assert result.email_content_audit == ()
    assert result.specification_request_intent["recommended_type"] == "uncertain"
    assert len([record for record in result.source_manifest if record["status"] == MISSING_CLIENT_EMAIL_REVIEW_STATUS]) == 1
    assert any(event["source_counts"]["review"] == 1 for event in callbacks)
    assert all(event["source_counts"]["failed"] == 0 for event in callbacks)
    assert result.sources["Drawing Title"] == "Provided Files"
    assert "Target U-value: 0.15" in contexts[0]


def test_real_email_arrival_removes_missing_email_marker_and_restores_request_date(tmp_path):
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "client@example.com", "sales@taperedplus.co.uk", "Example Campus"
    message["Date"] = "Fri, 13 Nov 2026 09:30:00 +0000"
    message.set_content("Please review the supplied files.")
    email = download(tmp_path, "request.eml", message.as_bytes(), "2")
    result = analyze_downloaded_email_assets([zip_download(tmp_path), email], client=SimpleNamespace(
        extract_design_parameters=lambda context: parameters(), query_llm=lambda *args: "Example Campus"))
    assert not any(record["status"] == MISSING_CLIENT_EMAIL_REVIEW_STATUS for record in result.source_manifest)
    assert result.parameters["Date Received"] == "13 Nov 2026" and result.parameters["Hour Received"] == "09:30"
