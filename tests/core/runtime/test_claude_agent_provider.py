"""Native Claude catalog enrollment, publication and Runtime transport lifetime."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

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

    def sessions() -> _NativeSessions:
        registry = _NativeSessions()
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
    await runtime.maybe_refresh_local_catalogs(force=True)
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
async def test_a_failed_native_login_check_keeps_the_last_published_catalog(
    native_runtime: tuple[Runtime, list[_NativeSessions]],
) -> None:
    runtime, registries = native_runtime
    runtime.storage.set_provider_connection_enabled(CONNECTION, True)
    await runtime.refresh_external_connection(PROVIDER, CONNECTION)
    catalog_path = runtime.storage.data_dir / "artifacts" / "models" / f"{PROVIDER}.json"
    before = catalog_path.read_bytes()
    previous = runtime.models.get(PROVIDER, "opus")

    registries[0].result = {
        "connected": False,
        "status": "logged_out",
        "message": "Synthetic external login ended",
    }
    status = await runtime.refresh_external_connection(PROVIDER, CONNECTION)

    assert status["connected"] is False
    assert status["status"] == "logged_out"
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
    await runtime.maybe_refresh_local_catalogs(force=True)
    status = runtime.external_connection_status(PROVIDER, CONNECTION)
    assert status["status"] == "busy"
    assert status["connected"] is True
    assert status["subscription"] is True
    assert runtime.connection_reachability(CONNECTION) is True
    assert catalog_path.read_bytes() == before


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
