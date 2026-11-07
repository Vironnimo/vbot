"""Native Claude catalog enrollment, publication and Runtime transport lifetime."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

from core.providers import _catalog_refresh
from core.providers.accounts import ConnectionRef
from core.runtime import Runtime
from core.utils.config import Config
from core.utils.errors import ConfigError

PROVIDER = "claude-agent"
CONNECTION = "claude-agent:subscription"
READY = {
    "connected": True,
    "status": "ready",
    "subscription": True,
    "models": [
        {
            "value": "opus",
            "displayName": "Subscription Opus",
            "supportedEffortLevels": ["low", "high"],
            "contextWindow": 1_000_000,
        },
    ],
}


class _NativeSessions:
    """The native process dependency; no Claude installation or network is used."""

    def __init__(self) -> None:
        self.result: dict[str, Any] = copy.deepcopy(READY)
        self.probes = 0
        self.closed_sessions: list[set[tuple[str, str]]] = []
        self.closed = 0
        self.aborted = 0
        self.bindings: dict[str, Any] = {}

    async def probe(self) -> dict[str, Any]:
        self.probes += 1
        return copy.deepcopy(self.result)

    async def close_sessions(self, sessions: set[tuple[str, str]]) -> None:
        self.closed_sessions.append(sessions)

    async def aclose(self) -> None:
        self.closed += 1

    def abort(self) -> None:
        self.aborted += 1


@pytest_asyncio.fixture
async def native_runtime(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Runtime, list[_NativeSessions]]]:
    registries: list[_NativeSessions] = []

    def sessions(**_kwargs: Any) -> _NativeSessions:
        registry = _NativeSessions()
        registry.bindings = _kwargs
        registries.append(registry)
        return registry

    monkeypatch.setattr("core.providers.runtime.ClaudeAgentSessions", sessions)
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    try:
        yield runtime, registries
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_native_catalog_requires_opt_in_then_publishes_only_to_the_instance(
    native_runtime: tuple[Runtime, list[_NativeSessions]],
) -> None:
    runtime, registries = native_runtime
    native = registries[0]
    registry = runtime.models
    notifications: list[bool] = []
    runtime.add_model_catalog_changed_callback(lambda: notifications.append(True))
    other_catalog = registry.list_for_provider("openai")
    system_catalog = runtime.storage.resources_dir / "models" / f"{PROVIDER}.json"
    assert not system_catalog.exists()
    assert runtime.provider_credentials.is_connection_added(PROVIDER, CONNECTION) is False
    assert await runtime.refresh_external_connection(PROVIDER, CONNECTION) == {
        "connected": False,
        "status": "disabled",
    }
    assert native.probes == 0

    runtime.storage.set_provider_connection_enabled(CONNECTION, True)
    native.result = {"connected": False, "status": "missing", "message": "Synthetic missing CLI"}
    assert (await runtime.refresh_external_connection(PROVIDER, CONNECTION))["status"] == "missing"
    assert registry.list_for_provider(PROVIDER) == []
    assert notifications == []
    before = native.probes
    await runtime.maybe_refresh_local_catalogs(force=True)
    assert native.probes == before
    assert runtime.connection_reachability(CONNECTION) is False

    native.result = {**copy.deepcopy(READY), "subscription": False}
    api_status = await runtime.refresh_external_connection(PROVIDER, CONNECTION)
    assert api_status["connected"] is False
    assert api_status["status"] == "api_billing"
    assert registry.list_for_provider(PROVIDER) == []

    native.result = copy.deepcopy(READY)
    status = await runtime.refresh_external_connection(PROVIDER, CONNECTION)

    assert status == {"connected": True, "status": "ready", "subscription": True}
    assert runtime.connection_reachability(CONNECTION) is True
    assert runtime.models is registry
    model = registry.get(PROVIDER, "opus")
    assert model.name == "Subscription Opus"
    assert model.context_window == 1_000_000
    assert model.connections == ("subscription",)
    assert model.capabilities.reasoning.levels == ("low", "high")
    assert registry.list_for_provider("openai") == other_catalog
    assert (runtime.storage.data_dir / "artifacts" / "models" / f"{PROVIDER}.json").is_file()
    assert not system_catalog.exists()

    assert notifications == [True]
    adapter = runtime.get_adapter(ConnectionRef(PROVIDER, CONNECTION))
    await adapter.aclose()
    with pytest.raises(ConfigError):
        runtime.get_adapter(ConnectionRef(PROVIDER, CONNECTION + ":work"))
    await runtime.refresh_external_connection(PROVIDER, CONNECTION)
    assert notifications == [True]

    before = native.probes
    with pytest.raises(ConfigError):
        await runtime.refresh_model_database(provider_id=PROVIDER, target="system")
    assert native.probes == before
    assert not system_catalog.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("login_status", ["logged_out", "subscription_unconfirmed"])
async def test_a_failed_native_login_check_keeps_the_last_published_catalog(
    native_runtime: tuple[Runtime, list[_NativeSessions]],
    login_status: str,
) -> None:
    runtime, registries = native_runtime
    runtime.storage.set_provider_connection_enabled(CONNECTION, True)
    await runtime.refresh_external_connection(PROVIDER, CONNECTION)
    catalog_path = runtime.storage.data_dir / "artifacts" / "models" / f"{PROVIDER}.json"
    before = catalog_path.read_bytes()
    previous = runtime.models.get(PROVIDER, "opus")

    registries[0].result = {
        "connected": login_status == "subscription_unconfirmed",
        "status": login_status,
        "subscription": False,
        "message": "Synthetic external login ended",
    }
    status = await runtime.refresh_external_connection(PROVIDER, CONNECTION)

    assert status["connected"] is False
    assert status["status"] == login_status
    assert runtime.external_connection_status(PROVIDER, CONNECTION) == status
    assert catalog_path.read_bytes() == before
    assert runtime.models.get(PROVIDER, "opus") == previous


@pytest.mark.asyncio
async def test_busy_native_discovery_preserves_readiness_catalog_and_reachability(
    native_runtime: tuple[Runtime, list[_NativeSessions]],
) -> None:
    runtime, registries = native_runtime
    runtime.storage.set_provider_connection_enabled(CONNECTION, True)
    await runtime.refresh_external_connection(PROVIDER, CONNECTION)
    catalog_path = runtime.storage.data_dir / "artifacts" / "models" / f"{PROVIDER}.json"
    before = catalog_path.read_bytes()
    registries[0].result = {
        "connected": False,
        "status": "busy",
        "message": "Synthetic busy",
        "subscription": False,
    }
    await runtime.refresh_external_connection(PROVIDER, CONNECTION)
    status = runtime.external_connection_status(PROVIDER, CONNECTION)
    assert status["status"] == "busy"
    assert status["connected"] is True
    assert status["subscription"] is True
    assert runtime.connection_reachability(CONNECTION) is True
    assert catalog_path.read_bytes() == before


@pytest.mark.asyncio
async def test_native_background_checks_are_hourly_and_never_part_of_listing_sweeps(
    native_runtime: tuple[Runtime, list[_NativeSessions]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, registries = native_runtime
    native = registries[0]
    runtime.storage.set_provider_connection_enabled(CONNECTION, True)
    now = [1000.0]
    sleeps: asyncio.Queue[float] = asyncio.Queue()
    wake: asyncio.Queue[None] = asyncio.Queue()

    async def controlled_sleep(delay: float) -> None:
        sleeps.put_nowait(delay)
        await wake.get()

    monkeypatch.setattr(_catalog_refresh, "_external_now", lambda: now[0])
    monkeypatch.setattr(_catalog_refresh, "_sleep", controlled_sleep)
    for _ in range(2):
        await runtime.maybe_refresh_local_catalogs(force=True)
    assert native.probes == 0

    task = runtime.start_external_catalog_refresh()
    assert task is not None
    assert await sleeps.get() == 3600
    assert native.probes == 1
    assert runtime.start_external_catalog_refresh() is task
    now[0] += 3599
    wake.put_nowait(None)
    assert await sleeps.get() == 1
    assert native.probes == 1
    now[0] += 1
    wake.put_nowait(None)
    assert await sleeps.get() == 3600
    assert native.probes == 2
    await runtime.refresh_external_connection(PROVIDER, CONNECTION)
    assert native.probes == 3
    await runtime.aclose()
    assert task.done()


@pytest.mark.asyncio
async def test_native_initialization_does_not_hold_catalog_publication_admission(
    native_runtime: tuple[Runtime, list[_NativeSessions]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, registries = native_runtime
    runtime.storage.set_provider_connection_enabled(CONNECTION, True)
    entered, release = asyncio.Event(), asyncio.Event()
    probes = 0

    async def blocked_probe() -> dict[str, Any]:
        nonlocal probes
        probes += 1
        entered.set()
        await release.wait()
        return copy.deepcopy(READY)

    monkeypatch.setattr(registries[0], "probe", blocked_probe)
    refresh = asyncio.create_task(runtime.refresh_external_connection(PROVIDER, CONNECTION))
    manual: asyncio.Task[dict[str, Any]] | None = None
    try:
        await entered.wait()
        started = asyncio.Event()

        async def manual_refresh() -> dict[str, Any]:
            started.set()
            return await runtime.refresh_model_database(provider_id=PROVIDER)

        manual = asyncio.create_task(manual_refresh())
        await started.wait()
        assert probes == 1
        async with asyncio.timeout(1), runtime.model_database_refresh():
            pass
    finally:
        release.set()
        await refresh
        if manual is not None:
            await manual
    assert probes == 2


@pytest.mark.asyncio
async def test_disconnecting_during_a_probe_cannot_restore_native_readiness_or_catalog(
    native_runtime: tuple[Runtime, list[_NativeSessions]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, registries = native_runtime
    runtime.storage.set_provider_connection_enabled(CONNECTION, True)
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_probe() -> dict[str, Any]:
        entered.set()
        await release.wait()
        return copy.deepcopy(READY)

    monkeypatch.setattr(registries[0], "probe", blocked_probe)
    refresh = asyncio.create_task(runtime.refresh_external_connection(PROVIDER, CONNECTION))
    await entered.wait()
    runtime.storage.set_provider_connection_enabled(CONNECTION, False)
    await runtime.close_connection_transports(PROVIDER, "subscription")
    release.set()
    with pytest.raises(ConfigError):
        await refresh
    assert runtime.external_connection_status(PROVIDER, CONNECTION)["connected"] is False
    assert runtime.models.list_for_provider(PROVIDER) == []

    # The superseded check must not throttle discovery on the replacement owner.
    sleeping = asyncio.Event()

    async def hold_background(_delay: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(_catalog_refresh, "_sleep", hold_background)
    runtime.storage.set_provider_connection_enabled(CONNECTION, True)
    runtime.start_external_catalog_refresh()
    await sleeping.wait()
    assert registries[1].probes == 1
    assert runtime.models.get(PROVIDER, "opus").name == "Subscription Opus"


@pytest.mark.asyncio
async def test_native_registries_follow_session_connection_and_runtime_lifetime(
    native_runtime: tuple[Runtime, list[_NativeSessions]],
) -> None:
    runtime, registries = native_runtime
    first = registries[0]
    await runtime.close_session_transports([("coder", "session-one"), ("other", "session-two")])
    assert first.closed_sessions == [{("coder", "session-one"), ("other", "session-two")}]
    assert first.closed == 0

    await runtime.close_connection_transports("openai", "subscription")
    assert registries == [first]
    assert first.closed == 0

    await runtime.close_connection_transports(PROVIDER, "subscription")
    second = registries[1]
    assert first.closed == 1
    assert second.closed == 0
    assert runtime.external_connection_status(PROVIDER, CONNECTION)["status"] == "not_checked"

    runtime.storage.set_provider_connection_enabled(CONNECTION, True)
    assert (await runtime.refresh_external_connection(PROVIDER, CONNECTION))["connected"] is True
    assert second.probes == 1
    assert first.probes == 0

    await runtime.aclose()
    assert first.closed == second.closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("writer", ["sections", "patch"])
async def test_native_path_changes_apply_on_the_loop_and_retire_stale_usage_callbacks(
    native_runtime: tuple[Runtime, list[_NativeSessions]],
    monkeypatch: pytest.MonkeyPatch,
    writer: str,
) -> None:
    from core.settings.paths import parse_patch_operations

    runtime, registries = native_runtime
    first = registries[0]
    info = {"status": "allowed", "rate_limit_type": "five_hour", "utilization": 0.25}
    first.bindings["observe_usage"](info)
    owner = runtime._provider_runtime
    assert owner is not None
    assert owner.claude_usage_windows()[0].used_percent == 25
    previous = runtime.storage.load_settings()
    path = "C:/synthetic/new-claude.exe"
    if writer == "patch":
        runtime.storage.patch_settings(
            parse_patch_operations(
                [{"op": "set", "path": "providers.claude_agent.executable_path", "value": path}]
            )
        )
    else:
        runtime.storage.update_settings_sections(
            {"providers": {"claude_agent": {"executable_path": path}}}
        )
    # A Settings response reads readiness from a worker after persisting. It
    # must not cancel Tasks or mutate native process ownership from that thread.
    await asyncio.to_thread(runtime.external_connection_status, PROVIDER, CONNECTION)
    assert first.closed == first.aborted == 0 and len(registries) == 1
    loop = asyncio.get_running_loop()
    closed = first.aclose

    async def close() -> None:
        assert asyncio.get_running_loop() is loop
        await closed()

    monkeypatch.setattr(first, "aclose", close)
    await runtime.apply_settings_change(previous, runtime.storage.load_settings())
    assert first.closed == 1 and len(registries) == 2
    assert owner.claude_usage_windows() == ()
    first.bindings["observe_usage"]({**info, "utilization": 0.9})
    assert owner.claude_usage_windows() == ()
    registries[-1].bindings["observe_usage"]({**info, "utilization": 0.5})
    assert owner.claude_usage_windows()[0].used_percent == 50
    assert registries[-1].bindings["executable_path"]() == path
