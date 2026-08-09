from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Literal, Sequence

from google.genai import types

from ..llm_interface import gemini_api_with_retry
from .parameter_extraction import (
    DesignParameterExtraction,
    PARAMETER_EXTRACTION_PROMPT,
)


ThinkingLevel = Literal["minimal", "low", "medium", "high"]
GenerateContent = Callable[[str, Any, types.GenerateContentConfig], Any]


def _default_generate_content(
    model: str,
    contents: Any,
    config: types.GenerateContentConfig,
) -> Any:
    return gemini_api_with_retry(model=model, contents=contents, config=config)


@dataclass(frozen=True, slots=True)
class LegacyGeminiClient:
    model: str
    thinking_level: ThinkingLevel = "medium"
    max_attachment_workers: int = 4
    generate_content: GenerateContent = _default_generate_content

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must not be empty")
        if self.thinking_level not in {"minimal", "low", "medium", "high"}:
            raise ValueError("thinking_level must be minimal, low, medium, or high")
        if self.max_attachment_workers < 1:
            raise ValueError("max_attachment_workers must be positive")

    @property
    def generation_config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel(self.thinking_level),
            ),
        )

    @property
    def parameter_extraction_config(self) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel(self.thinking_level),
            ),
            response_mime_type="application/json",
            response_json_schema=DesignParameterExtraction.model_json_schema(),
        )

    def extract_design_parameters(self, context: str) -> DesignParameterExtraction:
        prompt = f"""
        Please analyze the following information extracted from emails, PDF documents, and images:

        {context}

        QUESTION: {PARAMETER_EXTRACTION_PROMPT}

        Information may be found in any content source, including text from image descriptions.
        """
        response = self.generate_content(
            self.model,
            prompt,
            self.parameter_extraction_config,
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, DesignParameterExtraction):
            return parsed
        if isinstance(parsed, Mapping):
            return DesignParameterExtraction.model_validate(parsed)

        response_text = getattr(response, "text", None)
        if not isinstance(response_text, str) or not response_text.strip():
            raise ValueError("parameter extraction returned no structured response")
        return DesignParameterExtraction.model_validate_json(response_text)

    def query_llm(self, context: str, query: str) -> str:
        prompt = (
            context
            if not query
            else f"""
        Please analyze the following information extracted from emails, PDF documents, and images:
        
        {context}
        
        QUESTION: {query}
        
        Note that information may be found in any of the content sources, including text from image descriptions.
        """
        )
        response = self.generate_content(self.model, prompt, self.generation_config)
        return response.text

    @staticmethod
    def should_batch_pdfs(pdf_files: Sequence[dict[str, Any]]) -> bool:
        total_size = sum(len(pdf["content"]) for pdf in pdf_files)
        return total_size <= 100 * 1024 * 1024 and 1 < len(pdf_files) <= 3

    def process_pdf(self, pdf_content: bytes, filename: str) -> str:
        del filename
        response = self.generate_content(
            self.model,
            [
                types.Part.from_bytes(data=pdf_content, mime_type="application/pdf"),
                (
                    "Please extract all text content from this PDF document, "
                    "including text from tables, diagrams, and charts."
                ),
            ],
            self.generation_config,
        )
        return response.text

    def process_pdf_batch(self, pdf_files: Sequence[dict[str, Any]]) -> str:
        if not pdf_files:
            return ""
        if self.should_batch_pdfs(pdf_files):
            try:
                parts = [
                    types.Part.from_bytes(
                        data=pdf["content"],
                        mime_type="application/pdf",
                    )
                    for pdf in pdf_files
                ]
                filenames = ", ".join(pdf["filename"] for pdf in pdf_files)
                parts.append(
                    f"Please extract all text content from these {len(pdf_files)} PDF documents: {filenames}. "
                    "Including text from tables, diagrams, and charts. "
                    "For each document, start with '=== PDF: [filename] ===' header and then provide the extracted content."
                )
                return self.generate_content(
                    self.model,
                    parts,
                    self.generation_config,
                ).text
            except Exception:
                pass
        return self._process_pdfs_in_parallel(pdf_files)

    def _process_pdfs_in_parallel(
        self,
        pdf_files: Sequence[dict[str, Any]],
    ) -> str:
        indexed_results: list[tuple[int, str]] = []
        with ThreadPoolExecutor(
            max_workers=min(self.max_attachment_workers, len(pdf_files)),
        ) as executor:
            futures = {
                executor.submit(
                    self.process_pdf,
                    pdf["content"],
                    pdf["filename"],
                ): (index, pdf)
                for index, pdf in enumerate(pdf_files)
            }
            for future in as_completed(futures):
                index, pdf = futures[future]
                try:
                    text = future.result()
                    rendered = f"=== PDF: {pdf['filename']} ===\n{text}\n"
                except Exception as exc:
                    rendered = f"Error processing PDF: {exc}"
                indexed_results.append((index, rendered))
        return "\n".join(text for _, text in sorted(indexed_results))

    def process_image(
        self,
        image_content: bytes,
        filename: str,
        image_type: str = "ATTACHMENT",
    ) -> str:
        del image_type
        supported_formats = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }
        extension = filename.split(".")[-1].lower()
        if extension not in supported_formats:
            return (
                f"Unsupported image format: {extension}. Only "
                f"{', '.join(supported_formats)} are supported."
            )
        try:
            response = self.generate_content(
                self.model,
                [
                    types.Part.from_bytes(
                        data=image_content,
                        mime_type=supported_formats[extension],
                    ),
                    (
                        "Describe this image in detail, including any visible text, "
                        "diagrams, or drawings. Extract any technical parameters or "
                        "specifications you can see."
                    ),
                ],
                self.generation_config,
            )
            return response.text
        except Exception as exc:
            if "INVALID_ARGUMENT" in str(exc):
                return (
                    "Unable to process this image due to format compatibility issues. "
                    "Please note any visible information from the image might not be "
                    "included in the analysis."
                )
            return f"Error processing image: {exc}"
