"""Tests for debug settings storage normalization and persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.storage import (
    PHASE_TWO_DIRECTORIES,
    StorageError,
    StorageManager,
)


class TestLoadDebugSettings:
    def test_returns_defaults_when_missing(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        result = storage.load_debug_settings()

        assert result == {"enabled": False, "trace_limit": 50}

    def test_reads_and_normalizes_custom_values(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings({"debug": {"enabled": True, "trace_limit": 100}})

        result = storage.load_debug_settings()

        assert result == {"enabled": True, "trace_limit": 100}

    def test_defaults_apply_when_enabled_missing(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings({"debug": {"trace_limit": 25}})

        result = storage.load_debug_settings()

        assert result == {"enabled": False, "trace_limit": 25}

    def test_defaults_apply_when_trace_limit_missing(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings({"debug": {"enabled": True}})

        result = storage.load_debug_settings()

        assert result == {"enabled": True, "trace_limit": 50}

    def test_accepts_trace_limit_1(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings({"debug": {"trace_limit": 1}})

        result = storage.load_debug_settings()

        assert result == {"enabled": False, "trace_limit": 1}

    def test_accepts_trace_limit_500(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings({"debug": {"trace_limit": 500}})

        result = storage.load_debug_settings()

        assert result == {"enabled": False, "trace_limit": 500}

    def test_rejects_non_object_debug_section(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(json.dumps({"debug": []}), encoding="utf-8")

        with pytest.raises(StorageError, match=r"\$\.debug: must be an object"):
            storage.load_debug_settings()

    def test_rejects_non_boolean_enabled(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(
            json.dumps({"debug": {"enabled": "yes"}}), encoding="utf-8"
        )

        with pytest.raises(StorageError, match=r"\$\.debug\.enabled: must be a boolean"):
            storage.load_debug_settings()

    def test_rejects_non_integer_trace_limit(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(
            json.dumps({"debug": {"trace_limit": "fifty"}}), encoding="utf-8"
        )

        with pytest.raises(StorageError, match=r"\$\.debug\.trace_limit"):
            storage.load_debug_settings()

    def test_rejects_zero_trace_limit(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(
            json.dumps({"debug": {"trace_limit": 0}}), encoding="utf-8"
        )

        with pytest.raises(StorageError, match=r"\$\.debug\.trace_limit"):
            storage.load_debug_settings()

    def test_rejects_negative_trace_limit(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(
            json.dumps({"debug": {"trace_limit": -1}}), encoding="utf-8"
        )

        with pytest.raises(StorageError, match=r"\$\.debug\.trace_limit"):
            storage.load_debug_settings()

    def test_rejects_trace_limit_over_500(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(
            json.dumps({"debug": {"trace_limit": 501}}), encoding="utf-8"
        )

        with pytest.raises(StorageError, match=r"\$\.debug\.trace_limit"):
            storage.load_debug_settings()

    def test_rejects_boolean_trace_limit(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(
            json.dumps({"debug": {"trace_limit": True}}), encoding="utf-8"
        )

        with pytest.raises(StorageError, match=r"\$\.debug\.trace_limit"):
            storage.load_debug_settings()


class TestUpdateDebugSettings:
    def test_persists_under_debug_key(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings({"server_port": 8500})

        updated = storage.update_debug_settings({"enabled": True, "trace_limit": 100})

        assert updated == {"enabled": True, "trace_limit": 100}
        assert storage.load_settings() == {
            "debug": {"enabled": True, "trace_limit": 100},
            "server_port": 8500,
        }

    def test_partial_update_preserves_unspecified_fields(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings({"debug": {"enabled": True, "trace_limit": 100}})

        updated = storage.update_debug_settings({"trace_limit": 200})

        assert updated == {"enabled": True, "trace_limit": 200}
        assert storage.load_debug_settings() == {
            "enabled": True,
            "trace_limit": 200,
        }

    def test_rejects_unsupported_fields(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        with pytest.raises(StorageError, match="Unsupported debug settings: unknown"):
            storage.update_debug_settings({"enabled": True, "unknown": "value"})

    @pytest.mark.parametrize(
        ("debug", "message"),
        [
            ([], "Debug settings must be a mapping"),
            ("not a dict", "Debug settings must be a mapping"),
            (
                {"enabled": True, "extra": 1},
                "Unsupported debug settings: extra",
            ),
        ],
    )
    def test_rejects_invalid_payloads(
        self,
        tmp_path: Path,
        debug: Any,
        message: str,
    ) -> None:
        storage = StorageManager(tmp_path)

        with pytest.raises(StorageError, match=message):
            storage.update_debug_settings(debug)

    def test_leaves_file_unchanged_when_rejected(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        original = {"server_port": 8500, "debug": {"enabled": True, "trace_limit": 50}}
        storage.save_settings(original)

        with pytest.raises(StorageError, match="Unsupported debug settings"):
            storage.update_debug_settings({"unknown": 1})

        assert storage.load_settings() == original


class TestDebugDirectory:
    def test_ensure_directories_creates_debug_directory(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        storage.ensure_directories()

        assert "debug" in PHASE_TWO_DIRECTORIES
        assert (tmp_path / "debug").is_dir()

    def test_ensure_directories_creates_all_phase_two_dirs(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        storage.ensure_directories()

        for directory_name in PHASE_TWO_DIRECTORIES:
            assert (tmp_path / directory_name).is_dir(), f"Missing: {directory_name}"


class TestDebugSettingsRoundTrip:
    def test_save_and_load_debug_settings(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings(
            {
                "server_port": 8500,
                "debug": {"enabled": True, "trace_limit": 75},
            }
        )

        loaded = storage.load_debug_settings()

        assert loaded == {"enabled": True, "trace_limit": 75}
        assert storage.load_settings()["server_port"] == 8500

    def test_update_then_reload_preserves_values(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        storage.update_debug_settings({"enabled": True, "trace_limit": 30})
        reloaded = storage.load_debug_settings()

        assert reloaded == {"enabled": True, "trace_limit": 30}

    def test_disable_then_enable_cycle(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        storage.update_debug_settings({"enabled": True, "trace_limit": 100})
        assert storage.load_debug_settings() == {"enabled": True, "trace_limit": 100}

        storage.update_debug_settings({"enabled": False, "trace_limit": 100})
        assert storage.load_debug_settings() == {"enabled": False, "trace_limit": 100}

        storage.update_debug_settings({"enabled": True, "trace_limit": 100})
        assert storage.load_debug_settings() == {"enabled": True, "trace_limit": 100}


class TestUpdateSettingsSectionsWithDebug:
    def test_debug_with_other_sections_in_one_transaction(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings({"server_port": 8500})
        save_count = 0
        original_save_settings = storage.save_settings

        def count_save(settings: dict[str, Any]) -> None:
            nonlocal save_count
            save_count += 1
            original_save_settings(settings)

        monkeypatch.setattr(storage, "save_settings", count_save)

        updated = storage.update_settings_sections(
            {
                "appearance": {"language": "en"},
                "debug": {"enabled": True, "trace_limit": 200},
            }
        )

        assert save_count == 1
        assert updated == {
            "appearance": {"language": "en", "chat_width": "comfortable"},
            "debug": {"enabled": True, "trace_limit": 200},
        }
        assert storage.load_debug_settings() == {"enabled": True, "trace_limit": 200}

    def test_debug_section_leaves_file_unchanged_when_other_section_fails(
        self,
        tmp_path: Path,
    ) -> None:
        storage = StorageManager(tmp_path)
        original = {"server_port": 8500, "debug": {"enabled": False, "trace_limit": 50}}
        storage.save_settings(original)

        with pytest.raises(StorageError, match="Compaction setting threshold"):
            storage.update_settings_sections(
                {
                    "debug": {"enabled": True},
                    "compaction": {"threshold": 2},
                }
            )

        assert storage.load_settings() == original
