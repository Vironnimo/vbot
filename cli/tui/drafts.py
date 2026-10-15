"""Local terminal drafts, isolated by server and safe across concurrent accessors.

This is accessor state outside the server data directory, never Session history.
Writes preserve unknown fields and concurrent edits; no stored input is submitted.
"""

from __future__ import annotations

import copy
import hashlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from cli.tui.state import Conversation, JsonObject
from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    add_error,
    load_validated_json_file,
)
from core.json_documents import (
    JsonDocumentFormat,
    json_document,
    json_map,
    json_object,
    strip_unknown_fields,
    validate_format_version,
    write_json_document,
)

_FIELDS = frozenset(
    {
        "agent",
        "session",
        "project",
        "project_selected",
        "title",
        "draft",
        "attachments",
        "mentions",
        "overrides",
        "uncertain",
        "uncertain_submission",
        "pending_session_id",
    }
)
_SHAPE = json_document(
    {"server", "drafts"}, {"drafts": json_map(json_object(_FIELDS | {"edit_recovery"}))}
)


def _validate_input(value: JsonObject, location: str, errors: list[JsonDiagnostic]) -> None:
    for key in ("attachments", "mentions"):
        values = value.get(key)
        if not isinstance(values, list):
            add_error(errors, f"{location}.{key}", "must be a list")
            continue
        for item in values:
            valid = (
                isinstance(item, dict)
                and all(isinstance(item.get(field), str) for field in ("attachment_id", "filename"))
                if key == "attachments"
                else isinstance(item, str)
            )
            if not valid:
                add_error(errors, f"{location}.{key}", "contains an invalid entry")


def _validate(value: Any) -> list[JsonDiagnostic]:
    errors: list[JsonDiagnostic] = []
    if not isinstance(value, dict):
        add_error(errors, "$", "must be an object")
        return errors
    if not validate_format_version(errors, value, 1):
        return errors
    if not isinstance(value.get("server"), str):
        add_error(errors, "$.server", "must be a server URL")
    records = value.get("drafts")
    if not isinstance(records, dict):
        add_error(errors, "$.drafts", "must be an object")
        return errors
    for identifier, record in records.items():
        location = f"$.drafts.{identifier}"
        if not isinstance(record, dict):
            add_error(errors, location, "must be a draft object")
            continue
        for key in ("agent", "title", "draft"):
            if not isinstance(record.get(key), str):
                add_error(errors, f"{location}.{key}", "must be a string")
        for key in ("session", "project", "pending_session_id"):
            if record.get(key) is not None and not isinstance(record[key], str):
                add_error(errors, f"{location}.{key}", "must be a string or null")
        for key in ("project_selected", "uncertain"):
            if not isinstance(record.get(key), bool):
                add_error(errors, f"{location}.{key}", "must be a boolean")
        _validate_input(record, location, errors)
        if not isinstance(record.get("overrides"), dict):
            add_error(errors, f"{location}.overrides", "must be an object")
        submission = record.get("uncertain_submission")
        if submission is not None:
            if not isinstance(submission, dict):
                add_error(errors, f"{location}.uncertain_submission", "must be an object or null")
            else:
                _validate_input(submission, f"{location}.uncertain_submission", errors)
                if not isinstance(submission.get("text"), str):
                    add_error(errors, f"{location}.uncertain_submission.text", "must be a string")
        recovery = record.get("edit_recovery")
        if recovery is not None and (
            not isinstance(recovery, dict)
            or recovery.get("operation") != "chat.edit"
            or not isinstance(recovery.get("message_id"), str)
            or not isinstance(recovery.get("uncertain"), bool)
            or not isinstance(recovery.get("request"), dict)
        ):
            add_error(errors, f"{location}.edit_recovery", "must describe a saved edit")
    return errors


_FORMAT = JsonDocumentFormat("terminal drafts", 1, _SHAPE, _validate)


def draft_record(view: Conversation) -> JsonObject | None:
    if not (view.draft or view.attachments or view.mentions or view.uncertain):
        return None
    return {key: copy.deepcopy(getattr(view, key)) for key in _FIELDS}


def restore_draft(identifier: str, record: JsonObject) -> Conversation:
    values = {key: copy.deepcopy(value) for key, value in record.items() if key in _FIELDS}
    return Conversation(**values, draft_id=identifier)


def saved_input_record(value: JsonObject) -> JsonObject:
    """Convert displaced input history into an independently recoverable draft."""
    view = Conversation(
        value["agent"],
        session=value.get("session"),
        project=value.get("project"),
        project_selected=value.get("project_selected", False),
        draft=value["text"],
        attachments=copy.deepcopy(value.get("attachments", [])),
        mentions=list(value.get("mentions", [])),
    )
    record = {key: copy.deepcopy(getattr(view, key)) for key in _FIELDS}
    if value.get("operation") == "chat.edit":
        record["edit_recovery"] = {
            key: copy.deepcopy(value[key])
            for key in ("operation", "message_id", "uncertain", "request")
        }
    return record


def readdress_saved_inputs(
    saved: list[JsonObject],
    recovered: dict[str, JsonObject],
    source: str,
    destination: str,
    *,
    session: str | None = None,
) -> bool:
    """Move local recovery routing; retain original uncertain request envelopes."""
    changed = False
    for record in [*saved, *recovered.values()]:
        if record["agent"] == source and (session is None or record.get("session") == session):
            record["agent"] = destination
            changed = True
    return changed


class DraftStore:
    """Merge only this accessor's edits, retaining a second writer's conflicting draft."""

    def __init__(self, server: str, *, directory: Path | None = None) -> None:
        self.server = server.rstrip("/")
        root = directory if directory is not None else Path.home() / ".vbot-tui-state"
        self.path = root / f"{hashlib.sha256(self.server.encode()).hexdigest()}.json"
        self._known: dict[str, JsonObject] = {}

    def _read(self) -> JsonObject:
        result = load_validated_json_file(
            self.path,
            _validate,
            missing_ok=True,
            missing_default={"format_version": 1, "server": self.server, "drafts": {}},
        )
        if result["server"] != self.server:
            raise JsonConfigValidationError(
                "Stored drafts belong to another server; the file is unchanged."
            )
        return cast(JsonObject, strip_unknown_fields(result, _SHAPE))

    def load(self) -> dict[str, JsonObject]:
        self._known = copy.deepcopy(self._read()["drafts"])
        return copy.deepcopy(self._known)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix(".lock").open("a+b") as handle:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                handle.seek(0)
                if sys.platform == "win32":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def save(self, records: dict[str, JsonObject]) -> dict[str, str]:
        """Persist a captured local snapshot; return ids forked due to concurrent edits."""
        if records == self._known:
            return {}
        remapped: dict[str, str] = {}
        with self._locked():
            document = self._read()
            current = document["drafts"]
            for identifier in self._known.keys() - records.keys():
                if current.get(identifier) == self._known[identifier]:
                    current.pop(identifier, None)
            for identifier, record in records.items():
                if record == self._known.get(identifier):
                    continue
                destination = identifier
                if identifier in current and current[identifier] != self._known.get(identifier):
                    destination = uuid4().hex
                    remapped[identifier] = destination
                current[destination] = copy.deepcopy(record)
            write_json_document(self.path, document, _FORMAT, mode=0o600)
        self._known = {
            remapped.get(identifier, identifier): copy.deepcopy(record)
            for identifier, record in records.items()
        }
        return remapped
