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
DWG_SKIP_STATUS = "skipped:dwg"
ENCRYPTED_PDF_SKIP_STATUS = "skipped:encrypted_pdf"
ARCHIVE_EXPANDED_STATUS = "expanded:archive"
# Gemini withheld a transcription with finish_reason=RECITATION. The source has
# no text and cannot supply evidence, but unlike a skip it is never silent: it is
# published as an explicit source-review note instead of blocking extraction.
PROVIDER_RECITATION_REVIEW_STATUS = "review:provider_recitation"
REVIEW_SOURCE_STATUSES = frozenset({PROVIDER_RECITATION_REVIEW_STATUS})
COMPLETE_SOURCE_STATUSES = frozenset({
    "read", ANIMATED_IMAGE_SKIP_STATUS, DWG_SKIP_STATUS, ENCRYPTED_PDF_SKIP_STATUS, ARCHIVE_EXPANDED_STATUS,
}) | REVIEW_SOURCE_STATUSES
FIELD_DEFINITIONS = {
    "Scale": "The drawing scale, usually found in a drawing title block (for example, 1:100).",
    "Page Size": "The drawing page or paper size, usually found in a drawing title block (for example, A1).",
    "Drawing Reference": (
        "The Drawing Reference Number issued by TaperedPlus "
        "[e.g. TP*****_**.** - *]. Retain accepted AX references such as "
        "AX 16670_26.12 - B and D.S references exactly, including their actual issuer; "
        "never invent a TP prefix or replace AXTER branding with TaperedPlus. "
        "If several references exist, use the latest "
        "one associated with the request to TaperedPlus."
    ),
    "Drawing Title": "The drawing project name, usually the project location.",
    "Revision": (
        "The suffix of the selected TaperedPlus Drawing Reference. This is the "
        "suffix after the underscore in the drawing reference "
        "[e.g. **.** - * from TP*****_**.** - *]."
    ),
    "Date Received": (
        "The date the selected client sent the request to TaperedPlus. Company, "
        "Contact and Date Received share that client-request context. An internal "
        "TaperedPlus follow-up does not replace the client's date. A verified "
        "forwarded client header can identify that request when its original email is absent."
    ),
    "Company": (
        "The company directly requesting drawings or services from TaperedPlus. For "
        "a forwarded email, use the client sending to TaperedPlus, not an earlier "
        "third-party sender or the internal employee forwarding it. Preserve literal "
        "visible branding; never expand a domain or invent a legal suffix."
    ),
    "Contact": (
        "The person directly requesting the job or drawings from TaperedPlus. For a "
        "forwarded email, use the client sending to TaperedPlus, not an earlier "
        "third-party sender or an internal TaperedPlus follow-up."
    ),
    "Surveyor": "The surveyor's name, when provided.",
    "Target U Value": "The primary requested target U-value for the main insulation area, never an achieved calculation.",
    "Target Min U Value": (
        "A secondary or minimum requested target U-value, often for an area such as "
        "upstands. The source must establish that role; never infer it from an achieved result."
    ),
    "Tapered Insulation": "The type or brand of tapered insulation product requested.",
    "Decking": (
        "The type of roof decking material described for the applicable roof, "
        "including existing/baseline construction unless replaced or withdrawn. "
        "Retain a literal but ambiguous deck/system description for review; a brand "
        "alone does not establish structural material or timber thickness."
    ),
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


REFERENCE_FIELDS = frozenset({"Drawing Reference", "Revision"})
# Outlook and word processors often typeset 'TP123_45.67 - A' with an en dash.
_REFERENCE_DASHES = str.maketrans(dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"))


def normalize_reference_dashes(value):
    """Read typographic dashes in a drawing reference as the standard '-'."""
    return value.translate(_REFERENCE_DASHES) if isinstance(value, str) else value


U_VALUE_FIELDS = frozenset({"Target U Value", "Target Min U Value"})


def normalize_unit_spacing(value):
    """'0.15W/m²K' and '0.15 W/m²K' state the same U-value; other text is unchanged.

    One canonical space keeps a bare number such as 0.15 a whole word.
    """
    return re.sub(r"(?<=\d)\s*(?=W\s*/)", " ", value, flags=re.IGNORECASE) if isinstance(value, str) else value


# Any other two-letter prefix (e.g. FA18910_26.01 - A) is accepted for review only.
REVIEW_REFERENCE_PREFIX = r"(?!TP|AX)[A-Z]{2}\s*"
_REFERENCE_NUMBER = r"\d+_\d+\.\d+(?:\s*-\s*[A-Z]+)?"
# A literal reference inside source text, including review-only prefixes.
REFERENCE_TEXT_PATTERN = (
    r"(?<!\w)(?:(?:TP|AX)\s*|D\.S\s*-\s*|" + REVIEW_REFERENCE_PREFIX + r")?"
    + _REFERENCE_NUMBER + r"(?!\w|\.\w)"
)


def review_prefix_reference(reference: str | None) -> bool:
    """A reference whose two-letter prefix is neither TP nor the accepted AX family."""
    reference = normalize_reference_dashes(reference)
    return bool(reference and re.fullmatch(
        REVIEW_REFERENCE_PREFIX + _REFERENCE_NUMBER, reference.strip(), re.IGNORECASE))


def tp_reference_revision(reference: str | None, *, confirmed_crm: bool = False,
                          review_prefix: bool = False) -> str | None:
    """Accepted drawing families, preserving branding and the complete suffix.

    AX is an explicitly accepted legacy family. Bare numbers and D.S require
    confirmed CRM membership or independently verified TP correspondence.
    Other two-letter prefixes parse only with review_prefix; callers must flag
    those references for review rather than treat them as verified TP drawings.
    """
    prefix = r"(?:(?:TP|AX)\s*|D\.S\s*-\s*)?" if confirmed_crm else r"(?:TP|AX)\s*"
    if review_prefix:
        prefix = r"(?:" + REVIEW_REFERENCE_PREFIX + "|" + prefix + ")"
    pattern = prefix + _REFERENCE_NUMBER
    reference = normalize_reference_dashes(reference)
    if reference and re.fullmatch(pattern, reference.strip(), re.IGNORECASE):
        return reference.split("_", 1)[1].strip()
    return None


def drawing_reference_key(reference: str | None) -> str:
    value = re.sub(r"\s+", "", normalize_reference_dashes(str(reference or ""))).casefold()
    # Bare CRM references and TP-prefixed references share a family; AX does not.
    return value.removeprefix("tp") if re.match(r"^(?:tp)?\d+_", value) else value


def scale_ratio(value: str | None) -> tuple[str, str] | None:
    """Ordered terms of a value that is exactly one scale ratio (1:50, 1/50, 1 to 50).

    Notation is not a fact: these forms state the same scale. Term order still
    matters (1:50 is not 50:1), and any surrounding text is not a bare ratio.
    """
    match = re.fullmatch(r"\s*(\d+)\s*(?::|/|\bto\b)\s*(\d+)\s*", str(value or ""), re.IGNORECASE)
    return (match[1], match[2]) if match else None


class SpecificationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: SpecificationField = Field(description="Exact CAD field label from the supplied shared definitions.")
    value: str = Field(min_length=1, max_length=2048, description="Raw fact, preserving identifiers and units.")
    source_id: str = Field(description="Exact SOURCE ID from the supplied source inventory.")
    location: str = Field(description="Page/table, worksheet and cells, or email header/body/forwarded-message location.")
    snippet: str = Field(min_length=1, max_length=4000, description="Verbatim supporting excerpt from that source's extracted text.")
    scope: str | None = Field(description="Explicit roof or drawing identifier; null for a project-wide fact. Never invent a scope.")
    issuer: str | None = Field(description="Issuer of a drawing/reference, if explicit. Bauder is not TaperedPlus.")
    role: Literal["requested", "achieved", "lookup", "baseline", "withdrawn", "other"] = Field(
        description="requested = applicable request/specification; achieved = calculated result; lookup = unselected option; baseline = existing scheme being changed or compared; withdrawn = explicitly retracted statement."
    )
    request_role: Literal["direct_request", "forwarded_request", "document"] = Field(
        default="document", description="direct_request identifies the outer request to TaperedPlus or a form explicitly identifying its direct requester; forwarded_request is an earlier message in that chain."
    )
    message_date: str | None = Field(
        default=None, description="Verbatim sent-date/time from the corresponding email/forwarded-message header, including year. Null for an undated document; never use an upload timestamp."
    )
    supersedes: list[str] = Field(default_factory=list, description="Older raw values explicitly replaced by this fact in the same scope; do not infer chronology from asset IDs.")
    rationale: str = Field(max_length=2000, description="Why this fact applies, including forwarded-chain/request timing and scope.")
    reference_parts: list[str] = Field(default_factory=list, max_length=2,
        description="Only for Drawing Reference or Revision: [literal drawing number, literal revision letter] from the same title block. Snippet must quote that block verbatim. Drawing Reference is number + ' - ' + letter; Revision is its entire suffix after the underscore. Otherwise empty.")


class SpecificationRequestNote(BaseModel):
    """Verbatim constraints outside the 18 scalar CAD fields, never executable instructions."""
    model_config = ConfigDict(extra="forbid")
    source_id: str
    snippet: str = Field(min_length=1, max_length=2000)
    scope: str | None = None
    category: Literal["constraint", "option", "exclusion", "source_link"]
