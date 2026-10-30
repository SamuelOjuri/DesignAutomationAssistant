"""Inspect worker runtime and optionally test Gemini with synthetic data only.

Run on Render: python -m backend.scripts.check_design_processing_gemini --live
No emails, attachments or CRM records are read or sent by this diagnostic.
"""

import argparse
import json

from google import genai
from google.genai import types

from backend.app.config import settings
from backend.app.services.gemini_requests import GeminiJSONRequester, runtime_diagnostics
from backend.app.services.legacy_enquiry.llm import LegacyGeminiClient
from backend.app.services.legacy_enquiry.parameter_extraction import DesignParameterExtraction


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Make a small synthetic Gemini request")
    parser.add_argument("--json-mode", action="store_true", help="Exercise the JSON compatibility path directly")
    args = parser.parse_args()
    legacy = LegacyGeminiClient(settings.design_processing_extraction_model, thinking_level=settings.design_processing_thinking_level)
    config = legacy.parameter_extraction_config
    print(json.dumps({"event": "design_extraction_runtime", **runtime_diagnostics(legacy.model, legacy.thinking_level,
                                                                                  config.response_json_schema)}), flush=True)
    if not args.live:
        return 0
    client = genai.Client(api_key=settings.gemini_api_key,
                         http_options=types.HttpOptions(timeout=45000, retry_options=types.HttpRetryOptions(attempts=1)))
    def generate(model, contents, request_config):
        return client.models.generate_content(model=model, contents=contents, config=request_config)
    requester = GeminiJSONRequester(generate, legacy.model, config, DesignParameterExtraction)
    requester.json_mode = args.json_mode
    try:
        response = requester(
            "Synthetic compatibility check. No project information, people, dates, emails or drawings are supplied. "
            "Return null for every scalar field, an empty specification_evidence array, and all 18 CAD labels "
            "in specification_assessed_fields. Invent no facts.", stage="synthetic_diagnostic",
        )
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, dict):
            DesignParameterExtraction.model_validate(parsed)
        else:
            DesignParameterExtraction.model_validate_json(response.text or "")
        print(json.dumps({"status": "ok", "response_validated": True, "json_mode_used": requester.json_mode}), flush=True)
        return 0
    except Exception as exc:
        # Provider errors already carry safe metadata. Validation errors can echo
        # values, so print their type only, even though this prompt is synthetic.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "details": getattr(exc, "details", None)}), flush=True)
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
