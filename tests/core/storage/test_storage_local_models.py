"""Tests for local-models settings storage normalization and persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.storage import StorageError, StorageManager


class TestLoadLocalModelsSettings:
    def test_returns_empty_map_when_missing(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        result = storage.load_local_models_settings()

        assert result == {"context_windows": {}}

    def test_reads_persisted_windows(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings(
            {"local_models": {"context_windows": {"ollama/ministral-3:8b": 16384}}}
        )

        result = storage.load_local_models_settings()

        assert result == {"context_windows": {"ollama/ministral-3:8b": 16384}}

    def test_rejects_non_object_section(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(json.dumps({"local_models": []}), encoding="utf-8")

        with pytest.raises(StorageError, match=r"\$\.local_models: must be an object"):
            storage.load_local_models_settings()

    def test_rejects_invalid_window_value(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.ensure_directories()
        storage.settings_path.write_text(
            json.dumps({"local_models": {"context_windows": {"ollama/m": 0}}}),
            encoding="utf-8",
        )

        with pytest.raises(StorageError, match="must be a positive integer"):
            storage.load_local_models_settings()


class TestUpdateLocalModelsSettings:
    def test_sets_new_window(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        updated = storage.update_settings_sections(
            {"local_models": {"context_windows": {"ollama/ministral-3:8b": 16384}}}
        )

        assert updated["local_models"] == {
            "context_windows": {"ollama/ministral-3:8b": 16384}
        }
        assert storage.load_local_models_settings() == {
            "context_windows": {"ollama/ministral-3:8b": 16384}
        }

    def test_merge_preserves_unmentioned_keys(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.update_settings_sections(
            {"local_models": {"context_windows": {"ollama/a": 8192, "ollama/b": 16384}}}
        )

        storage.update_settings_sections(
            {"local_models": {"context_windows": {"ollama/a": 4096}}}
        )

        assert storage.load_local_models_settings() == {
            "context_windows": {"ollama/a": 4096, "ollama/b": 16384}
        }

    def test_null_removes_key(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.update_settings_sections(
            {"local_models": {"context_windows": {"ollama/a": 8192, "ollama/b": 16384}}}
        )

        storage.update_settings_sections(
            {"local_models": {"context_windows": {"ollama/a": None}}}
        )

        assert storage.load_local_models_settings() == {
            "context_windows": {"ollama/b": 16384}
        }

    def test_removing_unknown_key_is_a_no_op(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        storage.update_settings_sections(
            {"local_models": {"context_windows": {"ollama/never-set": None}}}
        )

        assert storage.load_local_models_settings() == {"context_windows": {}}

    def test_rejects_invalid_update_value(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        with pytest.raises(StorageError, match="must be a positive integer"):
            storage.update_settings_sections(
                {"local_models": {"context_windows": {"ollama/m": -5}}}
            )

    def test_rejects_unsupported_field(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        with pytest.raises(StorageError, match="Unsupported local_models settings"):
            storage.update_settings_sections(
                {"local_models": {"context_windows": {}, "extra": 1}}
            )
