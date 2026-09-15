"""Validated MCP connection records and atomic, Extension-owned persistence."""

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


class ConnectionStore:
    """Load connections independently and preserve unrecognized persisted data."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / "connections.json"
        self.issues: list[dict[str, Any]] = []

    def _read(self) -> list[Any]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("MCP connections document must be an array")
        return data

    def _parse(self, data: list[Any]) -> dict[str, dict[str, Any]]:
        self.issues = []
        identifiers = Counter(
            item["id"]
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )
        records = {}
        for index, item in enumerate(data):
            location: dict[str, Any] = {"index": index}
            if isinstance(item, dict):
                identifier = item.get("id")
                if isinstance(identifier, str) and re.fullmatch(CONNECTION_ID_PATTERN, identifier):
                    location["connection_id"] = identifier
                if isinstance(identifier, str) and identifiers[identifier] > 1:
                    self.issues.append(
                        {
                            **location,
                            "code": "duplicate_id",
                            "message": "Duplicate connection id; connection skipped",
                        }
                    )
                    continue
                unknown = sorted(set(item) - CONNECTION_SCHEMA["properties"].keys())
                if unknown:
                    self.issues.append(
                        {
                            **location,
                            "code": "unknown_fields",
                            "fields": unknown,
                            "message": "Unrecognized fields ignored",
                        }
                    )
                item = {
                    key: value
                    for key, value in item.items()
                    if key in CONNECTION_SCHEMA["properties"]
                }
            try:
                record = validate_connection(item)
            except ValueError as error:
                self.issues.append(
                    {**location, "code": "invalid_connection", "message": str(error)}
                )
                continue
            records[record["id"]] = record
        return records

    def load(self) -> dict[str, dict[str, Any]]:
        return self._parse(self._read())

    def save(self, records: dict[str, dict[str, Any]]) -> None:
        data = self._read()
        previous = self._parse(data)
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
                        key: value
                        for key, value in item.items()
                        if key not in CONNECTION_SCHEMA["properties"]
                    }
                    values.append({**extras, **normalized[identifier]})
                    written.add(identifier)
            elif not isinstance(identifier, str) or identifier not in previous:
                # An unrelated save must not erase a rejected connection.
                values.append(item)
        values.extend(
            record for identifier, record in normalized.items() if identifier not in written
        )
        atomic_json(self.path, values)
        self._parse(values)


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
