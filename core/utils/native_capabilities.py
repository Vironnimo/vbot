"""Lightweight identity and wire limits shared by native clients and the server."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, unquote

MAX_CAPABILITY_IDS_BYTES = 256 * 1024
MAX_CAPABILITY_CATALOG_BYTES = 4 * 1024 * 1024
_LOCAL_ID = re.compile(r"[a-z][a-z0-9_]{0,63}")


def qualified_capability_id(owner: str, name: str) -> str:
    return f"extension:{quote(owner, safe='')}:{name}"


def valid_capability_id(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 2400:
        return False
    if _LOCAL_ID.fullmatch(value):
        return value != "name"
    parts = value.split(":")
    if len(parts) != 3 or parts[0] != "extension" or not _LOCAL_ID.fullmatch(parts[2]):
        return False
    owner = unquote(parts[1])
    return (
        bool(owner)
        and owner not in {".", ".."}
        and owner.isprintable()
        and not any(char in owner for char in "/\\:")
        and quote(owner, safe="") == parts[1]
    )


def validate_capability_ids(value: Any) -> None:
    if not isinstance(value, list) or any(not valid_capability_id(item) for item in value):
        raise ValueError("Computer capabilities contain an invalid identifier")
    if len(set(value)) != len(value):
        raise ValueError("Computer capabilities contain a duplicate identifier")
    if len(json.dumps(value).encode()) > MAX_CAPABILITY_IDS_BYTES:
        raise ValueError("Computer capability identifiers exceed the 256 KiB transport budget")
