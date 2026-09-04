"""Retry decorator with exponential backoff and jitter."""

from __future__ import annotations

import asyncio
import random
from functools import wraps
from typing import Callable, TypeVar, Any

import structlog

logger = structlog.get_logger()

F = TypeVar("F", bound=Callable[..., Any])


def async_retry_with_backoff(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
):
    """Decorator for async functions: retries with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds before the first retry.
        max_delay: Maximum delay cap in seconds.
        exponential_base: Multiplier applied to delay each retry.
        jitter: If True, adds random jitter to prevent thundering herd.
        retryable_exceptions: Tuple of exception types that trigger a retry.

    Example:
        @async_retry_with_backoff(max_retries=3)
        async def call_external_api():
            ...
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(1, max_retries + 2):  # +1 for the initial attempt
                try:
                    return await func(*args, **kwargs)
                except retryable_exceptions as e:
                    last_exception = e

                    if attempt > max_retries:
                        logger.error(
                            "retry.exhausted",
                            function=func.__name__,
                            attempts=attempt,
                            error=str(e),
                        )
                        raise

                    # Calculate delay with exponential backoff
                    delay = min(base_delay * (exponential_base ** (attempt - 1)), max_delay)

                    if jitter:
                        delay = delay * (0.5 + random.random())  # noqa: S311

                    logger.warning(
                        "retry.attempt",
                        function=func.__name__,
                        attempt=attempt,
                        max_retries=max_retries,
                        delay_seconds=round(delay, 2),
                        error=str(e),
                    )

                    await asyncio.sleep(delay)

            raise last_exception  # Should never reach here, but just in case

        return wrapper  # type: ignore[return-value]

    return decorator
