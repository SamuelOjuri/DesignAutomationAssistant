"""Build Gemini wire schemas without expanding bounded-array decoding states.

The original Pydantic models still enforce their array limits after decoding.
Large maxItems constraints can make Gemini reject an otherwise valid schema:
https://ai.google.dev/gemini-api/docs/structured-output#limitations
"""

from typing import Any

from pydantic import BaseModel


def gemini_response_schema(model: type[BaseModel]) -> dict[str, Any]:
    def compact(value):
        if isinstance(value, dict):
            omitted = {"minItems", "maxItems"} if value.get("type") == "array" else set()
            return {key: compact(child) for key, child in value.items() if key not in omitted}
        if isinstance(value, list):
            return [compact(child) for child in value]
        return value

    return compact(model.model_json_schema())
