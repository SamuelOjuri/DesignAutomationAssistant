from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from pathlib import Path
from typing import Protocol, Sequence

from ..design_processing_inputs import DownloadedDesignEmailAsset
from .email_extraction import (
    AttachmentTextExtractor,
    extract_text_from_email,
    process_email_content,
)
from .parameter_extraction import (
    DesignParameterExtraction,
    QueryLlm,
    extract_parameters,
    extract_project_name_from_content,
)
from .source_documents import IncompleteSourceExtraction, MAX_CONTEXT_CHARS, extract_source_records, source_record
from ..specification_semantics import COMPLETE_SOURCE_STATUSES, SpecificationEvidence


class LegacyAnalysisClient(AttachmentTextExtractor, Protocol):
    def extract_design_parameters(self, context: str) -> DesignParameterExtraction: ...

    def query_llm(self, context: str, query: str) -> str: ...


@dataclass(frozen=True, slots=True)
class EmailContentAudit:
    asset_id: str
    filename: str
    content_sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class LegacyAnalysisResult:
    parameters: dict[str, str]
    sources: dict[str, str]
    project_name: str
    email_content_audit: tuple[EmailContentAudit, ...]
    extracted_text_sha256: str
    performance_parameters: dict[str, str | None] = field(default_factory=dict)
    specification_evidence: tuple[dict, ...] = ()
    specification_assessed_fields: tuple[str, ...] = ()
    source_manifest: tuple[dict, ...] = ()


def analyze_downloaded_email_assets(
    downloaded_assets: Sequence[DownloadedDesignEmailAsset],
    *,
    client: LegacyAnalysisClient,
) -> LegacyAnalysisResult:
    if not downloaded_assets:
        raise ValueError("at least one downloaded Email asset is required")

    all_text = ""
    first_email_text = ""
    audits: list[EmailContentAudit] = []
    manifest = []
    supplementary_records = []
    parsed_assets = []
    for downloaded in downloaded_assets:
        content = Path(downloaded.temp_path).read_bytes()
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != downloaded.content_sha256:
            raise ValueError(
                f"downloaded Email asset {downloaded.source.asset_id} hash changed on disk"
            )
        if len(content) != downloaded.size_bytes:
            raise ValueError(
                f"downloaded Email asset {downloaded.source.asset_id} size changed on disk"
            )
        if not downloaded.source.filename.lower().endswith((".eml", ".msg")):
            records = extract_source_records(
                content, downloaded.source.filename, client,
                source_id=f"asset:{downloaded.source.asset_id}",
            )
            manifest.extend(records)
            supplementary_records.extend(records)
            continue
        try:
            header, body, attachments, inline_images = process_email_content(content, downloaded.source.filename)
        except Exception as exc:
            manifest.append(source_record(f"asset:{downloaded.source.asset_id}", downloaded.source.filename, content,
                                          source_type="email_body", status="failed:" + type(exc).__name__))
            continue
        raw_date = next((line[6:] for line in header.splitlines() if line.startswith("Date: ")), "")
        try:
            request_time = parsedate_to_datetime(raw_date)
            if request_time.tzinfo is None:
                request_time = request_time.replace(tzinfo=timezone.utc)
            request_time = request_time.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError):
            request_time = None  # Upload time is not the date of the request.
        parsed_assets.append((request_time or "", downloaded.source.asset_id, downloaded, header, body, attachments, inline_images))
    # Display chronologically. ID is only a stable tie-breaker, never recency.
    for request_time, _, downloaded, header, body, attachments, inline_images in sorted(parsed_assets, key=lambda entry: (entry[0], int(entry[1]))):
        email_text = f"{header}\n{body}"
        try:
            extracted = extract_text_from_email(
                email_text, attachments, extractor=client, inline_images=inline_images,
                manifest=manifest, source_prefix=f"asset:{downloaded.source.asset_id}",
                observed_at=request_time or None,
            )
        except IncompleteSourceExtraction:
            continue  # Finish the inventory, then retry the incomplete extraction.
        all_text += (
            f"\n\nEMAIL FILE: {downloaded.source.filename}\n"
            f"{extracted}\n{'=' * 50}\n"
        )
        first_email_text = email_text  # Most recent request for title extraction.
        audits.append(
            EmailContentAudit(
                asset_id=downloaded.source.asset_id,
                filename=downloaded.source.filename,
                content_sha256=downloaded.content_sha256,
                size_bytes=downloaded.size_bytes,
            )
        )

    if any(source["status"] not in COMPLETE_SOURCE_STATUSES for source in manifest):
        raise IncompleteSourceExtraction(manifest)
    if len(parsed_assets) > 1 and any(not entry[0] for entry in parsed_assets):
        raise ValueError("Multiple email requests have unreadable sent dates; chronology requires review")
    for record in supplementary_records:
        if record["status"] == "read":
            all_text += f"\nSOURCE ID: {record['sourceId']}\nFILE: {record['filename']}\nATTACHMENT CONTENT:\n{record['text']}\n"
    if len(all_text) > MAX_CONTEXT_CHARS:
        raise ValueError("Combined source text exceeds extraction limit; review required")

    structured_parameters = client.extract_design_parameters(all_text)
    dated_requests = [entry for entry in parsed_assets if entry[0]]
    if dated_requests:
        latest = max(dated_requests, key=lambda entry: (entry[0], int(entry[1])))
        timestamp, asset_id, _, header, _, _, _ = latest
        request_date = parsedate_to_datetime(next(line[6:] for line in header.splitlines() if line.startswith("Date: ")))
        structured_parameters.date_received = request_date.strftime("%d %b %Y")
        structured_parameters.hour_received = request_date.strftime("%H:%M")
        structured_parameters.specification_evidence = [
            e for e in structured_parameters.specification_evidence if e.field != "Date Received"
        ] + [SpecificationEvidence(
            field="Date Received", value=structured_parameters.date_received,
            source_id=f"asset:{asset_id}:body", location="outer request Date header",
            snippet=next(line for line in header.splitlines() if line.startswith("Date: ")),
            scope=None, issuer=None, role="requested",
            request_role="direct_request",
            rationale="Latest actual outer request timestamp; earlier forwarded dates and upload times excluded",
        )]
    parameters = extract_parameters(
        all_text,
        extracted_parameters=structured_parameters,
    )
    query_llm: QueryLlm = client.query_llm
    project_name = extract_project_name_from_content(
        first_email_text,
        all_text,
        query_llm=query_llm,
    )
    if not project_name:
        raise ValueError("project-name extraction returned no title")
    sources = {parameter: "Email Content" for parameter in parameters}
    parameters["Reason for Change"] = "Reviewer decision required"
    sources["Reason for Change"] = "Business Rule"

    return LegacyAnalysisResult(
        parameters=parameters,
        sources=sources,
        project_name=project_name,
        email_content_audit=tuple(audits),
        extracted_text_sha256=hashlib.sha256(all_text.encode("utf-8")).hexdigest(),
        performance_parameters={
            **structured_parameters.as_canonical_parameters(),
            "Date Received": parameters["Date Received"],
            "Bauder Roof Num": structured_parameters.bauder_roof_num,
        },
        specification_evidence=tuple(e.model_dump() for e in structured_parameters.specification_evidence),
        specification_assessed_fields=tuple(structured_parameters.specification_assessed_fields),
        source_manifest=tuple(manifest),
    )
