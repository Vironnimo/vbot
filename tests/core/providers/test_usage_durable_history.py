"""Usage: durable history behavior."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from core.providers._usage_parsers import (
    _parse_openai_usage,
)
from core.providers.usage import (
    ProviderUsageHistoryStore,
    ProviderUsageService,
)
from tests.core.providers.usage_helpers import (
    _OPENAI_BODY,
    FakeResponse,
    FakeTransport,
    _openai_runtime,
)


# Durable hourly history
@pytest.mark.asyncio
async def test_collect_history_sample_reuses_live_cache_and_persists_structured_data(
    tmp_path: Any,
) -> None:
    observed_at = datetime(2026, 7, 25, 12, tzinfo=UTC)
    transport = FakeTransport(FakeResponse(payload=_OPENAI_BODY))
    service = ProviderUsageService(
        _openai_runtime(),
        transport=transport,
        data_root=tmp_path,
        clock=lambda: observed_at,
    )

    live = await service.report()
    stored = await service.collect_history_sample()
    history = service.history_report()

    assert stored is True
    assert len(transport.calls) == 1
    assert len(history.samples) == 1
    sample = history.samples[0]
    assert sample.sampled_at == observed_at.isoformat()
    assert sample.providers[0]["account"] == "default"
    assert sample.providers[0]["windows"][0]["window_seconds"] == 18_000
    assert sample.providers[0]["plan"] == live.providers[0].plan


def test_history_sampler_delays_until_one_hour_after_latest_sample(tmp_path: Any) -> None:
    observed_at = datetime(2026, 7, 25, 12, tzinfo=UTC)
    store = ProviderUsageHistoryStore(tmp_path)
    store.append(
        (observed_at - timedelta(minutes=15)).isoformat(),
        [
            _parse_openai_usage(
                "openai:subscription",
                "OpenAI",
                _OPENAI_BODY,
            ).to_dict()
        ],
    )
    service = ProviderUsageService(
        _openai_runtime(),
        history_store=store,
        clock=lambda: observed_at,
    )

    assert service._initial_history_delay() == 45 * 60  # noqa: SLF001


@pytest.mark.asyncio
async def test_history_sampler_continues_after_unexpected_sample_failure(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    continued = asyncio.Event()
    attempts = 0
    service = ProviderUsageService(
        _openai_runtime(),
        data_root=tmp_path,
        history_interval=0,
    )

    async def collect_with_one_failure() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("boom")
        continued.set()
        return True

    monkeypatch.setattr(service, "collect_history_sample", collect_with_one_failure)

    with caplog.at_level(logging.ERROR, logger="vbot.providers.usage"):
        service.start()
        await asyncio.wait_for(continued.wait(), timeout=1)
        await service.aclose()

    assert attempts >= 2
    assert service._history_started is False  # noqa: SLF001
    assert caplog.records
