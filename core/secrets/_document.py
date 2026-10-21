"""Secret Store document schema, structural validation and decoding."""

from __future__ import annotations

import json
import re
from typing import Any, Final, Literal

from core.config_validation import (
    JsonDiagnostic,
    add_error,
    error_diagnostic,
)
from core.json_documents import (
    JsonDocumentFormat,
    json_document,
    json_map,
    json_object,
    validate_format_version,
    warn_unknown_fields,
)

Section = Literal["credentials", "oauth_tokens", "webhook_secrets", "agent_secrets"]

SECRET_STORE_FILE_NAME = "secrets.json"

SECRET_STORE_FORMAT_VERSION = 1

#: Credentials by environment-variable name (API keys, tokens, Extension secrets).
CREDENTIALS: Final = "credentials"

#: OAuth token documents of Provider Connections by token id.
OAUTH_TOKENS: Final = "oauth_tokens"

#: The secrets of Cron webhook jobs by job id (``core.automation``).
WEBHOOK_SECRETS: Final = "webhook_secrets"

#: Agent Secrets by Secret id; each value is one sealed JSON record
#: (``core.secrets.agent_secrets``) holding metadata and field values together.
AGENT_SECRETS: Final = "agent_secrets"

SECTIONS: tuple[Section, ...] = (CREDENTIALS, OAUTH_TOKENS, WEBHOOK_SECRETS, AGENT_SECRETS)

#: The record of an import of plaintext credential files (``core.secrets.plaintext``).
PLAINTEXT_IMPORT_FIELD = "plaintext_import"

ENTRY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}$")

_STORE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_KEY_SHAPE = json_object({"id", "protection", "check", "wrapped"})

# ``credentials`` and ``oauth_tokens`` are the name lists of the first record
# format, still read (``core.secrets.plaintext``) but no longer written.
_IMPORT_SHAPE = json_object(
    {"imported_at", "sources", "retiring_sources", "credentials", "oauth_tokens", "backups_pending"}
)

SECRET_STORE_SHAPE = json_document(
    {
        "store_id",
        "key",
        CREDENTIALS,
        OAUTH_TOKENS,
        WEBHOOK_SECRETS,
        AGENT_SECRETS,
        PLAINTEXT_IMPORT_FIELD,
    },
    {
        "key": _KEY_SHAPE,
        CREDENTIALS: json_map(),
        OAUTH_TOKENS: json_map(),
        WEBHOOK_SECRETS: json_map(),
        AGENT_SECRETS: json_map(),
        PLAINTEXT_IMPORT_FIELD: _IMPORT_SHAPE,
    },
)


def validate_secret_store_data(data: Any) -> list[JsonDiagnostic]:
    """Validate the structure of a decoded store document (no decryption)."""
    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]
    diagnostics: list[JsonDiagnostic] = []
    if not validate_format_version(diagnostics, data, SECRET_STORE_FORMAT_VERSION):
        return diagnostics
    warn_unknown_fields(diagnostics, "$", data, SECRET_STORE_SHAPE, label="Secret Store field")
    store_id = data.get("store_id")
    if not isinstance(store_id, str) or _STORE_ID_PATTERN.fullmatch(store_id) is None:
        add_error(diagnostics, "$.store_id", "must be a store identifier")
    key = data.get("key")
    if not isinstance(key, dict):
        add_error(diagnostics, "$.key", "must be an object")
    else:
        for name in ("id", "protection", "check"):
            if not isinstance(key.get(name), str) or not key[name]:
                add_error(diagnostics, f"$.key.{name}", "must be a non-empty string")
        if "wrapped" in key and not isinstance(key["wrapped"], str):
            add_error(diagnostics, "$.key.wrapped", "must be a string")
    for section in SECTIONS:
        entries = data.get(section, {})
        if not isinstance(entries, dict):
            add_error(diagnostics, f"$.{section}", "must be an object")
            continue
        for name, value in entries.items():
            if ENTRY_NAME_PATTERN.fullmatch(name) is None:
                add_error(diagnostics, f"$.{section}", f"has an invalid entry name: {name!r}")
            if not isinstance(value, str):
                add_error(diagnostics, f"$.{section}.{name}", "must be a sealed string")
    record = data.get(PLAINTEXT_IMPORT_FIELD)
    if record is not None:
        _validate_import_record(diagnostics, record)
    return diagnostics


def _validate_import_record(diagnostics: list[JsonDiagnostic], record: Any) -> None:
    path = f"$.{PLAINTEXT_IMPORT_FIELD}"
    if not isinstance(record, dict):
        add_error(diagnostics, path, "must be an object")
        return
    for field in ("sources", "retiring_sources"):
        sources = record.get(field, {})
        if not isinstance(sources, dict) or any(
            not _is_import_source(value) for value in sources.values()
        ):
            add_error(diagnostics, f"{path}.{field}", "must map file paths to source records")
    for name in (CREDENTIALS, OAUTH_TOKENS):
        value = record.get(name, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            add_error(diagnostics, f"{path}.{name}", "must be a list of names")
    if not isinstance(record.get("backups_pending", False), bool):
        add_error(diagnostics, f"{path}.backups_pending", "must be a boolean")


def _is_import_source(value: Any) -> bool:
    """A source record: ``{"fingerprint", "entries": {name: value fingerprint}}``.

    The first record format kept only the content fingerprint as a string.
    """
    if isinstance(value, str):
        return True
    if not isinstance(value, dict) or not isinstance(value.get("fingerprint"), str):
        return False
    entries = value.get("entries", {})
    return isinstance(entries, dict) and all(isinstance(item, str) for item in entries.values())


SECRET_STORE_FORMAT = JsonDocumentFormat(
    name="Secret Store",
    version=SECRET_STORE_FORMAT_VERSION,
    shape=SECRET_STORE_SHAPE,
    validate=validate_secret_store_data,
    sort_keys=True,
)


def _decode_document(raw: bytes) -> tuple[Any, str | None]:
    """The decoded document and, when it does not load, why (``read_json_file`` wording)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeError as exc:
        return None, f"$: File is not valid UTF-8: {exc}"
    except json.JSONDecodeError as exc:
        return None, f"$: Invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
    except ValueError:
        return None, "$: JSON exceeds decoder limits"
    problems = "; ".join(
        f"{diagnostic.path}: {diagnostic.message}"
        for diagnostic in validate_secret_store_data(data)
        if diagnostic.severity == "error"
    )
    return data, problems or None
