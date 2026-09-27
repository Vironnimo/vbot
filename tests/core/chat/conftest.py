"""Fixtures shared by Chat tests."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def recovery_waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every Chat recovery backoff instead of waiting for it."""
    waits: list[float] = []

    async def record(delay: float) -> None:
        waits.append(delay)
        await asyncio.sleep(0)

    monkeypatch.setattr("core.chat.recovery._sleep", record)
    return waits
