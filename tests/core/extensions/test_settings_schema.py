"""Tests for the extension settings-schema field parser and config validator.

One assert per rejection rule in the field-declaration contract table, a fully
valid schema round-trip, and ``validate_extension_config`` accept/reject cases
(including the secret-in-config and required-empty-string rejections).
"""

from __future__ import annotations

import pytest

from core.extensions.settings_schema import (
    SettingsFieldDeclaration,
    parse_settings_fields,
    validate_extension_config,
)


def _field(**overrides: object) -> dict:
    base: dict[str, object] = {"key": "url", "type": "text", "label": "URL"}
    base.update(overrides)
    return base


# --- parse_settings_fields: rejection rules ---------------------------------


def test_reject_non_list_schema() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields({"key": "url"})  # type: ignore[arg-type]


def test_reject_non_dict_field() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields(["nope"])


def test_reject_missing_key() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([{"type": "text", "label": "URL"}])


def test_reject_bad_key_pattern() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(key="Bad-Key")])


def test_reject_duplicate_key() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(), _field(label="Second")])


def test_reject_unknown_attribute() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(placeholder="x")])


def test_reject_bad_type() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(type="date")])


def test_reject_empty_label() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(label="  ")])


def test_reject_non_string_description() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(description=123)])


def test_reject_non_bool_required() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(required="yes")])


def test_reject_secret_without_env_key() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(key="token", type="secret")])


def test_reject_secret_with_bad_env_key() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(key="token", type="secret", env_key="lower_case")])


def test_reject_env_key_on_non_secret() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(env_key="HASS_URL")])


def test_reject_default_on_secret() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(key="token", type="secret", env_key="TOKEN", default="x")])


def test_reject_text_default_wrong_type() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(default=1)])


def test_reject_number_default_wrong_type() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(key="port", type="number", default="80")])


def test_reject_number_default_bool() -> None:
    # bool is a subclass of int but must not pass as a number default.
    with pytest.raises(ValueError):
        parse_settings_fields([_field(key="port", type="number", default=True)])


def test_reject_toggle_default_wrong_type() -> None:
    with pytest.raises(ValueError):
        parse_settings_fields([_field(key="on", type="toggle", default="true")])


# --- parse_settings_fields: valid round-trip --------------------------------


def test_valid_schema_round_trip() -> None:
    fields = parse_settings_fields(
        [
            {
                "key": "url",
                "type": "text",
                "label": "URL",
                "description": "Server URL",
                "default": "http://localhost",
                "required": True,
            },
            {"key": "port", "type": "number", "label": "Port", "default": 8123},
            {"key": "verbose", "type": "toggle", "label": "Verbose", "default": False},
            {"key": "token", "type": "secret", "label": "Token", "env_key": "HASS_TOKEN"},
        ]
    )

    assert fields[0] == SettingsFieldDeclaration(
        key="url",
        type="text",
        label="URL",
        description="Server URL",
        default="http://localhost",
        required=True,
        env_key=None,
    )
    assert fields[1].default == 8123
    assert fields[2].default is False
    assert fields[3].env_key == "HASS_TOKEN"
    assert fields[3].default is None


# --- validate_extension_config ----------------------------------------------


def _schema() -> list[SettingsFieldDeclaration]:
    return parse_settings_fields(
        [
            {"key": "url", "type": "text", "label": "URL", "required": True},
            {"key": "port", "type": "number", "label": "Port"},
            {"key": "verbose", "type": "toggle", "label": "Verbose"},
            {"key": "token", "type": "secret", "label": "Token", "env_key": "HASS_TOKEN"},
        ]
    )


def test_valid_config_passes() -> None:
    errors = validate_extension_config(_schema(), {"url": "http://x", "port": 80, "verbose": True})
    assert errors == []


def test_reject_unknown_key() -> None:
    errors = validate_extension_config(_schema(), {"url": "http://x", "extra": 1})
    assert any("unknown settings key" in error for error in errors)


def test_reject_secret_in_config() -> None:
    errors = validate_extension_config(_schema(), {"url": "http://x", "token": "abc"})
    assert any("stored in .env" in error for error in errors)


def test_reject_type_mismatch() -> None:
    errors = validate_extension_config(_schema(), {"url": 5})
    assert any("must be a string" in error for error in errors)


def test_reject_number_bool_mismatch() -> None:
    errors = validate_extension_config(_schema(), {"url": "http://x", "port": True})
    assert any("must be a number" in error for error in errors)


def test_reject_required_missing() -> None:
    errors = validate_extension_config(_schema(), {"port": 80})
    assert any("is required" in error for error in errors)


def test_reject_required_empty_string() -> None:
    errors = validate_extension_config(_schema(), {"url": "   "})
    assert any("is required" in error for error in errors)
