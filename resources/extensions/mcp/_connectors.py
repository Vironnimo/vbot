"""The connector catalog: hosted MCP services a user adds in one step.

``catalog.json`` ships read-only with the Extension. Each entry names a
vendor-run MCP server reached over Streamable HTTP that needs either no sign-in
or an OAuth sign-in vBot completes on its own (dynamic client registration with
a loopback redirect), so adding one takes no setup beyond the browser sign-in.
This catalog of services is unrelated to a connection's catalog of Tools,
Resources and Prompts (``_catalog.py``).

The file is validated when it loads: an entry that fails is left out and
reported as an issue, and never stops the Extension.
"""

from __future__ import annotations

import copy
import json
import unicodedata
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from .config import CONNECTION_ID_PATTERN, validate_connection

CATALOG_PATH = Path(__file__).with_name("catalog.json")
CATEGORIES = ("knowledge", "productivity", "design", "development", "analytics", "business")
MAX_ENTRY_DESCRIPTION_CHARACTERS = 120
# The longest connection id ``CONNECTION_ID_PATTERN`` accepts.
_MAX_ID_LENGTH = 32

ENTRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "pattern": CONNECTION_ID_PATTERN},
        "name": {"type": "string", "minLength": 1, "maxLength": 60},
        "description": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_ENTRY_DESCRIPTION_CHARACTERS,
        },
        "category": {"enum": list(CATEGORIES)},
        "url": {"type": "string"},
        "auth": {"enum": ["none", "oauth"]},
        "read_only_url": {"type": "string"},
        "notes": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 200}},
        "docs_url": {"type": "string"},
    },
    "required": ["id", "name", "description", "category", "url", "auth"],
    "additionalProperties": False,
}
_ENTRY_VALIDATOR = Draft202012Validator(ENTRY_SCHEMA)


@dataclass(frozen=True)
class ConnectorCatalog:
    """The valid catalog entries in file order, and an issue for each one left out."""

    entries: tuple[dict[str, Any], ...]
    issues: tuple[dict[str, Any], ...] = ()

    def entry(self, identifier: str) -> dict[str, Any]:
        for entry in self.entries:
            if entry["id"] == identifier:
                return entry
        raise ValueError(
            f"No MCP catalog entry is named {identifier}; the catalog operation lists them"
        )

    def listing(self, connections: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
        """Each entry with the ids of the saved *connections* that reach its server."""
        return [
            {
                **copy.deepcopy(entry),
                "connections": [
                    identifier
                    for identifier, config in connections.items()
                    if config.get("url") is not None
                    and _url_key(config["url"]) in _entry_urls(entry)
                ],
            }
            for entry in self.entries
        ]

    def connection(
        self, identifier: str, *, read_only: bool = False, taken: Collection[str] = ()
    ) -> dict[str, Any]:
        """The enabled connection adding an entry creates, under the first id not *taken*.

        The id is the entry's id, else the entry's id with ``_2``, ``_3`` and so on.
        """
        entry = self.entry(identifier)
        if read_only and "read_only_url" not in entry:
            raise ValueError(f"The MCP catalog entry {identifier} has no read-only variant")
        return _entry_connection(entry, _free_id(entry["id"], taken), read_only=read_only)


def load_connectors(path: Path = CATALOG_PATH) -> ConnectorCatalog:
    """Load the catalog at *path*; entries that fail validation become issues."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return ConnectorCatalog((), ({"code": "invalid_catalog", "message": str(error)},))
    if not isinstance(document, dict) or not isinstance(document.get("entries"), list):
        return ConnectorCatalog(
            (),
            ({"code": "invalid_catalog", "message": "The catalog must hold an entries array"},),
        )
    items: list[Any] = document["entries"]
    identifiers = [item.get("id") if isinstance(item, dict) else None for item in items]
    entries: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        location: dict[str, Any] = {"index": index}
        if isinstance(identifiers[index], str):
            location["entry"] = identifiers[index]
        if identifiers[index] is not None and identifiers.count(identifiers[index]) > 1:
            issues.append({**location, "code": "duplicate_id", "message": "Duplicate entry id"})
            continue
        problem = _entry_problem(item)
        if problem is not None:
            issues.append({**location, "code": "invalid_entry", "message": problem})
            continue
        entries.append(item)
    return ConnectorCatalog(tuple(entries), tuple(issues))


def _entry_problem(item: Any) -> str | None:
    """Why *item* is not a usable catalog entry, or ``None``."""
    errors = list(_ENTRY_VALIDATOR.iter_errors(item))
    if errors:
        paths = ["/".join(map(str, error.absolute_path)) or "entry" for error in errors]
        return f"Invalid catalog entry at: {', '.join(paths)}"
    texts = {
        "name": item["name"],
        "description": item["description"],
        **{f"notes/{index}": note for index, note in enumerate(item.get("notes", []))},
    }
    for field, text in texts.items():
        if any(unicodedata.category(character) in {"Cc", "Zl", "Zp"} for character in text):
            return f"Catalog entry {field} must be a single line"
    for field in ("url", "read_only_url", "docs_url"):
        if field in item and urlsplit(item[field]).scheme != "https":
            return f"Catalog entry {field} must be an https URL"
    try:
        _entry_connection(item, item["id"], read_only=False)
        if "read_only_url" in item:
            _entry_connection(item, item["id"], read_only=True)
    except ValueError as error:
        return str(error)
    return None


def _entry_connection(entry: dict[str, Any], identifier: str, *, read_only: bool) -> dict[str, Any]:
    return validate_connection(
        {
            "id": identifier,
            "transport": "http",
            "url": entry["read_only_url"] if read_only else entry["url"],
            **({"oauth": True} if entry["auth"] == "oauth" else {}),
            "description": entry["description"],
            "enabled": True,
        }
    )


def _free_id(base: str, taken: Collection[str]) -> str:
    candidate, number = base, 1
    while candidate in taken:
        number += 1
        suffix = f"_{number}"
        candidate = base[: _MAX_ID_LENGTH - len(suffix)] + suffix
    return candidate


def _entry_urls(entry: dict[str, Any]) -> set[tuple[str, ...]]:
    return {_url_key(entry[field]) for field in ("url", "read_only_url") if field in entry}


def _url_key(url: str) -> tuple[str, ...]:
    """*url* compared as a server address: scheme and host in any case, no trailing slash."""
    parts = urlsplit(url)
    return (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query)
