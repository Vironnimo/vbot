"""Durable ownership of Desktop computer identities, separate from live connections."""

from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
from typing import Any

from core.access.devices import normalize_device_name
from core.config_validation import (
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    load_validated_json_file,
    validate_json_file,
)
from core.json_documents import (
    JsonDocumentFormat,
    json_document,
    json_list,
    json_object,
    strip_unknown_fields,
    validate_collection_root,
    warn_unknown_fields,
    write_json_document,
)
from core.utils.ids import is_safe_id

COMPUTERS_PATH = "access/computers.json"
_FIELDS = {"id", "name", "platform", "capabilities", "actor", "key_hash"}
_SHAPE = json_document({"computers"}, {"computers": json_list(json_object(_FIELDS), key="id")})


def validate_computers_data(data: Any) -> list[JsonDiagnostic]:
    diagnostics: list[JsonDiagnostic] = []
    entries = validate_collection_root(
        diagnostics, data, version=1, shape=_SHAPE, collection="computers"
    )
    if entries is None:
        return diagnostics
    ids: set[str] = set()
    for index, item in enumerate(entries):
        path = f"$.computers[{index}]"
        if not isinstance(item, dict):
            add_error(diagnostics, path, "must be an object")
            continue
        warn_unknown_fields(diagnostics, path, item, json_object(_FIELDS))
        try:
            validate_description(item)
            if item["id"] in ids:
                raise ValueError("computer id is duplicated")
            ids.add(item["id"])
            if not isinstance(item.get("actor"), str) or not item["actor"]:
                raise ValueError("actor is required")
            if not isinstance(item.get("key_hash"), str) or not re.fullmatch(
                r"[0-9a-f]{64}", item["key_hash"]
            ):
                raise ValueError("key_hash is invalid")
        except KeyError, TypeError, ValueError:
            add_error(diagnostics, path, "computer identity or metadata is invalid")
    return diagnostics


def validate_computers_file(path: Path) -> JsonValidationReport:
    return validate_json_file(path, validate_computers_data, missing_ok=True)


def validate_description(value: dict[str, Any]) -> None:
    computer_id = value.get("id")
    if (
        not isinstance(computer_id, str)
        or not is_safe_id(computer_id)
        or not computer_id.startswith("pc_")
    ):
        raise ValueError("Computer id must be a pc_ identity")
    normalize_device_name(value.get("name"))
    if value.get("platform") not in {"win32", "linux", "darwin"}:
        raise ValueError("Computer platform is invalid")
    capabilities = value.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > 32
        or any(
            not isinstance(item, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item)
            for item in capabilities
        )
    ):
        raise ValueError("Computer capabilities are invalid")


class ComputerStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        if path is not None:
            raw = load_validated_json_file(path, validate_computers_data, missing_ok=True)
            if raw is not None:
                self.entries = {
                    item["id"]: item for item in strip_unknown_fields(raw, _SHAPE)["computers"]
                }

    def claim(self, description: dict[str, Any], key: str, actor: str) -> None:
        """Publish an identity only after durable ownership was checked and written."""
        validate_description(description)
        if not isinstance(key, str) or not 32 <= len(key) <= 256:
            raise ValueError("Computer registration key is invalid")
        digest = hashlib.sha256(key.encode()).hexdigest()
        existing = self.entries.get(description["id"])
        if existing is not None and (
            existing["actor"] != actor or not hmac.compare_digest(existing["key_hash"], digest)
        ):
            raise PermissionError("Computer identity belongs to another installation")
        candidate = {**description, "actor": actor, "key_hash": digest}
        if existing == candidate:
            return
        entries = {**self.entries, candidate["id"]: candidate}
        if self.path is not None:
            write_json_document(
                self.path,
                {"computers": list(entries.values())},
                JsonDocumentFormat("computers", 1, _SHAPE, validate_computers_data),
                owner_only=True,
            )
        self.entries = entries
