"""Append-only chat session persistence."""

from __future__ import annotations

import asyncio
import builtins
import contextvars
import json
import os
import re
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from core.chat.errors import ChatMessageValidationError, ChatSessionError
from core.projects.store import project_sessions_dir
from core.settings import is_valid_agent_id
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat.chat import ChatMessage

JsonObject = dict[str, Any]

TIMESTAMP_SUFFIX = "+00:00"
UTC_Z_SUFFIX = "Z"
SESSION_FILE_EXTENSION = ".jsonl"
CONTINUATION_FILE_SUFFIX = ".continuation.jsonl"
SESSION_LINE_ENDING = "\n"
SESSION_LINE_ENDING_BYTES = b"\n"
SESSION_APPEND_FLAGS = os.O_APPEND | os.O_CREAT | os.O_WRONLY
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
# Sidecar key holding a session's user-facing display title. A safety cap only:
# the title is single-line and the UI ellipsizes, so this just bounds absurd
# input, it is not a meaningful length limit.
SESSION_TITLE_KEY = "title"
SESSION_TITLE_MAX_LENGTH = 200
# Sidecar key recording a forked session's provenance: which source session it was
# copied from and the fork point. Written on every fork (even when the source had no
# sidecar) so a fork is self-describing.
FORK_SOURCE_META_KEY = "fork_source"
# Fork strip policy, owned beside the fork primitive. A fork is a plain, unbound
# session: it must never inherit a channel binding, a sub-agent parent linkage, or
# accumulated reflection cadence counters. The key names are literals on purpose —
# the writing domains (channels engine, sub-agent coordinator, reflection service)
# own the authoritative constants, and importing them here would cycle back through
# ``core/chat``; drift is guarded by tests comparing against those constants.
SESSION_FORK_ALWAYS_STRIP_META_KEYS = frozenset(
    {
        "source_channel_id",
        "platform",
        "platform_conv_id",
        "last_reply_target",
        "is_subagent_session",
        "subagent_parent",
        "reflection_counters",
    }
)
# Additionally stripped when a fork re-homes to a *different* agent: the pinned
# skill catalog and its seen-skills set belong to the source agent's skill pool,
# so a different agent must re-pin its own catalog on the fork's first run. A
# same-agent fork (e.g. ``/reflect``) deliberately keeps them, so the fork stays
# prompt-cache-warm against the source.
SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS = frozenset({"pinned_skill_catalog", "seen_skills"})
# Strip policy for the ``/agent`` move, which is cross-agent by definition (a
# same-pair move is refused before any relocation): the cross-agent skill keys
# above, plus the visited-projects record — the recorded visit injections were
# made for the source agent's runs, so the destination agent must re-trigger
# them fresh. Same literal-key rationale as the fork sets: ``visited_projects``
# is owned by ``core/chat`` and importing it here would cycle.
SESSION_MOVE_STRIP_META_KEYS = SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS | frozenset(
    {"visited_projects"}
)
SKILL_CONTEXT_NOTE_PREFIX = "[skill-context] "
# The ``skill`` tool's message name and its fresh-activation status, matched as
# literals when scanning tool-result carriers (same rationale as the strip-key
# literals above: importing the tool module here would cycle through core/tools
# into the skills domain). Drift is guarded by tests against the tool's constants.
SKILL_TOOL_MESSAGE_NAME = "skill"
SKILL_TOOL_LOADED_STATUS = "loaded"
CHANNEL_MESSAGE_NOTE_PREFIX = "[channel-message] "
SKILL_AVAILABLE_NOTE_PREFIX = "[skill-available] "
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

    @property
    def continuation_path(self) -> Path:
        """Return the append-only continuation journal path for this session."""
        return self.path.with_name(f"{self.path.stem}{CONTINUATION_FILE_SUFFIX}")

    def append(self, message: ChatMessage) -> None:
        """Append one canonical message as a single JSONL line."""
        payload = json.dumps(message.to_dict(), ensure_ascii=False, separators=(",", ":"))
        line = (payload + SESSION_LINE_ENDING).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _append_bytes(self.path, line)
        except OSError as exc:
            raise ChatSessionError(f"failed to append message to session: {self.id}") from exc

    def append_continuation_record(self, record: JsonObject) -> None:
        """Append one compact object to the continuation journal and fsync it."""
        self.append_continuation_records([record])

    def append_continuation_records(self, records: list[JsonObject]) -> None:
        """Append one journal batch through a single append+fsync operation."""
        if not records:
            return
        encoded_lines: list[bytes] = []
        for record in records:
            if not isinstance(record, dict):
                raise ChatSessionError("continuation record must be an object")
            try:
                payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise ChatSessionError("continuation record must be JSON-serializable") from exc
            encoded_lines.append((payload + SESSION_LINE_ENDING).encode("utf-8"))
        self.continuation_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _append_bytes(self.continuation_path, b"".join(encoded_lines))
        except OSError as exc:
            raise ChatSessionError(
                f"failed to append continuation record for session: {self.id}"
            ) from exc

    def load_continuation_records(self) -> list[JsonObject]:
        """Load ordered continuation records, repairing only a torn final line."""
        path = self.continuation_path
        if not path.exists():
            return []
        records: list[JsonObject] = []
        with path.open("rb") as journal_file:
            line_number = 0
            while True:
                line_start_offset = journal_file.tell()
                line_bytes = journal_file.readline()
                if line_bytes == b"":
                    break
                line_number += 1
                if not line_bytes.strip():
                    continue
                try:
                    data = json.loads(line_bytes.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    if _is_unterminated_line(line_bytes):
                        self._truncate_continuation_tail(
                            byte_offset=line_start_offset,
                            line_number=line_number,
                        )
                        break
                    kind = "UTF-8" if isinstance(exc, UnicodeDecodeError) else "JSON"
                    raise ChatSessionError(
                        f"invalid continuation {kind} at line {line_number}"
                    ) from exc
                if not isinstance(data, dict):
                    raise ChatSessionError(
                        f"continuation record at line {line_number} must be an object"
                    )
                records.append(dict(data))
        return records

    def clear_continuation(self) -> None:
        """Remove the disposable continuation journal if it exists."""
        self.continuation_path.unlink(missing_ok=True)

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

    def register_skill_activation(self, name: str, content: str) -> bool:
        """Record a skill activation; return ``False`` when it was already active.

        Dedup seam for the ``skill`` tool: the tool result itself is the durable
        content carrier, so nothing is persisted here. The in-memory record keeps
        ``activated_skill_contents`` complete for same-process consumers (the
        post-compaction rebuild) before the tool message reaches the file.
        """
        activated_contents = self._load_activated_skill_contents()
        if name in activated_contents:
            return False
        self._activated_skill_names.add(name)
        self._activated_skill_contents[name] = content
        return True

    def activate_skill_context(self, name: str, data: JsonObject) -> bool:
        """Persist a user-triggered skill activation; ``False`` when already active.

        The trigger path's carrier: a ``[skill-context] `` note appended at the
        activation point (right after the triggering user message), rendered in
        place as a ``<skill_content>`` context message at request build.
        """
        content = data.get("content")
        resources = data.get("resources", [])
        if not isinstance(content, str):
            raise ChatSessionError("skill activation content must be a string")
        if not isinstance(resources, list):
            raise ChatSessionError("skill activation resources must be a list")

        if not self.register_skill_activation(name, content):
            return False
        self._persist_skill_context_note(name, content)
        return True

    def activated_skill_contents(
        self,
        messages: list[ChatMessage] | None = None,
    ) -> dict[str, str]:
        """Return activated skill contents by name, in activation order.

        Scans both carriers — trigger notes and ``skill`` tool results. Callers
        that already hold this session's loaded messages may pass them to avoid
        a second full session read.
        """
        return self._load_activated_skill_contents(messages)

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
        """Delete the session file and both sidecars if they exist."""
        self.path.unlink(missing_ok=True)
        self.sidecar_path.unlink(missing_ok=True)
        self.clear_continuation()

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

    def _truncate_continuation_tail(self, *, byte_offset: int, line_number: int) -> None:
        try:
            with self.continuation_path.open("r+b") as journal_file:
                journal_file.truncate(byte_offset)
                journal_file.flush()
                os.fsync(journal_file.fileno())
        except OSError as exc:
            raise ChatSessionError(
                f"failed to recover partial continuation write at line {line_number}"
            ) from exc
        _LOGGER.warning(
            "Recovered session %s continuation journal by truncating partial JSONL line %s",
            self.id,
            line_number,
        )


class _SessionWriteLock:
    """Context-reentrant async lock guarding one session transcript's appends.

    Reentrant for the holder *and any child tasks it spawns while holding the
    lock*, so a Run that holds the lock across its tool cycle can run a tool (for
    example ``channel_send``) that targets its own session without
    self-deadlocking — even though the tool executor runs each tool call in its
    own ``asyncio.create_task``. Ownership is tracked through a ``ContextVar``
    rather than the running task: ``asyncio`` copies the holder's context into
    those child tasks at creation, so they inherit the reentrancy depth and nest
    instead of blocking on the held lock. Keying on ``current_task()`` instead
    would deadlock here, because the tool runs in a different task than the one
    that acquired the lock.

    A task from an unrelated context chain (a channel observe worker, an RPC
    handler, a Run on another accessor) is not a child of the holder and does not
    inherit the depth, so it blocks until the owner releases — which is what keeps
    an out-of-band note from splitting an open tool cycle.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Per-instance reentrancy depth. A ContextVar so child tasks spawned by
        # the holder inherit the depth (contexts are copied at task creation) and
        # re-enter, while unrelated tasks see the default 0 and contend normally.
        self._depth: contextvars.ContextVar[int] = contextvars.ContextVar(
            "session_write_lock_depth", default=0
        )

    async def __aenter__(self) -> _SessionWriteLock:
        depth = self._depth.get()
        if depth > 0:
            self._depth.set(depth + 1)
            return self
        await self._lock.acquire()
        self._depth.set(1)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        depth = self._depth.get()
        self._depth.set(depth - 1)
        if depth == 1:
            self._lock.release()


class ChatSessionManager:
    """Manager for agent session files."""

    # Per-session-file append locks, shared process-wide. The manager is
    # constructed per-call in some paths (e.g. AgentStore), so this coordination
    # state must live on the class, not the instance, for every writer to a given
    # session to serialize against the others. A Run holds the lock across its
    # tool cycle (assistant tool-call message through its tool results) and every
    # out-of-band writer (channel observed notes, session.link_channel,
    # channel_send) acquires it, so a note can never split a tool cycle. Entries
    # are never reaped: one lock per session file ever written is negligible.
    _write_locks: ClassVar[dict[str, _SessionWriteLock]] = {}

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def sessions_dir(self, agent_id: str, project_id: str | None = None) -> Path:
        """Return the sessions directory for an agent.

        ``project_id=None`` keeps the global identity layout
        ``agents/<agent-id>/sessions/``. A set ``project_id`` resolves the
        project-anchor layout ``projects/<project-id>/agents/<agent-id>/sessions/``
        through :func:`core.projects.store.project_sessions_dir`, so the
        anchor path stays defined in one place (the projects domain).
        """
        _validate_agent_id(agent_id)
        if project_id is None:
            return self.data_dir / "agents" / agent_id / "sessions"
        return project_sessions_dir(self.data_dir, project_id, agent_id)

    def write_lock(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> _SessionWriteLock:
        """Return the shared append lock for one session's transcript file.

        Hold it around any append that must stay contiguous with neighbouring
        appends — see the class note. Single one-off appends still acquire it so
        they wait for an open tool cycle on the same session instead of splitting
        it. The lock is keyed by the resolved transcript path, so a global and a
        project-scoped session sharing one session id resolve to different
        locks — the project anchor is part of the resolved path.
        """
        _validate_session_id(session_id)
        session_path = (
            self.sessions_dir(agent_id, project_id) / f"{session_id}{SESSION_FILE_EXTENSION}"
        )
        key = str(session_path.resolve())
        lock = ChatSessionManager._write_locks.get(key)
        if lock is None:
            lock = _SessionWriteLock()
            ChatSessionManager._write_locks[key] = lock
        return lock

    def create(
        self, agent_id: str, session_id: str | None = None, project_id: str | None = None
    ) -> ChatSession:
        """Create a new session for an agent."""
        return ChatSession.create(self.sessions_dir(agent_id, project_id), session_id=session_id)

    def exists(self, agent_id: str, session_id: str, project_id: str | None = None) -> bool:
        """Return whether a valid session exists for an agent."""
        try:
            self.get(agent_id, session_id, project_id)
        except ChatSessionError:
            return False
        return True

    def get_or_create(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> ChatSession:
        """Return an existing session handle or create a new one."""
        _validate_session_id(session_id)
        session_path = (
            self.sessions_dir(agent_id, project_id) / f"{session_id}{SESSION_FILE_EXTENSION}"
        )
        if session_path.exists():
            return ChatSession(session_path)
        return self.create(agent_id, session_id=session_id, project_id=project_id)

    def get(self, agent_id: str, session_id: str, project_id: str | None = None) -> ChatSession:
        """Return a session handle for an existing agent session."""
        _validate_session_id(session_id)
        session_path = (
            self.sessions_dir(agent_id, project_id) / f"{session_id}{SESSION_FILE_EXTENSION}"
        )
        if not session_path.exists():
            raise ChatSessionError(f"session does not exist: {session_id}")
        return ChatSession(session_path)

    def get_metadata(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> JsonObject:
        """Load session metadata from sidecar JSON or return an empty object."""
        session = self.get(agent_id, session_id, project_id)
        return self._load_sidecar(session)

    def set_metadata(
        self,
        agent_id: str,
        session_id: str,
        data: dict[str, Any],
        project_id: str | None = None,
    ) -> None:
        """Persist session metadata to sidecar JSON using atomic replace."""
        if not isinstance(data, dict):
            raise ChatSessionError("session metadata must be an object")

        session = self.get(agent_id, session_id, project_id)
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

    def set_title(
        self,
        agent_id: str,
        session_id: str,
        title: str,
        project_id: str | None = None,
    ) -> str | None:
        """Set or clear a session's display title in its metadata sidecar.

        The single seam every titling path goes through — the rename RPC, the
        ``/rename`` command, and any later automatic titling all call this, so
        the rule lives in one place. The title is collapsed to a single trimmed
        line and capped at :data:`SESSION_TITLE_MAX_LENGTH`; a blank result
        clears any stored title, so the session falls back to its automatic
        display. Returns the stored title, or ``None`` when cleared.

        Last writer wins: the sidecar is rewritten through :meth:`set_metadata`'s
        atomic replace, so concurrent renames never corrupt it. Touches only the
        sidecar (never the transcript), so it needs no :meth:`write_lock`.
        """
        normalized_title = _normalize_session_title(title)
        metadata = dict(self.get_metadata(agent_id, session_id, project_id))
        if normalized_title is None:
            metadata.pop(SESSION_TITLE_KEY, None)
        else:
            metadata[SESSION_TITLE_KEY] = normalized_title
        self.set_metadata(agent_id, session_id, metadata, project_id)
        return normalized_title

    async def move(
        self,
        source_agent_id: str,
        session_id: str,
        target_agent_id: str,
        *,
        source_project_id: str | None = None,
        target_project_id: str | None = None,
        strip_meta_keys: frozenset[str] = frozenset(),
    ) -> ChatSession:
        """Relocate a session's transcript and sidecars to another home.

        Storage-only: this neither resets any "current" pointer nor touches
        derived indexes — the caller owns those, so the sessions domain stays
        free of chat/recall imports. ``strip_meta_keys`` is taken as a parameter
        for the same reason: the caller passes chat-owned keys (e.g. the
        visited-projects key) without this module importing a chat constant.

        Ordering is crash-safe. The transcript (``.jsonl``, which alone defines a
        session's existence to :meth:`list`) is relocated first with
        :func:`os.replace` (atomic per file on one filesystem); then the sidecar
        is written at the destination with ``strip_meta_keys`` removed; then the
        source sidecar remnant is deleted. A crash between steps never loses the
        conversation — the worst case is an orphan source sidecar, invisible to
        :meth:`list`. The source ``write_lock`` is held so an in-flight contiguous
        append cannot interleave, but the real guarantee that nothing recreates
        the source file is the caller's quiescence precondition (no active or
        queued run), not this lock.
        """
        _validate_session_id(session_id)
        async with self.write_lock(source_agent_id, session_id, source_project_id):
            source = self.get(source_agent_id, session_id, source_project_id)
            destination_dir = self.sessions_dir(target_agent_id, target_project_id)
            destination_path = destination_dir / f"{session_id}{SESSION_FILE_EXTENSION}"
            if destination_path.exists():
                raise ChatSessionError(f"destination session already exists: {session_id}")

            source_sidecar = source.sidecar_path
            source_continuation = source.continuation_path
            had_sidecar = source_sidecar.exists()
            had_continuation = source_continuation.exists()
            sidecar_data = self._load_sidecar(source)

            destination_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(source.path, destination_path)
            except OSError as exc:
                raise ChatSessionError(f"failed to move session transcript: {session_id}") from exc

            if had_sidecar:
                stripped = {
                    key: value for key, value in sidecar_data.items() if key not in strip_meta_keys
                }
                self.set_metadata(target_agent_id, session_id, stripped, target_project_id)
                source_sidecar.unlink(missing_ok=True)

            if had_continuation:
                destination_continuation = destination_path.with_name(
                    f"{session_id}{CONTINUATION_FILE_SUFFIX}"
                )
                try:
                    os.replace(source_continuation, destination_continuation)
                except OSError as exc:
                    raise ChatSessionError(
                        f"failed to move continuation journal: {session_id}"
                    ) from exc

            return ChatSession(destination_path)

    async def fork(
        self,
        source_agent_id: str,
        session_id: str,
        *,
        target_agent_id: str | None = None,
        source_project_id: str | None = None,
        target_project_id: str | None = None,
        strip_meta_keys: frozenset[str] = frozenset(),
    ) -> ChatSession:
        """Copy a session 1:1 into a fresh id, stamping fork provenance.

        Unlike :meth:`move`, the source is left completely untouched — no
        ``os.replace``, no sidecar deletion — so a fork is a pure read of the
        source plus two writes at the destination. The source ``write_lock`` is
        held for the whole copy, so the snapshot lands on a message boundary: a
        Run holds that lock across its tool cycle, so the copy blocks until an
        open cycle completes instead of capturing it half-written — the fork
        needs no separate run-quiescence precondition (see the class note).

        ``target_agent_id=None`` forks within the source's own agent. The fork
        receives a fresh session id generated exactly like
        :meth:`ChatSession.create` (a v4 UUID). Its metadata starts from the
        source sidecar (empty when the source had none), drops
        ``strip_meta_keys`` (caller-owned policy, so the sessions domain imports
        no chat/channel constant, same reasoning as :meth:`move`), and always
        gains the ``fork_source`` provenance key recording the source
        ``(agent, session, project)``, the fork timestamp, and the copied
        message count — so a fork is self-describing even when the source
        carried no sidecar.
        """
        _validate_session_id(session_id)
        destination_agent_id = target_agent_id or source_agent_id
        async with self.write_lock(source_agent_id, session_id, source_project_id):
            source = self.get(source_agent_id, session_id, source_project_id)
            destination_dir = self.sessions_dir(destination_agent_id, target_project_id)
            fork_session = ChatSession.create(destination_dir)

            try:
                transcript_bytes = source.path.read_bytes()
                fork_session.path.write_bytes(transcript_bytes)
            except OSError as exc:
                fork_session.delete()
                raise ChatSessionError(f"failed to copy session transcript: {session_id}") from exc
            message_count = transcript_bytes.count(SESSION_LINE_ENDING_BYTES)

            forked_metadata = {
                key: value
                for key, value in self._load_sidecar(source).items()
                if key not in strip_meta_keys
            }
            forked_metadata[FORK_SOURCE_META_KEY] = {
                "agent_id": source_agent_id,
                "session_id": session_id,
                "project_id": source_project_id,
                "forked_at": _format_timestamp(datetime.now(UTC)),
                "message_count": message_count,
            }
            self.set_metadata(
                destination_agent_id, fork_session.id, forked_metadata, target_project_id
            )
            return fork_session

    def list(self, agent_id: str, project_id: str | None = None) -> list[ChatSession]:
        """List session handles for an agent sorted by filename."""
        sessions_dir = self.sessions_dir(agent_id, project_id)
        if not sessions_dir.exists():
            return []
        return [
            ChatSession(path)
            for path in sorted(sessions_dir.glob(f"*{SESSION_FILE_EXTENSION}"))
            if _is_valid_session_id(path.stem)
        ]

    def list_with_metadata(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """List sessions with activity timestamps plus merged sidecar metadata."""
        sessions_with_metadata: builtins.list[dict[str, Any]] = []
        for session in self.list(agent_id, project_id):
            created_at, last_active_at = self._activity_timestamps(session)
            metadata = self._load_sidecar(session)

            session_data: dict[str, Any] = dict(metadata)
            session_data["id"] = session.id
            session_data["created_at"] = created_at
            session_data["last_active_at"] = last_active_at
            sessions_with_metadata.append(session_data)
        return sessions_with_metadata

    def delete(self, agent_id: str, session_id: str, project_id: str | None = None) -> None:
        """Hard-delete one agent session's transcript and sidecars.

        Low-level primitive (unlink transcript + sidecar). The session-deletion
        feature does not call this — it archives via :meth:`archive` so a
        removed session stays recoverable. Kept as the genuine "remove the
        files" capability that tests and staleness cleanup use.
        """
        self.get(agent_id, session_id, project_id).delete()

    async def archive(self, agent_id: str, session_id: str, project_id: str | None = None) -> Path:
        """Archive one session's transcript and sidecars instead of deleting them.

        The deletion feature's storage step: mirrors ``AgentStore``/
        ``ProjectStore`` by moving the transcript and sidecar under
        ``<data_dir>/archive/sessions/`` (recoverable by hand, no in-app
        restore) rather than unlinking them. An existing archive for the same id
        is replaced. Returns the archive directory.

        Crash-safety mirrors :meth:`move`: holds the source ``write_lock`` and
        moves the transcript first with :func:`os.replace` (atomic per file), so
        an out-of-band note append cannot interleave. The caller's quiescence
        precondition (no active or queued run on the session) is the real guard
        that nothing recreates the file mid-archive.
        """
        _validate_session_id(session_id)
        async with self.write_lock(agent_id, session_id, project_id):
            source = self.get(agent_id, session_id, project_id)
            archive_dir = self._archive_dir(agent_id, project_id)
            archive_dir.mkdir(parents=True, exist_ok=True)
            archived_transcript = archive_dir / source.path.name
            archived_sidecar = archive_dir / source.sidecar_path.name
            archived_continuation = archive_dir / source.continuation_path.name

            # Replace any prior archive for the same id (mirrors agent/project).
            archived_transcript.unlink(missing_ok=True)
            archived_sidecar.unlink(missing_ok=True)
            archived_continuation.unlink(missing_ok=True)

            had_sidecar = source.sidecar_path.exists()
            had_continuation = source.continuation_path.exists()
            try:
                os.replace(source.path, archived_transcript)
            except OSError as exc:
                raise ChatSessionError(
                    f"failed to archive session transcript: {session_id}"
                ) from exc
            if had_sidecar:
                os.replace(source.sidecar_path, archived_sidecar)
            if had_continuation:
                os.replace(source.continuation_path, archived_continuation)
            return archive_dir

    def _archive_dir(self, agent_id: str, project_id: str | None) -> Path:
        """Return the archive directory for one agent's deleted sessions.

        Mirrors the live layout beneath a dedicated archive root so a hand
        restore knows the origin, and so it never collides with the agent
        archive (``archive/agents/<agent-id>/``) or the project archive
        (``archive/projects/<project-id>/``):

        - identity: ``archive/sessions/agents/<agent-id>/``
        - project:  ``archive/sessions/projects/<project-id>/agents/<agent-id>/``
        """
        _validate_agent_id(agent_id)
        root = self.data_dir / "archive" / "sessions"
        if project_id is None:
            return root / "agents" / agent_id
        return root / "projects" / project_id / "agents" / agent_id

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


def _validate_agent_id(agent_id: str) -> None:
    # The agent id becomes a path segment under ``agents/`` (live and archive), so a
    # separator or ``..`` component must never reach the filesystem. Defense-in-depth:
    # RPC entry already validates agent addresses, but the path-building choke point
    # enforces the bare-slug rule too, mirroring project-id and session-id handling.
    if not is_valid_agent_id(agent_id):
        raise ChatSessionError(
            "agent id must be 1-64 characters using only letters, numbers, hyphen, or underscore"
        )


def _validate_session_id(session_id: str) -> None:
    if not _is_valid_session_id(session_id):
        raise ChatSessionError(
            "session id must be 1-128 characters of ASCII letters, digits, hyphen, "
            "or underscore and must not start with punctuation"
        )


def _is_valid_session_id(session_id: str) -> bool:
    return bool(SESSION_ID_PATTERN.fullmatch(session_id))


def _normalize_session_title(title: str) -> str | None:
    """Collapse a raw title to one trimmed line and cap it; blank → None (clear).

    Runs of whitespace, including newlines, collapse to single spaces so a title
    is always one line. ``None`` means "no title" — clear any stored value.
    """
    if not isinstance(title, str):
        raise ChatSessionError("session title must be a string")
    collapsed = " ".join(title.split())
    if not collapsed:
        return None
    return collapsed[:SESSION_TITLE_MAX_LENGTH]


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


def skill_context_note_name(message: ChatMessage) -> str | None:
    """Return the skill name recorded in a skill-context note, or ``None``.

    A skill activation is persisted as a ``[skill-context] ``-prefixed note whose
    payload is the compact JSON ``{"name": ..., "content": ...}``. This exposes
    just the activated skill's name for consumers (e.g. usage statistics) that
    must not re-implement the prefix/JSON parse. Returns ``None`` for any message
    that is not a skill-context note or whose payload is malformed or nameless, so
    a corrupt line is skipped rather than raising.
    """
    payload = skill_context_note_payload(message)
    return payload[0] if payload is not None else None


def skill_context_note_payload(message: ChatMessage) -> tuple[str, str] | None:
    """Return ``(name, content)`` from a skill-context note, or ``None``.

    The single prefix/JSON parse for the trigger carrier: the request build uses
    it to render the note in place as a ``<skill_content>`` context message.
    Malformed or incomplete payloads yield ``None`` so a corrupt line is skipped
    rather than raising.
    """
    if not is_skill_context_note(message) or not isinstance(message.content, str):
        return None
    try:
        payload = json.loads(message.content.removeprefix(SKILL_CONTEXT_NOTE_PREFIX))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    name = payload.get("name")
    content = payload.get("content")
    if isinstance(name, str) and name and isinstance(content, str):
        return name, content
    return None


def skill_tool_activation(message: ChatMessage) -> tuple[str, str] | None:
    """Return ``(name, content)`` from a loading ``skill`` tool result, or ``None``.

    The tool carrier: a fresh ``skill`` tool activation persists the full skill
    content inside its success envelope (``data.status == "loaded"``). Already-
    active stubs, failures, list-mode results, and any malformed content yield
    ``None``. The tool name and envelope fields are matched as literals here —
    importing the tool module would pull the whole skills domain into sessions.
    """
    if message.role != "tool" or message.name != SKILL_TOOL_MESSAGE_NAME:
        return None
    if not isinstance(message.content, str):
        return None
    try:
        envelope = json.loads(message.content)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        return None
    data = envelope.get("data")
    if not isinstance(data, dict) or data.get("status") != SKILL_TOOL_LOADED_STATUS:
        return None
    name = data.get("name")
    content = data.get("content")
    if isinstance(name, str) and name and isinstance(content, str) and content:
        return name, content
    return None


def skill_tool_activation_name(message: ChatMessage) -> str | None:
    """Return the skill name from a loading ``skill`` tool result, or ``None``."""
    activation = skill_tool_activation(message)
    return activation[0] if activation is not None else None


def skill_activation_names(messages: list[ChatMessage]) -> frozenset[str]:
    """Return the names of every skill activation carried in *messages*.

    Covers both carriers (trigger notes and ``skill`` tool results). The chat
    loop uses it on the preserved compaction tail to avoid front-injecting a
    skill whose carrier already survives verbatim.
    """
    return frozenset(_skill_contexts_from_messages(messages))


def is_channel_message_note(message: ChatMessage) -> bool:
    """Return whether a note holds a passively observed channel message."""
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(CHANNEL_MESSAGE_NOTE_PREFIX)
    )


def is_skill_available_note(message: ChatMessage) -> bool:
    """Return whether a note announces skills that became available mid-session."""
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(SKILL_AVAILABLE_NOTE_PREFIX)
    )


def _skill_contexts_from_messages(messages: list[ChatMessage]) -> dict[str, str]:
    """Collect activated skill contents from both carriers, in activation order."""
    contexts: dict[str, str] = {}
    for message in messages:
        activation = skill_context_note_payload(message) or skill_tool_activation(message)
        if activation is not None:
            contexts.setdefault(activation[0], activation[1])
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
