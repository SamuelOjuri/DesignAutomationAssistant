"""Attached current requests survive the full analysis and intent boundaries."""

from email.message import EmailMessage
from email.mime.message import MIMEMessage
import hashlib
import pytest

from backend.app.services.design_processing_inputs import DesignEmailAsset, DownloadedDesignEmailAsset
from backend.app.services.legacy_enquiry.analysis import analyze_downloaded_email_assets
from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction
from backend.app.services.legacy_enquiry.source_documents import source_record
from backend.app.services.performance_specification_evidence import (
    resolve_baseline_drawing_identity, resolve_email_evidence,
)
from backend.app.services.specification_intent import build_request_intent
from backend.app.services.specification_request_context import select_client_request, source_email_messages
from backend.app.services.specification_semantics import EVIDENCE_VERSION, FIELD_DEFINITIONS


OUTER = """From: Requester <requester@manufacturer.example>
To: Sales <sales@taperedplus.co.uk>
Subject: Synthetic Street
Date: Mon, 16 Nov 2026 11:23:25 +0000

Please see attached. Could you amend as per the attached please?
Manufacturer Ltd
Requester

From: Sales <sales@taperedplus.co.uk>
To: Requester <requester@manufacturer.example>
Date: Thu, 12 Nov 2026 13:52:00 +0000
Subject: Synthetic Street

Issued TP12345_22.02-D and TP12345_26.03-A. Existing 16mm LEWIS decking.
"""
NESTED = """From: Architect <architect@architect.example>
To: Requester <requester@manufacturer.example>
Subject: Synthetic Street
Date: Mon, 16 Nov 2026 10:45:51 +0000

Provide an additional calculation following the same principles as TP12345_22.02-D.
Use the proposed plywood deck and achieve 0.10 W/m²K.
Assume 150mm insulation inside the 200mm cassette zone.

From: Requester <requester@manufacturer.example>
To: Architect <architect@architect.example>
Date: Mon, 16 Nov 2026 10:01:00 +0000
Subject: Synthetic Street

The old scheme TP12345_26.03-A used 16mm LEWIS decking.
"""


def manifest():
    outer = source_record("asset:1:body", "Email body", OUTER.encode(), source_type="email_body",
                          observed_at="2026-11-16T11:23:25+00:00", text=OUTER)
    nested = source_record("asset:1:attachment:0:body", "Attached email body", NESTED.encode(),
                           source_type="email_body", observed_at="2026-11-16T10:45:51+00:00", text=NESTED)
    nested.update(parentSourceId=outer["sourceId"], attachedEmail=True,
                  emailContainerSha256=hashlib.sha256(NESTED.encode()).hexdigest())
    return [outer, nested]


def test_intent_discovers_adopted_head_reference_without_promoting_attached_history():
    sources = manifest()
    intent = build_request_intent(sources)
    assert intent.recommended_type == "amendment"
    references = [entry for entry in intent.detected_references
                  if entry.evidence.source_id == sources[1]["sourceId"]]
    assert {(entry.literal, entry.role) for entry in references} == {
        ("TP12345_22.02-D", "document"), ("TP12345_26.03-A", "forwarded_context"),
    }
    assert select_client_request(sources)["sender"] == "requester@manufacturer.example"


def test_fyi_attached_message_cannot_supply_current_intent_or_reference():
    sources = manifest()
    sources[0]["text"] = OUTER.replace(
        "Please see attached. Could you amend as per the attached please?", "Attached for information only.")
    intent = build_request_intent(sources)
    assert intent.recommended_type == "uncertain"
    assert not any(entry.evidence.source_id == sources[1]["sourceId"] for entry in intent.detected_references)


def test_intent_accepts_plain_alternative_with_verified_same_attached_head():
    sources = manifest()
    line = "Provide an additional calculation following the same principles as TP12345_22.02-D."
    sources[1]["emailHtmlText"] = NESTED.replace(line, line.replace("principles as", "principles\nas"))
    proposal = {"recommended_type": "amendment", "explanation": "Current request adopts the additional calculation",
        "evidence": [{"source_id": sources[0]["sourceId"], "excerpt": "Could you amend as per the attached please?",
                      "location": "current covering request"},
                     {"source_id": sources[1]["sourceId"], "excerpt": line, "location": "attached request"}]}
    intent = build_request_intent(sources, proposal)
    assert intent.recommended_type == "amendment" and not intent.ambiguity_reasons


class AnalysisClient:
    max_attachment_workers = 1

    def extract_design_parameters_with_manifest(self, context, sources):
        self.context = context
        outer = next(source for source in sources if source["sourceType"] == "email_body" and not source.get("attachedEmail"))
        nested = next(source for source in sources if source.get("attachedEmail"))
        outer_message = source_email_messages(outer)[0]
        nested_message = source_email_messages(nested)[0]
        historic = source_email_messages(outer)[1]

        def evidence(label, value, snippet, source, message, role="requested"):
            return {"field": label, "value": value, "source_id": source["sourceId"], "snippet": snippet,
                    "location": "literal message body", "scope": None, "issuer": None,
                    "role": role, "request_role": message["requestRole"], "message_date": message["sentDate"],
                    "rationale": "Literal source fact within its own message"}

        data = {key: None for key, field in DesignParameterExtraction.model_fields.items() if field.is_required()}
        data.update(email_subject="Synthetic Street", drawing_title="Synthetic Street",
            company="Manufacturer Ltd", contact="Requester", decking="plywood deck", target_u_value="0.10 W/m²K",
            specification_assessed_fields=list(set(FIELD_DEFINITIONS) - {"Incoming Information"}),
            specification_evidence=[
                evidence("Company", "Manufacturer Ltd", "Manufacturer Ltd", outer, outer_message),
                evidence("Contact", "Requester", "Requester", outer, outer_message),
                evidence("Drawing Title", "Synthetic Street", "Subject: Synthetic Street", outer, outer_message),
                evidence("Decking", "plywood deck", "Use the proposed plywood deck and achieve 0.10 W/m²K.", nested, nested_message, "baseline"),
                evidence("Decking", "16mm LEWIS decking", "Existing 16mm LEWIS decking.", outer, historic, "baseline"),
                evidence("Target U Value", "0.10 W/m²K", "Use the proposed plywood deck and achieve 0.10 W/m²K.", nested, nested_message),
                evidence("Drawing Reference", "TP12345_22.02-D", "following the same principles as TP12345_22.02-D.", nested, nested_message, "baseline"),
                evidence("Drawing Reference", "TP12345_22.02-D", "Issued TP12345_22.02-D and TP12345_26.03-A.", outer, historic, "baseline"),
                evidence("Drawing Reference", "TP12345_26.03-A", "Issued TP12345_22.02-D and TP12345_26.03-A.", outer, historic, "baseline"),
            ])
        return DesignParameterExtraction.model_validate(data)

    def query_llm(self, *args, **kwargs):
        raise AssertionError("The verified title needs no second model call")


def test_unnamed_attached_request_survives_full_analysis(tmp_path):
    from email import policy
    from email.parser import Parser

    outer = Parser(policy=policy.default).parsestr(OUTER)
    # Rebuild with a MIME container so the sole substantive attachment has no filename.
    email = EmailMessage()
    for key in ("From", "To", "Subject", "Date"):
        email[key] = outer[key]
    email.set_content(outer.get_content())
    email.make_mixed()
    nested_source = Parser(policy=policy.default).parsestr(NESTED)
    nested_email = EmailMessage()
    for key in ("From", "To", "Subject", "Date"):
        nested_email[key] = nested_source[key]
    nested_email.set_content(NESTED.split("\n\n", 1)[1])
    attached = MIMEMessage(nested_email)
    attached["Content-Disposition"] = "attachment"
    email.attach(attached)
    content = email.as_bytes()
    path = tmp_path / "request.eml"
    path.write_bytes(content)
    downloaded = DownloadedDesignEmailAsset(
        source=DesignEmailAsset("1", "request.eml", ".eml", len(content),
            "2026-11-16T11:24:00Z", "", False), temp_path=str(path),
        content_type="message/rfc822", content_sha256=hashlib.sha256(content).hexdigest(), size_bytes=len(content))
    client = AnalysisClient()
    result = analyze_downloaded_email_assets([downloaded], client=client)
    assert "plywood deck" in client.context and "PARENT" in client.context.upper()
    payload = {"specificationEvidenceVersion": EVIDENCE_VERSION,
        "specificationAssessedFields": result.specification_assessed_fields,
        "specificationEvidence": result.specification_evidence, "sourceManifest": list(result.source_manifest),
        "performanceParameters": result.performance_parameters}
    values, _, issues = resolve_email_evidence(payload)
    assert values["Decking"] == "plywood deck" and "Decking" not in issues
    baseline, issue = resolve_baseline_drawing_identity(payload)
    assert issue is None and {entry["value"] for entry in baseline} == {"TP12345_22.02-D"}
    assert values["Company"] == "Manufacturer Ltd" and values["Contact"] == "Requester"
    assert result.parameters["Hour Received"] == "11:23"
    assert result.specification_request_intent["recommended_type"] == "amendment"


def test_top_level_email_limit_retains_permanent_source_diagnostic(tmp_path, monkeypatch):
    from backend.app.services.legacy_enquiry import analysis
    from backend.app.services.legacy_enquiry.source_documents import (
        IncompleteSourceExtraction, SourceDocumentError, permanent_source_failure,
    )

    content = b"oversized or over-complex email"
    path = tmp_path / "limited.eml"
    path.write_bytes(content)
    downloaded = DownloadedDesignEmailAsset(
        DesignEmailAsset("1", "limited.eml", ".eml", len(content), "2026-11-16T11:24:00Z", "", False),
        str(path), "message/rfc822", hashlib.sha256(content).hexdigest(), len(content))

    def reject(*args, **kwargs):
        raise SourceDocumentError("email_part_limit", "Email contains too many MIME parts; split the source.")

    monkeypatch.setattr(analysis, "process_email_content", reject)
    with pytest.raises(IncompleteSourceExtraction) as error:
        analyze_downloaded_email_assets([downloaded], client=AnalysisClient())
    assert error.value.manifest[0]["error"]["code"] == "email_part_limit"
    assert permanent_source_failure(error.value.manifest)
