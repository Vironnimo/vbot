"""Async retry utility with exponential backoff and jitter.

Retries async callables on *retryable* errors only.  An error is retryable
when its ``retryable`` attribute is ``True``.  Auth errors, validation
errors, and other fatal errors are re-raised immediately.

Usage::

    result = await retry_async(fetch_data, url)

The function retries up to ``MAX_RETRIES`` times with exponential backoff
(initial delay ``INITIAL_DELAY_SECONDS``, factor ``BACKOFF_FACTOR``) plus
random jitter to avoid thundering-herd effects.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from core.utils.logging import get_logger

T = TypeVar("T")

_LOGGER = get_logger("utils.retry")

MAX_RETRIES = 3
INITIAL_DELAY_SECONDS = 1.0
BACKOFF_FACTOR = 2
JITTER_FACTOR = 0.5


async def retry_async(
    async_fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = MAX_RETRIES,
    initial_delay: float = INITIAL_DELAY_SECONDS,
    **kwargs: Any,
) -> T:
    """Call *async_fn* with retries on retryable errors.

    Args:
        async_fn: Async callable to execute.
        *args: Positional arguments forwarded to *async_fn*.
        max_retries: Maximum number of retry attempts.
        initial_delay: Base delay in seconds for the first retry.
        **kwargs: Keyword arguments forwarded to *async_fn*.

    Returns:
        The return value of *async_fn* on the first successful call.

    Raises:
        The original exception if it is not retryable.
        The last retryable exception if all retries are exhausted.
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return await async_fn(*args, **kwargs)
        except Exception as error:
            if not getattr(error, "retryable", False):
                raise
            last_error = error
            if attempt < max_retries:
                base_delay = initial_delay * (BACKOFF_FACTOR**attempt)
                jitter = random.uniform(0, base_delay * JITTER_FACTOR)
                delay = base_delay + jitter
                _LOGGER.warning(
                    "Retryable error on attempt %d/%d (%s: %s); retrying in %.2fs",
                    attempt + 1,
                    max_retries,
                    type(error).__name__,
                    error,
                    delay,
                )
                await asyncio.sleep(delay)

    # Should be unreachable when max_retries >= 0, but satisfies type checkers.
    assert last_error is not None
    _LOGGER.warning(
        "Retries exhausted after %d attempts (%s: %s); raising last error",
        max_retries + 1,
        type(last_error).__name__,
        last_error,
    )
    raise last_error
