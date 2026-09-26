"""Channels: recovery behavior."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from core.channels import (
    ChannelAdapter,
    ChannelConfig,
    ChannelConfigError,
    ChannelNotFoundError,
    ChannelStorage,
)
from core.channels.adapter import (
    FileData,
    RouteFacts,
)
from core.extensions import InteractionButton
from tests.core.channels.channels_helpers import (
    BlockingAdapter,
    DelayedStopAdapter,
    make_config,
    make_service,
    wait_until,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


class FailingAdapter(ChannelAdapter):
    platform = "telegram"

    def __init__(self, *, fail_on_start: bool) -> None:
        self._fail_on_start = fail_on_start
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()
        if self._fail_on_start:
            raise RuntimeError("adapter failed")
        await asyncio.Future()

    async def stop(self) -> None:
        self.stopped.set()

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

    async def ensure_outbound_session(
        self, platform_target: str, *, thread_id: str | None = None
    ) -> RouteFacts:
        raise NotImplementedError


class RunThenCrashAdapter(ChannelAdapter):
    """Adapter that starts, runs briefly, and then crashes."""

    platform = "telegram"

    def __init__(self, *, run_seconds: float) -> None:
        self._run_seconds = run_seconds
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self) -> None:
        self.started.set()
        await asyncio.sleep(self._run_seconds)
        raise RuntimeError("crashed after healthy run")

    async def stop(self) -> None:
        self.stopped.set()

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

    async def ensure_outbound_session(
        self, platform_target: str, *, thread_id: str | None = None
    ) -> RouteFacts:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_channel_service_adapter_crash_does_not_change_tool_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    hook_calls = 0

    def hook() -> None:
        nonlocal hook_calls
        hook_calls += 1

    service._notify_tool_registration_changed_hook = hook
    monkeypatch.setattr(
        service, "_create_adapter", lambda _config: FailingAdapter(fail_on_start=True)
    )
    monkeypatch.setattr(service, "_schedule_restart", lambda _channel_id: None)

    service.start()
    await wait_until(lambda: config.id not in service._adapter_tasks)

    assert service.has_active_channels() is False
    assert service.has_enabled_channels() is True
    assert hook_calls == 0


@pytest.mark.asyncio
async def test_channel_service_start_isolates_unexpected_adapter_construction_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)
    service = make_service(tmp_path)

    def fail_adapter_construction(_config: ChannelConfig) -> ChannelAdapter:
        raise RuntimeError("adapter dependency is broken")

    monkeypatch.setattr(service, "_create_adapter", fail_adapter_construction)

    with caplog.at_level(logging.ERROR, logger="vbot.channels"):
        service.start()

    assert config.id in service._failed_channels
    assert service._failure_reasons[config.id] == "adapter dependency is broken"
    assert caplog.records


@pytest.mark.asyncio
async def test_channel_service_ignores_stale_adapter_task_done_callback(
    tmp_path: Path,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

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


@pytest.mark.asyncio
async def test_channel_service_update_rejects_unknown_fields(tmp_path: Path) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=False)
    storage.save(config)
    service = make_service(tmp_path)

    with pytest.raises(ChannelConfigError):
        await service.update_channel(config.id, unknown_field="value")


@pytest.mark.asyncio
async def test_channel_service_create_rolls_back_when_start_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    service = make_service(tmp_path)
    config = make_config(enabled=True)

    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)

    def fail_start_channel(
        _channel_id: str,
        *,
        reset_backoff: bool = True,
        config_override: ChannelConfig | None = None,
    ) -> None:
        raise ChannelConfigError("start failed")

    monkeypatch.setattr(service, "start_channel", fail_start_channel)

    with pytest.raises(ChannelConfigError, match="start failed"):
        await service.create_channel(config)

    with pytest.raises(ChannelNotFoundError):
        storage.get(config.id)
    # The registration written with the config is removed with it.
    with service.database.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("via_enable", [False, True], ids=["update", "enable"])
async def test_channel_service_enabling_rolls_back_when_start_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    via_enable: bool,
) -> None:
    storage = ChannelStorage(tmp_path)
    original = make_config(enabled=False)
    storage.save(original)
    service = make_service(tmp_path)

    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)

    def fail_start_channel(
        _channel_id: str,
        *,
        reset_backoff: bool = True,
        config_override: ChannelConfig | None = None,
    ) -> None:
        raise ChannelConfigError("restart failed")

    monkeypatch.setattr(service, "start_channel", fail_start_channel)

    with pytest.raises(ChannelConfigError, match="restart failed"):
        if via_enable:
            await service.enable_channel(original.id)
        else:
            await service.update_channel(original.id, enabled=True)

    assert storage.get(original.id).to_dict() == original.to_dict()
    assert service._pending_config_changes == set()


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


@pytest.mark.asyncio
async def test_channel_service_update_waits_for_adapter_stop_before_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    stop_gate = asyncio.Event()
    lifecycle_events: list[str] = []
    created: list[DelayedStopAdapter] = []

    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        label = "old" if not created else "new"
        adapter = DelayedStopAdapter(label=label, stop_gate=stop_gate, events=lifecycle_events)
        created.append(adapter)
        return adapter

    monkeypatch.setattr(service, "_create_adapter", create_adapter)

    service.start()
    await asyncio.wait_for(created[0].started.wait(), timeout=1)

    await service.update_channel(config.id, token_env_var="TELEGRAM_BOT_TOKEN_OTHER")
    await wait_until(lambda: "stop:old:begin" in lifecycle_events)

    assert "start:new" not in lifecycle_events

    stop_gate.set()
    await wait_until(lambda: "start:new" in lifecycle_events)
    assert lifecycle_events.index("stop:old:end") < lifecycle_events.index("start:new")

    service.stop()


@pytest.mark.asyncio
async def test_channel_service_restart_rebuilds_only_after_adapter_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    stop_gate = asyncio.Event()
    lifecycle_events: list[str] = []
    created: list[DelayedStopAdapter] = []

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        label = "old" if not created else "new"
        adapter = DelayedStopAdapter(label=label, stop_gate=stop_gate, events=lifecycle_events)
        created.append(adapter)
        return adapter

    monkeypatch.setattr(service, "_create_adapter", create_adapter)

    service.start()
    await asyncio.wait_for(created[0].started.wait(), timeout=1)

    assert await service.restart_channel(config.id) is True
    await wait_until(lambda: "stop:old:begin" in lifecycle_events)
    assert "start:new" not in lifecycle_events

    stop_gate.set()
    await wait_until(lambda: "start:new" in lifecycle_events)
    assert lifecycle_events.index("stop:old:end") < lifecycle_events.index("start:new")

    service.stop()


@pytest.mark.asyncio
async def test_channel_service_restarts_failed_adapter_with_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    created: list[FailingAdapter] = []
    starts = 0

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        nonlocal starts
        starts += 1
        adapter = FailingAdapter(fail_on_start=starts == 1)
        created.append(adapter)
        return adapter

    delays: list[float] = []
    original_restart_delay = service._restart_delay_seconds

    def immediate_restart_delay(attempt: int) -> float:
        delays.append(original_restart_delay(attempt))
        return 0.0

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_restart_delay_seconds", immediate_restart_delay)

    service.start()
    await wait_until(lambda: len(created) >= 2)
    await asyncio.wait_for(created[1].started.wait(), timeout=1)

    assert delays == [1.0]
    assert service.has_active_channels() is True
    assert config.id not in service._failed_channels

    service.stop()
    await asyncio.wait_for(created[-1].stopped.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_channel_service_keeps_retrying_failed_adapter_after_max_restart_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    created: list[FailingAdapter] = []

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        adapter = FailingAdapter(fail_on_start=True)
        created.append(adapter)
        return adapter

    delays: list[float] = []
    original_restart_delay = service._restart_delay_seconds

    def immediate_restart_delay(attempt: int) -> float:
        delays.append(original_restart_delay(attempt))
        return 0.0

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_restart_delay_seconds", immediate_restart_delay)

    service.start()
    # Exhausting the fast-retry budget marks the channel failed …
    await wait_until(lambda: config.id in service._failed_channels)
    # … but recovery attempts continue at the capped backoff interval instead
    # of giving up (a channel is the operator's lifeline and must self-heal).
    await wait_until(lambda: len(created) >= 7)

    assert delays[:4] == [1.0, 2.0, 4.0, 8.0]
    assert all(delay >= 16.0 for delay in delays[4:])
    assert all(delay <= 30.0 for delay in delays)
    # The raw failure marker stays set while recovery attempts continue; the
    # public is_failed() view flaps with the in-flight restart task, so the
    # deterministic assertion is on the marker itself.
    assert config.id in service._failed_channels
    assert service._adapter_restart_attempts[config.id] >= 4

    service.stop()


@pytest.mark.asyncio
async def test_channel_service_recovers_and_clears_failed_marker_after_exhausted_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    created: list[FailingAdapter] = []
    starts = 0

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        nonlocal starts
        starts += 1
        adapter = FailingAdapter(fail_on_start=starts <= 4)
        created.append(adapter)
        return adapter

    delays: list[float] = []
    original_restart_delay = service._restart_delay_seconds

    def immediate_restart_delay(attempt: int) -> float:
        delays.append(original_restart_delay(attempt))
        return 0.0

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_restart_delay_seconds", immediate_restart_delay)

    service.start()
    await wait_until(lambda: config.id in service._failed_channels)
    await wait_until(lambda: len(created) >= 5)
    await asyncio.wait_for(created[4].started.wait(), timeout=1)

    assert delays == [1.0, 2.0, 4.0, 8.0]
    assert service._is_running(config.id) is True
    assert service.is_failed(config.id) is False
    assert service.failure_reason(config.id) is None

    service.stop()
    await asyncio.wait_for(created[-1].stopped.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_channel_service_resets_restart_attempts_after_healthy_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.channels.channels._ADAPTER_HEALTHY_RUN_RESET_SECONDS", 0.02)
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    created: list[ChannelAdapter] = []
    blocking: list[BlockingAdapter] = []
    mode = {"phase": "chronic"}

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        if mode["phase"] == "chronic":
            adapter: ChannelAdapter = FailingAdapter(fail_on_start=True)
        elif mode["phase"] == "healthy-run":
            adapter = RunThenCrashAdapter(run_seconds=0.05)
        else:
            adapter = BlockingAdapter()
            blocking.append(adapter)
        created.append(adapter)
        return adapter

    delays: list[float] = []
    original_restart_delay = service._restart_delay_seconds

    def immediate_restart_delay(attempt: int) -> float:
        delays.append(original_restart_delay(attempt))
        return 0.0

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_restart_delay_seconds", immediate_restart_delay)

    service.start()
    await wait_until(lambda: config.id in service._failed_channels)
    # Next adapter runs briefly and crashes: that healthy run must reset the
    # restart-attempt counter …
    mode["phase"] = "healthy-run"
    await wait_until(lambda: any(isinstance(a, RunThenCrashAdapter) for a in created))
    # … so the following restart starts a fresh cycle (delay 1.0 again) and the
    # blocking adapter keeps it running, clearing the failed marker.
    mode["phase"] = "blocking"
    await wait_until(
        lambda: any(isinstance(a, BlockingAdapter) and a.started.is_set() for a in created)
    )

    assert delays[-1] == 1.0
    assert service._adapter_restart_attempts[config.id] == 1
    assert service.is_failed(config.id) is False
    assert service._is_running(config.id) is True

    service.stop()
    await asyncio.wait_for(blocking[-1].stopped.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_channel_service_keeps_attempt_count_without_healthy_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("core.channels.channels._ADAPTER_HEALTHY_RUN_RESET_SECONDS", 3600.0)
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    created: list[ChannelAdapter] = []
    blocking: list[BlockingAdapter] = []
    mode = {"phase": "chronic"}

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        if mode["phase"] == "chronic":
            adapter: ChannelAdapter = FailingAdapter(fail_on_start=True)
        elif mode["phase"] == "short-run":
            adapter = RunThenCrashAdapter(run_seconds=0.05)
        else:
            adapter = BlockingAdapter()
            blocking.append(adapter)
        created.append(adapter)
        return adapter

    delays: list[float] = []
    original_restart_delay = service._restart_delay_seconds

    def immediate_restart_delay(attempt: int) -> float:
        delays.append(original_restart_delay(attempt))
        return 0.0

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_restart_delay_seconds", immediate_restart_delay)

    service.start()
    await wait_until(lambda: config.id in service._failed_channels)
    # A crash after only a short run is NOT healthy: no attempt-counter reset,
    # the backoff continues from the chronic cycle instead of restarting at 1.0s.
    mode["phase"] = "short-run"
    await wait_until(lambda: any(isinstance(a, RunThenCrashAdapter) for a in created))
    mode["phase"] = "blocking"
    await wait_until(
        lambda: any(isinstance(a, BlockingAdapter) and a.started.is_set() for a in created)
    )

    assert delays[-1] >= 8.0
    assert service._adapter_restart_attempts[config.id] >= 5

    service.stop()
    await asyncio.wait_for(blocking[-1].stopped.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_configuration_save_failure_keeps_existing_adapter_running(tmp_path, monkeypatch):
    storage = ChannelStorage(tmp_path)
    original = make_config(enabled=True)
    storage.save(original)
    service = make_service(tmp_path)
    monkeypatch.setattr(service, "_is_running", lambda _id: True)
    monkeypatch.setattr(service, "_preflight_adapter_start", lambda _config: None)

    def forbidden_stop(*args, **kwargs):
        raise AssertionError("a failed save must not stop the running adapter")

    def fail_save(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(service, "stop_channel", forbidden_stop)
    monkeypatch.setattr(service._storage, "save", fail_save)
    with pytest.raises(OSError, match="disk full"):
        await service.update_channel(original.id, enabled=False)
    assert storage.get(original.id).to_dict() == original.to_dict()


def test_restart_delay_remains_bounded_after_many_failures(tmp_path):
    service = make_service(tmp_path)
    assert service._restart_delay_seconds(1025) <= 30
    assert service._restart_delay_seconds(1000000) <= 30


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_initial_construction", [False, True])
async def test_construction_failure_does_not_end_automatic_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_initial_construction: bool,
) -> None:
    config = make_config(enabled=True)
    ChannelStorage(tmp_path).save(config)
    service = make_service(tmp_path)
    recovered = BlockingAdapter()
    attempts = 0
    delays: list[float] = []
    original_delay = service._restart_delay_seconds

    def create_adapter(_config: ChannelConfig) -> ChannelAdapter:
        nonlocal attempts
        attempts += 1
        if attempts == 1 and not fail_initial_construction:
            return FailingAdapter(fail_on_start=True)
        if attempts <= 3:
            raise ChannelConfigError("credential temporarily unavailable")
        return recovered

    def immediate_delay(attempt: int) -> float:
        delays.append(original_delay(attempt))
        return 0.0

    monkeypatch.setattr(service, "_create_adapter", create_adapter)
    monkeypatch.setattr(service, "_restart_delay_seconds", immediate_delay)
    service.start()
    try:
        await asyncio.wait_for(recovered.started.wait(), timeout=1)
        assert attempts == 4
        assert delays == [1.0, 2.0, 4.0]
        assert service.is_running(config.id)
        assert not service.is_failed(config.id)
    finally:
        await service.aclose()


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


@pytest.mark.asyncio
async def test_disabling_channel_cancels_construction_failure_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(enabled=True)
    ChannelStorage(tmp_path).save(config)
    service = make_service(tmp_path)

    def fail_construction(_config: ChannelConfig) -> ChannelAdapter:
        raise ChannelConfigError("credential temporarily unavailable")

    monkeypatch.setattr(service, "_create_adapter", fail_construction)
    service.start()
    try:
        retry = service._adapter_restart_tasks[config.id]
        await service.disable_channel(config.id)
        await asyncio.gather(retry, return_exceptions=True)
        assert not service._adapter_restart_tasks
        assert not service.is_running(config.id)
        assert not service.is_failed(config.id)
    finally:
        await service.aclose()
