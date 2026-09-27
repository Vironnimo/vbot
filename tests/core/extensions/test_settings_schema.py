"""Extension settings schema: field declarations and config validation.

Every rejection rule of the field-declaration contract, a fully valid schema
round trip, and ``validate_extension_config`` accept/reject cases (including the
secret-in-config and required-empty-string rejections).
"""

from __future__ import annotations

from typing import Any

from core.extensions.settings_schema import (
    SettingsFieldDeclaration,
    parse_settings_fields,
    validate_extension_config,
)


def _field(**overrides: object) -> dict:
    base: dict[str, object] = {"key": "url", "type": "text", "label": "URL"}
    base.update(overrides)
    return base


_SECRET = {"key": "token", "type": "secret"}

# rule -> (schema, expected message fragment)
_REJECTED_SCHEMAS: dict[str, tuple[Any, str]] = {
    "schema is not a list": ({"key": "url"}, "must be a list"),
    "field is not a dict": (["nope"], "must be a dict"),
    "missing key": ([{"type": "text", "label": "URL"}], "key must match"),
    "bad key pattern": ([_field(key="Bad-Key")], "key must match"),
    "duplicate key": ([_field(), _field(label="Second")], "duplicate key"),
    "unknown attribute": ([_field(placeholder="x")], "unknown attribute"),
    "unknown type": ([_field(type="date")], "type must be one of"),
    "empty label": ([_field(label="  ")], "label must be a non-empty string"),
    "description not a string": ([_field(description=123)], "description must be a string"),
    "required not a bool": ([_field(required="yes")], "required must be a boolean"),
    "secret without env_key": ([_field(**_SECRET)], "secret env_key must match"),
    "secret with bad env_key": ([_field(**_SECRET, env_key="lower_case")], "env_key must match"),
    "env_key on a non-secret": ([_field(env_key="HASS_URL")], "only valid for a secret"),
    "default on a secret": (
        [_field(**_SECRET, env_key="TOKEN", default="x")],
        "cannot declare a default",
    ),
    "text default not a string": ([_field(default=1)], "default must be a string"),
    "number default not a number": (
        [_field(key="port", type="number", default="80")],
        "default must be a number",
    ),
    # bool is a subclass of int but must not pass as a number default.
    "number default is a bool": (
        [_field(key="port", type="number", default=True)],
        "default must be a number",
    ),
    "toggle default not a bool": (
        [_field(key="on", type="toggle", default="true")],
        "default must be a boolean",
    ),
}


def test_every_invalid_field_declaration_is_rejected_with_its_rule() -> None:
    outcomes: dict[str, str] = {}
    for rule, (schema, fragment) in _REJECTED_SCHEMAS.items():
        try:
            parse_settings_fields(schema)
        except ValueError as error:
            outcomes[rule] = "rejected" if fragment in str(error) else f"wrong error: {error}"
        else:
            outcomes[rule] = "accepted"

    assert outcomes == dict.fromkeys(_REJECTED_SCHEMAS, "rejected")


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


def test_config_validation_accepts_a_matching_config_and_names_each_violation() -> None:
    schema = parse_settings_fields(
        [
            {"key": "url", "type": "text", "label": "URL", "required": True},
            {"key": "port", "type": "number", "label": "Port"},
            {"key": "verbose", "type": "toggle", "label": "Verbose"},
            {"key": "token", "type": "secret", "label": "Token", "env_key": "HASS_TOKEN"},
        ]
    )
    rejected: dict[str, tuple[dict[str, Any], str]] = {
        "unknown key": ({"url": "http://x", "extra": 1}, "unknown settings key"),
        "secret in config": ({"url": "http://x", "token": "abc"}, "stored in .env"),
        "text type mismatch": ({"url": 5}, "must be a string"),
        "number is a bool": ({"url": "http://x", "port": True}, "must be a number"),
        "toggle type mismatch": ({"url": "http://x", "verbose": "yes"}, "must be a boolean"),
        "required missing": ({"port": 80}, "is required"),
        "required empty string": ({"url": "   "}, "is required"),
    }

    assert validate_extension_config(schema, {"url": "http://x", "port": 80, "verbose": True}) == []
    outcomes = {
        case: any(fragment in error for error in validate_extension_config(schema, config))
        for case, (config, fragment) in rejected.items()
    }
    assert outcomes == dict.fromkeys(rejected, True)
