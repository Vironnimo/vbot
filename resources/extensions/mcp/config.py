"""Validated MCP connection records and atomic, Extension-owned persistence.

``connections.json`` is a versioned JSON document ``{"format_version": 1,
"connections": [...]}``. Each connection loads on its own: an invalid or
duplicate one is skipped with an issue and kept verbatim on every save, and
unknown fields are reported, ignored, and written back unchanged. A document
whose root fails to load, including one from a newer vBot, is never overwritten.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from core.config_validation import (
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    format_report_diagnostics,
    read_json_file,
    validate_json_file,
    warn_unknown_keys,
)
from core.json_documents import (
    JsonDocumentFormat,
    json_document,
    json_list,
    json_object,
    validate_collection_root,
    write_json_document,
)

CONNECTION_ID_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
ENVIRONMENT_KEY_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 86400
CONNECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "pattern": CONNECTION_ID_PATTERN},
        "transport": {"enum": ["stdio", "http", "sse"]},
        "command": {"type": "string", "minLength": 1},
        "args": {"type": "array", "items": {"type": "string"}},
        "cwd": {"type": "string", "minLength": 1},
        "url": {"type": "string", "minLength": 1},
        "environment": {"type": "object", "additionalProperties": {"type": "string"}},
        "credential_environment": {"type": "object", "additionalProperties": {"type": "string"}},
        "credential_headers": {"type": "object", "additionalProperties": {"type": "string"}},
        "enabled": {"type": "boolean"},
        "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_TIMEOUT_SECONDS},
        "oauth": {"type": "boolean"},
        "oauth_redirect_uri": {"type": "string"},
    },
    "required": ["id", "transport"],
    "additionalProperties": False,
}
CONNECTIONS_FORMAT_VERSION = 1
CONNECTION_FIELDS = frozenset(CONNECTION_SCHEMA["properties"])
CONNECTIONS_SHAPE = json_document(
    {"connections"}, {"connections": json_list(json_object(CONNECTION_FIELDS), key="id")}
)


def validate_connection(value: Any) -> dict[str, Any]:
    errors = list(Draft202012Validator(CONNECTION_SCHEMA).iter_errors(value))
    if errors:
        paths = ["/".join(map(str, error.absolute_path)) or "connection" for error in errors]
        raise ValueError(f"Invalid MCP configuration at: {', '.join(paths)}")
    record = dict(value)
    if record["transport"] == "stdio":
        if not record.get("command") or record.get("url") or record.get("oauth"):
            raise ValueError("A stdio connection requires a command and cannot use a URL or OAuth")
        if record.get("cwd") and not Path(record["cwd"]).is_absolute():
            raise ValueError("MCP working directory must be absolute on the vBot server")
    else:
        parsed = urlsplit(record.get("url", ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("HTTP MCP connections require an http or https URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("MCP URL must not contain credentials or fragments")
        if record.get("command") or record.get("args") or record.get("cwd"):
            raise ValueError("HTTP MCP connections cannot specify a local command or directory")
    for field in ("environment", "credential_environment"):
        if any(not re.fullmatch(ENVIRONMENT_KEY_PATTERN, key) for key in record.get(field, {})):
            raise ValueError("MCP environment names must be valid environment variable names")
    for field in ("credential_headers", "credential_environment"):
        if any(
            not re.fullmatch(ENVIRONMENT_KEY_PATTERN, key) for key in record.get(field, {}).values()
        ):
            raise ValueError("MCP credentials must reference named environment credentials")
    if any("\r" in key or "\n" in key for key in record.get("credential_headers", {})):
        raise ValueError("MCP header names cannot contain line breaks")
    record.setdefault("enabled", True)
    record.setdefault("timeout", DEFAULT_TIMEOUT_SECONDS)
    return record


def validate_connections_document(data: Any) -> list[JsonDiagnostic]:
    """Validate the root of ``connections.json``; connections load one by one."""
    diagnostics: list[JsonDiagnostic] = []
    validate_collection_root(
        diagnostics,
        data,
        version=CONNECTIONS_FORMAT_VERSION,
        shape=CONNECTIONS_SHAPE,
        collection="connections",
        label="MCP connections field",
    )
    return diagnostics


CONNECTIONS_FORMAT = JsonDocumentFormat(
    name="MCP connections",
    version=CONNECTIONS_FORMAT_VERSION,
    shape=CONNECTIONS_SHAPE,
    validate=validate_connections_document,
)


def validate_connections_file(path: str | Path) -> JsonValidationReport:
    """Validate an optional ``connections.json``, including every connection."""
    return validate_json_file(path, _validate_connections_data, missing_ok=True)


def _validate_connections_data(data: Any) -> list[JsonDiagnostic]:
    diagnostics = validate_connections_document(data)
    if any(diagnostic.severity == "error" for diagnostic in diagnostics):
        return diagnostics
    _records, issues = parse_connections(data["connections"])
    for issue in issues:
        path = f"$.connections[{issue['index']}]"
        if issue["code"] == "unknown_fields":
            item = data["connections"][issue["index"]]
            warn_unknown_keys(diagnostics, path, item, CONNECTION_FIELDS, "MCP connection field")
        else:
            add_error(diagnostics, path, issue["message"])
    return diagnostics


def parse_connections(
    entries: list[Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Return the valid connections by id and an issue for every skipped entry."""
    issues: list[dict[str, Any]] = []
    identifiers = Counter(
        item["id"] for item in entries if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    records = {}
    for index, item in enumerate(entries):
        location: dict[str, Any] = {"index": index}
        if isinstance(item, dict):
            identifier = item.get("id")
            if isinstance(identifier, str) and re.fullmatch(CONNECTION_ID_PATTERN, identifier):
                location["connection_id"] = identifier
            if isinstance(identifier, str) and identifiers[identifier] > 1:
                issues.append(
                    {
                        **location,
                        "code": "duplicate_id",
                        "message": "Duplicate connection id; connection skipped",
                    }
                )
                continue
            unknown = sorted(set(item) - CONNECTION_FIELDS)
            if unknown:
                issues.append(
                    {
                        **location,
                        "code": "unknown_fields",
                        "fields": unknown,
                        "message": "Unrecognized fields ignored",
                    }
                )
            item = {key: value for key, value in item.items() if key in CONNECTION_FIELDS}
        try:
            record = validate_connection(item)
        except ValueError as error:
            issues.append({**location, "code": "invalid_connection", "message": str(error)})
            continue
        records[record["id"]] = record
    return records, issues


class ConnectionStore:
    """Load connections independently and preserve unrecognized persisted data."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "connections.json"
        self.issues: list[dict[str, Any]] = []

    def _read(self) -> dict[str, Any] | None:
        report, data = read_json_file(self.path, validate_connections_document, missing_ok=True)
        if not report.exists:
            return None
        if not report.ok:
            details = "; ".join(format_report_diagnostics(report))
            raise ValueError(f"MCP connections document failed to load: {details}")
        return data if isinstance(data, dict) else None

    def _parse(self, document: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        if document is None:
            self.issues = []
            return {}
        records, self.issues = parse_connections(document["connections"])
        unknown = sorted(set(document) - CONNECTIONS_SHAPE.fields)
        if unknown:
            self.issues.insert(
                0,
                {
                    "code": "unknown_fields",
                    "fields": unknown,
                    "message": "Unrecognized document fields ignored",
                },
            )
        return records

    def load(self) -> dict[str, dict[str, Any]]:
        return self._parse(self._read())

    def save(self, records: dict[str, dict[str, Any]]) -> None:
        document = self._read()
        data: list[Any] = document["connections"] if document is not None else []
        previous = self._parse(document)
        normalized = {
            identifier: validate_connection(record) for identifier, record in records.items()
        }
        if any(identifier != record["id"] for identifier, record in normalized.items()):
            raise ValueError("MCP connection ids must match their record keys")
        values: list[Any] = []
        written: set[str] = set()
        for item in data:
            identifier = item.get("id") if isinstance(item, dict) else None
            if isinstance(identifier, str) and identifier in normalized:
                if identifier not in written:
                    extras = {
                        key: value for key, value in item.items() if key not in CONNECTION_FIELDS
                    }
                    values.append({**extras, **normalized[identifier]})
                    written.add(identifier)
            elif not isinstance(identifier, str) or identifier not in previous:
                # An unrelated save must not erase a rejected connection.
                values.append(item)
        values.extend(
            record for identifier, record in normalized.items() if identifier not in written
        )
        write_json_document(self.path, {"connections": values}, CONNECTIONS_FORMAT)
        self._parse(self._read())


def atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".mcp-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
