"""Tests for rpc models refresh."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from core.models.database import (
    MODEL_DATABASE_SOURCE_RUNTIME,
    MODEL_DATABASE_SOURCE_SYSTEM,
    read_model_database_manifest,
)
from server.rpc import (
    model_methods,
)
from server.rpc.methods import dispatch_rpc
from tests.server.rpc_test_support import (
    FAKE_REFRESH_MODEL_CALLS,
    FAKE_REFRESH_MODEL_KWARGS,
    FAKE_REFRESH_MODEL_PROVIDER_IDS,
    StubAdapter,
    fake_refresh_models,
    make_state,
    openrouter_provider,
)
from tests.server.rpc_test_support import _no_models_dev_fetch as _no_models_dev_fetch


@pytest.mark.asyncio
async def test_model_refresh_db_refreshes_provider_models_and_runtime_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    assert response == {
        "ok": True,
        "result": {
            "provider_id": "openrouter",
            "model_count": 1,
            "fetched_at": "2026-05-08T19:08:00+00:00",
        },
    }
    assert FAKE_REFRESH_MODEL_PROVIDER_IDS == ["openrouter"]
    assert FAKE_REFRESH_MODEL_CALLS == ["openrouter-key"]
    refreshed_model = state.runtime.models.get("openrouter", "fresh-model")
    assert refreshed_model.name == "Fresh Model"


@pytest.mark.asyncio
async def test_normal_model_refresh_copies_complete_system_db_to_runtime_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    system_models_dir = state.runtime.resources_dir / "models"
    system_models_dir.mkdir(parents=True)
    override_text = '{"models": {"fresh-model": {"name": "Manual"}}}\n'
    system_models_dir.joinpath("openrouter.overrides.json").write_text(
        override_text,
        encoding="utf-8",
    )

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    assert response["ok"] is True, response
    runtime_models_dir = state.runtime.storage.layout.models
    assert (
        runtime_models_dir.joinpath("openrouter.overrides.json").read_text(encoding="utf-8")
        == override_text
    )
    assert runtime_models_dir.joinpath("openrouter.json").is_file()
    assert not system_models_dir.joinpath("openrouter.json").exists()
    manifest = read_model_database_manifest(runtime_models_dir)
    assert manifest is not None
    assert manifest.source == MODEL_DATABASE_SOURCE_RUNTIME


@pytest.mark.asyncio
async def test_explicit_system_refresh_writes_only_serving_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {
            "method": "model.refresh_db",
            "params": {
                "provider_id": "openrouter",
                "target": "system",
                "expected_resources_dir": str(state.runtime.resources_dir),
            },
        },
    )

    assert response["ok"] is True
    system_models_dir = state.runtime.resources_dir / "models"
    assert system_models_dir.joinpath("openrouter.json").is_file()
    # ``artifacts/models`` is now created by ``initialize_data_directory``
    # on startup, so it may exist as an empty directory. The refresh must
    # not write the model file there.
    assert not state.runtime.storage.layout.models.joinpath("openrouter.json").exists()
    manifest = read_model_database_manifest(system_models_dir)
    assert manifest is not None
    assert manifest.source == MODEL_DATABASE_SOURCE_SYSTEM


@pytest.mark.asyncio
async def test_system_refresh_rejects_a_different_serving_checkout(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter())

    response = await dispatch_rpc(
        state,
        {
            "method": "model.refresh_db",
            "params": {
                "target": "system",
                "expected_resources_dir": str(tmp_path / "other-resources"),
            },
        },
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_model_refresh_db_provider_publishes_models_resource_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    # The per-provider path returns early but must still tell open windows to
    # reload their model lists.
    assert response["ok"] is True
    assert [
        event["payload"] for event in state.event_bus.events if event["type"] == "resource_changed"
    ] == [{"kind": "models"}]


@pytest.mark.asyncio
async def test_model_refresh_db_global_publishes_models_resource_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    response = await dispatch_rpc(state, {"method": "model.refresh_db"})

    assert response["ok"] is True
    assert [
        event["payload"] for event in state.event_bus.events if event["type"] == "resource_changed"
    ] == [{"kind": "models"}]


@pytest.mark.asyncio
async def test_model_refresh_db_updates_registry_in_place_for_captured_holders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh reloads the registry in place rather than rebinding it.

    Services that captured the registry at construction (task-model targets for
    speech/image/embeddings, the status display, the recall backend) hold the
    same instance, so the refreshed catalog must reach them without a restart.
    """

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())

    # The instance every holder captured at construction time.
    registry_before = state.runtime.models

    response = await dispatch_rpc(
        state,
        {"method": "model.refresh_db", "params": {"provider_id": "openrouter"}},
    )

    assert response["ok"] is True
    # Not rebound: holders still share this same instance...
    assert state.runtime.models is registry_before
    # ...and it now carries the refreshed catalog.
    assert registry_before.get("openrouter", "fresh-model").name == "Fresh Model"


@pytest.mark.asyncio
async def test_model_refresh_db_without_params_refreshes_only_eligible_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("OPENROUTER_SECONDARY_API_KEY", "secondary-key")
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    state.runtime.providers.add(
        SimpleNamespace(
            id="refreshable-missing-credentials",
            name="Refreshable Missing Credentials",
            adapter="openai_compatible",
            base_url="https://missing.example/v1",
            defaults={},
            extra_headers={},
            models_endpoint="/models",
            connections=[
                SimpleNamespace(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=SimpleNamespace(credential_key="MISSING_REFRESH_API_KEY"),
                )
            ],
        )
    )
    state.runtime.providers.add(
        SimpleNamespace(
            id="refreshable-secondary",
            name="Refreshable Secondary",
            adapter="openai_compatible",
            base_url="https://secondary.example/v1",
            defaults={},
            extra_headers={},
            models_endpoint="/models",
            connections=[
                SimpleNamespace(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=SimpleNamespace(credential_key="OPENROUTER_SECONDARY_API_KEY"),
                )
            ],
        )
    )

    response = await dispatch_rpc(state, {"method": "model.refresh_db"})

    assert response == {
        "ok": True,
        "result": {
            "providers": [
                {
                    "provider_id": "openrouter",
                    "model_count": 1,
                    "fetched_at": "2026-05-08T19:08:00+00:00",
                },
                {
                    "provider_id": "refreshable-secondary",
                    "model_count": 1,
                    "fetched_at": "2026-05-08T19:08:00+00:00",
                },
            ],
            "refreshed_count": 2,
            "model_count": 2,
            "canonical": None,
        },
    }
    assert FAKE_REFRESH_MODEL_PROVIDER_IDS == ["openrouter", "refreshable-secondary"]
    assert FAKE_REFRESH_MODEL_CALLS == ["openrouter-key", "secondary-key"]
    assert state.runtime.models.get("openrouter", "fresh-model").name == "Fresh Model"
    assert state.runtime.models.get("refreshable-secondary", "fresh-model").name == "Fresh Model"


@pytest.mark.asyncio
async def test_model_refresh_db_empty_params_reloads_runtime_registry_after_global_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr(model_methods, "refresh_models", fake_refresh_models)
    FAKE_REFRESH_MODEL_PROVIDER_IDS.clear()
    FAKE_REFRESH_MODEL_CALLS.clear()
    FAKE_REFRESH_MODEL_KWARGS.clear()
    state = make_state(tmp_path, StubAdapter())
    state.runtime.providers.add(openrouter_provider())
    previous_models = state.runtime.models

    response = await dispatch_rpc(state, {"method": "model.refresh_db", "params": {}})

    assert response["ok"] is True
    # The global refresh reloads the registry in place: the same instance every
    # holder captured stays, now carrying the refreshed catalog.
    assert state.runtime.models is previous_models
    assert state.runtime.models.get("openrouter", "fresh-model").name == "Fresh Model"
