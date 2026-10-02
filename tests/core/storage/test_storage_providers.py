"""Provider settings: Connection overrides, OpenRouter routing and Custom Providers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.storage import StorageError, StorageManager

ROUTING = {
    "mode": "allowed",
    "providers": ["anthropic"],
    "blocked": ["deepinfra"],
    "allow_fallbacks": True,
}


def _update_routing(storage: StorageManager, default: dict[str, Any]) -> None:
    storage.update_settings_sections(
        {"providers": {"openrouter": {"routing": {"default": default, "models": {}}}}}
    )


def test_connection_overrides_are_stored_verbatim_and_accumulate(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    assert storage.load_providers_settings() == {"connections": {}}

    # An explicit False is kept even for a Connection that defaults to disabled.
    storage.set_provider_connection_enabled("ollama:local", False)
    assert storage.load_providers_settings() == {"connections": {"ollama:local": False}}
    storage.set_provider_connection_enabled("openai:api-key", True)
    storage.set_provider_connection_enabled("ollama:local", True)

    assert storage.load_providers_settings() == {
        "connections": {"ollama:local": True, "openai:api-key": True}
    }


@pytest.mark.parametrize(("connection_key", "enabled"), [("ollama", True), ("ollama:local", "yes")])
def test_connection_override_rejects_invalid_key_or_value(
    tmp_path: Path, connection_key: str, enabled: Any
) -> None:
    storage = StorageManager(tmp_path)

    with pytest.raises(StorageError):
        storage.set_provider_connection_enabled(connection_key, enabled)

    assert storage.load_providers_settings() == {"connections": {}}


def test_routing_and_connection_writes_preserve_each_other(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.set_provider_connection_enabled("openrouter:api-key", True)

    _update_routing(storage, ROUTING)
    storage.set_provider_connection_enabled("openrouter:api-key", False)

    assert storage.load_providers_settings() == {"connections": {"openrouter:api-key": False}}
    assert storage.load_openrouter_routing_settings()["default"] == ROUTING


def test_custom_provider_crud_preserves_other_provider_settings(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.set_provider_connection_enabled("openrouter:api-key", False)

    saved = storage.save_custom_provider_settings(
        "local-ai",
        {
            "name": "Local AI",
            "adapter": "openai_compatible",
            "base_url": "http://127.0.0.1:8080/v1/",
            "auth": "none",
            "models_endpoint": "/models",
            "models": {"chat-model": {"capabilities": {}}},
        },
    )

    assert saved["base_url"] == "http://127.0.0.1:8080/v1"
    assert storage.load_providers_settings() == {
        "connections": {"local-ai:default": True, "openrouter:api-key": False}
    }
    custom = storage.load_custom_providers_settings()
    assert custom["local-ai"]["models"]["chat-model"]["capabilities"]["tools"] is True

    _update_routing(storage, {**ROUTING, "mode": "automatic", "providers": []})
    assert "local-ai" in storage.load_custom_providers_settings()

    # An update transforms the stored record in one transaction and keeps the rest.
    wire = {"defaults": {"reasoning": {"dialect": "thinking_toggle"}}}
    updated = storage.update_custom_provider_settings(
        "local-ai", lambda record: {**record, "wire": wire}
    )
    assert updated["wire"] == wire
    assert storage.load_custom_providers_settings()["local-ai"] == updated
    assert storage.load_openrouter_routing_settings()["default"]["mode"] == "automatic"
    with pytest.raises(StorageError, match="does not exist"):
        storage.update_custom_provider_settings("other-ai", lambda record: record)

    assert storage.delete_custom_provider_settings("local-ai") is not None
    assert storage.delete_custom_provider_settings("local-ai") is None
    assert storage.load_custom_providers_settings() == {}
    assert storage.load_providers_settings() == {"connections": {"openrouter:api-key": False}}
