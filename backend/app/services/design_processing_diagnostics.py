"""Identify the process and source-extraction contract producing worker logs."""

import os

from ..config import DESIGN_PROCESSING_PIPELINE_REVISION, settings
from .legacy_enquiry.source_documents import MAX_SOURCE_BYTES
from .specification_semantics import (
    ARCHIVE_EXPANDED_STATUS, DWG_SKIP_STATUS, ENCRYPTED_PDF_SKIP_STATUS, UNSUPPORTED_SOURCE_SKIP_STATUS,
)


def source_runtime_diagnostics():
    return {
        "pid": os.getpid(),
        "pipeline_revision": DESIGN_PROCESSING_PIPELINE_REVISION,
        "dwg_status": DWG_SKIP_STATUS,
        "encrypted_pdf_status": ENCRYPTED_PDF_SKIP_STATUS,
        "unsupported_format_status": UNSUPPORTED_SOURCE_SKIP_STATUS,
        "zip_status": ARCHIVE_EXPANDED_STATUS,
        "max_source_bytes": MAX_SOURCE_BYTES,
        "max_source_mib": MAX_SOURCE_BYTES // (1024 * 1024),
        "spreadsheet_full_cells": True,
        "native_pdf_text_enabled": settings.design_processing_native_pdf_text_enabled,
        "transcription_cache_enabled": settings.design_processing_transcription_cache_enabled,
        "transcription_cache_ttl_days": settings.design_processing_transcription_cache_ttl_days,
        "gemini_shared_budget_mode": "shared_database" if settings.gemini_shared_rate_limit_enabled else "disabled",
        "gemini_shared_max_concurrent": settings.gemini_shared_max_concurrent,
        "gemini_shared_requests_per_minute": settings.gemini_shared_requests_per_minute,
    }
