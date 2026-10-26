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
MAX_COMPUTERS = 32
# The local execution target's display name; a Desktop cannot pose as it.
_RESERVED_NAMES = frozenset({"server"})
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


class ComputerClaimError(Exception):
    """A registration the store refuses for a reason its owner can fix."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


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
        """Publish an identity only after durable ownership was checked and written.

        The registration key proves the installation. A changed sign-in takes the
        registration over, except that a connection without one ("open") cannot
        take over a registration made with one.
        """
        validate_description(description)
        if not isinstance(key, str) or not 32 <= len(key) <= 256:
            raise ValueError("Computer registration key is invalid")
        digest = hashlib.sha256(key.encode()).hexdigest()
        existing = self.entries.get(description["id"])
        if existing is not None:
            if not hmac.compare_digest(existing["key_hash"], digest):
                raise PermissionError("Computer identity belongs to another installation")
            if actor == "open" and existing["actor"] != "open":
                raise ComputerClaimError(
                    "computer_sign_in_required",
                    "This computer was registered with a sign-in, but this connection has "
                    "none. Sign this Desktop app in to the server again, or remove the "
                    "computer in the server's Settings and connect again.",
                )
        if existing is None or existing["name"] != description["name"]:
            self._check_name(description["id"], description["name"])
        if existing is None and len(self.entries) >= MAX_COMPUTERS:
            raise ComputerClaimError(
                "computer_limit_reached",
                f"This server already has {MAX_COMPUTERS} registered computers. Remove one "
                "you no longer use in the server's Settings, then connect again.",
            )
        candidate = {**description, "actor": actor, "key_hash": digest}
        if existing == candidate:
            return
        self._write({**self.entries, candidate["id"]: candidate})

    def forget(self, computer_id: str) -> None:
        """Remove a registration; its installation can register again as new."""
        if computer_id in self.entries:
            self._write({key: value for key, value in self.entries.items() if key != computer_id})

    def _check_name(self, computer_id: str, name: str) -> None:
        folded = normalize_device_name(name).casefold()
        if folded in _RESERVED_NAMES:
            raise ComputerClaimError(
                "computer_name_reserved",
                f"The name {name!r} is reserved for the server itself. Choose another "
                "name for this computer in its Desktop app.",
            )
        if any(
            other_id != computer_id and normalize_device_name(entry["name"]).casefold() == folded
            for other_id, entry in self.entries.items()
        ):
            raise ComputerClaimError(
                "computer_name_taken",
                f"Another computer on this server is already named {name!r}. Choose "
                "another name for this computer in its Desktop app.",
            )

    def _write(self, entries: dict[str, dict[str, Any]]) -> None:
        if self.path is not None:
            write_json_document(
                self.path,
                {"computers": list(entries.values())},
                JsonDocumentFormat("computers", 1, _SHAPE, validate_computers_data),
                owner_only=True,
            )
        self.entries = entries
