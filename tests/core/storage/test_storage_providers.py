"""Tests for providers settings storage normalization and persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.storage import StorageError, StorageManager


class TestLoadProvidersSettings:
    def test_returns_empty_map_when_missing(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        result = storage.load_providers_settings()

        assert result == {"connections": {}}

    def test_reads_persisted_overrides(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings(
            {"providers": {"connections": {"ollama:local": True, "openai:api-key": False}}}
        )

        result = storage.load_providers_settings()

        assert result == {"connections": {"ollama:local": True, "openai:api-key": False}}

    def test_defaults_non_object_section(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(json.dumps({"providers": []}), encoding="utf-8")

        assert storage.load_providers_settings() == {"connections": {}}

    def test_defaults_non_boolean_value(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(
            json.dumps({"providers": {"connections": {"ollama:local": "yes"}}}),
            encoding="utf-8",
        )

        assert storage.load_providers_settings() == {"connections": {}}

    def test_defaults_key_without_connection_part(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(
            json.dumps({"providers": {"connections": {"ollama": True}}}),
            encoding="utf-8",
        )

        assert storage.load_providers_settings() == {"connections": {}}


class TestSetProviderConnectionEnabled:
    def test_persists_explicit_override(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        storage.set_provider_connection_enabled("ollama:local", True)

        assert storage.load_providers_settings() == {"connections": {"ollama:local": True}}

    def test_stores_explicit_false_even_for_default_disabled(self, tmp_path: Path) -> None:
        """The value is stored verbatim, never collapsed against the type default."""
        storage = StorageManager(tmp_path)

        storage.set_provider_connection_enabled("ollama:local", False)

        assert storage.load_providers_settings() == {"connections": {"ollama:local": False}}

    def test_preserves_other_overrides(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.set_provider_connection_enabled("ollama:local", True)

        storage.set_provider_connection_enabled("openai:api-key", False)

        assert storage.load_providers_settings() == {
            "connections": {"ollama:local": True, "openai:api-key": False}
        }

    def test_rejects_key_without_connection_part(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        with pytest.raises(StorageError):
            storage.set_provider_connection_enabled("ollama", True)

    def test_rejects_non_boolean_value(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        with pytest.raises(StorageError):
            storage.set_provider_connection_enabled("ollama:local", "yes")  # type: ignore[arg-type]

    def test_preserves_openrouter_routing(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.update_settings_sections(
            {
                "providers": {
                    "openrouter": {
                        "routing": {
                            "default": {
                                "mode": "allowed",
                                "providers": ["anthropic"],
                                "blocked": ["deepinfra"],
                                "allow_fallbacks": True,
                            },
                            "models": {},
                        }
                    }
                }
            }
        )

        storage.set_provider_connection_enabled("openrouter:api-key", False)

        assert storage.load_providers_settings() == {"connections": {"openrouter:api-key": False}}
        assert storage.load_openrouter_routing_settings()["default"] == {
            "mode": "allowed",
            "providers": ["anthropic"],
            "blocked": ["deepinfra"],
            "allow_fallbacks": True,
        }


def test_openrouter_routing_update_preserves_connection_overrides(tmp_path: Path) -> None:
    storage = StorageManager(tmp_path)
    storage.set_provider_connection_enabled("openrouter:api-key", True)

    storage.update_settings_sections(
        {
            "providers": {
                "openrouter": {
                    "routing": {
                        "default": {
                            "mode": "automatic",
                            "providers": [],
                            "blocked": ["deepinfra"],
                            "allow_fallbacks": False,
                        },
                        "models": {},
                    }
                }
            }
        }
    )

    assert storage.load_providers_settings() == {"connections": {"openrouter:api-key": True}}
    assert storage.load_openrouter_routing_settings()["default"]["blocked"] == ["deepinfra"]


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
        "connections": {
            "local-ai:default": True,
            "openrouter:api-key": False,
        }
    }
    assert (
        storage.load_custom_providers_settings()["local-ai"]["models"]["chat-model"][
            "capabilities"
        ]["tools"]
        is True
    )

    storage.update_settings_sections(
        {
            "providers": {
                "openrouter": {
                    "routing": {
                        "default": {
                            "mode": "automatic",
                            "providers": [],
                            "blocked": [],
                            "allow_fallbacks": True,
                        },
                        "models": {},
                    }
                }
            }
        }
    )
    assert "local-ai" in storage.load_custom_providers_settings()

    removed = storage.delete_custom_provider_settings("local-ai")

    assert removed is not None
    assert storage.load_custom_providers_settings() == {}
    assert storage.load_providers_settings() == {"connections": {"openrouter:api-key": False}}
