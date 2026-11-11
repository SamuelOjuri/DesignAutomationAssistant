"""Recovery remains for missing assessments and invalid evidence, not verified absence."""

from types import SimpleNamespace

import pytest

from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.performance_specification_evidence import resolve_email_evidence
from backend.app.services.specification_recovery import extraction_payload
from backend.tests.test_specification_evidence import candidate, source
from backend.tests.test_specification_publication_hardening import parameters


def extract(initial, recovered=None, *, manifest):
    responses = iter([initial.model_dump(), (recovered or initial).model_dump()])
    calls = []

    def generate(model, contents, config):
        calls.append(contents)
        return SimpleNamespace(parsed=next(responses))

    result = LegacyGeminiClient("offline", generate_content=generate).extract_design_parameters(
        "Local source context", source_manifest=manifest,
    )
    return result, calls


@pytest.mark.parametrize("skip_status", [
    None, "skipped:unsupported_format", "skipped:dwg", "skipped:animated_image", "skipped:encrypted_pdf", "expanded:archive",
])
def test_complete_sources_and_explicitly_assessed_absence_do_not_repeat_model_call(skip_status):
    manifest = [source("asset:1:attachment:0", "No roof performance specification was supplied.")]
    if skip_status:
        skipped = source("asset:1:attachment:1", "")
        skipped["status"] = skip_status
        manifest.append(skipped)
    result, calls = extract(parameters(), manifest=manifest)
    assert result.vcl is None and len(calls) == 1
    assert not resolve_email_evidence(extraction_payload(result, manifest))[2]


def test_unassessed_absent_field_still_recovers_its_literal_fact():
    initial = parameters()
    initial.specification_assessed_fields.remove("VCL")
    recovered = parameters(vcl="Polythene AVCL", specification_evidence=[
        candidate("VCL", "Polythene AVCL", "VCL: Polythene AVCL"),
    ])
    result, calls = extract(initial, recovered, manifest=[source("asset:1:attachment:0", "VCL: Polythene AVCL")])
    assert len(calls) == 2 and result.vcl == "Polythene AVCL"


@pytest.mark.parametrize("initial_evidence", [[], [candidate("VCL", "Polythene AVCL", "Invented excerpt")]])
def test_populated_fact_without_verified_evidence_still_gets_recovery(initial_evidence):
    initial = parameters(vcl="Polythene AVCL", specification_evidence=initial_evidence)
    recovered = parameters(vcl="Polythene AVCL", specification_evidence=[
        candidate("VCL", "Polythene AVCL", "VCL: Polythene AVCL"),
    ])
    manifest = [source("asset:1:attachment:0", "VCL: Polythene AVCL")]
    result, calls = extract(initial, recovered, manifest=manifest)
    assert len(calls) == 2 and result.vcl == "Polythene AVCL"
    assert not resolve_email_evidence(extraction_payload(result, manifest))[2]


def test_failed_evidence_is_not_accepted_when_recovery_keeps_the_invalid_excerpt():
    initial = parameters(vcl="Polythene AVCL", specification_evidence=[
        candidate("VCL", "Polythene AVCL", "Invented excerpt"),
    ])
    with pytest.raises(ValueError, match="recovery unresolved"):
        extract(initial, manifest=[source("asset:1:attachment:0", "VCL: Polythene AVCL")])


@pytest.mark.parametrize("manifest", [None, [source("asset:1:attachment:0", "No specification supplied."),
    {**source("asset:1:attachment:1", ""), "status": "review:provider_recitation"}]])
def test_absence_without_fully_readable_or_intentionally_skipped_sources_keeps_recovery(manifest):
    _, calls = extract(parameters(), manifest=manifest)
    assert len(calls) == 2
