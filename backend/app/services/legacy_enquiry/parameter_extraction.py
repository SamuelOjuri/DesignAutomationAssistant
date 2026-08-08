from __future__ import annotations

import re
from typing import Callable


QueryLlm = Callable[[str, str], str]

CANONICAL_PARAMETER_ORDER = (
    "Email Subject",
    "Post Code",
    "Drawing Reference",
    "Drawing Title",
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
)

PARAMETER_EXTRACTION_PROMPT = """Extract the following design parameters from the documents for a TaperedPlus technical drawing request: 
            - Email Subject: (The subject line of the email requesting the service from TaperedPlus).
            - Post Code of Project Location: (Mostly found in the title block of the drawing attached to emails. Ignore the postcode of any company office address or sender/recipient address and use the post code of the project location only, otherwise state 'Not provided').
            - Drawing Reference: (TaperedPlus Reference Number e.g. TP*****_**.** - *. Look for references associated with TaperedPlus specifically. If multiple exist, prioritize the latest one mentioned in the context of the request *to* TaperedPlus).
            - Drawing Title (The Project Name, usually the project location).
            - Revision (Suffix of the drawing reference e.g. **.** - A. If multiple exist, use the one associated with the Drawing Reference identified above).
            - Date Received: (Date the email requesting the service *from TaperedPlus* was sent by the client. In a forwarded email chain, this is the date the email was *sent to TaperedPlus*, NOT the date of the original email further down the chain).
            - Hour Received: (Local time the email was sent *to TaperedPlus*. Use 24-hour format, e.g. 14:23).
            - Company: (Identify the company *directly requesting* technical drawings or services *from TaperedPlus*. In a forwarded email, this is the company of the person *sending the email to TaperedPlus*, NOT the company of the original sender further down the chain. Look for the company directly communicating with TaperedPlus).
            - Contact: (Identify the contact person *directly requesting* the job or drawings *from TaperedPlus*. In a forwarded email, this is the person *sending the email to TaperedPlus*, NOT the original sender further down the chain. Look for the individual directly communicating with TaperedPlus).
            - Surveyor: (Name of the surveyor if provided).
            - Target U-Value: (The primary target U-Value requested for the main insulation area).
            - Target Min U-Value: (A secondary or minimum target U-Value if specified, often for specific areas like upstands).
            - Fall of Tapered: (The required fall or slope for the tapered insulation).
            - Tapered Insulation: (The type or brand of tapered insulation product requested).
            - Decking: (The type of roof decking material described)."""


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
    query_llm: QueryLlm,
) -> dict[str, str]:
    response = query_llm(
        all_text,
        PARAMETER_EXTRACTION_PROMPT,
    )
    parameters: dict[str, str] = {}
    for parameter in CANONICAL_PARAMETER_ORDER:
        match = re.search(
            rf"{parameter}\s*:?\s*(.*?)(?:\n|$)",
            response,
            re.IGNORECASE,
        )
        value = match.group(1).strip() if match else "Not found"
        value = re.sub(r"^\*+\s*", "", value)

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
