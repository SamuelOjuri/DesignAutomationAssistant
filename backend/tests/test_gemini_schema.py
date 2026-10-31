"""Wire-schema compatibility must not weaken backend validation."""

import pytest
from pydantic import BaseModel, Field, ValidationError

from backend.app.services.gemini_schema import gemini_response_schema
from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction
from backend.app.services.performance_specification_contract import CADTranslation


class Child(BaseModel):
    labels: list[str] = Field(min_length=1, max_length=2)


class Request(BaseModel):
    children: list[Child] = Field(max_length=10)
    maxItems: str  # An actual field name must not be removed as a schema keyword.


def test_wire_schema_removes_only_array_bounds_including_nested_definitions():
    original = Request.model_json_schema()
    wire = gemini_response_schema(Request)
    assert "maxItems" not in wire["properties"]["children"]
    assert "minItems" not in wire["$defs"]["Child"]["properties"]["labels"]
    assert "maxItems" not in wire["$defs"]["Child"]["properties"]["labels"]
    assert wire["properties"]["maxItems"] == original["properties"]["maxItems"]
    assert wire["required"] == original["required"]
    assert Request.model_json_schema() == original
    with pytest.raises(ValidationError):
        Request.model_validate({"children": [{"labels": []}], "maxItems": "kept"})
    with pytest.raises(ValidationError):
        Request.model_validate({"children": [{"labels": ["a", "b", "c"]}], "maxItems": "kept"})


def test_extraction_wire_schema_keeps_field_meanings_and_local_evidence_limits():
    original = DesignParameterExtraction.model_json_schema()
    wire = gemini_response_schema(DesignParameterExtraction)
    assert original["properties"]["specification_evidence"]["maxItems"] == 512
    assert "maxItems" not in wire["properties"]["specification_evidence"]
    assert "maxItems" not in wire["properties"]["specification_assessed_fields"]
    assert wire["properties"]["revision"] == original["properties"]["revision"]
    assert wire["$defs"]["SpecificationEvidence"]["properties"]["role"] == original["$defs"]["SpecificationEvidence"]["properties"]["role"]
    assert "maxItems" not in wire["$defs"]["SpecificationEvidence"]["properties"]["reference_parts"]
    data = {name: None for name in DesignParameterExtraction.model_fields
            if not name.startswith("specification_")}
    data["specification_assessed_fields"] = ["Scale"] * 19
    with pytest.raises(ValidationError):
        DesignParameterExtraction.model_validate(data)
    data["specification_assessed_fields"] = []
    evidence = {"field": "Scale", "value": "1:100", "source_id": "mail", "location": "body", "snippet": "Scale: 1:100",
                "scope": None, "issuer": None, "role": "requested", "rationale": "Explicit"}
    data["specification_evidence"] = [evidence] * 513
    with pytest.raises(ValidationError):
        DesignParameterExtraction.model_validate(data)


def test_cad_wire_schema_keeps_enums_and_client_warning_limits():
    original = CADTranslation.model_json_schema()
    wire = gemini_response_schema(CADTranslation)
    assert wire["$defs"] == original["$defs"]
    assert "maxItems" not in wire["properties"]["warnings"]
    values = {field.alias: None for field in CADTranslation.model_fields["values"].annotation.model_fields.values()}
    values["Incoming Information"] = ""
    with pytest.raises(ValidationError):
        CADTranslation.model_validate({"values": values, "warnings": ["warning"] * 65})
