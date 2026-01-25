import time
import random
import logging
from google import genai

from .rate_limiter import get_rate_limiter
from ..config import settings

logger = logging.getLogger(__name__)

def is_rate_limit_error(exception):
    return "429" in str(exception) or "RESOURCE_EXHAUSTED" in str(exception) or "RATE_LIMIT" in str(exception)

def gemini_api_with_retry(model, contents, max_retries=5, initial_backoff=5):
    client = genai.Client(api_key=settings.gemini_api_key)
    rate_limiter = get_rate_limiter()
    retries = 0

    while retries <= max_retries:
        if not rate_limiter.wait_for_availability():
            raise Exception("Could not acquire API rate limit slot within timeout")

        try:
            response = client.models.generate_content(model=model, contents=contents)
            rate_limiter.release()
            return response
        except Exception as e:
            rate_limiter.release()
            if is_rate_limit_error(e) and retries < max_retries:
                base_sleep = initial_backoff * (2 ** retries)
                jitter = random.uniform(0, base_sleep * 0.1)
                time.sleep(base_sleep + jitter)
                retries += 1
                continue
            raise e

    raise Exception(f"Failed after {max_retries} retries due to rate limiting")