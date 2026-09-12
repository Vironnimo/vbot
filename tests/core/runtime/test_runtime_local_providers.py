"""Tests for runtime local providers."""

from unittest.mock import Mock

import pytest

from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.runtime.runtime import Runtime
from core.storage.layout import DataDirectoryLayout
from tests.core.runtime.runtime_providers_test_support import (
    runtime as runtime,
)


def test_local_context_resolver_enforces_effective_window(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The injected resolver returns the effective window only for flagged-local models."""
    # Arrange
    local_model = Model(
        model_id="ministral-3:8b",
        name="ministral-3:8b",
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=262144,
        max_output_tokens=None,
        metadata={"ollama": {"local": True}},
    )
    cloud_model = Model(
        model_id="kimi-k2.6:cloud",
        name="kimi-k2.6:cloud",
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=True),
        ),
        context_window=262144,
        max_output_tokens=None,
        metadata={"ollama": {"remote": True}},
    )
    entries = {"ministral-3:8b": local_model, "kimi-k2.6:cloud": cloud_model}
    monkeypatch.setattr(runtime.models, "get", lambda provider_id, model_id: entries[model_id])
    runtime.storage.update_settings_sections(
        {"local_models": {"context_windows": {"ollama/ministral-3:8b": 16384}}}
    )

    # Act
    resolver = runtime._provider_operations()._local_context_resolver("ollama")

    # Assert — user-set window for the local model, None for the proxied cloud one.
    assert resolver("ministral-3:8b") == 16384
    assert resolver("kimi-k2.6:cloud") is None


def test_local_context_resolver_defaults_to_cap_without_setting(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    local_model = Model(
        model_id="big-local",
        name="big-local",
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=262144,
        max_output_tokens=None,
        metadata={"ollama": {"local": True}},
    )
    monkeypatch.setattr(runtime.models, "get", lambda provider_id, model_id: local_model)

    # Act / Assert
    assert runtime._provider_operations()._local_context_resolver("ollama")("big-local") == 32768


def test_ollama_local_connection_reports_credentials_configured(runtime: Runtime) -> None:
    """The keyless local connection passes the credential gate with no env at all."""
    # Assert
    assert runtime.provider_credentials.has_credentials("ollama", "ollama:local") is True
    assert runtime.provider_credentials.get_credentials("ollama", "ollama:local") == ""


# ------------------------------------------------------------------
# Local catalog auto-refresh
# ------------------------------------------------------------------
def test_auto_refresh_targets_include_enabled_ollama_local(runtime: Runtime) -> None:
    """The shipped Ollama local connection is an auto-refresh target once enabled."""
    # Arrange
    runtime.storage.set_provider_connection_enabled("ollama:local", True)

    # Act
    targets = runtime._provider_operations()._auto_refresh_targets()

    # Assert
    assert [(provider_id, connection.id) for provider_id, _, connection in targets] == [
        ("ollama", "local")
    ]


def test_auto_refresh_targets_exclude_disabled_ollama_local(runtime: Runtime) -> None:
    """A disabled local connection is completely passive — never probed."""
    # Act — no enable: the keyless default is disabled.
    targets = runtime._provider_operations()._auto_refresh_targets()

    # Assert
    assert targets == []


@pytest.mark.asyncio
async def test_maybe_refresh_local_catalogs_throttles_within_ttl(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two calls inside the TTL run exactly one refresh sweep."""
    # Arrange
    import core.models.discovery as discovery_module

    runtime.storage.set_provider_connection_enabled("ollama:local", True)
    calls: list[str] = []

    async def _fake_refresh(provider, credential, resources_dir, **kwargs):
        calls.append(provider.id)
        return {"provider_id": provider.id, "model_count": 0}

    monkeypatch.setattr(discovery_module, "refresh_models", _fake_refresh)
    reloads: list[object] = []
    monkeypatch.setattr(runtime.models, "reload", lambda resources_dir, **kwargs: reloads.append(1))

    # Act
    await runtime.maybe_refresh_local_catalogs()
    await runtime.maybe_refresh_local_catalogs()

    # Assert — one sweep, one in-place registry reload.
    assert calls == ["ollama"]
    assert len(reloads) == 1


@pytest.mark.asyncio
async def test_maybe_refresh_local_catalogs_refreshes_again_after_ttl(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    import core.models.discovery as discovery_module

    runtime.storage.set_provider_connection_enabled("ollama:local", True)
    calls: list[str] = []

    async def _fake_refresh(provider, credential, resources_dir, **kwargs):
        calls.append(provider.id)
        return {"provider_id": provider.id, "model_count": 0}

    monkeypatch.setattr(discovery_module, "refresh_models", _fake_refresh)
    monkeypatch.setattr(runtime.models, "reload", lambda resources_dir, **kwargs: None)

    # Act — expire the throttle between the calls.
    await runtime.maybe_refresh_local_catalogs()
    assert runtime._provider_runtime is not None
    assert runtime._provider_runtime.refresh_at is not None
    runtime._provider_runtime.refresh_at -= 31.0
    await runtime.maybe_refresh_local_catalogs()

    # Assert
    assert calls == ["ollama", "ollama"]


@pytest.mark.asyncio
async def test_maybe_refresh_local_catalogs_degrades_when_server_down(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing local server logs, throttles, and keeps the stale catalog."""
    # Arrange
    import core.models.discovery as discovery_module
    from core.models.discovery import ModelDiscoveryError

    runtime.storage.set_provider_connection_enabled("ollama:local", True)

    async def _fake_refresh(provider, credential, resources_dir, **kwargs):
        raise ModelDiscoveryError("connection refused")

    monkeypatch.setattr(discovery_module, "refresh_models", _fake_refresh)
    reloads: list[object] = []
    monkeypatch.setattr(runtime.models, "reload", lambda resources_dir, **kwargs: reloads.append(1))

    # Act — must not raise.
    await runtime.maybe_refresh_local_catalogs()

    # Assert — no reload of an unchanged catalog; failure stamped the throttle
    # and the probe outcome is recorded as unreachable.
    assert reloads == []
    assert runtime._provider_runtime is not None
    assert runtime._provider_runtime.refresh_at is not None
    assert runtime.connection_reachability("ollama:local") is False


@pytest.mark.asyncio
async def test_maybe_refresh_local_catalogs_degrades_when_staged_db_is_invalid(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validation failure leaves the published runtime database untouched."""
    import core.models.discovery as discovery_module

    runtime.storage.set_provider_connection_enabled("ollama:local", True)

    async def _fake_refresh(provider, credential, resources_dir, **kwargs):
        return {"provider_id": provider.id, "model_count": 1}

    def _fail_validation(cls, resources_dir, **kwargs):
        raise ValueError("invalid staged Model DB")

    monkeypatch.setattr(discovery_module, "refresh_models", _fake_refresh)
    monkeypatch.setattr(ModelRegistry, "load", classmethod(_fail_validation))
    reloads: list[object] = []
    monkeypatch.setattr(runtime.models, "reload", lambda resources_dir, **kwargs: reloads.append(1))

    await runtime.maybe_refresh_local_catalogs()

    assert reloads == []
    assert list(DataDirectoryLayout(runtime.storage.data_dir).models.iterdir()) == []
    assert not (runtime.storage.data_dir / "models").exists()
    assert runtime.connection_reachability("ollama:local") is True


@pytest.mark.asyncio
async def test_maybe_refresh_records_reachability_on_success(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful probe records the connection as reachable."""
    # Arrange
    import core.models.discovery as discovery_module

    runtime.storage.set_provider_connection_enabled("ollama:local", True)

    async def _fake_refresh(provider, credential, resources_dir, **kwargs):
        return {"provider_id": provider.id, "model_count": 1}

    monkeypatch.setattr(discovery_module, "refresh_models", _fake_refresh)
    monkeypatch.setattr(runtime.models, "reload", lambda resources_dir, **kwargs: None)

    # Act
    await runtime.maybe_refresh_local_catalogs()

    # Assert
    assert runtime.connection_reachability("ollama:local") is True


@pytest.mark.asyncio
async def test_local_provider_health_logs_only_transitions(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.models.discovery as discovery_module
    from core.models.discovery import ModelDiscoveryError

    runtime.storage.set_provider_connection_enabled("ollama:local", True)
    outcomes: list[Exception | None] = [
        None,
        ModelDiscoveryError("connection refused"),
        ModelDiscoveryError("connection refused"),
        None,
    ]

    async def _fake_refresh(provider, credential, resources_dir, **kwargs):
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return {"provider_id": provider.id, "model_count": 1}

    monkeypatch.setattr(discovery_module, "refresh_models", _fake_refresh)
    monkeypatch.setattr(runtime.models, "reload", lambda resources_dir, **kwargs: None)
    logger = Mock()
    runtime.logger = logger

    for _ in range(4):
        await runtime.maybe_refresh_local_catalogs(force=True)

    assert logger.warning.call_count == 1
    assert logger.info.call_count == 1
    assert "became unreachable" in logger.warning.call_args.args[0]
    assert "recovered" in logger.info.call_args.args[0]


@pytest.mark.asyncio
async def test_maybe_refresh_force_bypasses_throttle(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``force=True`` re-probes inside the TTL (used right after an enable)."""
    # Arrange
    import core.models.discovery as discovery_module

    runtime.storage.set_provider_connection_enabled("ollama:local", True)
    calls: list[str] = []

    async def _fake_refresh(provider, credential, resources_dir, **kwargs):
        calls.append(provider.id)
        return {"provider_id": provider.id, "model_count": 0}

    monkeypatch.setattr(discovery_module, "refresh_models", _fake_refresh)
    monkeypatch.setattr(runtime.models, "reload", lambda resources_dir, **kwargs: None)

    # Act
    await runtime.maybe_refresh_local_catalogs()
    await runtime.maybe_refresh_local_catalogs(force=True)

    # Assert
    assert calls == ["ollama", "ollama"]
