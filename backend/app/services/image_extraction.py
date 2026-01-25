from google.genai import types
from .llm_interface import gemini_api_with_retry
from ..config import settings

def process_image_with_gemini(image_content, filename, image_type="ATTACHMENT"):
    supported = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    ext = filename.split(".")[-1].lower()
    if ext not in supported:
        return f"Unsupported image format: {ext}."

    response = gemini_api_with_retry(
        model=settings.gemini_model,
        contents=[
            types.Part.from_bytes(data=image_content, mime_type=supported[ext]),
            "Describe this image in detail, including any visible text, diagrams, or drawings.",
        ],
    )
    return response.text