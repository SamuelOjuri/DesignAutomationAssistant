"""Identify the process and source-extraction contract producing worker logs."""

import os

from ..config import DESIGN_PROCESSING_PIPELINE_REVISION
from .specification_semantics import ARCHIVE_EXPANDED_STATUS, DWG_SKIP_STATUS, ENCRYPTED_PDF_SKIP_STATUS


def source_runtime_diagnostics():
    return {
        "pid": os.getpid(),
        "pipeline_revision": DESIGN_PROCESSING_PIPELINE_REVISION,
        "dwg_status": DWG_SKIP_STATUS,
        "encrypted_pdf_status": ENCRYPTED_PDF_SKIP_STATUS,
        "zip_status": ARCHIVE_EXPANDED_STATUS,
        "spreadsheet_full_cells": True,
    }
