from __future__ import annotations

import re
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field


QueryLlm = Callable[[str, str], str]

CANONICAL_PARAMETER_ORDER = (
    "Email Subject",
    "Post Code",
    "Drawing Title",
    "Drawing Reference",
    "Revision",
    "Date Received",
    "Hour Received",
    "Company",
    "Contact",
    "Reason for Change",
    "Surveyor",
    "Target U-Value",
    "Target Min U-Value",
    "Fall of Tapered",
    "Tapered Insulation",
    "Decking",
    "Scale",
    "Page Size",
    "Bauder Contract Number",
    "Membrane",
    "VCL",
)

PARAMETER_EXTRACTION_PROMPT = (
    "Extract the requested design parameters from the supplied email and document "
    "content for a TaperedPlus technical drawing request. Return null when a value "
    "is not provided by the source material."
)


class DesignParameterExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email_subject: str | None = Field(
        description="The subject line of the email requesting the service from TaperedPlus."
    )
    post_code: str | None = Field(
        description=(
            "The project-location postcode, usually from a drawing title block. Ignore "
            "company, sender, and recipient addresses."
        )
    )
    drawing_title: str | None = Field(
        description="The drawing project name, usually the project location."
    )
    drawing_reference: str | None = Field(
        description=(
            "The Drawing Reference Number issued by TaperedPlus "
            "[e.g. TP*****_**.** - *]. If several references exist, use the latest "
            "one associated with the request to TaperedPlus."
        )
    )
    revision: str | None = Field(
        description=(
            "The suffix of the selected TaperedPlus Drawing Reference. This is the "
            "suffix after the underscore in the drawing reference "
            "[e.g. **.** - * from TP*****_**.** - *]."
        )
    )
    date_received: str | None = Field(
        description=(
            "The date the client sent the request to TaperedPlus. For forwarded chains, "
            "do not use the date of an earlier message."
        )
    )
    hour_received: str | None = Field(
        description=(
            "The local time the client sent the request to TaperedPlus, in 24-hour HH:MM "
            "format."
        )
    )
    company: str | None = Field(
        description=(
            "The company directly requesting drawings or services from TaperedPlus. For "
            "a forwarded email, use the company of the person sending to TaperedPlus, "
            "not an earlier sender."
        )
    )
    contact: str | None = Field(
        description=(
            "The person directly requesting the job or drawings from TaperedPlus. For a "
            "forwarded email, use the person sending to TaperedPlus, not an earlier sender."
        )
    )
    surveyor: str | None = Field(description="The surveyor's name, when provided.")
    target_u_value: str | None = Field(
        description="The primary target U-value for the main insulation area."
    )
    target_min_u_value: str | None = Field(
        description=(
            "A secondary or minimum target U-value, often for an area such as upstands."
        )
    )
    fall_of_tapered: str | None = Field(
        description="The required fall or slope for the tapered insulation scheme."
    )
    tapered_insulation: str | None = Field(
        description="The type or brand of tapered insulation product requested."
    )
    decking: str | None = Field(
        description="The type of roof decking material described."
    )
    scale: str | None = Field(
        description=(
            "The drawing scale, usually found in a drawing title block "
            "(for example, 1:100)."
        )
    )
    page_size: str | None = Field(
        description=(
            "The drawing page or paper size, usually found in a drawing title block "
            "(for example, A1)."
        )
    )
    bauder_contract_number: str | None = Field(
        description=(
            "The Bauder contract number for a design request from Bauder, usually "
            "found in the email subject. Return only the contract identifier "
            "(for example, B******). Return null for non-Bauder requests or when absent."
        )
    )
    membrane: str | None = Field(
        description=(
            "The type of waterproofing membrane material requested for the roofing "
            "solution."
        )
    )
    vcl: str | None = Field(
        description=(
            "The Air and Vapour Control Layer (VCL or AVCL) component requested for "
            "the roof insulation."
        )
    )

    def as_canonical_parameters(self) -> dict[str, str | None]:
        return {
            "Email Subject": self.email_subject,
            "Post Code": self.post_code,
            "Drawing Title": self.drawing_title,
            "Drawing Reference": self.drawing_reference,
            "Revision": self.revision,
            "Date Received": self.date_received,
            "Hour Received": self.hour_received,
            "Company": self.company,
            "Contact": self.contact,
            "Surveyor": self.surveyor,
            "Target U-Value": self.target_u_value,
            "Target Min U-Value": self.target_min_u_value,
            "Fall of Tapered": self.fall_of_tapered,
            "Tapered Insulation": self.tapered_insulation,
            "Decking": self.decking,
            "Scale": self.scale,
            "Page Size": self.page_size,
            "Bauder Contract Number": self.bauder_contract_number,
            "Membrane": self.membrane,
            "VCL": self.vcl,
        }


def map_tapered_insulation_value(value: str) -> str:
    insulation_mappings = {
        "TissueFaced PIR": [
            "TT47",
            "TR27",
            "Glass Tissue PIR",
            "Powerdeck F",
            "Adhered",
            "MG",
            "TR/MG",
            "FR/MG",
            "BauderPIR FA-TE",
            "PU 25W",
            "Evatherm A",
            "Hytherm ADH",
        ],
        "TorchOn PIR": [
            "TT44",
            "TR24",
            "Torched",
            "Powerdeck U",
            "Torched",
            "BGM",
            "TR/BGM",
            "FR/BGM",
            "BauderPIR FA",
        ],
        "FoilFaced PIR": [
            "TT46",
            "TR26",
            "Foil",
            "Powerdeck Eurodeck",
            "Mech Fixed",
            "ALU",
            "TR/ALU",
            "FR/ALU",
            "Aluminium Faced",
        ],
        "ROCKWOOL HardRock MultiFix DD": [
            "Mineral wool",
            "Hardrock",
            "stonewool",
            "stone wool",
            "rock wool",
            "bauderrock",
        ],
        "Foamglas T3+": ["Cellular Glass", "foamed glass", "Bauderglas"],
        "EPS": ["Expanded Polystrene"],
        "XPS": ["Extruded Polystyrene"],
    }
    if value and value != "Not found":
        for category, products in insulation_mappings.items():
            for product in products:
                if product.lower() in value.lower() or value.lower() in product.lower():
                    return category
    return value


def extract_parameters(
    all_text: str,
    *,
    extracted_parameters: DesignParameterExtraction,
) -> dict[str, str]:
    parameters: dict[str, str] = {}
    for parameter, raw_value in extracted_parameters.as_canonical_parameters().items():
        value = raw_value.strip() if raw_value and raw_value.strip() else "Not provided"

        if parameter == "Tapered Insulation":
            value = map_tapered_insulation_value(value)
        elif parameter == "Post Code":
            cleaned_value = re.sub(
                r"^\s*of Project Location:?\*?\s*",
                "",
                value,
                flags=re.IGNORECASE,
            ).strip()
            if re.search(
                r"not\s+provided|not\s+found|none",
                cleaned_value,
                re.IGNORECASE,
            ):
                value = "Not provided"
            else:
                postcode_match = re.search(r"([A-Z]{1,2})[0-9]", cleaned_value.upper())
                value = postcode_match.group(1) if postcode_match else cleaned_value

        parameters[parameter] = value

    date_match = re.search(
        r"EMAIL CONTENT:\s*From:.*?\nTo:.*?\nSubject:.*?\nDate:\s*(.+?)\s*(?:\n|$)",
        all_text,
        re.DOTALL | re.IGNORECASE,
    )
    if date_match:
        full_date = date_match.group(1).strip()
        try:
            parameters["Date Received"] = re.search(
                r"\d{1,2} \w{3} \d{4}",
                full_date,
            ).group(0)
            parameters["Hour Received"] = re.search(
                r"\d{2}:\d{2}",
                full_date,
            ).group(0)
        except (AttributeError, IndexError):
            pass

    return parameters


def extract_project_name_from_content(
    email_text: str,
    all_extracted_text: str,
    *,
    query_llm: QueryLlm,
) -> str:
    del email_text
    prompt = f"""
    Based on the following email content and attachments, extract the project name (drawing title) which is usually the project location.
    Return only the project name, nothing else.
    
    {all_extracted_text}
    """
    response = query_llm(prompt, "")
    return "" if response is None else response.strip()
