"""Extension mutations keep late failures observable after caller cancellation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.extensions.extensions import ExtensionRegistry
from core.extensions.runtime import ExtensionRuntime
from core.tools import ToolRegistry

_LOGGER_NAME = "vbot.extensions.runtime"


def _runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reload_skills: Callable[[], Awaitable[None]],
) -> ExtensionRuntime:
    registry = ExtensionRegistry()

    async def load(*_args, **_kwargs) -> ExtensionRegistry:
        return registry

    monkeypatch.setattr(ExtensionRegistry, "aload", load)
    return ExtensionRuntime(
        storage=cast(Any, SimpleNamespace(data_dir=tmp_path, load_settings=lambda: {})),
        resources_path=tmp_path,
        tools=ToolRegistry(),
        get_registry=lambda: registry,
        set_registry=lambda _registry: None,
        get_command_dispatcher=lambda: None,
        extra_directories=lambda _settings: [],
        load_options=lambda _settings: (set(), {}),
        live_config=lambda _name: {},
        resolve_credential=lambda _key: "",
        reload_recall=lambda: None,
        refresh_prompts=lambda: None,
        reload_skills=reload_skills,
        recover_recall=lambda _names: None,
        logger=logging.getLogger(_LOGGER_NAME),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reload", "disable"])
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_cancelled_mutation_reports_late_failure_once_after_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
    outcome: str,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    failure = RuntimeError("test-sentinel")

    async def refresh() -> None:
        entered.set()
        await release.wait()
        if outcome == "failure":
            raise failure
        if outcome == "cancelled":
            raise asyncio.CancelledError

    runtime = _runtime(tmp_path, monkeypatch, refresh)
    caller = asyncio.create_task(
        runtime.reload() if operation == "reload" else runtime.apply_disabled_change({"fixture"})
    )
    queued: asyncio.Task[None] | None = None
    try:
        await entered.wait()
        for _ in range(2):
            caller.cancel()
            await asyncio.sleep(0)
            assert not caller.done()
        queued = asyncio.create_task(runtime.startup())
        await asyncio.sleep(0)
        assert not queued.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await queued

        records = [record for record in caplog.records if record.name == _LOGGER_NAME]
        assert len(records) == (1 if outcome == "failure" else 0)
        if records:
            assert records[0].levelno == logging.ERROR
            assert records[0].exc_info is not None
            assert records[0].exc_info[1] is failure
        assert not [record for record in caplog.records if record.name == "asyncio"]
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        if queued is not None:
            await queued


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reload", "disable"])
async def test_uncancelled_mutation_preserves_failure_without_duplicate_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
) -> None:
    failure = RuntimeError("test-sentinel")

    async def refresh() -> None:
        raise failure

    runtime = _runtime(tmp_path, monkeypatch, refresh)
    with pytest.raises(RuntimeError) as caught:
        if operation == "reload":
            await runtime.reload()
        else:
            await runtime.apply_disabled_change({"fixture"})
    assert caught.value is failure
    assert not [record for record in caplog.records if record.name == _LOGGER_NAME]
