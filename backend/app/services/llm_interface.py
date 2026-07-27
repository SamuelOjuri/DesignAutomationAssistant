import logging
from google import genai
from google.genai import types

from .rate_limiter import get_rate_limiter
from ..config import settings

logger = logging.getLogger(__name__)

TRANSIENT_GEMINI_HTTP_STATUS_CODES = [408, 429, *range(500, 600)]


def create_gemini_client(*, max_retries: int = 5, initial_backoff: float = 1.0):
    return genai.Client(
        api_key=settings.gemini_api_key,
        http_options=types.HttpOptions(
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


def gemini_api_with_retry(model, contents, max_retries=5, initial_backoff=1):
    client = create_gemini_client(
        max_retries=max_retries,
        initial_backoff=initial_backoff,
    )
    rate_limiter = get_rate_limiter()
    if not rate_limiter.wait_for_availability():
        raise RuntimeError("Could not acquire API rate limit slot within timeout")

    try:
        return client.models.generate_content(model=model, contents=contents)
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