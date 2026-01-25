import threading
import time
import logging

logger = logging.getLogger(__name__)

class GlobalGeminiRateLimiter:
    def __init__(self, requests_per_minute: int = 950, max_concurrent: int = 15):
        self.requests_per_minute = requests_per_minute
        self.max_concurrent = max_concurrent
        self._semaphore = threading.Semaphore(max_concurrent)
        self._tokens = requests_per_minute
        self._last_refill = time.time()
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            now = time.time()
            tokens_to_add = int((now - self._last_refill) * self.requests_per_minute / 60)
            if tokens_to_add > 0:
                self._tokens = min(self.requests_per_minute, self._tokens + tokens_to_add)
                self._last_refill = now

            if self._tokens <= 0:
                return False

            if not self._semaphore.acquire(blocking=False):
                return False

            self._tokens -= 1
            return True

    def release(self):
        self._semaphore.release()

    def wait_for_availability(self, timeout: int = 300) -> bool:
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.acquire():
                return True
            time.sleep(1)
        return False

_rate_limiter = None

def get_rate_limiter() -> GlobalGeminiRateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = GlobalGeminiRateLimiter()
    return _rate_limiter