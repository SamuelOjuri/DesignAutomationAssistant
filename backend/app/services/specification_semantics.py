"""Shared field meanings and evidence contract for extraction and CAD export.

These definitions describe business fields, not material aliases. Product-to-CAD
translation remains a model task, checked against the export schema.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


EVIDENCE_VERSION = 2
ANIMATED_IMAGE_SKIP_STATUS = "skipped:animated_image"
COMPLETE_SOURCE_STATUSES = frozenset({"read", ANIMATED_IMAGE_SKIP_STATUS})
FIELD_DEFINITIONS = {
    "Scale": "The drawing scale, usually found in a drawing title block (for example, 1:100).",
    "Page Size": "The drawing page or paper size, usually found in a drawing title block (for example, A1).",
    "Drawing Reference": (
        "The Drawing Reference Number issued by TaperedPlus "
        "[e.g. TP*****_**.** - *]. If several references exist, use the latest "
        "one associated with the request to TaperedPlus."
    ),
    "Drawing Title": "The drawing project name, usually the project location.",
    "Revision": (
        "The suffix of the selected TaperedPlus Drawing Reference. This is the "
        "suffix after the underscore in the drawing reference "
        "[e.g. **.** - * from TP*****_**.** - *]."
    ),
    "Date Received": (
        "The date the client sent the request to TaperedPlus. For forwarded chains, "
        "do not use the date of an earlier message."
    ),
    "Company": (
        "The company directly requesting drawings or services from TaperedPlus. For "
        "a forwarded email, use the company of the person sending to TaperedPlus, "
        "not an earlier sender."
    ),
    "Contact": (
        "The person directly requesting the job or drawings from TaperedPlus. For a "
        "forwarded email, use the person sending to TaperedPlus, not an earlier sender."
    ),
    "Surveyor": "The surveyor's name, when provided.",
    "Target U Value": "The primary target U-value for the main insulation area.",
    "Target Min U Value": "A secondary or minimum target U-value, often for an area such as upstands.",
    "Tapered Insulation": "The type or brand of tapered insulation product requested.",
    "Decking": "The type of roof decking material described.",
    "Bauder Contract Number": (
        "The contract or project reference assigned by Bauder Ltd, the roofing-system "
        "manufacturer and supplier. It is usually found in the email subject, but may "
        "also appear in the email body or an attachment, and commonly has the format "
        "B followed by six digits (for example, B123456). Extract only a reference "
        "explicitly identified as Bauder's contract or project number. Do not assume "
        "that Bauder is the customer or requester, and do not infer a number merely "
        "because Bauder products are specified. Return null when no such reference "
        "is present."
    ),
    "Bauder Roof Num": (
        "A roof number explicitly identified as Bauder's roof number in the email "
        "subject or body. Preserve leading zeroes and letters. Do not substitute a "
        "contract number, drawing reference, or infer a roof number. Usually absent; "
        "return null when not explicitly specified."
    ),
    "Membrane": "The type of waterproofing membrane material requested for the roofing solution.",
    "VCL": "The Air and Vapour Control Layer (VCL or AVCL) component requested for the roof insulation.",
    "Incoming Information": "Always blank; do not extract or translate this field.",
}

SpecificationField = Literal[tuple(FIELD_DEFINITIONS)]


def missing(value) -> bool:
    return value is None or not str(value).strip() or bool(re.match(
        r"^(?:not\s+(?:provided|found|available)|none|null|n/?a)(?:$|\s*\()",
        str(value).strip(), re.IGNORECASE,
    ))


def canonical_label(label: str) -> str:
    return label.replace("U-Value", "U Value")


def tp_reference_revision(reference: str | None, *, confirmed_crm: bool = False) -> str | None:
    """Only TP-issued references qualify; CRM drawing membership verifies issuer.

    A bare manufacturer reference must not acquire a TP prefix by inference.
    """
    prefix = r"(?:TP)?" if confirmed_crm else "TP"
    pattern = prefix + r"\d+_[A-Z0-9][A-Z0-9. /-]{0,127}"
    if reference and re.fullmatch(pattern, reference.strip(), re.IGNORECASE):
        return reference.split("_", 1)[1].strip()
    return None


class SpecificationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: SpecificationField = Field(description="Exact CAD field label from the supplied shared definitions.")
    value: str = Field(min_length=1, max_length=2048, description="Raw fact, preserving identifiers and units.")
    source_id: str = Field(description="Exact SOURCE ID from the supplied source inventory.")
    location: str = Field(description="Page/table, worksheet and cells, or email header/body/forwarded-message location.")
    snippet: str = Field(min_length=1, max_length=4000, description="Verbatim supporting excerpt from that source's extracted text.")
    scope: str | None = Field(description="Explicit roof or drawing identifier; null for a project-wide fact. Never invent a scope.")
    issuer: str | None = Field(description="Issuer of a drawing/reference, if explicit. Bauder is not TaperedPlus.")
    role: Literal["requested", "achieved", "lookup", "other"] = Field(
        description="requested = applicable request/specification; achieved = calculated result; lookup = unselected catalogue/template option."
    )
    request_role: Literal["direct_request", "forwarded_request", "document"] = Field(
        default="document", description="direct_request identifies the outer request to TaperedPlus or a form explicitly identifying its direct requester; forwarded_request is an earlier message in that chain."
    )
    message_date: str | None = Field(
        default=None, description="Verbatim sent-date/time from the corresponding email/forwarded-message header, including year. Null for an undated document; never use an upload timestamp."
    )
    supersedes: list[str] = Field(default_factory=list, description="Older raw values explicitly replaced by this fact in the same scope; do not infer chronology from asset IDs.")
    rationale: str = Field(max_length=2000, description="Why this fact applies, including forwarded-chain/request timing and scope.")
