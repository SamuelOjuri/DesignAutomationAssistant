from google.genai import types
from typing import List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import time

from .llm_interface import gemini_api_with_retry
from ..config import settings

logger = logging.getLogger(__name__)

def should_batch_pdfs(pdf_files: List[Dict]) -> bool:
    MAX_BATCH_SIZE = 100 * 1024 * 1024
    MAX_FILES_PER_BATCH = 3
    total_size = sum(len(f["content"]) for f in pdf_files)
    return total_size <= MAX_BATCH_SIZE and len(pdf_files) <= MAX_FILES_PER_BATCH and len(pdf_files) > 1

def process_pdf_with_gemini(pdf_content: bytes, filename: str) -> str:
    prompt = "Please extract all text content from this PDF document, including text from tables, diagrams, and charts."
    model = settings.gemini_model
    response = gemini_api_with_retry(
        model=model,
        contents=[
            types.Part.from_bytes(data=pdf_content, mime_type="application/pdf"),
            prompt,
        ],
    )
    return response.text

def process_multiple_pdfs_single_call(pdf_files: List[Dict]) -> str:
    parts = [types.Part.from_bytes(data=f["content"], mime_type="application/pdf") for f in pdf_files]
    filenames = ", ".join(f["filename"] for f in pdf_files)
    parts.append(
        f"Please extract all text content from these {len(pdf_files)} PDF documents: {filenames}. "
        "For each document, start with '=== PDF: [filename] ===' header."
    )
    response = gemini_api_with_retry(model=settings.gemini_model, contents=parts)
    return response.text

def process_pdfs_in_parallel(pdf_files: List[Dict]) -> str:
    def _process(pdf_file: Dict) -> Tuple[str, str]:
        text = process_pdf_with_gemini(pdf_file["content"], pdf_file["filename"])
        return pdf_file["filename"], text

    results = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(_process, pdf): pdf for pdf in pdf_files}
        for f in as_completed(futures):
            pdf = futures[f]
            try:
                filename, text = f.result()
                results.append((filename, f"=== PDF: {filename} ===\n{text}\n"))
            except Exception as e:
                filename = pdf.get("filename", "unknown")
                results.append((filename, f"=== PDF: {filename} ===\nError processing PDF: {e}\n"))

    return "\n".join(
        text
        for _, text in sorted(
            results,
            key=lambda x: pdf_files.index(next(p for p in pdf_files if p["filename"] == x[0]))
        )
    )

def process_pdf_batch(pdf_files: List[Dict]) -> str:
    if not pdf_files:
        return ""
    if should_batch_pdfs(pdf_files):
        try:
            return process_multiple_pdfs_single_call(pdf_files)
        except Exception:
            return process_pdfs_in_parallel(pdf_files)
    return process_pdfs_in_parallel(pdf_files)