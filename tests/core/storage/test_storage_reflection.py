"""Tests for reflection settings storage normalization and persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.storage import StorageError, StorageManager

DEFAULTS = {"enabled": False, "memory_turn_interval": 10, "skill_tool_call_interval": 25}


class TestLoadReflectionSettings:
    def test_returns_defaults_when_missing(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        result = storage.load_reflection_settings()

        assert result == DEFAULTS

    def test_reads_and_normalizes_custom_values(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings(
            {
                "reflection": {
                    "enabled": True,
                    "memory_turn_interval": 5,
                    "skill_tool_call_interval": 40,
                }
            }
        )

        result = storage.load_reflection_settings()

        assert result == {
            "enabled": True,
            "memory_turn_interval": 5,
            "skill_tool_call_interval": 40,
        }

    def test_defaults_fill_missing_fields(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings({"reflection": {"enabled": True}})

        result = storage.load_reflection_settings()

        assert result == {**DEFAULTS, "enabled": True}

    def test_rejects_non_object_reflection_section(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(json.dumps({"reflection": []}), encoding="utf-8")

        with pytest.raises(StorageError, match=r"\$\.reflection: must be an object"):
            storage.load_reflection_settings()

    def test_rejects_non_boolean_enabled(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(
            json.dumps({"reflection": {"enabled": "yes"}}), encoding="utf-8"
        )

        with pytest.raises(StorageError, match=r"\$\.reflection\.enabled"):
            storage.load_reflection_settings()

    @pytest.mark.parametrize("field", ["memory_turn_interval", "skill_tool_call_interval"])
    @pytest.mark.parametrize("value", ["five", 0, -1, True])
    def test_rejects_invalid_intervals(self, tmp_path: Path, field: str, value: object) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(
            json.dumps({"reflection": {field: value}}), encoding="utf-8"
        )

        with pytest.raises(StorageError, match=rf"\$\.reflection\.{field}"):
            storage.load_reflection_settings()


class TestUpdateReflectionSettings:
    def test_persists_under_reflection_key(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings({"server_port": 8500})

        updated = storage.update_settings_sections(
            {
                "reflection": {
                    "enabled": True,
                    "memory_turn_interval": 5,
                    "skill_tool_call_interval": 40,
                }
            }
        )

        assert updated["reflection"] == {
            "enabled": True,
            "memory_turn_interval": 5,
            "skill_tool_call_interval": 40,
        }
        assert storage.load_settings() == {
            "reflection": {
                "enabled": True,
                "memory_turn_interval": 5,
                "skill_tool_call_interval": 40,
            },
            "server_port": 8500,
        }

    def test_partial_update_preserves_unspecified_fields(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings(
            {
                "reflection": {
                    "enabled": True,
                    "memory_turn_interval": 7,
                    "skill_tool_call_interval": 33,
                }
            }
        )

        updated = storage.update_settings_sections({"reflection": {"memory_turn_interval": 12}})

        assert updated["reflection"] == {
            "enabled": True,
            "memory_turn_interval": 12,
            "skill_tool_call_interval": 33,
        }
        assert storage.load_reflection_settings() == {
            "enabled": True,
            "memory_turn_interval": 12,
            "skill_tool_call_interval": 33,
        }

    def test_rejects_unsupported_fields(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        with pytest.raises(StorageError, match="Unsupported reflection settings: unknown"):
            storage.update_settings_sections({"reflection": {"enabled": True, "unknown": 1}})

    def test_leaves_file_unchanged_when_rejected(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        original = {"server_port": 8500, "reflection": {"enabled": True}}
        storage.save_settings(original)

        with pytest.raises(StorageError, match="Unsupported reflection settings"):
            storage.update_settings_sections({"reflection": {"unknown": 1}})

        assert storage.load_settings() == original

    def test_enable_disable_cycle(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        storage.update_settings_sections({"reflection": {"enabled": True}})
        assert storage.load_reflection_settings()["enabled"] is True

        storage.update_settings_sections({"reflection": {"enabled": False}})
        assert storage.load_reflection_settings() == DEFAULTS
