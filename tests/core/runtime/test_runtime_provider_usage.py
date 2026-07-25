"""Integration tests for Runtime-owned Provider usage sampling."""

import logging
from pathlib import Path

import pytest

from core.providers.usage import ProviderUsageService
from core.runtime.runtime import Runtime
from core.utils.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "data")


@pytest.mark.asyncio
async def test_runtime_starts_shared_provider_usage_service_and_closes_it(
    config: Config,
) -> None:
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    service = runtime.provider_usage
    assert isinstance(service, ProviderUsageService)
    assert service._history_started is True  # noqa: SLF001

    await runtime.aclose()

    assert runtime._provider_usage is None  # noqa: SLF001
    assert service._history_started is False  # noqa: SLF001
    with pytest.raises(RuntimeError, match="not started"):
        _ = runtime.provider_usage
