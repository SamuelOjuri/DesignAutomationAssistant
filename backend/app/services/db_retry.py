from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

POSTGRES_DEADLOCK_SQLSTATE = "40P01"

ResultT = TypeVar("ResultT")


class AutoSyncConcurrencyError(RuntimeError):
    pass


def database_sqlstate(exc: BaseException) -> str | None:
    original = exc.orig if isinstance(exc, DBAPIError) else exc
    return getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)


def is_postgres_deadlock(exc: BaseException) -> bool:
    return database_sqlstate(exc) == POSTGRES_DEADLOCK_SQLSTATE


def is_retryable_auto_sync_error(exc: BaseException) -> bool:
    return is_postgres_deadlock(exc) or isinstance(exc, AutoSyncConcurrencyError)


def run_transaction_with_retry(
    db: Session,
    operation: Callable[[], ResultT],
    *,
    operation_name: str,
    max_attempts: int = 3,
    base_delay_seconds: float = 0.05,
    retry_if: Callable[[BaseException], bool] = is_retryable_auto_sync_error,
) -> ResultT:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if not retry_if(exc):
                raise

            db.rollback()
            if attempt == max_attempts:
                raise

            maximum_delay = base_delay_seconds * (2 ** (attempt - 1))
            delay = random.uniform(maximum_delay / 2, maximum_delay)
            logger.warning(
                "Retryable database conflict during %s; retrying attempt %s/%s in %.3fs (sqlstate=%s)",
                operation_name,
                attempt + 1,
                max_attempts,
                delay,
                database_sqlstate(exc),
            )
            time.sleep(delay)

    raise AssertionError("transaction retry loop exited unexpectedly")