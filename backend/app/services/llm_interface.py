import logging
import time
from google import genai
from google.genai import types

from .rate_limiter import get_rate_limiter
from ..config import settings
from .shared_gemini_budget import shared_generation_slot

logger = logging.getLogger(__name__)

TRANSIENT_GEMINI_HTTP_STATUS_CODES = [408, 429, *range(500, 600)]


def create_gemini_client(*, max_retries: int = 5, initial_backoff: float = 1.0):
    return genai.Client(
        api_key=settings.gemini_api_key,
        http_options=types.HttpOptions(
            timeout=settings.gemini_request_timeout_seconds * 1000,
            retry_options=types.HttpRetryOptions(
                attempts=max_retries + 1,
                initial_delay=initial_backoff,
                max_delay=60,
                exp_base=2,
                jitter=0.1,
                http_status_codes=TRANSIENT_GEMINI_HTTP_STATUS_CODES,
            )
        ),
    )


def gemini_api_with_retry(
    model,
    contents,
    max_retries=5,
    initial_backoff=1,
    config=None,
):
    shared = settings.gemini_shared_rate_limit_enabled
    client = create_gemini_client(
        max_retries=0 if shared else max_retries,
        initial_backoff=initial_backoff,
    )
    rate_limiter = get_rate_limiter()
    if not rate_limiter.wait_for_availability():
        raise RuntimeError("Could not acquire API rate limit slot within timeout")

    try:
        for attempt in range((max_retries if shared else 0) + 1):
            try:
                with shared_generation_slot():
                    return client.models.generate_content(model=model, contents=contents, config=config)
            except Exception as exc:
                # Shared mode admits every HTTP attempt, including retries,
                # rather than hiding SDK retries behind one admission token.
                code = getattr(exc, "code", None)
                transient = str(code).isdigit() and int(code) in TRANSIENT_GEMINI_HTTP_STATUS_CODES
                if not shared or not transient or attempt >= max_retries:
                    raise
                time.sleep(min(60, initial_backoff * 2 ** attempt))
    finally:
        rate_limiter.release()


def gemini_embed_content_with_retry(client, model, contents, config):
    rate_limiter = get_rate_limiter()
    if not rate_limiter.wait_for_availability():
        raise RuntimeError("Could not acquire API rate limit slot within timeout")

    try:
        return client.models.embed_content(
            model=model,
            contents=contents,
            config=config,
        )
    finally:
        rate_limiter.release()
