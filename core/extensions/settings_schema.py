"""Structured per-extension settings schema: field declarations + validation.

This lives in its own file (not ``extensions.py``, already over the 1000-line
soft limit) because the schema is a cohesive sub-part of the extensions domain
that is still exposed through the module's public API. An extension declares its
settings schema at register time via ``api.register_settings(fields)``; the
server renders a WebUI form from it and validates config on save.

The key/env-key regexes are defined locally on purpose — the module stays
decoupled from ``core/storage/`` even though the env-key rule intentionally
matches storage's ``ENV_KEY_PATTERN`` semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Field key: snake_case, must start with a lowercase letter. Matches the shape
# used elsewhere for identifiers; kept local so this module never imports storage.
_FIELD_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
# Secret env key: shell-style uppercase constant. Semantically the same rule as
# storage's ``ENV_KEY_PATTERN`` for the keys the UI is allowed to declare.
_ENV_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")

_FIELD_TYPES = ("text", "number", "toggle", "secret")


@dataclass(frozen=True)
class SettingsFieldDeclaration:
    """One declared settings field on an extension's schema.

    ``default`` is forbidden for ``secret`` fields and must match the field type
    otherwise. ``env_key`` is required for ``secret`` fields (naming the ``.env``
    key the secret is stored under) and forbidden for every other type.
    """

    key: str
    type: str
    label: str
    description: str | None = None
    default: Any = None
    required: bool = False
    env_key: str | None = None


def parse_settings_fields(raw: list[Any]) -> list[SettingsFieldDeclaration]:
    """Validate and freeze an extension's declared settings fields.

    Raises ``ValueError`` (naming the offending field key when known) on any
    violation of the field-declaration contract, so the mistake surfaces inside
    the extension's ``register()`` and the extension becomes a ``failed`` record.
    """
    if not isinstance(raw, list):
        raise ValueError("settings schema must be a list of field declarations")

    fields: list[SettingsFieldDeclaration] = []
    seen_keys: set[str] = set()
    for entry in raw:
        field = _parse_one_field(entry)
        if field.key in seen_keys:
            raise ValueError(f"settings field {field.key!r}: duplicate key")
        seen_keys.add(field.key)
        fields.append(field)
    return fields


def _parse_one_field(entry: Any) -> SettingsFieldDeclaration:
    if not isinstance(entry, dict):
        raise ValueError("settings field declaration must be a dict")

    key = entry.get("key")
    if not isinstance(key, str) or not _FIELD_KEY_PATTERN.fullmatch(key):
        raise ValueError(f"settings field {key!r}: key must match ^[a-z][a-z0-9_]*$")

    unknown = sorted(
        set(entry) - {"key", "type", "label", "description", "default", "required", "env_key"}
    )
    if unknown:
        raise ValueError(f"settings field {key!r}: unknown attribute(s) {', '.join(unknown)}")

    field_type = entry.get("type")
    if field_type not in _FIELD_TYPES:
        raise ValueError(f"settings field {key!r}: type must be one of {', '.join(_FIELD_TYPES)}")

    label = entry.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError(f"settings field {key!r}: label must be a non-empty string")

    description = entry.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError(f"settings field {key!r}: description must be a string")

    required = entry.get("required", False)
    if not isinstance(required, bool):
        raise ValueError(f"settings field {key!r}: required must be a boolean")

    env_key = _validate_env_key(key, field_type, entry.get("env_key"))
    default = _validate_default(key, field_type, entry.get("default"))

    return SettingsFieldDeclaration(
        key=key,
        type=field_type,
        label=label,
        description=description,
        default=default,
        required=required,
        env_key=env_key,
    )


def _validate_env_key(key: str, field_type: str, env_key: Any) -> str | None:
    """Enforce the env-key rule: required for secrets, forbidden otherwise."""
    if field_type == "secret":
        if not isinstance(env_key, str) or not _ENV_KEY_PATTERN.fullmatch(env_key):
            raise ValueError(f"settings field {key!r}: secret env_key must match ^[A-Z][A-Z0-9_]*$")
        return env_key
    if env_key is not None:
        raise ValueError(f"settings field {key!r}: env_key is only valid for a secret field")
    return None


def _validate_default(key: str, field_type: str, default: Any) -> Any:
    """Enforce the default rule: forbidden for secrets, type-matched otherwise."""
    if default is None:
        return None
    if field_type == "secret":
        raise ValueError(f"settings field {key!r}: a secret field cannot declare a default")
    if field_type == "text" and not isinstance(default, str):
        raise ValueError(f"settings field {key!r}: default must be a string")
    if field_type == "number" and (
        isinstance(default, bool) or not isinstance(default, int | float)
    ):
        raise ValueError(f"settings field {key!r}: default must be a number")
    if field_type == "toggle" and not isinstance(default, bool):
        raise ValueError(f"settings field {key!r}: default must be a boolean")
    return default


def validate_extension_config(
    fields: list[SettingsFieldDeclaration], config: dict[str, Any]
) -> list[str]:
    """Validate a persisted config dict against a declared schema.

    Returns a list of human-readable error strings (empty when valid). Per the
    contract: unknown keys are rejected, a key naming a ``secret`` field is
    rejected (secrets live in ``.env``, never in config), type mismatches are
    rejected, and a ``required`` non-secret field that is absent or an empty
    string is rejected.
    """
    errors: list[str] = []
    fields_by_key = {field.key: field for field in fields}
    secret_keys = {field.key for field in fields if field.type == "secret"}

    for key in config:
        if key in secret_keys:
            errors.append(f"{key!r}: secret values are stored in .env, not in config")
        elif key not in fields_by_key:
            errors.append(f"{key!r}: unknown settings key")

    for field in fields:
        if field.type == "secret":
            continue
        _validate_config_field(field, config, errors)

    return errors


def _validate_config_field(
    field: SettingsFieldDeclaration, config: dict[str, Any], errors: list[str]
) -> None:
    present = field.key in config
    value = config.get(field.key)

    if present:
        if field.type == "text" and not isinstance(value, str):
            errors.append(f"{field.key!r}: must be a string")
        elif field.type == "number" and (
            isinstance(value, bool) or not isinstance(value, int | float)
        ):
            errors.append(f"{field.key!r}: must be a number")
        elif field.type == "toggle" and not isinstance(value, bool):
            errors.append(f"{field.key!r}: must be a boolean")

    if field.required:
        missing = not present
        empty_text = field.type == "text" and isinstance(value, str) and not value.strip()
        if missing or empty_text:
            errors.append(f"{field.key!r}: is required")
