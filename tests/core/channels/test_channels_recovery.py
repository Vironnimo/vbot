"""Channels: adapter failure recovery, restart backoff and start rollbacks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import override

import pytest

from core.channels import (
    ChannelAdapter,
    ChannelConfig,
    ChannelConfigError,
    ChannelNotFoundError,
    ChannelStorage,
)
from core.channels.adapter import FileData, RouteFacts
from core.extensions import InteractionButton
from tests.core.channels.channels_test_support import (
    BlockingAdapter,
    DelayedStopAdapter,
    make_config,
    make_service,
    wait_until,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


class CrashingAdapter(ChannelAdapter):
    """Adapter whose task ends with an error once it started."""

    platform = "telegram"

    def __init__(self, *, while_running: Callable[[], None] | None = None) -> None:
        self._while_running = while_running
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    @override
    async def start(self) -> None:
        self.started.set()
        if self._while_running is not None:
            self._while_running()
        raise RuntimeError("adapter failed")

    @override
    async def stop(self) -> None:
        self.stopped.set()

    @override
    async def send(
        self,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
        thread_id: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
    ) -> None:
        return

    @override
    async def ensure_outbound_session(
        self, platform_target: str, *, thread_id: str | None = None
    ) -> RouteFacts:
        raise NotImplementedError


def _record_restart_delays(service: object, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record each restart delay the service chooses and restart at once instead."""
    delays: list[float] = []
    original = service._restart_delay_seconds  # type: ignore[attr-defined]

    def immediate(attempt: int) -> float:
        delays.append(original(attempt))
        return 0.0

    monkeypatch.setattr(service, "_restart_delay_seconds", immediate)
    return delays


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failures", "expected_delays", "failed_while_retrying", "warnings"),
    [
        (0, [], [], 0),
        (1, [1.0], [False], 1),
        (
            7,
            [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0],
            [False, False, False, True, True, True, True],
            2,
        ),
    ],
    ids=["no-crash", "one-crash", "exhausted-fast-retries"],
)
async def test_a_crashing_adapter_restarts_with_capped_backoff_until_it_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failures: int,
    expected_delays: list[float],
    failed_while_retrying: list[bool],
    warnings: int,
) -> None:
    caplog.set_level(logging.INFO, logger="vbot.channels")
    ChannelStorage(tmp_path).save(make_config(enabled=True))
    service = make_service(tmp_path)
    recovered = BlockingAdapter()
    constructions = 0
    failed_seen: list[bool] = []
    hook_calls = 0

    def hook() -> None:
        nonlocal hook_calls
        hook_calls += 1

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        nonlocal constructions
        constructions += 1
        if constructions > 1:
            failed_seen.append(service.is_failed("tg-assistant"))
        return CrashingAdapter() if constructions <= failures else recovered

    original_delay = service._restart_delay_seconds
    service._notify_tool_registration_changed_hook = hook
    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    delays = _record_restart_delays(service, monkeypatch)
    service.start()
    try:
        await asyncio.wait_for(recovered.started.wait(), timeout=5)

        # After three fast retries the Channel is marked failed, but recovery
        # continues at the capped interval: a Channel must self-heal.
        assert delays == expected_delays
        assert failed_seen == failed_while_retrying
        assert service.is_running("tg-assistant")
        assert not service.is_failed("tg-assistant")
        assert service.failure_reason("tg-assistant") is None
        # Crashes never change the Tool registration, which follows config only.
        assert hook_calls == 0
        assert service.has_enabled_channels() is True
        # The exponent is capped before conversion, even after years offline.
        assert original_delay(1025) == original_delay(1_000_000) == 30.0

        # The first connection logs once: a start, or after crashes one recovery
        # line with the restart count; stopping the Channel logs once more.
        await service.aclose()
        info = [
            record.getMessage()
            for record in caplog.records
            if record.name == "vbot.channels" and record.levelno == logging.INFO
        ]
        assert len(info) == 2
        assert (f"attempts={failures}" in info[0]) is (failures > 0)
        # An outage warns at its first crash, with traceback, and once more when
        # the fast retries are exhausted; the crashes and restarts between are DEBUG.
        warning_records = [
            record
            for record in caplog.records
            if record.name == "vbot.channels" and record.levelno >= logging.WARNING
        ]
        assert [record.levelno for record in warning_records] == [logging.WARNING] * warnings
        assert all(record.exc_info is not None for record in warning_records[:1])
    finally:
        await service.aclose()
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uptime_seconds", "delay_after_the_run"),
    [(300.0, 1.0), (299.0, 30.0)],
    ids=["five-minutes-is-healthy", "shorter-run-keeps-counting"],
)
async def test_a_crash_after_a_healthy_run_starts_a_fresh_backoff_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    uptime_seconds: float,
    delay_after_the_run: float,
) -> None:
    # The service measures adapter uptime on a controlled clock; nothing waits.
    now = [1000.0]
    monkeypatch.setattr("core.channels.channels.time", SimpleNamespace(monotonic=lambda: now[0]))

    def run_for_uptime() -> None:
        now[0] += uptime_seconds

    ChannelStorage(tmp_path).save(make_config(enabled=True))
    service = make_service(tmp_path)
    recovered = BlockingAdapter()
    constructions = 0

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        # Five immediate crashes mark the Channel failed; the sixth adapter is up
        # for ``uptime_seconds`` before crashing, and the seventh keeps running.
        nonlocal constructions
        constructions += 1
        if constructions <= 5:
            return CrashingAdapter()
        if constructions == 6:
            return CrashingAdapter(while_running=run_for_uptime)
        return recovered

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    delays = _record_restart_delays(service, monkeypatch)
    service.start()
    try:
        await asyncio.wait_for(recovered.started.wait(), timeout=5)

        assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, delay_after_the_run]
        assert service.is_running("tg-assistant")
        assert not service.is_failed("tg-assistant")
    finally:
        await service.aclose()
        service.close()


@pytest.mark.asyncio
async def test_channel_service_ignores_stale_adapter_task_done_callback(
    tmp_path: Path,
) -> None:
    config = make_config(enabled=True)
    ChannelStorage(tmp_path).save(config)
    service = make_service(tmp_path)
    adapter = BlockingAdapter()
    stale_task = asyncio.create_task(asyncio.sleep(0))
    current_task = asyncio.create_task(asyncio.sleep(60))
    await stale_task

    service._adapters[config.id] = adapter
    service._adapter_tasks[config.id] = current_task

    service._on_adapter_task_done(config.id, stale_task)

    assert service._adapters[config.id] is adapter
    assert service._adapter_tasks[config.id] is current_task

    current_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await current_task
    service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["create", "update", "enable"])
async def test_a_failed_adapter_start_rolls_back_the_enabling_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    storage = ChannelStorage(tmp_path)
    original = make_config(enabled=False)
    if change != "create":
        storage.save(original)
    service = make_service(tmp_path)

    def fail_start_channel(
        _channel_id: str,
        *,
        reset_backoff: bool = True,
        config_override: ChannelConfig | None = None,
    ) -> None:
        raise ChannelConfigError("start failed")

    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)
    monkeypatch.setattr(service, "start_channel", fail_start_channel)
    try:
        with pytest.raises(ChannelConfigError, match="start failed"):
            if change == "create":
                await service.create_channel(make_config(enabled=True))
            elif change == "update":
                await service.update_channel(original.id, enabled=True)
            else:
                await service.enable_channel(original.id)

        if change == "create":
            with pytest.raises(ChannelNotFoundError):
                storage.get(original.id)
            # The registration written with the config is removed with it.
            with service.database.read() as connection:
                assert connection.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 0
        else:
            assert storage.get(original.id).to_dict() == original.to_dict()
        assert service._pending_config_changes == set()
    finally:
        service.close()


@pytest.mark.asyncio
async def test_a_failed_adapter_rebuild_restores_the_previous_config_and_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    original = make_config(enabled=True)
    storage.save(original)
    service = make_service(tmp_path)
    built_from: list[str] = []
    adapters: list[BlockingAdapter] = []

    def create_adapter(config: ChannelConfig) -> ChannelAdapter:
        built_from.append(config.token_env_var)
        adapters.append(BlockingAdapter())
        return adapters[-1]

    real_start_channel = service.start_channel

    def start_channel(
        channel_id: str,
        *,
        reset_backoff: bool = True,
        config_override: ChannelConfig | None = None,
    ) -> None:
        if config_override is not None and config_override.token_env_var != original.token_env_var:
            raise ChannelConfigError("start failed")
        real_start_channel(channel_id, reset_backoff=reset_backoff, config_override=config_override)

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)
    service.start()
    try:
        await asyncio.wait_for(adapters[0].started.wait(), timeout=1)
        monkeypatch.setattr(service, "start_channel", start_channel)

        with pytest.raises(ChannelConfigError, match="start failed"):
            await service.update_channel(original.id, token_env_var="TELEGRAM_BOT_TOKEN_OTHER")

        # The disturbed adapter comes back with the previous config, which is on disk again.
        await asyncio.wait_for(adapters[0].stopped.wait(), timeout=1)
        await wait_until(lambda: len(adapters) == 2)
        await asyncio.wait_for(adapters[1].started.wait(), timeout=1)
        assert built_from == [original.token_env_var] * 2
        assert storage.get(original.id).to_dict() == original.to_dict()
        assert service.is_running(original.id)
        assert service._pending_config_changes == set()
    finally:
        await service.aclose()
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["update", "restart"])
async def test_a_rebuilt_adapter_starts_only_after_the_old_one_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    config = make_config(enabled=True)
    ChannelStorage(tmp_path).save(config)
    service = make_service(tmp_path)
    stop_gate = asyncio.Event()
    lifecycle_events: list[str] = []
    created: list[DelayedStopAdapter] = []

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        label = "old" if not created else "new"
        adapter = DelayedStopAdapter(label=label, stop_gate=stop_gate, events=lifecycle_events)
        created.append(adapter)
        return adapter

    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)
    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    service.start()
    try:
        await asyncio.wait_for(created[0].started.wait(), timeout=1)

        if change == "update":
            await service.update_channel(config.id, token_env_var="TELEGRAM_BOT_TOKEN_OTHER")
        else:
            assert await service.restart_channel(config.id) is True
        await wait_until(lambda: "stop:old:begin" in lifecycle_events)
        assert "start:new" not in lifecycle_events

        stop_gate.set()
        await wait_until(lambda: "start:new" in lifecycle_events)
        assert lifecycle_events.index("stop:old:end") < lifecycle_events.index("start:new")
    finally:
        stop_gate.set()
        await service.aclose()
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_initial_construction", [False, True])
async def test_construction_failure_does_not_end_automatic_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fail_initial_construction: bool,
) -> None:
    config = make_config(enabled=True)
    ChannelStorage(tmp_path).save(config)
    service = make_service(tmp_path)
    recovered = BlockingAdapter()
    attempts = 0

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        nonlocal attempts
        attempts += 1
        if attempts == 1 and not fail_initial_construction:
            return CrashingAdapter()
        if attempts <= 3:
            raise ChannelConfigError("credential temporarily unavailable")
        return recovered

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    delays = _record_restart_delays(service, monkeypatch)
    caplog.set_level(logging.WARNING, logger="vbot.channels")
    service.start()
    try:
        if fail_initial_construction:
            # A constructor failure at startup marks only that Channel failed.
            assert service.is_failed(config.id)
            assert service.failure_reason(config.id) == "credential temporarily unavailable"
            # The expected failure warns without a traceback; recovery retries it.
            [startup] = [r for r in caplog.records if r.name == "vbot.channels"]
            assert (startup.levelno, startup.exc_info) == (logging.WARNING, None)
        await asyncio.wait_for(recovered.started.wait(), timeout=1)
        assert attempts == 4
        assert delays == [1.0, 2.0, 4.0]
        assert service.is_running(config.id)
        assert not service.is_failed(config.id)
        # Only the outage's first failure is logged above DEBUG, not each failed restart.
        assert len([r for r in caplog.records if r.name == "vbot.channels"]) == 1
    finally:
        await service.aclose()
        service.close()


@pytest.mark.asyncio
async def test_queued_construction_failure_recovers_after_old_adapter_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(enabled=True)
    ChannelStorage(tmp_path).save(config)
    service = make_service(tmp_path)
    old = BlockingAdapter()
    recovered = BlockingAdapter()
    attempts = 0

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return old
        if attempts == 2:
            raise ChannelConfigError("credential temporarily unavailable")
        return recovered

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)
    monkeypatch.setattr(service, "_restart_delay_seconds", lambda _attempt: 0.0)
    service.start()
    try:
        await old.started.wait()
        await service.restart_channel(config.id)
        await asyncio.wait_for(recovered.started.wait(), timeout=1)
        assert old.stopped.is_set()
        assert attempts == 3
    finally:
        await service.aclose()
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["disable", "service-stop"])
async def test_ending_a_channel_ends_its_construction_failure_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    config = make_config(enabled=True)
    ChannelStorage(tmp_path).save(config)
    service = make_service(tmp_path)
    constructions = 0

    def fail_construction(_config: ChannelConfig) -> ChannelAdapter:
        nonlocal constructions
        constructions += 1
        if ending == "service-stop" and constructions == 2:
            # The service stops just after this retry fails, before the retry's
            # own completion is processed.
            asyncio.get_running_loop().call_soon(service.stop)
        raise ChannelConfigError("credential temporarily unavailable")

    monkeypatch.setattr(service, "_create_adapter", fail_construction)
    # Disabling must end the retry before it fires, however slowly the disable
    # runs; stopping the service must happen right after a retry has failed.
    retry_delay = 0.0 if ending == "service-stop" else 3600.0
    monkeypatch.setattr(service, "_restart_delay_seconds", lambda _attempt: retry_delay)
    service.start()
    try:
        retry = service._adapter_restart_tasks[config.id]
        if ending == "disable":
            await service.disable_channel(config.id)
            await asyncio.gather(retry, return_exceptions=True)
            assert retry.cancelled()
        else:
            await wait_until(lambda: constructions == 2)
            await asyncio.gather(retry, return_exceptions=True)
            await asyncio.sleep(0)
        assert not service._adapter_restart_tasks
        assert constructions == (1 if ending == "disable" else 2)
        assert not service.is_running(config.id)
        assert not service.is_failed(config.id)
    finally:
        await service.aclose()
        service.close()
