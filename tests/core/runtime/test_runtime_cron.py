"""Integration tests for runtime cron lifecycle wiring."""

import logging
from pathlib import Path

import pytest

from core.automation import CronService
from core.runtime.runtime import Runtime
from core.utils.config import Config


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Provide isolated runtime config."""
    return Config(data_dir=tmp_path / "data")


@pytest.mark.asyncio
async def test_runtime_start_exposes_cron_service_and_stop_clears_it(config: Config) -> None:
    """Runtime wires CronService on start and clears it on stop."""
    logging.getLogger("vbot").handlers = []
    runtime = Runtime(config)

    runtime.start()

    assert isinstance(runtime.cron_service, CronService)
    assert runtime._cron_service is not None  # noqa: SLF001
    assert runtime._cron_service._started is True  # noqa: SLF001

    runtime.stop()

    assert runtime._cron_service is None  # noqa: SLF001
    with pytest.raises(RuntimeError, match="not started"):
        _ = runtime.cron_service
