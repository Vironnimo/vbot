"""Append-only chat session persistence."""

from __future__ import annotations

import builtins
import json
import os
import re
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.chat.errors import ChatMessageValidationError, ChatSessionError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat.chat import ChatMessage

JsonObject = dict[str, Any]

TIMESTAMP_SUFFIX = "+00:00"
UTC_Z_SUFFIX = "Z"
SESSION_FILE_EXTENSION = ".jsonl"
SESSION_LINE_ENDING = "\n"
SESSION_LINE_ENDING_BYTES = b"\n"
SESSION_APPEND_FLAGS = os.O_APPEND | os.O_CREAT | os.O_WRONLY
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SKILL_CONTEXT_NOTE_PREFIX = "[skill-context] "
PARTIAL_THINKING_NOTE_PREFIX = "[partial-thinking] "
_TAIL_CHUNK_SIZE = 8192
_LOGGER = get_logger("sessions")


class ChatSession:
    """Append-only UTF-8 JSONL session file."""

    def __init__(self, path: Path) -> None:
        if path.suffix != SESSION_FILE_EXTENSION:
            raise ChatSessionError("session path must end with .jsonl")
        self.path = path
        self._pending_notes: deque[ChatMessage] = deque()
        self._defer_notes = False
        self._deferred_note_messages: list[ChatMessage] = []
        self._activated_skill_names: set[str] = set()
        self._activated_skill_contents: dict[str, str] = {}

    @classmethod
    def create(cls, sessions_dir: Path, session_id: str | None = None) -> ChatSession:
        """Create an empty session file under a sessions directory."""
        session_identifier = str(uuid.uuid4()) if session_id is None else session_id
        _validate_session_id(session_identifier)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_path = sessions_dir / f"{session_identifier}{SESSION_FILE_EXTENSION}"
        if session_path.exists():
            raise ChatSessionError(f"session already exists: {session_identifier}")
        session_path.touch()
        return cls(session_path)

    @property
    def id(self) -> str:
        """Return the session identifier derived from the JSONL filename."""
        return self.path.stem

    @property
    def sidecar_path(self) -> Path:
        """Return the JSON metadata sidecar path for this session."""
        return self.path.with_name(f"{self.path.stem}.meta.json")

    def append(self, message: ChatMessage) -> None:
        """Append one canonical message as a single JSONL line."""
        payload = json.dumps(message.to_dict(), ensure_ascii=False, separators=(",", ":"))
        line = (payload + SESSION_LINE_ENDING).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _append_bytes(self.path, line)
        except OSError as exc:
            raise ChatSessionError(f"failed to append message to session: {self.id}") from exc

    def begin_defer_notes(self) -> None:
        """Defer note persistence until tool-result messages have been appended."""
        self._defer_notes = True

    def flush_deferred_notes(self) -> None:
        """Persist deferred notes and stop note deferral mode."""
        deferred_notes = list(self._deferred_note_messages)
        self._deferred_note_messages.clear()
        self._defer_notes = False
        for note in deferred_notes:
            self.append(note)

    def add_note(self, content: str) -> None:
        """Persist a kernel-internal note and enqueue it for provider-request injection."""
        from core.chat.chat import ChatMessage

        note = ChatMessage.note(content)
        if self._defer_notes:
            self._deferred_note_messages.append(note)
        else:
            self.append(note)
        self._pending_notes.append(note)

    def drain_pending_notes(self) -> list[ChatMessage]:
        """Return all pending notes and clear the in-memory pending buffer."""
        notes = list(self._pending_notes)
        self._pending_notes.clear()
        return notes

    def activate_skill_context(self, name: str, data: JsonObject) -> JsonObject:
        """Store skill context once per session and return a result envelope."""
        from core.tools.tools import tool_success

        activated_contents = self._load_activated_skill_contents()
        if name in activated_contents:
            return tool_success(
                {
                    "content": (
                        f"Skill '{name}' was already activated in this session. "
                        "Skipping re-activation."
                    ),
                    "resources": [],
                    "already_active": True,
                }
            )

        content = data.get("content")
        resources = data.get("resources", [])
        if not isinstance(content, str):
            raise ChatSessionError("skill activation content must be a string")
        if not isinstance(resources, list):
            raise ChatSessionError("skill activation resources must be a list")

        self._activated_skill_names.add(name)
        self._activated_skill_contents[name] = content
        self._persist_skill_context_note(name, content)
        return tool_success({"content": content, "resources": list(resources)})

    def skill_context_messages(
        self,
        messages: list[ChatMessage] | None = None,
    ) -> list[JsonObject]:
        """Return currently activated skill context as provider request messages.

        Callers that already hold this session's loaded messages may pass them
        to avoid a second full session read.
        """
        activated_contents = self._load_activated_skill_contents(messages)
        return [
            {"role": "user", "content": content}
            for _name, content in sorted(activated_contents.items())
        ]

    def _load_activated_skill_contents(
        self,
        preloaded_messages: list[ChatMessage] | None = None,
    ) -> dict[str, str]:
        if self._activated_skill_contents:
            return dict(self._activated_skill_contents)

        source_messages = self.load() if preloaded_messages is None else preloaded_messages
        activated_contents = _skill_contexts_from_messages(source_messages)
        self._activated_skill_names = set(activated_contents)
        self._activated_skill_contents = dict(activated_contents)
        return activated_contents

    def _persist_skill_context_note(self, name: str, content: str) -> None:
        from core.chat.chat import ChatMessage

        self.append(ChatMessage.note(_skill_context_note_content(name, content)))

    def bookend_timestamps(self) -> tuple[str, str] | None:
        """Return (first, last) message timestamps without loading the full session.

        Reads only the first and last complete JSONL lines. Returns None when
        the fast path cannot determine both timestamps (empty file, partial
        trailing write, unparseable bookend line); callers must then fall back
        to load(), which also handles partial-write recovery.
        """
        try:
            first_line = _read_first_complete_line(self.path)
            last_line = _read_last_complete_line(self.path)
        except OSError:
            return None
        if first_line is None or last_line is None:
            return None
        first_timestamp = _timestamp_from_line(first_line)
        last_timestamp = _timestamp_from_line(last_line)
        if first_timestamp is None or last_timestamp is None:
            return None
        return first_timestamp, last_timestamp

    def load(self) -> list[ChatMessage]:
        """Load all valid JSONL messages from this session file."""
        if not self.path.exists():
            raise ChatSessionError(f"session does not exist: {self.path}")

        messages: list[ChatMessage] = []
        with self.path.open("rb") as session_file:
            line_number = 0
            while True:
                line_start_offset = session_file.tell()
                line_bytes = session_file.readline()
                if line_bytes == b"":
                    break
                line_number += 1
                if not line_bytes.strip():
                    continue
                try:
                    messages.append(self._parse_line_bytes(line_bytes, line_number))
                except UnicodeDecodeError as exc:
                    if _is_unterminated_line(line_bytes):
                        self._truncate_partial_tail(
                            byte_offset=line_start_offset,
                            line_number=line_number,
                        )
                        break
                    raise ChatSessionError(f"invalid UTF-8 at line {line_number}") from exc
                except json.JSONDecodeError as exc:
                    if _is_unterminated_line(line_bytes):
                        self._truncate_partial_tail(
                            byte_offset=line_start_offset,
                            line_number=line_number,
                        )
                        break
                    raise ChatSessionError(f"invalid JSON at line {line_number}") from exc
        return messages

    def delete(self) -> None:
        """Delete the session file and metadata sidecar if they exist."""
        self.path.unlink(missing_ok=True)
        self.sidecar_path.unlink(missing_ok=True)

    @staticmethod
    def _parse_line_bytes(line: bytes, line_number: int) -> ChatMessage:
        data = json.loads(line.decode("utf-8"))
        return ChatSession._message_from_data(data, line_number)

    @staticmethod
    def _message_from_data(data: Any, line_number: int) -> ChatMessage:
        from core.chat.chat import ChatMessage

        if not isinstance(data, dict):
            raise ChatSessionError(f"message at line {line_number} must be an object")

        try:
            return ChatMessage.from_dict(data)
        except ChatMessageValidationError as exc:
            raise ChatSessionError(f"invalid message at line {line_number}: {exc}") from exc

    def _truncate_partial_tail(
        self,
        *,
        byte_offset: int,
        line_number: int,
    ) -> None:
        try:
            with self.path.open("r+b") as session_file:
                session_file.truncate(byte_offset)
                session_file.flush()
                os.fsync(session_file.fileno())
        except OSError as exc:
            raise ChatSessionError(
                f"failed to recover partial session write at line {line_number}"
            ) from exc
        _LOGGER.warning(
            "Recovered session %s by truncating partial JSONL line %s",
            self.id,
            line_number,
        )


class ChatSessionManager:
    """Manager for agent session files."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def sessions_dir(self, agent_id: str) -> Path:
        """Return the sessions directory for an agent."""
        if not agent_id:
            raise ChatSessionError("agent id must not be empty")
        return self.data_dir / "agents" / agent_id / "sessions"

    def create(self, agent_id: str, session_id: str | None = None) -> ChatSession:
        """Create a new session for an agent."""
        return ChatSession.create(self.sessions_dir(agent_id), session_id=session_id)

    def exists(self, agent_id: str, session_id: str) -> bool:
        """Return whether a valid session exists for an agent."""
        try:
            self.get(agent_id, session_id)
        except ChatSessionError:
            return False
        return True

    def get_or_create(self, agent_id: str, session_id: str) -> ChatSession:
        """Return an existing session handle or create a new one."""
        _validate_session_id(session_id)
        session_path = self.sessions_dir(agent_id) / f"{session_id}{SESSION_FILE_EXTENSION}"
        if session_path.exists():
            return ChatSession(session_path)
        return self.create(agent_id, session_id=session_id)

    def get(self, agent_id: str, session_id: str) -> ChatSession:
        """Return a session handle for an existing agent session."""
        _validate_session_id(session_id)
        session_path = self.sessions_dir(agent_id) / f"{session_id}{SESSION_FILE_EXTENSION}"
        if not session_path.exists():
            raise ChatSessionError(f"session does not exist: {session_id}")
        return ChatSession(session_path)

    def get_metadata(self, agent_id: str, session_id: str) -> JsonObject:
        """Load session metadata from sidecar JSON or return an empty object."""
        session = self.get(agent_id, session_id)
        return self._load_sidecar(session)

    def set_metadata(self, agent_id: str, session_id: str, data: dict[str, Any]) -> None:
        """Persist session metadata to sidecar JSON using atomic replace."""
        if not isinstance(data, dict):
            raise ChatSessionError("session metadata must be an object")

        session = self.get(agent_id, session_id)
        sidecar_path = session.sidecar_path
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = sidecar_path.with_name(f".{sidecar_path.name}.{uuid.uuid4().hex}.tmp")

        try:
            serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ChatSessionError("session metadata must be JSON-serializable") from exc

        try:
            temp_path.write_text(serialized, encoding="utf-8")
            os.replace(temp_path, sidecar_path)
        except OSError as exc:
            raise ChatSessionError(f"failed to write metadata for session: {session_id}") from exc
        finally:
            temp_path.unlink(missing_ok=True)

    def list(self, agent_id: str) -> list[ChatSession]:
        """List session handles for an agent sorted by filename."""
        sessions_dir = self.sessions_dir(agent_id)
        if not sessions_dir.exists():
            return []
        return [
            ChatSession(path)
            for path in sorted(sessions_dir.glob(f"*{SESSION_FILE_EXTENSION}"))
            if _is_valid_session_id(path.stem)
        ]

    def list_with_metadata(self, agent_id: str) -> builtins.list[dict[str, Any]]:
        """List sessions with activity timestamps plus merged sidecar metadata."""
        sessions_with_metadata: builtins.list[dict[str, Any]] = []
        for session in self.list(agent_id):
            created_at, last_active_at = self._activity_timestamps(session)
            metadata = self._load_sidecar(session)

            session_data: dict[str, Any] = dict(metadata)
            session_data["id"] = session.id
            session_data["created_at"] = created_at
            session_data["last_active_at"] = last_active_at
            sessions_with_metadata.append(session_data)
        return sessions_with_metadata

    def delete(self, agent_id: str, session_id: str) -> None:
        """Delete one agent session file."""
        self.get(agent_id, session_id).delete()

    def _load_sidecar(self, session: ChatSession) -> JsonObject:
        sidecar_path = session.sidecar_path
        if not sidecar_path.exists():
            return {}

        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ChatSessionError(f"failed to read metadata for session: {session.id}") from exc
        except json.JSONDecodeError as exc:
            raise ChatSessionError(f"invalid metadata JSON for session: {session.id}") from exc

        if not isinstance(data, dict):
            raise ChatSessionError(f"metadata for session must be an object: {session.id}")
        return dict(data)

    def _activity_timestamps(self, session: ChatSession) -> tuple[str, str]:
        bookends = session.bookend_timestamps()
        if bookends is not None:
            return bookends

        fallback_timestamp = self._file_mtime(session.path)
        messages = session.load()
        if not messages:
            return fallback_timestamp, fallback_timestamp
        return messages[0].timestamp, messages[-1].timestamp

    @staticmethod
    def _file_mtime(path: Path) -> str:
        try:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError as exc:
            raise ChatSessionError(f"failed to read file metadata: {path}") from exc
        return _format_timestamp(modified_at)


def _validate_session_id(session_id: str) -> None:
    if not _is_valid_session_id(session_id):
        raise ChatSessionError(
            "session id must be 1-128 characters of ASCII letters, digits, hyphen, "
            "or underscore and must not start with punctuation"
        )


def _is_valid_session_id(session_id: str) -> bool:
    return bool(SESSION_ID_PATTERN.fullmatch(session_id))


def _skill_context_note_content(name: str, content: str) -> str:
    return SKILL_CONTEXT_NOTE_PREFIX + json.dumps(
        {"name": name, "content": content},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def is_skill_context_note(message: ChatMessage) -> bool:
    """Return whether a note message stores activated skill context."""
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(SKILL_CONTEXT_NOTE_PREFIX)
    )


def is_partial_thinking_note(message: ChatMessage) -> bool:
    """Return whether a note holds partial thinking from an interrupted run."""
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(PARTIAL_THINKING_NOTE_PREFIX)
    )


def _skill_contexts_from_messages(messages: list[ChatMessage]) -> dict[str, str]:
    contexts: dict[str, str] = {}
    for message in messages:
        if not is_skill_context_note(message):
            continue
        content = message.content
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content.removeprefix(SKILL_CONTEXT_NOTE_PREFIX))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        name = payload.get("name")
        content = payload.get("content")
        if isinstance(name, str) and isinstance(content, str):
            contexts[name] = content
    return contexts


def _read_first_complete_line(path: Path) -> bytes | None:
    """Return the first non-blank, newline-terminated line, or None."""
    with path.open("rb") as session_file:
        for line in session_file:
            if not line.endswith(SESSION_LINE_ENDING_BYTES):
                return None
            if line.strip():
                return line
    return None


def _read_last_complete_line(path: Path) -> bytes | None:
    """Return the last non-blank, newline-terminated line via backward reads, or None.

    Returns None for an empty file or when the file does not end with a
    newline (a partial trailing write that load() recovery must handle).
    """
    with path.open("rb") as session_file:
        session_file.seek(0, os.SEEK_END)
        file_size = session_file.tell()
        if file_size == 0:
            return None
        session_file.seek(file_size - 1)
        if session_file.read(1) != SESSION_LINE_ENDING_BYTES:
            return None

        buffer = b""
        position = file_size
        while position > 0:
            read_size = min(_TAIL_CHUNK_SIZE, position)
            position -= read_size
            session_file.seek(position)
            buffer = session_file.read(read_size) + buffer
            lines = buffer.split(SESSION_LINE_ENDING_BYTES)
            # The buffer's first segment may continue an earlier, unread line.
            candidates = lines if position == 0 else lines[1:]
            for line in reversed(candidates):
                if line.strip():
                    return line + SESSION_LINE_ENDING_BYTES
    return None


def _timestamp_from_line(line: bytes) -> str | None:
    """Extract the timestamp field from one JSONL message line, or None."""
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    timestamp = data.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        return None
    return timestamp


def _append_bytes(path: Path, data: bytes) -> None:
    file_descriptor = os.open(path, SESSION_APPEND_FLAGS, 0o600)
    try:
        _write_all(file_descriptor, data)
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)


def _write_all(file_descriptor: int, data: bytes) -> None:
    written_bytes = 0
    while written_bytes < len(data):
        chunk_bytes = os.write(file_descriptor, data[written_bytes:])
        if chunk_bytes == 0:
            raise OSError("session append wrote zero bytes")
        written_bytes += chunk_bytes


def _is_unterminated_line(line: bytes) -> bool:
    return not line.endswith(SESSION_LINE_ENDING_BYTES)


def _format_timestamp(timestamp: datetime | None) -> str:
    value = datetime.now(UTC) if timestamp is None else timestamp.astimezone(UTC)
    return value.isoformat().replace(UTC_Z_SUFFIX, TIMESTAMP_SUFFIX)
