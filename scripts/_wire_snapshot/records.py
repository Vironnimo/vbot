"""Canonical JSON, secret masking and JSONL storage for wire snapshot records.

A snapshot is a directory with one ``<provider>.jsonl`` file per Provider and a
``summary.json``. Every line is one record: a JSON object with a unique ``key``
and a ``kind``. Lines are sorted by key and every object is written with sorted
keys, so the same code produces byte-identical files.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx

SUMMARY_FILE_NAME = "summary.json"
RECORD_FILE_SUFFIX = ".jsonl"

_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# Headers whose value is a credential even when it contains no known fake secret.
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "api-key",
        "cookie",
        "proxy-authorization",
        "x-api-key",
        "x-goog-api-key",
    }
)
# Derived from the recorded body or URL; recording them would duplicate every change.
_OMITTED_HEADERS = frozenset({"content-length", "host"})


def to_jsonable(value: Any) -> Any:
    """Convert dataclasses, mappings, tuples and sets into plain JSON values."""

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_jsonable(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, frozenset | set):
        return sorted(to_jsonable(item) for item in value)
    if isinstance(value, list | tuple):
        return [to_jsonable(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return repr(value)


def canonical_json(value: Any) -> str:
    """Serialize one JSON value with sorted keys and no insignificant whitespace."""

    return json.dumps(to_jsonable(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class Masker:
    """Replace fake secrets, the temporary data directory and volatile ids in records.

    The snapshot only ever uses fake credentials, but records must still never
    carry a credential-shaped value: each known fake secret becomes a named
    placeholder (``<secret:ANTHROPIC_API_KEY>``) so a record shows which
    credential a request used without containing it.
    """

    def __init__(self, secrets: Mapping[str, str], data_dir: Path | None = None) -> None:
        replacements = {value: f"<secret:{name}>" for value, name in secrets.items() if value}
        if data_dir is not None:
            for spelling in {str(data_dir), data_dir.as_posix(), str(data_dir.resolve())}:
                replacements[spelling] = "<data_dir>"
        self._replacements = sorted(replacements.items(), key=lambda item: (-len(item[0]), item[0]))

    def text(self, value: str) -> str:
        """Mask one string."""

        for secret, placeholder in self._replacements:
            if secret in value:
                value = value.replace(secret, placeholder)
        return _UUID_PATTERN.sub("<uuid>", value)

    def json_value(self, value: Any) -> Any:
        """Mask every string inside one JSON value."""

        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Mapping):
            return {self.text(str(key)): self.json_value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.json_value(item) for item in value]
        return value

    def headers(self, headers: Iterable[tuple[str, str]]) -> dict[str, str]:
        """Return lower-cased, masked headers without the derived ones."""

        recorded: dict[str, str] = {}
        for raw_name, raw_value in headers:
            name = raw_name.lower()
            if name in _OMITTED_HEADERS:
                continue
            value = self.text(str(raw_value))
            if name in _SENSITIVE_HEADERS and "<secret:" not in value:
                value = "<masked>"
            recorded[name] = f"{recorded[name]}, {value}" if name in recorded else value
        return dict(sorted(recorded.items()))

    def request(self, request: httpx.Request) -> dict[str, Any]:
        """Record one HTTP request: method, URL, headers and parsed body."""

        return {
            "method": request.method,
            "url": self.text(str(request.url)),
            "headers": self.headers(request.headers.multi_items()),
            "body": self.body(request.content),
        }

    def body(self, content: bytes) -> Any:
        """Parse a JSON body; keep any other body as masked text."""

        if not content:
            return None
        text = content.decode("utf-8", errors="replace")
        try:
            return self.json_value(json.loads(text))
        except ValueError:
            return {"text": self.text(text)}

    def error(self, error: BaseException) -> dict[str, str]:
        """Record an exception as its type and masked message."""

        return {"type": type(error).__name__, "message": self.text(str(error))}


def write_snapshot(
    out_dir: Path,
    records_by_provider: Mapping[str, list[dict[str, Any]]],
    summary: Mapping[str, Any],
) -> None:
    """Write one sorted JSONL file per Provider plus the summary."""

    out_dir.mkdir(parents=True, exist_ok=True)
    for provider_id, records in records_by_provider.items():
        lines = [
            canonical_json(record) for record in sorted(records, key=lambda item: str(item["key"]))
        ]
        key_counts = Counter(str(record["key"]) for record in records)
        duplicates = sorted(key for key, count in key_counts.items() if count > 1)
        if duplicates:
            raise ValueError(f"duplicate snapshot record keys: {duplicates[:3]}")
        path = out_dir / f"{provider_id}{RECORD_FILE_SUFFIX}"
        path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8", newline="\n")
    (out_dir / SUMMARY_FILE_NAME).write_text(
        json.dumps(to_jsonable(summary), sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def read_snapshot(
    snapshot_dir: Path, providers: Iterable[str] | None = None
) -> dict[str, dict[str, dict[str, Any]]]:
    """Read ``{provider: {key: record}}`` from a snapshot directory."""

    selected = set(providers) if providers else None
    snapshot: dict[str, dict[str, dict[str, Any]]] = {}
    for path in sorted(snapshot_dir.glob(f"*{RECORD_FILE_SUFFIX}")):
        provider_id = path.name.removesuffix(RECORD_FILE_SUFFIX)
        if selected is not None and provider_id not in selected:
            continue
        records: dict[str, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                record = json.loads(line)
                records[record["key"]] = record
        snapshot[provider_id] = records
    return snapshot
