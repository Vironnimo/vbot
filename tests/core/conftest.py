"""Core test isolation: the shared retry backoff does not wait."""

from __future__ import annotations

import asyncio

import pytest


async def _skip_backoff_wait(_delay: float) -> None:
    # Yield once like a real wait so concurrent tasks still interleave.
    await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _no_retry_backoff_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip ``core.utils.retry`` backoff waits in every core test.

    A test that observes the waits patches ``core.utils.retry._sleep`` itself.
    """
    monkeypatch.setattr("core.utils.retry._sleep", _skip_backoff_wait)
