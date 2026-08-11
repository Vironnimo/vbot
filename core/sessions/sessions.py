"""Append-only chat session persistence."""

from __future__ import annotations

import asyncio
import builtins
import contextvars
import hashlib
import json
import os
import re
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from core.chat.errors import ChatMessageValidationError, ChatSessionError
from core.projects.store import project_sessions_dir
from core.runs import RunKind
from core.settings import is_valid_agent_id, is_valid_project_id
from core.skills.skills import format_skill_activation_context
from core.utils.atomic import atomic_write_text
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.chat.chat import ChatMessage

JsonObject = dict[str, Any]
_SessionIoResult = TypeVar("_SessionIoResult")

TIMESTAMP_SUFFIX = "+00:00"
UTC_Z_SUFFIX = "Z"
SESSION_FILE_EXTENSION = ".jsonl"
CONTINUATION_FILE_SUFFIX = ".continuation.jsonl"
SESSION_ACTIVITY_FILE_SUFFIX = ".activity.json"
SESSION_LINE_ENDING = "\n"
SESSION_LINE_ENDING_BYTES = b"\n"
SESSION_APPEND_FLAGS = os.O_APPEND | os.O_CREAT | os.O_WRONLY
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
# Sidecar key holding a session's user-facing display title. A safety cap only:
# the title is single-line and the UI ellipsizes, so this just bounds absurd
# input, it is not a meaningful length limit.
SESSION_TITLE_KEY = "title"
SESSION_AUTO_TITLE_KEY = "auto_title"
SESSION_AUTO_TITLE_INITIALIZED_KEY = "auto_title_initialized"
SESSION_TITLE_MAX_LENGTH = 200
SESSION_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
# Sidecar key recording a forked session's provenance: which source session it was
# copied from and the fork point. Written on every fork (even when the source had no
# sidecar) so a fork is self-describing.
FORK_SOURCE_META_KEY = "fork_source"
SESSION_RUN_KINDS_META_KEY = "run_kinds"
# Internal sidecar key for the Provider prompt-cache routing lineage. It is
# deliberately separate from Session identity: cache-compatible forks inherit
# it while keeping their own transcript, Run, and transport-continuation scope.
PROMPT_CACHE_AFFINITY_META_KEY = "prompt_cache_affinity_id"
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
        SESSION_RUN_KINDS_META_KEY,
    }
)
# Additionally stripped when a fork re-homes to a *different* agent: the pinned
# skill catalog and its seen-skills set belong to the source agent's skill pool,
# so a different agent must re-pin its own catalog on the fork's first run. A
# same-agent fork (e.g. ``/reflect``) deliberately keeps them, so the fork stays
# prompt-cache-warm against the source.
SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS = frozenset(
    {"pinned_skill_catalog", "seen_skills", PROMPT_CACHE_AFFINITY_META_KEY}
)
# A ``/agent`` move is cross-agent by definition, so it uses the same Agent-owned
# Skill-catalog strip set as a cross-Agent fork.
SESSION_MOVE_STRIP_META_KEYS = SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS
SKILL_CONTEXT_NOTE_PREFIX = "[skill-context] "
# The ``skill`` tool's message name and its fresh-activation status, matched as
# literals when scanning tool-result carriers (same rationale as the strip-key
# literals above: importing the tool module here would cycle through core/tools
# into the skills domain). Drift is guarded by tests against the tool's constants.
SKILL_TOOL_MESSAGE_NAME = "skill"
SKILL_TOOL_LOADED_STATUS = "loaded"
# The explicit Project Context Tool persists its successful selection as an ordinary
# Tool Result. Chat scans that carrier to recover the latest Project whose Skills
# should be available to an Identity Agent without adding parallel Session state.
PROJECT_TOOL_MESSAGE_NAME = "project"
PROJECT_TOOL_LOADED_STATUS = "loaded"
CHANNEL_MESSAGE_NOTE_PREFIX = "[channel-message] "
SKILL_AVAILABLE_NOTE_PREFIX = "[skill-available] "
_TAIL_CHUNK_SIZE = 8192
_LOGGER = get_logger("sessions")
SESSION_IO_WORKER_LIMIT = 8
_SESSION_IO_WORKERS = BoundedWorkerPool(
    name="session-io",
    max_workers=SESSION_IO_WORKER_LIMIT,
)


def _new_prompt_cache_affinity_id() -> str:
    """Return one opaque 128-bit prompt-cache lineage id."""
    return uuid.uuid4().hex


def _default_prompt_cache_affinity_id(
    agent_id: str,
    session_id: str,
    project_id: str | None,
) -> str:
    """Derive the stable initial lineage without creating a metadata sidecar."""
    address = json.dumps(
        [project_id, agent_id, session_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(address).hexdigest()[:32]


def _is_prompt_cache_affinity_id(value: Any) -> bool:
    """Return whether *value* is one canonical 128-bit lowercase hex id."""
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


@dataclass(frozen=True)
class SessionIdentityReferenceUpdate:
    """Exact sidecar snapshot changed by an Identity Agent rename."""

    agent_id: str
    session_id: str
    project_id: str | None
    previous_metadata: JsonObject


@dataclass(frozen=True)
class SessionReadCursor:
    """Opaque append cursor for incrementally reading canonical Session messages."""

    byte_offset: int
    message_count: int
    last_message_id: str | None


@dataclass(frozen=True)
class SessionReadBatch:
    """Validated messages appended after a matching :class:`SessionReadCursor`."""

    messages: tuple[ChatMessage, ...]
    cursor: SessionReadCursor


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
        self._state_lock = threading.RLock()

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

    @property
    def activity_path(self) -> Path:
        """Return the durable Run-completion/read-state sidecar path."""
        return self.path.with_name(f"{self.path.stem}{SESSION_ACTIVITY_FILE_SUFFIX}")

    def append(self, message: ChatMessage) -> None:
        """Append one canonical message as a single JSONL line."""
        self.append_many([message])

    def append_many(self, messages: list[ChatMessage]) -> None:
        """Append ordered canonical messages through one durable write."""
        if not messages:
            return
        encoded_lines = [
            (
                json.dumps(message.to_dict(), ensure_ascii=False, separators=(",", ":"))
                + SESSION_LINE_ENDING
            ).encode("utf-8")
            for message in messages
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _append_bytes(self.path, b"".join(encoded_lines))
        except OSError as exc:
            raise ChatSessionError(f"failed to append message to session: {self.id}") from exc

    async def append_async(self, message: ChatMessage) -> None:
        """Append one message without running durable filesystem I/O on the event loop."""
        await _run_session_io(self.append, message)

    async def append_many_async(self, messages: list[ChatMessage]) -> None:
        """Append one ordered batch without blocking the event loop."""
        if not messages:
            return
        await _run_session_io(self.append_many, list(messages))

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

    async def append_continuation_records_async(self, records: list[JsonObject]) -> None:
        """Append one continuation batch without blocking the event loop."""
        if not records:
            return
        await _run_session_io(self.append_continuation_records, list(records))

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
                    if _is_unterminated_line(line_bytes):
                        self._truncate_continuation_tail(
                            byte_offset=line_start_offset,
                            line_number=line_number,
                        )
                        break
                    raise ChatSessionError(
                        f"continuation record at line {line_number} must be an object"
                    )
                records.append(dict(data))
        return records

    async def load_continuation_records_async(self) -> list[JsonObject]:
        """Load continuation records without blocking the event loop."""
        return await _run_session_io(self.load_continuation_records)

    def clear_continuation(self) -> None:
        """Remove the disposable continuation journal if it exists."""
        self.continuation_path.unlink(missing_ok=True)

    async def clear_continuation_async(self) -> None:
        """Remove the continuation journal without blocking the event loop."""
        await _run_session_io(self.clear_continuation)

    def begin_defer_notes(self) -> None:
        """Defer note persistence until tool-result messages have been appended."""
        with self._state_lock:
            self._defer_notes = True

    def flush_deferred_notes(self) -> None:
        """Persist deferred notes and stop note deferral mode."""
        self.append_many(self._take_deferred_notes())

    async def flush_deferred_notes_async(self) -> None:
        """Persist deferred notes as one batch without blocking the event loop."""
        await self.append_many_async(self._take_deferred_notes())

    def take_deferred_notes(self) -> list[ChatMessage]:
        """Stop note deferral and return the ordered unpersisted notes."""
        return self._take_deferred_notes()

    def _take_deferred_notes(self) -> list[ChatMessage]:
        with self._state_lock:
            deferred_notes = list(self._deferred_note_messages)
            self._deferred_note_messages.clear()
            self._defer_notes = False
            return deferred_notes

    def add_note(self, content: str) -> None:
        """Persist a kernel-internal note and enqueue it for provider-request injection."""
        from core.chat.chat import ChatMessage

        note = ChatMessage.note(content)
        with self._state_lock:
            deferred = self._defer_notes
            if deferred:
                self._deferred_note_messages.append(note)
            self._pending_notes.append(note)
        if not deferred:
            self.append(note)

    async def add_note_async(self, content: str) -> None:
        """Persist one note without running durable filesystem I/O on the event loop."""
        from core.chat.chat import ChatMessage

        note = ChatMessage.note(content)
        with self._state_lock:
            deferred = self._defer_notes
            if deferred:
                self._deferred_note_messages.append(note)
            self._pending_notes.append(note)
        if not deferred:
            await self.append_async(note)

    def drain_pending_notes(self) -> list[ChatMessage]:
        """Return all pending notes and clear the in-memory pending buffer."""
        with self._state_lock:
            notes = list(self._pending_notes)
            self._pending_notes.clear()
            return notes

    def register_skill_activation(self, name: str, content: str) -> bool:
        """Record a Skill version; return ``False`` when identical content is active.

        Dedup seam for the ``skill`` tool: the tool result itself is the durable
        content carrier, so nothing is persisted here. The in-memory record keeps
        ``activated_skill_contents`` complete for same-process consumers (the
        post-compaction rebuild) before the tool message reaches the file. A changed
        package may activate again in the same Session; the latest content wins.
        """
        activated_contents = self._load_activated_skill_contents()
        with self._state_lock:
            if activated_contents.get(name) == content:
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
        content = data.get("activation_content")
        if not isinstance(content, str):
            raise ChatSessionError("skill activation content must be a string")

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
        with self._state_lock:
            if self._activated_skill_contents:
                return dict(self._activated_skill_contents)

        source_messages = self.load() if preloaded_messages is None else preloaded_messages
        activated_contents = _skill_contexts_from_messages(source_messages)
        with self._state_lock:
            if not self._activated_skill_contents:
                self._activated_skill_names = set(activated_contents)
                self._activated_skill_contents = dict(activated_contents)
            return dict(self._activated_skill_contents)

    def _persist_skill_context_note(self, name: str, content: str) -> None:
        self.add_note(_skill_context_note_content(name, content))

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
        batch = self._load_since(
            None,
            repair_partial_tail=_current_context_holds_session_write_lock(self.path),
        )
        if batch is None:  # A full read has no prefix cursor that can become stale.
            raise ChatSessionError(f"failed to read session: {self.path}")
        return list(batch.messages)

    async def load_async(self) -> list[ChatMessage]:
        """Load all canonical messages without blocking the event loop."""
        repair_partial_tail = _current_context_holds_session_write_lock(self.path)
        batch = await _run_session_io(self._load_since, None, repair_partial_tail)
        if batch is None:  # A full read has no prefix cursor that can become stale.
            raise ChatSessionError(f"failed to read session: {self.path}")
        return list(batch.messages)

    def load_since(self, cursor: SessionReadCursor | None = None) -> SessionReadBatch | None:
        """Load and validate only messages appended after *cursor*.

        ``None`` starts at the beginning. A supplied cursor is accepted only when
        its byte boundary still ends at the same last canonical Message id; a
        mismatching or truncated boundary returns ``None`` so a disposable read
        model can discard that Session's projection and rebuild it. The returned
        cursor always points immediately after the last complete line observed by
        this read.
        """
        return self._load_since(
            cursor,
            repair_partial_tail=_current_context_holds_session_write_lock(self.path),
        )

    def _load_since(
        self,
        cursor: SessionReadCursor | None,
        repair_partial_tail: bool,
    ) -> SessionReadBatch | None:
        if not self.path.exists():
            raise ChatSessionError(f"session does not exist: {self.path}")

        current_cursor = cursor or SessionReadCursor(
            byte_offset=0,
            message_count=0,
            last_message_id=None,
        )
        if not self._cursor_matches(current_cursor):
            return None

        messages: list[ChatMessage] = []
        with self.path.open("rb") as session_file:
            session_file.seek(current_cursor.byte_offset)
            line_number = current_cursor.message_count
            message_count = current_cursor.message_count
            last_message_id = current_cursor.last_message_id
            next_offset = current_cursor.byte_offset
            while True:
                line_start_offset = session_file.tell()
                line_bytes = session_file.readline()
                if line_bytes == b"":
                    next_offset = session_file.tell()
                    break
                line_number += 1
                if not line_bytes.strip():
                    next_offset = session_file.tell()
                    continue
                try:
                    message = self._parse_line_bytes(line_bytes, line_number)
                except UnicodeDecodeError as exc:
                    if _is_unterminated_line(line_bytes):
                        if repair_partial_tail:
                            self._truncate_partial_tail(
                                byte_offset=line_start_offset,
                                line_number=line_number,
                            )
                        next_offset = line_start_offset
                        break
                    raise ChatSessionError(f"invalid UTF-8 at line {line_number}") from exc
                except json.JSONDecodeError as exc:
                    if _is_unterminated_line(line_bytes):
                        if repair_partial_tail:
                            self._truncate_partial_tail(
                                byte_offset=line_start_offset,
                                line_number=line_number,
                            )
                        next_offset = line_start_offset
                        break
                    raise ChatSessionError(f"invalid JSON at line {line_number}") from exc
                except ChatSessionError:
                    if _is_unterminated_line(line_bytes):
                        if repair_partial_tail:
                            self._truncate_partial_tail(
                                byte_offset=line_start_offset,
                                line_number=line_number,
                            )
                        next_offset = line_start_offset
                        break
                    raise
                messages.append(message)
                message_count += 1
                last_message_id = message.id
                next_offset = session_file.tell()
        return SessionReadBatch(
            messages=tuple(messages),
            cursor=SessionReadCursor(
                byte_offset=next_offset,
                message_count=message_count,
                last_message_id=last_message_id,
            ),
        )

    async def load_since_async(
        self, cursor: SessionReadCursor | None = None
    ) -> SessionReadBatch | None:
        """Load one append-only delta without blocking the event loop."""
        repair_partial_tail = _current_context_holds_session_write_lock(self.path)
        return await _run_session_io(self._load_since, cursor, repair_partial_tail)

    def _cursor_matches(self, cursor: SessionReadCursor) -> bool:
        if (
            isinstance(cursor.byte_offset, bool)
            or not isinstance(cursor.byte_offset, int)
            or cursor.byte_offset < 0
            or isinstance(cursor.message_count, bool)
            or not isinstance(cursor.message_count, int)
            or cursor.message_count < 0
        ):
            return False
        if cursor.byte_offset == 0:
            return cursor.message_count == 0 and cursor.last_message_id is None
        if cursor.message_count == 0 or not cursor.last_message_id:
            return False
        try:
            line = _read_complete_line_ending_at(self.path, cursor.byte_offset)
        except OSError:
            return False
        return line is not None and _message_id_from_line(line) == cursor.last_message_id

    def delete(self) -> None:
        """Delete the session file and its sidecars if they exist."""
        self.path.unlink(missing_ok=True)
        self.sidecar_path.unlink(missing_ok=True)
        self.activity_path.unlink(missing_ok=True)
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


@dataclass
class _SessionWriteLease:
    """One live ownership lease shared with reentrant child contexts."""

    holders: int = 1
    active: bool = True


class _SessionWriteLock:
    """Context-reentrant async lock guarding one session transcript's appends.

    Reentrant for the holder *and any child tasks it spawns while holding the
    lock*, so a Run that holds the lock across its tool cycle can run a tool (for
    example ``channel_send``) that targets its own session without
    self-deadlocking — even though the tool executor runs each tool call in its
    own ``asyncio.create_task``. Ownership is tracked through a live lease in a
    ``ContextVar`` rather than the running task: ``asyncio`` copies the holder's
    context into those child tasks at creation, so they can nest while that lease
    still owns the underlying lock. Keying on ``current_task()`` instead would
    deadlock here, because the tool runs in a different task than the one that
    acquired the lock.

    A child that outlives the original holder keeps a copied reference to an
    *inactive* lease. It must acquire a new lease normally and therefore cannot
    bypass an unrelated writer that acquired the lock after the parent released
    it.

    A task from an unrelated context chain (a channel observe worker, an RPC
    handler, a Run on another accessor) is not a child of the holder and does not
    inherit the depth, so it blocks until the owner releases — which is what keeps
    an out-of-band note from splitting an open tool cycle.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._lease_stack: contextvars.ContextVar[tuple[_SessionWriteLease, ...]] = (
            contextvars.ContextVar("session_write_lock_leases", default=())
        )

    async def __aenter__(self) -> _SessionWriteLock:
        stack = self._lease_stack.get()
        while stack and not stack[-1].active:
            stack = stack[:-1]
        if stack:
            lease = stack[-1]
            lease.holders += 1
            self._lease_stack.set((*stack, lease))
            return self
        await self._lock.acquire()
        self._lease_stack.set((*stack, _SessionWriteLease()))
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        stack = self._lease_stack.get()
        if not stack:
            raise RuntimeError("session write lock exited without an active lease")
        lease = stack[-1]
        self._lease_stack.set(stack[:-1])
        lease.holders -= 1
        if lease.holders == 0:
            lease.active = False
            self._lock.release()

    def held_by_current_context(self) -> bool:
        """Return whether this context owns one active lease for the lock."""
        return any(lease.active for lease in self._lease_stack.get())


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
    # Completion and read-receipt updates are short synchronous JSON replaces.
    # One process-wide lock keeps their read-modify-write cycle atomic across
    # manager instances without adding an async lock to the RPC boundary.
    _activity_lock: ClassVar[threading.RLock] = threading.RLock()

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._title_changed_callbacks: list[Callable[[str, str, str | None], None]] = []
        self._completion_read_callbacks: list[Callable[[str, str, str | None], None]] = []

    def add_title_changed_callback(
        self, callback: Callable[[str, str, str | None], None]
    ) -> Callable[[], None]:
        """Register a display-title change callback and return its unsubscribe."""
        self._title_changed_callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._title_changed_callbacks:
                self._title_changed_callbacks.remove(callback)

        return unsubscribe

    def add_completion_read_callback(
        self, callback: Callable[[str, str, str | None], None]
    ) -> Callable[[], None]:
        """Register a successful completion-read callback and return its unsubscribe."""
        self._completion_read_callbacks.append(callback)

        def unsubscribe() -> None:
            if callback in self._completion_read_callbacks:
                self._completion_read_callbacks.remove(callback)

        return unsubscribe

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

    async def create_async(
        self,
        agent_id: str,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> ChatSession:
        """Create a Session without blocking an async caller on filesystem I/O."""
        return await _run_session_io(self.create, agent_id, session_id, project_id)

    def exists(self, agent_id: str, session_id: str, project_id: str | None = None) -> bool:
        """Return whether a valid session exists for an agent."""
        try:
            self.get(agent_id, session_id, project_id)
        except ChatSessionError:
            return False
        return True

    async def exists_async(
        self,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
    ) -> bool:
        """Check Session existence through the async storage boundary."""
        return await _run_session_io(self.exists, agent_id, session_id, project_id)

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

    async def get_or_create_async(
        self,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
    ) -> ChatSession:
        """Resolve or create a Session without blocking an async caller."""
        return await _run_session_io(self.get_or_create, agent_id, session_id, project_id)

    def get(self, agent_id: str, session_id: str, project_id: str | None = None) -> ChatSession:
        """Return a session handle for an existing agent session."""
        _validate_session_id(session_id)
        session_path = (
            self.sessions_dir(agent_id, project_id) / f"{session_id}{SESSION_FILE_EXTENSION}"
        )
        if not session_path.exists():
            raise ChatSessionError(f"session does not exist: {session_id}")
        return ChatSession(session_path)

    async def get_async(
        self,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
    ) -> ChatSession:
        """Resolve an existing Session through the async storage boundary."""
        return await _run_session_io(self.get, agent_id, session_id, project_id)

    def get_metadata(
        self, agent_id: str, session_id: str, project_id: str | None = None
    ) -> JsonObject:
        """Load session metadata from sidecar JSON or return an empty object."""
        session = self.get(agent_id, session_id, project_id)
        return self._load_sidecar(session)

    async def get_metadata_async(
        self,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
    ) -> JsonObject:
        """Read Session metadata without blocking the Event Loop."""
        return await _run_session_io(self.get_metadata, agent_id, session_id, project_id)

    def prompt_cache_affinity_id(
        self,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
    ) -> str:
        """Return the Session's stable, provider-neutral prompt-cache lineage.

        Sessions created before the first metadata write derive an opaque id
        deterministically from their full address. A Compaction or other cache
        epoch boundary persists a replacement id in the sidecar. This keeps the
        source of a fork byte-for-byte untouched while still letting the fork
        inherit the exact affinity the source resolves.
        """
        metadata = self.get_metadata(agent_id, session_id, project_id)
        stored = metadata.get(PROMPT_CACHE_AFFINITY_META_KEY)
        if stored is None:
            return _default_prompt_cache_affinity_id(agent_id, session_id, project_id)
        if not isinstance(stored, str) or not _is_prompt_cache_affinity_id(stored):
            raise ChatSessionError(f"invalid prompt cache affinity id for session: {session_id}")
        return stored

    def rotate_prompt_cache_affinity_id(
        self,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
    ) -> str:
        """Start and persist a fresh prompt-cache epoch for one Session."""
        affinity_id = _new_prompt_cache_affinity_id()
        metadata = self.get_metadata(agent_id, session_id, project_id)
        metadata[PROMPT_CACHE_AFFINITY_META_KEY] = affinity_id
        self.set_metadata(agent_id, session_id, metadata, project_id)
        return affinity_id

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

        try:
            serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ChatSessionError("session metadata must be JSON-serializable") from exc

        try:
            atomic_write_text(sidecar_path, serialized)
        except OSError as exc:
            raise ChatSessionError(f"failed to write metadata for session: {session_id}") from exc

    async def set_metadata_async(
        self,
        agent_id: str,
        session_id: str,
        data: dict[str, Any],
        project_id: str | None = None,
    ) -> None:
        """Persist Session metadata without blocking the Event Loop."""
        await _run_session_io(self.set_metadata, agent_id, session_id, data, project_id)

    def record_run_kind(
        self,
        agent_id: str,
        session_id: str,
        run_kind: RunKind,
        project_id: str | None = None,
    ) -> None:
        """Record one distinct Run origin category on its owning Session."""
        if not isinstance(run_kind, RunKind):
            raise ChatSessionError("session run kind must be a RunKind")
        metadata = self.get_metadata(agent_id, session_id, project_id)
        raw_run_kinds = metadata.get(SESSION_RUN_KINDS_META_KEY, [])
        valid_run_kinds = {kind.value for kind in RunKind}
        if not isinstance(raw_run_kinds, list) or any(
            not isinstance(value, str) or value not in valid_run_kinds for value in raw_run_kinds
        ):
            raise ChatSessionError(f"invalid run kinds for session: {session_id}")
        if run_kind.value in raw_run_kinds:
            return
        metadata[SESSION_RUN_KINDS_META_KEY] = [*raw_run_kinds, run_kind.value]
        self.set_metadata(agent_id, session_id, metadata, project_id)

    def retarget_identity_agent_references(
        self,
        old_agent_id: str,
        new_agent_id: str,
    ) -> tuple[SessionIdentityReferenceUpdate, ...]:
        """Retarget live ``subagent_parent`` navigation links across all Sessions.

        Only an unqualified parent (``project_id is None``) names an Identity
        Agent. Fork provenance and transcript records remain immutable history.
        Exact sidecar snapshots make the multi-file mutation reversible.
        """
        _validate_agent_id(old_agent_id)
        _validate_agent_id(new_agent_id)
        updates: list[SessionIdentityReferenceUpdate] = []
        try:
            for agent_id, session_id, project_id in self._all_session_owners():
                metadata = self.get_metadata(agent_id, session_id, project_id)
                parent = metadata.get("subagent_parent")
                if (
                    not isinstance(parent, dict)
                    or parent.get("project_id") is not None
                    or parent.get("agent_id") != old_agent_id
                ):
                    continue
                previous_metadata = dict(metadata)
                updated_parent = dict(parent)
                updated_parent["agent_id"] = new_agent_id
                metadata["subagent_parent"] = updated_parent
                update = SessionIdentityReferenceUpdate(
                    agent_id=agent_id,
                    session_id=session_id,
                    project_id=project_id,
                    previous_metadata=previous_metadata,
                )
                updates.append(update)
                self.set_metadata(agent_id, session_id, metadata, project_id)
        except Exception:
            self.restore_identity_agent_references(tuple(updates))
            raise
        return tuple(updates)

    def restore_identity_agent_references(
        self,
        updates: tuple[SessionIdentityReferenceUpdate, ...],
    ) -> None:
        """Restore exact sidecars changed by a reference retarget."""
        for update in reversed(updates):
            self.set_metadata(
                update.agent_id,
                update.session_id,
                update.previous_metadata,
                update.project_id,
            )

    def _all_session_owners(self) -> builtins.list[tuple[str, str, str | None]]:
        """Enumerate canonical live Session transcripts in both storage layouts."""
        owners: builtins.list[tuple[str, str, str | None]] = []
        for path in sorted((self.data_dir / "agents").glob("*/sessions/*.jsonl")):
            agent_id = path.parent.parent.name
            if is_valid_agent_id(agent_id) and _is_valid_session_id(path.stem):
                owners.append((agent_id, path.stem, None))

        projects_root = self.data_dir / "projects"
        if projects_root.exists():
            for path in sorted(projects_root.glob("*/agents/*/sessions/*.jsonl")):
                project_id = path.parents[3].name
                agent_id = path.parent.parent.name
                if (
                    is_valid_project_id(project_id)
                    and is_valid_agent_id(agent_id)
                    and _is_valid_session_id(path.stem)
                ):
                    owners.append((agent_id, path.stem, project_id))
        return owners

    def record_terminal_run(
        self,
        agent_id: str,
        session_id: str,
        run_id: str,
        status: str,
        timestamp: str,
        project_id: str | None = None,
    ) -> None:
        """Persist the latest terminal Run as an unread completion.

        This state deliberately lives outside the general metadata sidecar: Run
        completion and read acknowledgement are one tiny state machine with its
        own atomic writer, so unrelated title/Skill/channel metadata rewrites
        cannot lose a notification. An absent activity sidecar means no unread
        completion, which also gives pre-feature Sessions a clean baseline.
        """
        if not isinstance(run_id, str) or not run_id:
            raise ChatSessionError("terminal run id must be a non-empty string")
        if status not in SESSION_TERMINAL_RUN_STATUSES:
            raise ChatSessionError("terminal run status is invalid")
        if not isinstance(timestamp, str) or not timestamp:
            raise ChatSessionError("terminal run timestamp must be a non-empty string")

        session = self.get(agent_id, session_id, project_id)
        with self._activity_lock:
            if not session.path.exists():
                raise ChatSessionError(f"session does not exist: {session_id}")
            activity = self._load_activity(session)
            activity["latest_completion"] = {
                "run_id": run_id,
                "status": status,
                "timestamp": timestamp,
            }
            self._write_activity(session, activity)

    def mark_terminal_run_read(
        self,
        agent_id: str,
        session_id: str,
        run_id: str,
        project_id: str | None = None,
    ) -> JsonObject:
        """Acknowledge exactly the terminal Run the accessor displayed.

        A stale acknowledgement never clears a newer completion: the caller
        supplies the Run id it rendered, and the write advances only while that
        id is still the latest completion in this Session.
        """
        if not isinstance(run_id, str) or not run_id:
            raise ChatSessionError("read run id must be a non-empty string")
        session = self.get(agent_id, session_id, project_id)
        with self._activity_lock:
            if not session.path.exists():
                raise ChatSessionError(f"session does not exist: {session_id}")
            activity = self._load_activity(session)
            latest = _valid_latest_completion(activity)
            marked_read = False
            if (
                latest is not None
                and latest["run_id"] == run_id
                and activity.get("read_run_id") != run_id
            ):
                activity["read_run_id"] = run_id
                self._write_activity(session, activity)
                marked_read = True
            payload = _completion_activity_payload(activity)
            payload["marked_read"] = marked_read
        if marked_read:
            self._notify_completion_read(agent_id, session_id, project_id)
        return payload

    async def mark_terminal_run_read_async(
        self,
        agent_id: str,
        session_id: str,
        run_id: str,
        project_id: str | None = None,
    ) -> JsonObject:
        """Acknowledge a completion without blocking the Event Loop."""
        return await _run_session_io(
            self.mark_terminal_run_read,
            agent_id,
            session_id,
            run_id,
            project_id,
        )

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
        previous_title = metadata.get(SESSION_TITLE_KEY)
        if normalized_title is None:
            metadata.pop(SESSION_TITLE_KEY, None)
        else:
            metadata[SESSION_TITLE_KEY] = normalized_title
        self.set_metadata(agent_id, session_id, metadata, project_id)
        if previous_title != normalized_title:
            self._notify_title_changed(agent_id, session_id, project_id)
        return normalized_title

    async def set_title_async(
        self,
        agent_id: str,
        session_id: str,
        title: str,
        project_id: str | None = None,
    ) -> str | None:
        """Set a manual Session title without blocking the Event Loop."""
        return await _run_session_io(
            self.set_title,
            agent_id,
            session_id,
            title,
            project_id,
        )

    def set_auto_title(
        self,
        agent_id: str,
        session_id: str,
        title: str,
        project_id: str | None = None,
        *,
        initialized: bool = True,
    ) -> str | None:
        """Set the automatic title beneath the optional manual override.

        ``title`` is the immediate local fallback first and may later be
        replaced by the background Model result. The independent ``title`` key
        remains the manual override, so a late background result can never
        replace what the user sees after a rename; clearing the manual name
        reveals the best automatic title again.
        """
        normalized_title = _normalize_session_title(title)
        metadata = dict(self.get_metadata(agent_id, session_id, project_id))
        previous_title = metadata.get(SESSION_AUTO_TITLE_KEY)
        if normalized_title is None:
            metadata.pop(SESSION_AUTO_TITLE_KEY, None)
        else:
            metadata[SESSION_AUTO_TITLE_KEY] = normalized_title
        if initialized:
            metadata[SESSION_AUTO_TITLE_INITIALIZED_KEY] = True
        self.set_metadata(agent_id, session_id, metadata, project_id)
        if previous_title != normalized_title:
            self._notify_title_changed(agent_id, session_id, project_id)
        return normalized_title

    def mark_auto_title_initialized(
        self,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
    ) -> None:
        """Record that an existing Session must not be backfilled later."""
        metadata = dict(self.get_metadata(agent_id, session_id, project_id))
        metadata[SESSION_AUTO_TITLE_INITIALIZED_KEY] = True
        self.set_metadata(agent_id, session_id, metadata, project_id)

    def _notify_title_changed(self, agent_id: str, session_id: str, project_id: str | None) -> None:
        for callback in list(self._title_changed_callbacks):
            try:
                callback(agent_id, session_id, project_id)
            except Exception:
                _LOGGER.warning(
                    "Session title change callback failed (agent=%s session=%s)",
                    agent_id,
                    session_id,
                    exc_info=True,
                )

    def _notify_completion_read(
        self, agent_id: str, session_id: str, project_id: str | None
    ) -> None:
        for callback in list(self._completion_read_callbacks):
            try:
                callback(agent_id, session_id, project_id)
            except Exception:
                _LOGGER.warning(
                    "Session completion-read callback failed (agent=%s session=%s)",
                    agent_id,
                    session_id,
                    exc_info=True,
                )

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
        for the same reason: callers can remove domain-owned keys without this
        module importing those domains.

        Ordering is crash-safe. The transcript (``.jsonl``, which alone defines a
        session's existence to :meth:`list`) is relocated first with
        :func:`os.replace` (atomic per file on one filesystem); then the sidecar
        is written at the destination with ``strip_meta_keys`` removed; then the
        source sidecar remnant is deleted. A crash between steps never loses the
        conversation — the worst case is an orphan source sidecar, invisible to
        :meth:`list`. The source ``write_lock`` is held so an in-flight contiguous
        append cannot interleave. The Agent Takeover caller additionally holds a
        Run Admission Guard over both source and destination Session keys; that
        guard, not this storage lock, prevents new Runs from writing either home
        during the transition.
        """
        _validate_session_id(session_id)
        async with self.write_lock(source_agent_id, session_id, source_project_id):
            return await _run_session_io(
                self._move_storage,
                source_agent_id,
                session_id,
                target_agent_id,
                source_project_id,
                target_project_id,
                strip_meta_keys,
            )

    def _move_storage(
        self,
        source_agent_id: str,
        session_id: str,
        target_agent_id: str,
        source_project_id: str | None,
        target_project_id: str | None,
        strip_meta_keys: frozenset[str],
    ) -> ChatSession:
        source = self.get(source_agent_id, session_id, source_project_id)
        destination_dir = self.sessions_dir(target_agent_id, target_project_id)
        destination_path = destination_dir / f"{session_id}{SESSION_FILE_EXTENSION}"
        if destination_path.exists():
            raise ChatSessionError(f"destination session already exists: {session_id}")

        source_sidecar = source.sidecar_path
        source_activity = source.activity_path
        source_continuation = source.continuation_path
        had_sidecar = source_sidecar.exists()
        had_continuation = source_continuation.exists()
        sidecar_data = self._load_sidecar(source)

        destination_dir.mkdir(parents=True, exist_ok=True)
        with self._activity_lock:
            had_activity = source_activity.exists()
            try:
                os.replace(source.path, destination_path)
            except OSError as exc:
                raise ChatSessionError(f"failed to move session transcript: {session_id}") from exc
            if had_activity:
                destination_activity = destination_path.with_name(
                    f"{session_id}{SESSION_ACTIVITY_FILE_SUFFIX}"
                )
                try:
                    os.replace(source_activity, destination_activity)
                except OSError as exc:
                    raise ChatSessionError(
                        f"failed to move session activity: {session_id}"
                    ) from exc

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
            return await _run_session_io(
                self._fork_storage,
                source_agent_id,
                session_id,
                destination_agent_id,
                source_project_id,
                target_project_id,
                strip_meta_keys,
            )

    def _fork_storage(
        self,
        source_agent_id: str,
        session_id: str,
        destination_agent_id: str,
        source_project_id: str | None,
        target_project_id: str | None,
        strip_meta_keys: frozenset[str],
    ) -> ChatSession:
        source = self.get(source_agent_id, session_id, source_project_id)
        destination_dir = self.sessions_dir(destination_agent_id, target_project_id)
        fork_session = ChatSession.create(destination_dir)

        try:
            try:
                transcript_bytes = source.path.read_bytes()
                fork_session.path.write_bytes(transcript_bytes)
            except OSError as exc:
                raise ChatSessionError(f"failed to copy session transcript: {session_id}") from exc
            message_count = transcript_bytes.count(SESSION_LINE_ENDING_BYTES)

            source_metadata = self._load_sidecar(source)
            forked_metadata = {
                key: value for key, value in source_metadata.items() if key not in strip_meta_keys
            }
            same_prompt_scope = (
                destination_agent_id == source_agent_id and target_project_id == source_project_id
            )
            if same_prompt_scope:
                stored_affinity = source_metadata.get(PROMPT_CACHE_AFFINITY_META_KEY)
                if stored_affinity is None:
                    stored_affinity = _default_prompt_cache_affinity_id(
                        source_agent_id,
                        session_id,
                        source_project_id,
                    )
                elif not _is_prompt_cache_affinity_id(stored_affinity):
                    raise ChatSessionError(
                        f"invalid prompt cache affinity id for session: {session_id}"
                    )
                forked_metadata[PROMPT_CACHE_AFFINITY_META_KEY] = stored_affinity
            else:
                forked_metadata[PROMPT_CACHE_AFFINITY_META_KEY] = _new_prompt_cache_affinity_id()
            forked_metadata[FORK_SOURCE_META_KEY] = {
                "agent_id": source_agent_id,
                "session_id": session_id,
                "project_id": source_project_id,
                "forked_at": _format_timestamp(datetime.now(UTC)),
                "message_count": message_count,
            }
            self.set_metadata(
                destination_agent_id,
                fork_session.id,
                forked_metadata,
                target_project_id,
            )
        except Exception:
            fork_session.delete()
            raise
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

    async def list_async(
        self,
        agent_id: str,
        project_id: str | None = None,
    ) -> builtins.list[ChatSession]:
        """Enumerate Session handles without blocking the Event Loop."""
        return await _run_session_io(self.list, agent_id, project_id)

    def list_with_metadata(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """List sessions with activity timestamps plus merged sidecar metadata."""
        sessions_with_metadata: builtins.list[dict[str, Any]] = []
        for session in self.list(agent_id, project_id):
            created_at, last_active_at = self._activity_timestamps(session)
            metadata = self._load_sidecar(session)

            session_data: dict[str, Any] = dict(metadata)
            session_data.update(self._completion_activity(session))
            session_data["id"] = session.id
            session_data["created_at"] = created_at
            session_data["last_active_at"] = last_active_at
            sessions_with_metadata.append(session_data)
        return sessions_with_metadata

    async def list_with_metadata_async(
        self,
        agent_id: str,
        project_id: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List Session summaries through the async storage boundary."""
        return await _run_session_io(self.list_with_metadata, agent_id, project_id)

    def list_completion_activity(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[dict[str, Any]]:
        """List only the durable completion projection for every Session.

        Agent attention badges need Session identity plus terminal completion/read
        state, not transcript timestamps or general metadata. Keeping that narrow
        projection here prevents an accessor refresh from opening both transcript
        bookends and metadata sidecars for every Session it merely wants to mark
        running, unread, or idle.
        """
        return [
            {
                "id": session.id,
                **self._completion_activity(session),
            }
            for session in self.list(agent_id, project_id)
        ]

    async def list_completion_activity_async(
        self,
        agent_id: str,
        project_id: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List completion activity through the async storage boundary."""
        return await _run_session_io(self.list_completion_activity, agent_id, project_id)

    def delete(self, agent_id: str, session_id: str, project_id: str | None = None) -> None:
        """Hard-delete one agent session's transcript and sidecars.

        Low-level primitive (unlink transcript + sidecar). The session-deletion
        feature does not call this — it archives via :meth:`archive` so a
        removed session stays recoverable. Kept as the genuine "remove the
        files" capability that tests and staleness cleanup use.
        """
        session = self.get(agent_id, session_id, project_id)
        with self._activity_lock:
            session.delete()

    async def archive(self, agent_id: str, session_id: str, project_id: str | None = None) -> Path:
        """Archive one session's transcript and sidecars instead of deleting them.

        The deletion feature's storage step: mirrors ``AgentStore``/
        ``ProjectStore`` by moving the transcript and sidecar under
        ``<data_dir>/archive/sessions/`` (recoverable by hand, no in-app
        restore) rather than unlinking them. An existing archive for the same id
        is replaced. Returns the archive directory.

        Crash-safety mirrors :meth:`move`: holds the source ``write_lock`` and
        moves the transcript first with :func:`os.replace` (atomic per file), so
        an out-of-band note append cannot interleave. The ``session.delete``
        caller holds a Run Admission Guard across this await; that guard, not
        this storage lock, prevents new Runs from recreating the file.
        """
        _validate_session_id(session_id)
        async with self.write_lock(agent_id, session_id, project_id):
            return await _run_session_io(
                self._archive_storage,
                agent_id,
                session_id,
                project_id,
            )

    def _archive_storage(
        self,
        agent_id: str,
        session_id: str,
        project_id: str | None,
    ) -> Path:
        source = self.get(agent_id, session_id, project_id)
        archive_dir = self._archive_dir(agent_id, project_id)
        archive_dir.mkdir(parents=True, exist_ok=True)
        archived_transcript = archive_dir / source.path.name
        archived_sidecar = archive_dir / source.sidecar_path.name
        archived_activity = archive_dir / source.activity_path.name
        archived_continuation = archive_dir / source.continuation_path.name

        # Replace any prior archive for the same id (mirrors agent/project).
        archived_transcript.unlink(missing_ok=True)
        archived_sidecar.unlink(missing_ok=True)
        archived_activity.unlink(missing_ok=True)
        archived_continuation.unlink(missing_ok=True)

        had_sidecar = source.sidecar_path.exists()
        had_continuation = source.continuation_path.exists()
        with self._activity_lock:
            had_activity = source.activity_path.exists()
            try:
                os.replace(source.path, archived_transcript)
            except OSError as exc:
                raise ChatSessionError(
                    f"failed to archive session transcript: {session_id}"
                ) from exc
            if had_activity:
                os.replace(source.activity_path, archived_activity)
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

    def _completion_activity(self, session: ChatSession) -> JsonObject:
        with self._activity_lock:
            return _completion_activity_payload(self._load_activity(session))

    def _load_activity(self, session: ChatSession) -> JsonObject:
        activity_path = session.activity_path
        if not activity_path.exists():
            return {}
        try:
            data = json.loads(activity_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ChatSessionError(f"failed to read activity for session: {session.id}") from exc
        except json.JSONDecodeError as exc:
            raise ChatSessionError(f"invalid activity JSON for session: {session.id}") from exc
        if not isinstance(data, dict):
            raise ChatSessionError(f"activity for session must be an object: {session.id}")
        return dict(data)

    def _write_activity(self, session: ChatSession, data: JsonObject) -> None:
        activity_path = session.activity_path
        try:
            serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            atomic_write_text(activity_path, serialized)
        except (OSError, TypeError, ValueError) as exc:
            raise ChatSessionError(f"failed to write activity for session: {session.id}") from exc

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


def _valid_latest_completion(activity: JsonObject) -> JsonObject | None:
    latest = activity.get("latest_completion")
    if latest is None:
        return None
    if not isinstance(latest, dict):
        raise ChatSessionError("session activity latest_completion must be an object")
    run_id = latest.get("run_id")
    status = latest.get("status")
    timestamp = latest.get("timestamp")
    if (
        not isinstance(run_id, str)
        or not run_id
        or status not in SESSION_TERMINAL_RUN_STATUSES
        or not isinstance(timestamp, str)
        or not timestamp
    ):
        raise ChatSessionError("session activity latest_completion is invalid")
    return {"run_id": run_id, "status": status, "timestamp": timestamp}


def _completion_activity_payload(activity: JsonObject) -> JsonObject:
    latest = _valid_latest_completion(activity)
    latest_run_id = latest["run_id"] if latest is not None else None
    if latest is None or activity.get("read_run_id") == latest_run_id:
        return {
            "latest_completion_run_id": latest_run_id,
            "has_unread_completion": False,
            "unread_run_id": None,
            "unread_run_status": None,
            "unread_run_at": None,
        }
    return {
        "latest_completion_run_id": latest_run_id,
        "has_unread_completion": True,
        "unread_run_id": latest["run_id"],
        "unread_run_status": latest["status"],
        "unread_run_at": latest["timestamp"],
    }


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
    if not isinstance(name, str) or not name or not isinstance(content, str) or not content:
        return None

    resource_paths: list[str] = []
    resource_guidance = ""
    resource_files = data.get("resource_files")
    if resource_files is not None:
        if not isinstance(resource_files, dict):
            return None
        resource_guidance_value = resource_files.get("guidance")
        resource_paths_value = resource_files.get("files")
        if not isinstance(resource_guidance_value, str) or not isinstance(
            resource_paths_value, list
        ):
            return None
        if not all(isinstance(file_path, str) and file_path for file_path in resource_paths_value):
            return None
        resource_guidance = resource_guidance_value
        resource_paths = resource_paths_value

    environment_access_value = data.get("environment_access", "")
    if not isinstance(environment_access_value, str):
        return None
    activation_content = format_skill_activation_context(
        name,
        content,
        resource_files=resource_paths,
        resource_guidance=resource_guidance,
        environment_access=environment_access_value,
    )
    return name, activation_content


def skill_tool_activation_name(message: ChatMessage) -> str | None:
    """Return the skill name from a loading ``skill`` tool result, or ``None``."""
    activation = skill_tool_activation(message)
    return activation[0] if activation is not None else None


def project_tool_context_id(message: ChatMessage) -> str | None:
    """Return the Project id from a successful Project Context Tool Result.

    The Project Tool Result is the durable context carrier. Failures, malformed
    envelopes, and unrelated Tool messages yield ``None`` so callers can scan
    Session history without special error handling.
    """
    if message.role != "tool" or message.name != PROJECT_TOOL_MESSAGE_NAME:
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
    if not isinstance(data, dict) or data.get("status") != PROJECT_TOOL_LOADED_STATUS:
        return None
    project_id = data.get("project_id")
    if isinstance(project_id, str) and project_id:
        return project_id
    return None


def latest_project_tool_context_id(messages: list[ChatMessage]) -> str | None:
    """Return the most recently loaded explicit Project Context, if any."""
    for message in reversed(messages):
        project_id = project_tool_context_id(message)
        if project_id is not None:
            return project_id
    return None


def skill_activation_names(messages: list[ChatMessage]) -> frozenset[str]:
    """Return the names of every skill activation carried in *messages*.

    Covers both carriers (trigger notes and ``skill`` tool results). The chat
    loop uses it on the preserved compaction tail to avoid front-injecting a
    skill whose carrier already survives verbatim.
    """
    return frozenset(_skill_contexts_from_messages(messages))


def skill_activation_contents(messages: list[ChatMessage]) -> dict[str, str]:
    """Return the latest carried content for each activated Skill in *messages*."""
    return _skill_contexts_from_messages(messages)


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
    """Collect latest Skill content from both carriers, preserving first-name order."""
    contexts: dict[str, str] = {}
    for message in messages:
        activation = skill_context_note_payload(message) or skill_tool_activation(message)
        if activation is not None:
            contexts[activation[0]] = activation[1]
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


def _read_complete_line_ending_at(path: Path, end_offset: int) -> bytes | None:
    """Return the last non-blank complete line ending at *end_offset*."""
    with path.open("rb") as session_file:
        session_file.seek(0, os.SEEK_END)
        file_size = session_file.tell()
        if end_offset <= 0 or end_offset > file_size:
            return None
        session_file.seek(end_offset - 1)
        if session_file.read(1) != SESSION_LINE_ENDING_BYTES:
            return None

        buffer = b""
        position = end_offset
        while position > 0:
            read_size = min(_TAIL_CHUNK_SIZE, position)
            position -= read_size
            session_file.seek(position)
            buffer = session_file.read(read_size) + buffer
            lines = buffer.split(SESSION_LINE_ENDING_BYTES)
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


def _message_id_from_line(line: bytes) -> str | None:
    """Extract the canonical Message id from one JSONL line, or ``None``."""
    try:
        data = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    message_id = data.get("id")
    return message_id if isinstance(message_id, str) and message_id else None


def _append_bytes(path: Path, data: bytes) -> None:
    file_descriptor = os.open(path, SESSION_APPEND_FLAGS, 0o600)
    try:
        _write_all(file_descriptor, data)
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)


def _current_context_holds_session_write_lock(path: Path) -> bool:
    key = str(path.resolve())
    lock = ChatSessionManager._write_locks.get(key)
    return lock is not None and lock.held_by_current_context()


async def _run_session_io(
    function: Callable[..., _SessionIoResult], *arguments: Any
) -> _SessionIoResult:
    """Run one storage operation off-loop while preserving cancellation ordering.

    A worker-thread filesystem call cannot be cancelled once started. Waiting for
    that worker even after task cancellation keeps a Session write lock from being
    released while its durable append is still in flight.
    """
    return await _SESSION_IO_WORKERS.run(function, *arguments)


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
