"""Runtime wiring of local Provider Connections: opt-in, context windows, catalog refresh."""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

import core.models.discovery as discovery_module
import core.providers.runtime as provider_runtime_module
from core.models.database import read_model_database_manifest
from core.models.discovery import ModelDiscoveryError
from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.providers.accounts import ConnectionRef
from core.providers.errors import NetworkError
from core.providers.ollama import OllamaAdapter
from core.providers.token_getter import StaticTokenGetter
from core.runtime.runtime import Runtime
from core.storage.layout import DataDirectoryLayout
from core.utils.errors import StorageError
from core.utils.retry import retry_async

LOCAL = ConnectionRef("ollama", "ollama:local")


def _record_reloads(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Replace the live registry swap; return the list of recorded reloads."""
    reloads: list[object] = []

    async def reload_async(resources_dir: Any, **kwargs: Any) -> None:
        reloads.append(1)

    monkeypatch.setattr(runtime.models, "reload_async", reload_async)
    return reloads


def _local_model(model_id: str, metadata: dict[str, Any]) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=262144,
        max_output_tokens=None,
        metadata=metadata,
    )


@pytest.mark.asyncio
async def test_keyless_local_connection_is_usable_only_after_opt_in(runtime: Runtime) -> None:
    # The keyless Connection passes the credential gate without any environment.
    assert runtime.provider_credentials.has_credentials("ollama", "ollama:local") is True
    assert runtime.provider_credentials.get_credentials("ollama", "ollama:local") == ""
    getter = runtime.get_connection_token_getter(LOCAL)
    assert isinstance(getter, StaticTokenGetter)
    assert await getter() == ""

    runtime.storage.set_provider_connection_enabled("ollama:local", True)

    assert isinstance(runtime.get_adapter(LOCAL), OllamaAdapter)


@pytest.mark.parametrize(
    "settings_read_fails", [False, True], ids=["user-override", "settings-read-failure"]
)
def test_local_context_resolver_enforces_the_effective_window(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch, settings_read_fails: bool
) -> None:
    entries = {
        "ministral-3:8b": _local_model("ministral-3:8b", {"ollama": {"local": True}}),
        "big-local": _local_model("big-local", {"ollama": {"local": True}}),
        "kimi-k2.6:cloud": _local_model("kimi-k2.6:cloud", {"ollama": {"remote": True}}),
    }
    monkeypatch.setattr(runtime.models, "get", lambda provider_id, model_id: entries[model_id])
    runtime.storage.update_settings_sections(
        {"local_models": {"context_windows": {"ollama/ministral-3:8b": 16384}}}
    )
    if settings_read_fails:
        monkeypatch.setattr(
            runtime.storage,
            "load_local_models_settings",
            Mock(side_effect=StorageError("Settings could not be read")),
        )

    resolver = runtime._provider_operations()._local_context_resolver("ollama")  # noqa: SLF001

    # An unreadable override uses the same local cap as an absent one. Remote
    # and unknown Models never receive a local limit.
    assert resolver("ministral-3:8b") == (32768 if settings_read_fails else 16384)
    assert resolver("big-local") == 32768
    assert resolver("kimi-k2.6:cloud") is None
    assert resolver("missing") is None


@pytest.mark.asyncio
async def test_local_catalog_refresh_throttles_probes_and_logs_reachability_transitions(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(
        provider_runtime_module, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    ttl = provider_runtime_module.LOCAL_CATALOG_REFRESH_TTL_SECONDS
    sweep_seconds: list[float] = []
    outcomes: list[Exception | None] = []
    calls: list[str] = []
    probe_attempts: list[str] = []

    async def fake_refresh(provider: Any, credential: str, resources_dir: Any, **kwargs: Any):
        calls.append(provider.id)
        clock[0] += sweep_seconds.pop(0) if sweep_seconds else 0.0
        outcome = outcomes.pop(0) if outcomes else None
        if outcome is not None:
            # Like discovery, probe through the shared transient-failure retry.
            async def probe() -> None:
                probe_attempts.append(provider.id)
                raise outcome

            try:
                await retry_async(probe)
            except NetworkError as error:
                raise ModelDiscoveryError(str(error)) from error
        return {"provider_id": provider.id, "model_count": 1}

    monkeypatch.setattr(discovery_module, "refresh_models", fake_refresh)
    reloads = _record_reloads(runtime, monkeypatch)
    logger = Mock()
    runtime.logger = logger

    # A disabled local Connection is completely passive: never probed.
    await runtime.maybe_refresh_local_catalogs()
    assert calls == []
    assert runtime.connection_reachability("ollama:local") is None

    runtime.storage.set_provider_connection_enabled("ollama:local", True)
    # The TTL runs from the end of a sweep, so one slower than the TTL does not
    # let the next caller start another sweep at once.
    sweep_seconds.append(ttl + 5)
    await runtime.maybe_refresh_local_catalogs()
    await runtime.maybe_refresh_local_catalogs()
    # One sweep inside the TTL, one in-place registry reload, a reachable server.
    assert calls == ["ollama"]
    assert len(reloads) == 1
    assert runtime.connection_reachability("ollama:local") is True

    # ``force`` re-probes inside the TTL (used right after an enable).
    outcomes.append(NetworkError("connection refused"))
    await runtime.maybe_refresh_local_catalogs(force=True)
    assert calls == ["ollama", "ollama"]
    assert runtime.connection_reachability("ollama:local") is False
    # A failing server keeps the stale catalog.
    assert len(reloads) == 1

    clock[0] += ttl + 1
    outcomes.append(NetworkError("connection refused"))
    await runtime.maybe_refresh_local_catalogs()
    await runtime.maybe_refresh_local_catalogs(force=True)
    assert calls == ["ollama"] * 4
    assert len(reloads) == 2
    # Each failed sweep probed once: the TTL, not a backoff series, is its retry.
    assert probe_attempts == ["ollama", "ollama"]

    # Only the transitions are logged: one unreachable warning, one recovery.
    assert logger.warning.call_count == 1
    assert "became unreachable" in logger.warning.call_args.args[0]
    assert logger.info.call_count == 1
    assert "recovered" in logger.info.call_args.args[0]


@pytest.mark.asyncio
async def test_local_catalog_refresh_keeps_the_published_database_when_staging_is_invalid(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.storage.set_provider_connection_enabled("ollama:local", True)

    async def fake_refresh(provider: Any, credential: str, resources_dir: Any, **kwargs: Any):
        return {"provider_id": provider.id, "model_count": 1}

    def fail_validation(cls: type, resources_dir: Any) -> list[str]:
        raise ValueError("invalid staged Model DB")

    monkeypatch.setattr(discovery_module, "refresh_models", fake_refresh)
    monkeypatch.setattr(ModelRegistry, "validate", classmethod(fail_validation))
    reloads = _record_reloads(runtime, monkeypatch)

    await runtime.maybe_refresh_local_catalogs()

    assert reloads == []
    assert list(DataDirectoryLayout(runtime.storage.data_dir).models.iterdir()) == []
    assert not (runtime.storage.data_dir / "models").exists()
    assert runtime.connection_reachability("ollama:local") is True


@pytest.mark.asyncio
async def test_local_catalog_refresh_stages_the_model_db_off_the_event_loop(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.storage.set_provider_connection_enabled("ollama:local", True)

    async def fake_refresh(provider: Any, credential: str, resources_dir: Any, **kwargs: Any):
        return {"provider_id": provider.id, "model_count": 1}

    monkeypatch.setattr(discovery_module, "refresh_models", fake_refresh)
    reloads = _record_reloads(runtime, monkeypatch)
    begin = provider_runtime_module.begin_runtime_model_database_refresh
    entered = threading.Event()
    release = threading.Event()
    threads: list[int] = []
    staging_dirs: list[Any] = []

    def blocked_begin(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        entered.set()
        release.wait(timeout=5)
        refresh = begin(*args, **kwargs)
        staging_dirs.append(refresh.resources_dir)
        return refresh

    monkeypatch.setattr(
        provider_runtime_module, "begin_runtime_model_database_refresh", blocked_begin
    )
    refreshing = asyncio.create_task(runtime.maybe_refresh_local_catalogs())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        # The Event Loop keeps serving while the staging copy is blocked.
        for _ in range(3):
            await asyncio.sleep(0)
        assert not refreshing.done()
    finally:
        release.set()
    await asyncio.wait_for(refreshing, timeout=10)

    assert threads and threading.get_ident() not in threads
    assert reloads == [1]
    assert staging_dirs and not staging_dirs[0].exists()


@pytest.mark.asyncio
async def test_local_catalog_refresh_signals_only_a_changed_model_catalog(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime.storage.set_provider_connection_enabled("ollama:local", True)
    served = ["signal-a:latest"]
    failures: list[Exception] = []

    async def fake_refresh(provider: Any, credential: str, resources_dir: Any, **kwargs: Any):
        if failures:
            raise failures.pop()
        record = {
            "name": "Local",
            "capabilities": {
                "vision": False,
                "tools": True,
                "json_mode": False,
                "reasoning": {"supported": False},
            },
            "context_window": 32768,
        }
        catalog = {"provider_id": provider.id, "models": dict.fromkeys(served, record)}
        resources_dir.joinpath("models", f"{provider.id}.json").write_text(
            json.dumps(catalog), encoding="utf-8"
        )
        return {"provider_id": provider.id, "model_count": len(served)}

    monkeypatch.setattr(discovery_module, "refresh_models", fake_refresh)
    logger = Mock()
    runtime.logger = logger
    signals: list[str] = []

    def failing_subscriber() -> None:
        raise RuntimeError("subscriber failed")

    runtime.add_model_catalog_changed_callback(failing_subscriber)
    unsubscribe = runtime.add_model_catalog_changed_callback(lambda: signals.append("models"))

    # A newly served Model changes the catalog; a failing subscriber is only logged.
    await runtime.maybe_refresh_local_catalogs(force=True)
    assert signals == ["models"]
    assert runtime.models.get("ollama", "signal-a:latest").name == "Local"
    assert logger.error.call_count == 1
    # The sweep publishes and records only the local catalog it fetched, so it
    # never makes a bundled catalog look newer than a later vBot update's.
    runtime_models_dir = runtime.storage.layout.models
    assert sorted(path.name for path in runtime_models_dir.iterdir()) == [
        "manifest.json",
        "ollama.json",
    ]
    manifest = read_model_database_manifest(runtime_models_dir)
    assert manifest is not None
    assert set(manifest.catalogs) == {"ollama.json"}

    # Republishing the same catalog and a failed sweep leave it unchanged.
    await runtime.maybe_refresh_local_catalogs(force=True)
    failures.append(ModelDiscoveryError("connection refused"))
    await runtime.maybe_refresh_local_catalogs(force=True)
    assert signals == ["models"]

    unsubscribe()
    served.append("signal-b:latest")
    await runtime.maybe_refresh_local_catalogs(force=True)
    assert runtime.models.get("ollama", "signal-b:latest").name == "Local"
    assert signals == ["models"]
