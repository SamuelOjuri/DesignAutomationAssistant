from __future__ import annotations

from dataclasses import dataclass
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
    QueryLlm,
    extract_parameters,
    extract_project_name_from_content,
)


class LegacyAnalysisClient(AttachmentTextExtractor, Protocol):
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
    ordered_assets = sorted(
        downloaded_assets,
        key=lambda downloaded: (
            int(downloaded.source.asset_id),
            downloaded.source.filename,
        ),
    )
    for downloaded in ordered_assets:
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
        header, body, attachments, inline_images = process_email_content(
            content,
            downloaded.source.filename,
        )
        email_text = f"{header}\n{body}"
        extracted = extract_text_from_email(
            email_text,
            attachments,
            extractor=client,
            inline_images=inline_images,
        )
        all_text += (
            f"\n\nEMAIL FILE: {downloaded.source.filename}\n"
            f"{extracted}\n{'=' * 50}\n"
        )
        if not first_email_text:
            first_email_text = email_text
        audits.append(
            EmailContentAudit(
                asset_id=downloaded.source.asset_id,
                filename=downloaded.source.filename,
                content_sha256=downloaded.content_sha256,
                size_bytes=downloaded.size_bytes,
            )
        )

    query_llm: QueryLlm = client.query_llm
    parameters = extract_parameters(all_text, query_llm=query_llm)
    project_name = extract_project_name_from_content(
        first_email_text,
        all_text,
        query_llm=query_llm,
    )
    if not project_name:
        raise ValueError("project-name extraction returned no title")
    sources = {parameter: "Email Content" for parameter in parameters}
    if not parameters.get("Reason for Change") or parameters["Reason for Change"] == "Not found":
        parameters["Reason for Change"] = "New Enquiry"
        sources["Reason for Change"] = "Business Rule"

    return LegacyAnalysisResult(
        parameters=parameters,
        sources=sources,
        project_name=project_name,
        email_content_audit=tuple(audits),
        extracted_text_sha256=hashlib.sha256(all_text.encode("utf-8")).hexdigest(),
    )
