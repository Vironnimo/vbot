"""Durable operator-surface state for interactive Terminal Sessions.

Owns the two persisted files under the data directory — user groups
(``groups.json``) and the manual launch history (``launch-history.json``) —
including their strict document parsing, the live in-memory group collection,
and the newest-first launch-history bookkeeping. Process/PTY lifecycle and
session state stay in :mod:`core.tools.terminal_manager`; this module never
touches adapters, renderers, or sessions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from core.config_validation import (
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    validate_json_file,
)
from core.json_documents import (
    JsonDocumentFormat,
    JsonDocumentWriteError,
    JsonShape,
    json_document,
    json_list,
    json_object,
    strip_unknown_fields,
    validate_format_version,
    warn_unknown_fields,
    write_json_document,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("tools.terminal_store")

TERMINAL_LAUNCH_HISTORY_FORMAT_VERSION = 1
TERMINAL_LAUNCH_HISTORY_MAX_ENTRIES = 50
TERMINAL_GROUPS_FORMAT_VERSION = 1
TERMINAL_GROUP_NAME_MAX_CHARS = 80
TERMINAL_FINISHED_GROUP_ID = "finished"
TERMINAL_MANUAL_GROUP_ID = "auto:manual"
TERMINAL_AGENT_GROUP_ID_PREFIX = "auto:agent:"

GroupKind = Literal["user", "agent", "automatic", "finished"]

_LAUNCH_HISTORY_ENTRY_FIELDS = frozenset(("id", "command", "args", "workdir", "used_at"))
_GROUP_FIELDS = frozenset(("id", "name", "order", "created_at"))
LAUNCH_HISTORY_SHAPE = json_document(
    {"entries"}, {"entries": json_list(json_object(_LAUNCH_HISTORY_ENTRY_FIELDS), key="id")}
)
GROUPS_SHAPE = json_document(
    {"groups"}, {"groups": json_list(json_object(_GROUP_FIELDS), key="id")}
)


@dataclass(frozen=True, slots=True)
class TerminalLaunchHistoryEntry:
    """One durable, most-recently-used manual Terminal launch."""

    id: str
    command: str | None
    arguments: tuple[str, ...]
    workdir: str | None
    used_at: datetime


@dataclass(slots=True)
class TerminalGroup:
    """One operator-visible collection of Terminal Sessions.

    ``kind`` decides durability and lifecycle:
    - ``user`` groups are persisted and stay when empty.
    - ``agent`` groups are created by the terminal Tool and live in memory.
    - ``automatic`` groups are synthesized per Agent and for manual terminals,
      never durable, and removed when empty.
    - ``finished`` is the single retention group for exited/error terminals.
    """

    group_id: str
    name: str
    kind: GroupKind
    order: list[str]
    created_at: datetime
    source: str | None = None


def agent_group_id(agent_id: str) -> str:
    """Return the automatic group id for one Agent owner."""
    return f"{TERMINAL_AGENT_GROUP_ID_PREFIX}{agent_id}"


def validate_group_name(name: str) -> str:
    """Return the trimmed group name or raise ``ValueError`` on invalid input."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Terminal group name must not be empty")
    if len(name.strip()) > TERMINAL_GROUP_NAME_MAX_CHARS:
        raise ValueError(
            f"Terminal group name must be at most {TERMINAL_GROUP_NAME_MAX_CHARS} characters"
        )
    return name.strip()


def launch_history_document(entries: Sequence[TerminalLaunchHistoryEntry]) -> list[dict[str, Any]]:
    """Serialize entries into the one shape shared by the file document and RPC."""
    return [
        {
            "id": entry.id,
            "command": entry.command,
            "args": list(entry.arguments),
            "workdir": entry.workdir,
            "used_at": entry.used_at.isoformat(),
        }
        for entry in entries
    ]


def launch_history_id(
    command: str | None,
    arguments: Sequence[str],
    workdir: str | None,
) -> str:
    """Return the stable content id that deduplicates identical launches."""
    encoded = json.dumps(
        {"command": command, "args": list(arguments), "workdir": workdir},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_launch_history(document: Any) -> list[TerminalLaunchHistoryEntry]:
    """Parse the modeled fields of a launch-history document.

    Unknown fields are ignored; any other deviation raises ``ValueError``.
    """
    document = _modeled_document(
        document, TERMINAL_LAUNCH_HISTORY_FORMAT_VERSION, LAUNCH_HISTORY_SHAPE, "launch history"
    )
    entries = document.get("entries")
    if not isinstance(entries, list) or len(entries) > TERMINAL_LAUNCH_HISTORY_MAX_ENTRIES:
        raise ValueError("Terminal launch history entries are invalid")

    parsed: list[TerminalLaunchHistoryEntry] = []
    seen_ids: set[str] = set()
    for raw_entry in entries:
        if not isinstance(raw_entry, dict) or set(raw_entry) != _LAUNCH_HISTORY_ENTRY_FIELDS:
            raise ValueError("Terminal launch history entry shape is invalid")
        entry_id = raw_entry["id"]
        command = raw_entry["command"]
        arguments = raw_entry["args"]
        workdir = raw_entry["workdir"]
        if not isinstance(entry_id, str) or not entry_id:
            raise ValueError("Terminal launch history id is invalid")
        if command is not None and (not isinstance(command, str) or not command):
            raise ValueError("Terminal launch history command is invalid")
        if not isinstance(arguments, list) or any(not isinstance(item, str) for item in arguments):
            raise ValueError("Terminal launch history arguments are invalid")
        if workdir is not None and (not isinstance(workdir, str) or not workdir):
            raise ValueError("Terminal launch history workdir is invalid")
        if entry_id != launch_history_id(command, arguments, workdir) or entry_id in seen_ids:
            raise ValueError("Terminal launch history id does not match its configuration")
        used_at = _parse_utc_timestamp(raw_entry["used_at"], what="launch history")
        seen_ids.add(entry_id)
        parsed.append(
            TerminalLaunchHistoryEntry(
                id=entry_id,
                command=command,
                arguments=tuple(arguments),
                workdir=workdir,
                used_at=used_at,
            )
        )
    return parsed


def parse_groups(document: Any) -> list[TerminalGroup]:
    """Parse the modeled fields of a user-groups document.

    Unknown fields are ignored; any other deviation raises ``ValueError``.
    """
    document = _modeled_document(document, TERMINAL_GROUPS_FORMAT_VERSION, GROUPS_SHAPE, "groups")
    raw_groups = document.get("groups")
    if not isinstance(raw_groups, list):
        raise ValueError("Terminal groups entries are invalid")

    parsed: list[TerminalGroup] = []
    seen_ids: set[str] = set()
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict) or set(raw_group) != _GROUP_FIELDS:
            raise ValueError("Terminal group shape is invalid")
        group_id = raw_group["id"]
        name = raw_group["name"]
        order = raw_group["order"]
        created_at = raw_group["created_at"]
        if not isinstance(group_id, str) or not group_id or group_id in seen_ids:
            raise ValueError("Terminal group id is invalid or duplicated")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Terminal group name is invalid")
        if not isinstance(order, list) or any(
            not isinstance(item, str) or not item for item in order
        ):
            raise ValueError("Terminal group order is invalid")
        parsed_at = _parse_utc_timestamp(created_at, what="group")
        seen_ids.add(group_id)
        parsed.append(
            TerminalGroup(
                group_id=group_id,
                name=name.strip(),
                kind="user",
                order=list(order),
                created_at=parsed_at,
            )
        )
    return parsed


def validate_terminal_launch_history_file(path: str | Path) -> JsonValidationReport:
    """Validate the optional persisted Terminal launch history without consuming it."""
    return validate_json_file(path, _validate_launch_history_document, missing_ok=True)


def validate_terminal_groups_file(path: str | Path) -> JsonValidationReport:
    """Validate the optional persisted Terminal groups without consuming it."""
    return validate_json_file(path, _validate_groups_document, missing_ok=True)


def _modeled_document(document: Any, version: int, shape: JsonShape, what: str) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError(f"Terminal {what} must be a JSON object")
    diagnostics: list[JsonDiagnostic] = []
    if not validate_format_version(diagnostics, document, version):
        raise ValueError(f"Terminal {what} format_version {diagnostics[0].message}")
    modeled: dict[str, Any] = strip_unknown_fields(document, shape)
    return modeled


def _document_diagnostics(
    data: Any,
    *,
    version: int,
    shape: JsonShape,
    parse: Callable[[Any], object],
) -> list[JsonDiagnostic]:
    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]
    diagnostics: list[JsonDiagnostic] = []
    if not validate_format_version(diagnostics, data, version):
        return diagnostics
    warn_unknown_fields(diagnostics, "$", data, shape)
    try:
        parse(data)
    except ValueError as error:
        add_error(diagnostics, "$", str(error))
    return diagnostics


def _validate_launch_history_document(data: Any) -> list[JsonDiagnostic]:
    return _document_diagnostics(
        data,
        version=TERMINAL_LAUNCH_HISTORY_FORMAT_VERSION,
        shape=LAUNCH_HISTORY_SHAPE,
        parse=parse_launch_history,
    )


def _validate_groups_document(data: Any) -> list[JsonDiagnostic]:
    return _document_diagnostics(
        data, version=TERMINAL_GROUPS_FORMAT_VERSION, shape=GROUPS_SHAPE, parse=parse_groups
    )


LAUNCH_HISTORY_FORMAT = JsonDocumentFormat(
    name="Terminal launch history",
    version=TERMINAL_LAUNCH_HISTORY_FORMAT_VERSION,
    shape=LAUNCH_HISTORY_SHAPE,
    validate=_validate_launch_history_document,
)
GROUPS_FORMAT = JsonDocumentFormat(
    name="Terminal groups",
    version=TERMINAL_GROUPS_FORMAT_VERSION,
    shape=GROUPS_SHAPE,
    validate=_validate_groups_document,
)


def _parse_utc_timestamp(value: Any, *, what: str) -> datetime:
    """Parse a strict UTC ISO timestamp; ``what`` names the document in errors."""
    if not isinstance(value, str):
        raise ValueError(f"Terminal {what} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Terminal {what} timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"Terminal {what} timestamp must be UTC")
    return parsed.astimezone(UTC)


class TerminalOperatorStore:
    """Own durable operator state: the group collection and the launch history.

    Both files load once at construction; a missing or unreadable file degrades
    to an empty collection with a warning instead of failing startup. Writes are
    atomic, keep the unknown fields of the file on disk, and never overwrite a
    file that fails to load: until it is repaired or removed, changes stay in
    memory and each write logs a warning.
    """

    def __init__(
        self,
        *,
        launch_history_path: Path | None,
        groups_path: Path | None,
        data_dir: Path | None = None,
    ) -> None:
        self._launch_history_path = launch_history_path
        self._groups_path = groups_path
        self._data_dir = data_dir
        self.launch_history: list[TerminalLaunchHistoryEntry] = []
        self.groups: dict[str, TerminalGroup] = {}
        self._load_launch_history()
        self._load_groups()

    def remember_launch(
        self,
        *,
        command: str | None,
        arguments: Sequence[str],
        workdir: str | None,
    ) -> None:
        """Record one manual launch at the front, deduplicated and capped."""
        entry = TerminalLaunchHistoryEntry(
            id=launch_history_id(command, arguments, workdir),
            command=command,
            arguments=tuple(arguments),
            workdir=workdir,
            used_at=datetime.now(UTC),
        )
        self.launch_history = [
            entry,
            *(item for item in self.launch_history if item.id != entry.id),
        ][:TERMINAL_LAUNCH_HISTORY_MAX_ENTRIES]
        self.persist_launch_history()

    def persist_launch_history(self) -> None:
        path = self._launch_history_path
        if path is None:
            return
        document = {"entries": launch_history_document(self.launch_history)}
        try:
            write_json_document(path, document, LAUNCH_HISTORY_FORMAT, data_dir=self._data_dir)
        except (JsonDocumentWriteError, OSError) as error:
            _LOGGER.warning("Could not persist Terminal launch history to '%s': %s", path, error)

    def persist_groups(self) -> None:
        path = self._groups_path
        if path is None:
            return
        document = {
            "groups": [
                {
                    "id": group.group_id,
                    "name": group.name,
                    "order": list(group.order),
                    "created_at": group.created_at.isoformat(),
                }
                for group in self.groups.values()
                if group.kind == "user"
            ],
        }
        try:
            write_json_document(path, document, GROUPS_FORMAT, data_dir=self._data_dir)
        except (JsonDocumentWriteError, OSError) as error:
            _LOGGER.warning("Could not persist Terminal groups to '%s': %s", path, error)

    def group_by_name(self, name: str) -> TerminalGroup | None:
        """Return the user or agent group with this case-insensitive name."""
        lowered = name.casefold()
        for group in self.groups.values():
            if group.kind in {"user", "agent"} and group.name.casefold() == lowered:
                return group
        return None

    def group_name_taken(self, name: str, *, exclude: str | None = None) -> bool:
        lowered = name.casefold()
        return any(
            group.kind in {"user", "agent"}
            and group.group_id != exclude
            and group.name.casefold() == lowered
            for group in self.groups.values()
        )

    def _load_launch_history(self) -> None:
        path = self._launch_history_path
        if path is None or not path.exists():
            return
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            self.launch_history = parse_launch_history(document)
        except (OSError, UnicodeError, ValueError) as error:
            _LOGGER.warning("Could not load Terminal launch history from '%s': %s", path, error)

    def _load_groups(self) -> None:
        path = self._groups_path
        if path is None or not path.exists():
            return
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            groups = parse_groups(document)
        except (OSError, UnicodeError, ValueError) as error:
            _LOGGER.warning("Could not load Terminal groups from '%s': %s", path, error)
            return
        self.groups = {group.group_id: group for group in groups}
