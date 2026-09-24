"""Integration tests for Runtime-owned Provider usage sampling."""

import logging
from pathlib import Path

import pytest

from core.database import canonical_database_path
from core.providers.usage import ProviderUsageService
from core.runtime.databases import canonical_database_specs
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
    database = service.history_database
    assert database is not None
    assert database.path == canonical_database_path(config.data_dir, "provider_usage")
    assert database in runtime.canonical_databases()

    await runtime.aclose()

    assert runtime._provider_usage is None  # noqa: SLF001
    assert service._history_started is False  # noqa: SLF001
    assert database.is_closed()
    with pytest.raises(RuntimeError):
        _ = runtime.provider_usage


def test_offline_tools_know_the_provider_usage_database(tmp_path: Path) -> None:
    specs = {spec.name: spec for spec in canonical_database_specs(tmp_path)}

    assert specs["provider_usage"].path == tmp_path / "provider-usage.db"
    assert specs["provider_usage"].profile == "canonical"
