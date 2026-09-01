"""Tests for server settings storage persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.storage import StorageError, StorageManager


class TestApplyServerSettings:
    def test_update_persists_flat_raw_key(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        updated = storage.update_settings_sections({"server": {"keep_awake": True}})

        assert updated["server"]["keep_awake"] is True
        assert isinstance(updated["server"]["timezone"], str)
        assert storage.load_settings()["keep_awake"] is True

    def test_timezone_update_persists_flat_raw_key(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        updated = storage.update_settings_sections({"server": {"timezone": "America/New_York"}})

        assert updated["server"]["timezone"] == "America/New_York"
        assert storage.load_settings()["timezone"] == "America/New_York"

    def test_sparse_update_preserves_stored_value(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings({"keep_awake": True})

        storage.update_settings_sections({"server": {}})

        assert storage.load_settings()["keep_awake"] is True

    def test_disable_overrides_stored_value(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)
        storage.save_settings({"keep_awake": True})

        storage.update_settings_sections({"server": {"keep_awake": False}})

        assert storage.load_settings()["keep_awake"] is False

    def test_non_boolean_value_is_rejected(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        with pytest.raises(StorageError):
            storage.update_settings_sections({"server": {"keep_awake": "yes"}})

    def test_unknown_field_is_rejected(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        with pytest.raises(StorageError):
            storage.update_settings_sections({"server": {"port": 8421}})

    def test_unknown_timezone_is_rejected(self, tmp_path: Path) -> None:
        storage = StorageManager(tmp_path)

        with pytest.raises(StorageError):
            storage.update_settings_sections({"server": {"timezone": "Mars/Olympus"}})
