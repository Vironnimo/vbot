"""Tests for server settings parsing (``settings.update`` section)."""

from __future__ import annotations

import pytest

from core.settings import SettingsValidationError, parse_settings_update


class TestParseServerUpdate:
    def test_keep_awake_true(self) -> None:
        result = parse_settings_update({"server": {"keep_awake": True}})
        assert result == {"server": {"keep_awake": True}}

    def test_keep_awake_false(self) -> None:
        result = parse_settings_update({"server": {"keep_awake": False}})
        assert result == {"server": {"keep_awake": False}}

    def test_timezone_accepts_iana_name(self) -> None:
        result = parse_settings_update({"server": {"timezone": "Europe/Berlin"}})
        assert result == {"server": {"timezone": "Europe/Berlin"}}

    @pytest.mark.parametrize("value", ["Berlin", "", 1, None])
    def test_timezone_rejects_unknown_or_malformed_value(self, value: object) -> None:
        with pytest.raises(SettingsValidationError):
            parse_settings_update({"server": {"timezone": value}})

    def test_empty_section_is_sparse(self) -> None:
        result = parse_settings_update({"server": {}})
        assert result == {"server": {}}

    def test_server_not_a_dict(self) -> None:
        with pytest.raises(SettingsValidationError):
            parse_settings_update({"server": []})

    def test_unknown_field(self) -> None:
        with pytest.raises(SettingsValidationError):
            parse_settings_update({"server": {"keep_awake": True, "extra_key": 1}})

    @pytest.mark.parametrize(
        "value",
        ["yes", 1, 0, None, 1.0, [], {}],
    )
    def test_keep_awake_not_boolean(self, value: object) -> None:
        with pytest.raises(SettingsValidationError):
            parse_settings_update({"server": {"keep_awake": value}})
