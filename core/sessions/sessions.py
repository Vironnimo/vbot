"""SQLite-backed public Session facade."""

from __future__ import annotations

import asyncio
import builtins
import contextvars
import hashlib
import json
import logging
import re
import threading
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from core.chat.errors import ChatSessionError
from core.runs import RunKind
from core.sessions.store import SessionStore
from core.settings import is_valid_agent_id, is_valid_project_id
from core.skills.skills import format_skill_activation_context
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage

JsonObject = dict[str, Any]
_SessionIoResult = TypeVar("_SessionIoResult")
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SESSION_TITLE_KEY = "title"
SESSION_AUTO_TITLE_KEY = "auto_title"
SESSION_AUTO_TITLE_INITIALIZED_KEY = "auto_title_initialized"
SESSION_TITLE_MAX_LENGTH = 200
SESSION_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
FORK_SOURCE_META_KEY = "fork_source"
SESSION_RUN_KINDS_META_KEY = "run_kinds"
PROMPT_CACHE_AFFINITY_META_KEY = "prompt_cache_affinity_id"
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
SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS = frozenset(
    {"pinned_skill_catalog", "seen_skills", PROMPT_CACHE_AFFINITY_META_KEY}
)
SESSION_MOVE_STRIP_META_KEYS = SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS
SKILL_CONTEXT_NOTE_PREFIX = "[skill-context] "
SKILL_TOOL_MESSAGE_NAME = "skill"
SKILL_TOOL_LOADED_STATUS = "loaded"
PROJECT_TOOL_MESSAGE_NAME = "project"
PROJECT_TOOL_LOADED_STATUS = "loaded"
CHANNEL_MESSAGE_NOTE_PREFIX = "[channel-message] "
SKILL_AVAILABLE_NOTE_PREFIX = "[skill-available] "
_SESSION_IO_WORKERS = BoundedWorkerPool(name="session-io", max_workers=8)


@dataclass(frozen=True)
class SessionAddress:
    project_id: str | None
    agent_id: str
    session_id: str


@dataclass(frozen=True)
class SessionIdentityReferenceUpdate:
    address: SessionAddress
    previous_metadata: JsonObject


@dataclass(frozen=True)
class SessionReadCursor:
    generation_id: str
    history_revision: int
    next_seq: int
    message_count: int
    last_message_id: str | None


@dataclass(frozen=True)
class SessionReadBatch:
    messages: tuple[ChatMessage, ...]
    cursor: SessionReadCursor


def _validate_agent_id(agent_id: str) -> None:
    if not is_valid_agent_id(agent_id):
        raise ChatSessionError(
            "agent id must be 1-64 characters using only letters, numbers, hyphen, or underscore"
        )


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or SESSION_ID_PATTERN.fullmatch(session_id) is None:
        raise ChatSessionError(
            "session id must be 1-128 characters of ASCII letters, digits, hyphen, "
            "or underscore and must not start with punctuation"
        )


def _normalize_session_title(title: str) -> str | None:
    if not isinstance(title, str):
        raise ChatSessionError("session title must be a string")
    value = " ".join(title.split())
    return value[:SESSION_TITLE_MAX_LENGTH] or None


def _format_timestamp(timestamp: datetime | None) -> str:
    value = datetime.now(UTC) if timestamp is None else timestamp.astimezone(UTC)
    return value.isoformat().replace("Z", "+00:00")


def _new_prompt_cache_affinity_id() -> str:
    return uuid.uuid4().hex


def _default_prompt_cache_affinity_id(address: SessionAddress) -> str:
    encoded = json.dumps(
        [address.project_id, address.agent_id, address.session_id],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def _is_prompt_cache_affinity_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(char in "0123456789abcdef" for char in value)
    )


def _decode_state_object(payload: str, name: str) -> JsonObject:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ChatSessionError(f"invalid {name}") from exc
    if not isinstance(value, dict):
        raise ChatSessionError(f"invalid {name}")
    return value


def active_session_messages(messages: Sequence[ChatMessage]) -> list[ChatMessage]:
    active: list[ChatMessage] = []
    for message in messages:
        if message.role == "history_edit":
            active = active[: editable_session_message_index(active, message.target_message_id)]
        else:
            active.append(message)
    return active


def editable_session_message_index(messages: Sequence[ChatMessage], message_id: str | None) -> int:
    if not isinstance(message_id, str) or not message_id:
        raise ChatSessionError("history edit target must be a non-empty message id")
    index = next(
        (index for index, message in enumerate(messages) if message.id == message_id), None
    )
    if index is None:
        raise ChatSessionError(f"history edit target is not active: {message_id}")
    target = messages[index]
    if target.role != "user" or not isinstance(target.content, str) or target.sender is not None:
        raise ChatSessionError("history edit target must be an own plain-text user message")
    latest_takeover = max(
        (index for index, message in enumerate(messages) if message.role == "agent_takeover"),
        default=-1,
    )
    if index <= latest_takeover:
        raise ChatSessionError("history edit target cannot precede the latest agent takeover")
    return index


def editable_session_message_ids(messages: Sequence[ChatMessage]) -> frozenset[str]:
    active = active_session_messages(messages)
    takeover = max(
        (index for index, message in enumerate(active) if message.role == "agent_takeover"),
        default=-1,
    )
    return frozenset(
        message.id
        for index, message in enumerate(active)
        if index > takeover
        and message.role == "user"
        and isinstance(message.content, str)
        and message.sender is None
    )


def _skill_context_note_content(name: str, content: str) -> str:
    return SKILL_CONTEXT_NOTE_PREFIX + json.dumps(
        {"name": name, "content": content}, ensure_ascii=False, separators=(",", ":")
    )


def is_skill_context_note(message: ChatMessage) -> bool:
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(SKILL_CONTEXT_NOTE_PREFIX)
    )


def skill_context_note_payload(message: ChatMessage) -> tuple[str, str] | None:
    if not is_skill_context_note(message) or not isinstance(message.content, str):
        return None
    try:
        data = json.loads(message.content.removeprefix(SKILL_CONTEXT_NOTE_PREFIX))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name, content = data.get("name"), data.get("content")
    return (name, content) if isinstance(name, str) and name and isinstance(content, str) else None


def skill_context_note_name(message: ChatMessage) -> str | None:
    payload = skill_context_note_payload(message)
    return payload[0] if payload else None


def skill_tool_activation(message: ChatMessage) -> tuple[str, str] | None:
    if (
        message.role != "tool"
        or message.name != SKILL_TOOL_MESSAGE_NAME
        or not isinstance(message.content, str)
    ):
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
    name, content = data.get("name"), data.get("content")
    if not isinstance(name, str) or not name or not isinstance(content, str) or not content:
        return None
    resources = data.get("resource_files")
    files: list[str] = []
    guidance = ""
    if resources is not None:
        if not isinstance(resources, dict):
            return None
        resource_guidance = resources.get("guidance")
        resource_files = resources.get("files")
        if (
            not isinstance(resource_guidance, str)
            or not isinstance(resource_files, list)
            or not all(isinstance(file, str) and file for file in resource_files)
        ):
            return None
        guidance = resource_guidance
        files = resource_files
    access = data.get("environment_access", "")
    if not isinstance(access, str):
        return None
    return name, format_skill_activation_context(
        name, content, resource_files=files, resource_guidance=guidance, environment_access=access
    )


def skill_tool_activation_name(message: ChatMessage) -> str | None:
    activation = skill_tool_activation(message)
    return activation[0] if activation else None


def project_tool_context_id(message: ChatMessage) -> str | None:
    if (
        message.role != "tool"
        or message.name != PROJECT_TOOL_MESSAGE_NAME
        or not isinstance(message.content, str)
    ):
        return None
    try:
        envelope = json.loads(message.content)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        return None
    data = envelope.get("data")
    value = (
        data.get("project_id")
        if isinstance(data, dict) and data.get("status") == PROJECT_TOOL_LOADED_STATUS
        else None
    )
    return value if isinstance(value, str) and value else None


def latest_project_tool_context_id(messages: list[ChatMessage]) -> str | None:
    for message in reversed(messages):
        if project_id := project_tool_context_id(message):
            return project_id
    return None


def _skill_contexts(messages: list[ChatMessage]) -> dict[str, str]:
    result: dict[str, str] = {}
    for message in messages:
        activation = skill_context_note_payload(message) or skill_tool_activation(message)
        if activation:
            result[activation[0]] = activation[1]
    return result


def skill_activation_names(messages: list[ChatMessage]) -> frozenset[str]:
    return frozenset(_skill_contexts(messages))


def skill_activation_contents(messages: list[ChatMessage]) -> dict[str, str]:
    return _skill_contexts(messages)


def current_skill_activation_contents(messages: list[ChatMessage]) -> dict[str, str]:
    checkpoint = max(
        (
            index
            for index, message in enumerate(messages)
            if message.role == "compaction_checkpoint"
        ),
        default=-1,
    )
    return _skill_contexts(messages[checkpoint + 1 :])


def is_channel_message_note(message: ChatMessage) -> bool:
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(CHANNEL_MESSAGE_NOTE_PREFIX)
    )


def is_skill_available_note(message: ChatMessage) -> bool:
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(SKILL_AVAILABLE_NOTE_PREFIX)
    )


def _valid_latest_completion(activity: JsonObject) -> JsonObject | None:
    latest = activity.get("latest_completion")
    if latest is None:
        return None
    if not isinstance(latest, dict):
        raise ChatSessionError("session activity latest_completion must be an object")
    run_id, status, timestamp = latest.get("run_id"), latest.get("status"), latest.get("timestamp")
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
    latest_id = latest["run_id"] if latest else None
    if latest is None or activity.get("read_run_id") == latest_id:
        return {
            "latest_completion_run_id": latest_id,
            "has_unread_completion": False,
            "unread_run_id": None,
            "unread_run_status": None,
            "unread_run_at": None,
        }
    return {
        "latest_completion_run_id": latest_id,
        "has_unread_completion": True,
        "unread_run_id": latest["run_id"],
        "unread_run_status": latest["status"],
        "unread_run_at": latest["timestamp"],
    }


def _completion_activity_from_state(state: Any) -> JsonObject:
    latest_id = state["latest_completion_run_id"]
    if latest_id is None or state["read_completion_run_id"] == latest_id:
        return {
            "latest_completion_run_id": latest_id,
            "has_unread_completion": False,
            "unread_run_id": None,
            "unread_run_status": None,
            "unread_run_at": None,
        }
    return {
        "latest_completion_run_id": latest_id,
        "has_unread_completion": True,
        "unread_run_id": latest_id,
        "unread_run_status": state["latest_completion_status"],
        "unread_run_at": state["latest_completion_at"],
    }


@dataclass
class _SessionWriteLease:
    holders: int = 1
    active: bool = True


class _SessionWriteLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._leases: contextvars.ContextVar[tuple[_SessionWriteLease, ...]] = (
            contextvars.ContextVar("session_write_lock_leases", default=())
        )

    async def __aenter__(self) -> _SessionWriteLock:
        stack = tuple(lease for lease in self._leases.get() if lease.active)
        if stack:
            stack[-1].holders += 1
            self._leases.set((*stack, stack[-1]))
            return self
        await self._lock.acquire()
        self._leases.set((*stack, _SessionWriteLease()))
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        stack = self._leases.get()
        if not stack:
            raise RuntimeError("session write lock exited without an active lease")
        lease = stack[-1]
        self._leases.set(stack[:-1])
        lease.holders -= 1
        if lease.holders == 0:
            lease.active = False
            self._lock.release()


async def _run_session_io(
    function: Callable[..., _SessionIoResult], *arguments: Any
) -> _SessionIoResult:
    return await _SESSION_IO_WORKERS.run(function, *arguments)


class ChatSession:
    """Path-free Session handle backed by the canonical SQLite store."""

    def __init__(self, store: SessionStore, address: SessionAddress) -> None:
        self._store = store
        self.address = address
        self._pending_notes: deque[ChatMessage] = deque()
        self._defer_notes = False
        self._deferred_note_messages: list[ChatMessage] = []
        self._activated_skill_contents: dict[str, str] = {}
        self._activated_skill_cache_loaded = False
        self._state_lock = threading.RLock()

    @property
    def id(self) -> str:
        return self.address.session_id

    def append(self, message: ChatMessage) -> None:
        self.append_many([message])

    def append_many(self, messages: list[ChatMessage]) -> None:
        self._store.append_messages(self.address, messages)
        if any(message.role == "compaction_checkpoint" for message in messages):
            with self._state_lock:
                self._activated_skill_contents = current_skill_activation_contents(self.load())
                self._activated_skill_cache_loaded = True

    async def append_async(self, message: ChatMessage) -> None:
        await _run_session_io(self.append, message)

    async def append_many_async(self, messages: list[ChatMessage]) -> None:
        if messages:
            await _run_session_io(self.append_many, list(messages))

    def append_continuation_record(self, record: JsonObject) -> None:
        self.append_continuation_records([record])

    def append_continuation_records(self, records: list[JsonObject]) -> None:
        self._store.append_continuation(self.address, records)

    async def append_continuation_records_async(self, records: list[JsonObject]) -> None:
        if records:
            await _run_session_io(self.append_continuation_records, list(records))

    def load_continuation_records(self) -> list[JsonObject]:
        return self._store.continuation(self.address)

    async def load_continuation_records_async(self) -> list[JsonObject]:
        return await _run_session_io(self.load_continuation_records)

    def clear_continuation(self) -> None:
        self._store.clear_continuation(self.address)

    async def clear_continuation_async(self) -> None:
        await _run_session_io(self.clear_continuation)

    def begin_defer_notes(self) -> None:
        with self._state_lock:
            self._defer_notes = True

    def _take_deferred_notes(self) -> list[ChatMessage]:
        with self._state_lock:
            notes = list(self._deferred_note_messages)
            self._deferred_note_messages.clear()
            self._defer_notes = False
            return notes

    def take_deferred_notes(self) -> list[ChatMessage]:
        return self._take_deferred_notes()

    def flush_deferred_notes(self) -> None:
        self.append_many(self._take_deferred_notes())

    async def flush_deferred_notes_async(self) -> None:
        await self.append_many_async(self._take_deferred_notes())

    def add_note(self, content: str) -> None:
        from core.chat.messages import ChatMessage

        note = ChatMessage.note(content)
        with self._state_lock:
            deferred = self._defer_notes
            if deferred:
                self._deferred_note_messages.append(note)
            self._pending_notes.append(note)
        if not deferred:
            self.append(note)

    async def add_note_async(self, content: str) -> None:
        await _run_session_io(self.add_note, content)

    def drain_pending_notes(self) -> list[ChatMessage]:
        with self._state_lock:
            notes = list(self._pending_notes)
            self._pending_notes.clear()
            return notes

    def _load_activated_skill_contents(self) -> dict[str, str]:
        with self._state_lock:
            if not self._activated_skill_cache_loaded:
                self._activated_skill_contents = current_skill_activation_contents(self.load())
                self._activated_skill_cache_loaded = True
            return dict(self._activated_skill_contents)

    def register_skill_activation(self, name: str, content: str) -> bool:
        active = self._load_activated_skill_contents()
        with self._state_lock:
            if active.get(name) == content:
                return False
            self._activated_skill_contents[name] = content
            self._activated_skill_cache_loaded = True
            return True

    def activate_skill_context(self, name: str, data: JsonObject) -> bool:
        from core.chat.messages import ChatMessage

        content = data.get("activation_content")
        if not isinstance(content, str) or not content:
            raise ChatSessionError("skill activation context must be a non-empty string")
        if not self.register_skill_activation(name, content):
            return False
        self.append(ChatMessage.note(_skill_context_note_content(name, content)))
        return True

    def activated_skill_contents(self, messages: list[ChatMessage] | None = None) -> dict[str, str]:
        if messages is None:
            return self._load_activated_skill_contents()
        current = current_skill_activation_contents(messages)
        with self._state_lock:
            self._activated_skill_contents = dict(current)
            self._activated_skill_cache_loaded = True
        return dict(current)

    def bookend_timestamps(self) -> tuple[str, str] | None:
        return self._store.bookend_timestamps(self.address)

    def load(self) -> list[ChatMessage]:
        return self._store.messages(self.address)

    async def load_async(self) -> list[ChatMessage]:
        return await _run_session_io(self.load)

    def load_active(self) -> list[ChatMessage]:
        return active_session_messages(self.load())

    async def load_active_async(self) -> list[ChatMessage]:
        return await _run_session_io(self.load_active)

    def load_since(self, cursor: SessionReadCursor | None = None) -> SessionReadBatch | None:
        result = self._store.messages_since(self.address, cursor)
        return None if result is None else SessionReadBatch(tuple(result[0]), result[1])

    async def load_since_async(
        self, cursor: SessionReadCursor | None = None
    ) -> SessionReadBatch | None:
        return await _run_session_io(self.load_since, cursor)

    def delete(self) -> None:
        self._store.delete(self.address)


class ChatSessionManager:
    """One SQLite-only Session service, injected into Runtime consumers."""

    def __init__(
        self, data_dir: Path, store: SessionStore | None = None, *, store_path: Path | None = None
    ) -> None:
        self.data_dir = data_dir
        self._store = store or SessionStore(store_path or data_dir / "sessions.db")
        self._owns_store = store is None
        self._title_changed_callbacks: list[Callable[[SessionAddress], None]] = []
        self._completion_read_callbacks: list[Callable[[SessionAddress], None]] = []
        self._write_locks: dict[SessionAddress, _SessionWriteLock] = {}
        self._write_locks_guard = threading.Lock()

    def close(self) -> None:
        with self._write_locks_guard:
            self._write_locks.clear()
        if self._owns_store:
            self._store.close()

    def add_title_changed_callback(
        self, callback: Callable[[SessionAddress], None]
    ) -> Callable[[], None]:
        self._title_changed_callbacks.append(callback)
        return lambda: (
            self._title_changed_callbacks.remove(callback)
            if callback in self._title_changed_callbacks
            else None
        )

    def add_completion_read_callback(
        self, callback: Callable[[SessionAddress], None]
    ) -> Callable[[], None]:
        self._completion_read_callbacks.append(callback)
        return lambda: (
            self._completion_read_callbacks.remove(callback)
            if callback in self._completion_read_callbacks
            else None
        )

    def write_lock(self, address: SessionAddress) -> _SessionWriteLock:
        _validate_session_id(address.session_id)
        with self._write_locks_guard:
            if address not in self._write_locks:
                self._write_locks[address] = _SessionWriteLock()
            return self._write_locks[address]

    def create(
        self, agent_id: str, session_id: str | None = None, project_id: str | None = None
    ) -> ChatSession:
        _validate_agent_id(agent_id)
        if project_id is not None and not is_valid_project_id(project_id):
            raise ChatSessionError("invalid project id")
        session_id = str(uuid.uuid4()) if session_id is None else session_id
        _validate_session_id(session_id)
        address = SessionAddress(project_id, agent_id, session_id)
        self._store.create(address)
        return ChatSession(self._store, address)

    async def create_async(
        self, agent_id: str, session_id: str | None = None, project_id: str | None = None
    ) -> ChatSession:
        return await _run_session_io(self.create, agent_id, session_id, project_id)

    def exists(self, address: SessionAddress) -> bool:
        _validate_session_id(address.session_id)
        return self._store.exists(address)

    async def exists_async(self, address: SessionAddress) -> bool:
        return await _run_session_io(self.exists, address)

    def get(self, address: SessionAddress) -> ChatSession:
        _validate_session_id(address.session_id)
        self._store.state(address)
        return ChatSession(self._store, address)

    async def get_async(self, address: SessionAddress) -> ChatSession:
        return await _run_session_io(self.get, address)

    def get_or_create(self, address: SessionAddress) -> ChatSession:
        _validate_agent_id(address.agent_id)
        _validate_session_id(address.session_id)
        if address.project_id is not None and not is_valid_project_id(address.project_id):
            raise ChatSessionError("invalid project id")
        self._store.ensure_live(address)
        return ChatSession(self._store, address)

    async def get_or_create_async(self, address: SessionAddress) -> ChatSession:
        return await _run_session_io(self.get_or_create, address)

    def get_metadata(self, address: SessionAddress) -> JsonObject:
        return self._store.metadata(address)

    async def get_metadata_async(self, address: SessionAddress) -> JsonObject:
        return await _run_session_io(self.get_metadata, address)

    def set_metadata(self, address: SessionAddress, data: JsonObject) -> None:
        self._store.replace_metadata(address, data)

    def mutate_metadata(
        self, address: SessionAddress, mutation: Callable[[JsonObject], None]
    ) -> JsonObject:
        """Atomically mutate metadata without overwriting concurrent domain fields."""
        _previous, updated = self.mutate_metadata_with_previous(address, mutation)
        return updated

    def mutate_metadata_with_previous(
        self, address: SessionAddress, mutation: Callable[[JsonObject], None]
    ) -> tuple[JsonObject, JsonObject]:
        """Atomically mutate metadata and return exact before/after snapshots."""
        return self._store.mutate_metadata(address, mutation)

    async def set_metadata_async(self, address: SessionAddress, data: JsonObject) -> None:
        await _run_session_io(self.set_metadata, address, data)

    def prompt_cache_affinity_id(self, address: SessionAddress) -> str:
        value = self.get_metadata(address).get(PROMPT_CACHE_AFFINITY_META_KEY)
        if value is None:
            return _default_prompt_cache_affinity_id(address)
        if not _is_prompt_cache_affinity_id(value):
            raise ChatSessionError(
                f"invalid prompt cache affinity id for session: {address.session_id}"
            )
        return cast(str, value)

    def rotate_prompt_cache_affinity_id(self, address: SessionAddress) -> str:
        value = _new_prompt_cache_affinity_id()
        self._store.mutate_metadata(
            address, lambda metadata: metadata.__setitem__(PROMPT_CACHE_AFFINITY_META_KEY, value)
        )
        return value

    def record_run_kind(self, address: SessionAddress, run_kind: RunKind) -> None:
        if not isinstance(run_kind, RunKind):
            raise ChatSessionError("run kind must be a RunKind")

        def update(metadata: JsonObject) -> None:
            values = metadata.get(SESSION_RUN_KINDS_META_KEY, [])
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value in {kind.value for kind in RunKind}
                for value in values
            ):
                raise ChatSessionError("session run_kinds metadata is invalid")
            if run_kind.value not in values:
                metadata[SESSION_RUN_KINDS_META_KEY] = [*values, run_kind.value]

        self._store.mutate_metadata(address, update)

    def record_terminal_run(
        self, address: SessionAddress, run_id: str, status: str, timestamp: str
    ) -> None:
        if not run_id or status not in SESSION_TERMINAL_RUN_STATUSES or not timestamp:
            raise ChatSessionError("invalid terminal Run completion")

        def update(activity: JsonObject) -> None:
            activity["latest_completion"] = {
                "run_id": run_id,
                "status": status,
                "timestamp": timestamp,
            }

        self._store.mutate_activity(address, update)

    def mark_terminal_run_read(self, address: SessionAddress, run_id: str) -> JsonObject:
        marked = False

        def update(activity: JsonObject) -> None:
            nonlocal marked
            latest = _valid_latest_completion(activity)
            marked = bool(
                latest and latest["run_id"] == run_id and activity.get("read_run_id") != run_id
            )
            if marked:
                activity["read_run_id"] = run_id

        _previous, activity = self._store.mutate_activity(address, update)
        result = _completion_activity_payload(activity)
        result["marked_read"] = marked
        if marked:
            self._notify_callbacks(self._completion_read_callbacks, address)
        return result

    async def mark_terminal_run_read_async(
        self, address: SessionAddress, run_id: str
    ) -> JsonObject:
        return await _run_session_io(self.mark_terminal_run_read, address, run_id)

    def set_title(self, address: SessionAddress, title: str) -> str | None:
        normalized = _normalize_session_title(title)

        def update(metadata: JsonObject) -> None:
            if normalized is None:
                metadata.pop(SESSION_TITLE_KEY, None)
            else:
                metadata[SESSION_TITLE_KEY] = normalized

        previous_metadata, _updated = self._store.mutate_metadata(address, update)
        previous = previous_metadata.get(SESSION_TITLE_KEY)
        if previous != normalized:
            self._notify_callbacks(self._title_changed_callbacks, address)
        return normalized

    async def set_title_async(self, address: SessionAddress, title: str) -> str | None:
        return await _run_session_io(self.set_title, address, title)

    def set_auto_title(
        self, address: SessionAddress, title: str, *, initialized: bool = True
    ) -> str | None:
        normalized = _normalize_session_title(title)

        def update(metadata: JsonObject) -> None:
            if normalized is None:
                metadata.pop(SESSION_AUTO_TITLE_KEY, None)
            else:
                metadata[SESSION_AUTO_TITLE_KEY] = normalized
            if initialized:
                metadata[SESSION_AUTO_TITLE_INITIALIZED_KEY] = True

        previous_metadata, _updated = self._store.mutate_metadata(address, update)
        previous = previous_metadata.get(SESSION_AUTO_TITLE_KEY)
        if previous != normalized:
            self._notify_callbacks(self._title_changed_callbacks, address)
        return normalized

    def reset_auto_title(self, address: SessionAddress) -> None:
        def update(metadata: JsonObject) -> None:
            metadata.pop(SESSION_AUTO_TITLE_KEY, None)
            metadata.pop(SESSION_AUTO_TITLE_INITIALIZED_KEY, None)

        self._store.mutate_metadata(address, update)

    def mark_auto_title_initialized(self, address: SessionAddress) -> None:
        self._store.mutate_metadata(
            address,
            lambda metadata: metadata.__setitem__(SESSION_AUTO_TITLE_INITIALIZED_KEY, True),
        )

    def list(self, agent_id: str, project_id: str | None = None) -> list[ChatSession]:
        return [
            ChatSession(self._store, address)
            for address in self._store.list_addresses(project_id=project_id, agent_id=agent_id)
        ]

    async def list_async(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[ChatSession]:
        return await _run_session_io(self.list, agent_id, project_id)

    def list_addresses(self, project_id: str | None = None) -> builtins.list[SessionAddress]:
        return self._store.list_addresses(project_id=project_id, agent_id=None)

    def list_with_metadata(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[JsonObject]:
        result: builtins.list[JsonObject] = []
        for state in self._store.list_state_rows(project_id, agent_id):
            summary = _decode_state_object(state["metadata_json"], "Session metadata")
            summary.update(_completion_activity_from_state(state))
            summary.update(
                id=state["session_id"],
                created_at=state["created_at"],
                last_active_at=state["last_message_at"] or state["created_at"],
            )
            result.append(summary)
        return result

    async def list_with_metadata_async(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[JsonObject]:
        return await _run_session_io(self.list_with_metadata, agent_id, project_id)

    def list_completion_activity(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[JsonObject]:
        result: builtins.list[JsonObject] = []
        for state in self._store.list_state_rows(project_id, agent_id):
            result.append({"id": state["session_id"], **_completion_activity_from_state(state)})
        return result

    async def list_completion_activity_async(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[JsonObject]:
        return await _run_session_io(self.list_completion_activity, agent_id, project_id)

    def list_history_revisions(
        self, agent_id: str, project_id: str | None = None
    ) -> builtins.list[tuple[SessionAddress, str, int]]:
        return self._store.list_history_revisions(project_id, agent_id)

    def history_version(self, address: SessionAddress) -> tuple[str, int]:
        state = self._store.state(address)
        return str(state["generation_id"]), int(state["history_revision"])

    def history_revision(self, address: SessionAddress) -> int:
        return int(self._store.state(address)["history_revision"])

    def retarget_identity_agent_references(
        self, old_agent_id: str, new_agent_id: str
    ) -> tuple[SessionIdentityReferenceUpdate, ...]:
        updates: builtins.list[SessionIdentityReferenceUpdate] = []
        for address in self._store.list_addresses(include_all_scopes=True):
            changed = False

            def retarget(metadata: JsonObject) -> None:
                nonlocal changed
                parent = metadata.get("subagent_parent")
                if (
                    not isinstance(parent, dict)
                    or parent.get("project_id") is not None
                    or parent.get("agent_id") != old_agent_id
                ):
                    return
                parent = dict(parent)
                parent["agent_id"] = new_agent_id
                metadata["subagent_parent"] = parent
                changed = True

            previous, _updated = self.mutate_metadata_with_previous(address, retarget)
            if changed:
                updates.append(SessionIdentityReferenceUpdate(address, previous))
        return tuple(updates)

    def restore_identity_agent_references(
        self, updates: tuple[SessionIdentityReferenceUpdate, ...]
    ) -> None:
        for update in reversed(updates):
            self.set_metadata(update.address, update.previous_metadata)

    async def move(
        self,
        source: SessionAddress,
        target: SessionAddress,
        *,
        strip_meta_keys: frozenset[str] = frozenset(),
    ) -> ChatSession:
        _validate_session_id(source.session_id)
        _validate_session_id(target.session_id)
        async with self.write_lock(source):
            return await _run_session_io(self._move, source, target, strip_meta_keys)

    def _move(
        self, source: SessionAddress, target: SessionAddress, strip_meta_keys: frozenset[str]
    ) -> ChatSession:
        metadata = {
            key: value
            for key, value in self.get_metadata(source).items()
            if key not in strip_meta_keys
        }
        self._store.move(source, target, metadata)
        return ChatSession(self._store, target)

    async def fork(
        self,
        source: SessionAddress,
        *,
        target_agent_id: str | None = None,
        target_project_id: str | None = None,
        strip_meta_keys: frozenset[str] = frozenset(),
    ) -> ChatSession:
        _validate_session_id(source.session_id)
        async with self.write_lock(source):
            return await _run_session_io(
                self._fork,
                source,
                target_agent_id or source.agent_id,
                target_project_id,
                strip_meta_keys,
            )

    def _fork(
        self,
        source: SessionAddress,
        target_agent_id: str,
        target_project_id: str | None,
        strip_meta_keys: frozenset[str],
    ) -> ChatSession:
        _validate_agent_id(target_agent_id)
        metadata = {
            key: value
            for key, value in self.get_metadata(source).items()
            if key not in strip_meta_keys
        }
        same_scope = target_agent_id == source.agent_id and target_project_id == source.project_id
        metadata[PROMPT_CACHE_AFFINITY_META_KEY] = (
            self.prompt_cache_affinity_id(source) if same_scope else _new_prompt_cache_affinity_id()
        )
        target = SessionAddress(target_project_id, target_agent_id, str(uuid.uuid4()))
        metadata[FORK_SOURCE_META_KEY] = {
            "agent_id": source.agent_id,
            "session_id": source.session_id,
            "project_id": source.project_id,
            "forked_at": _format_timestamp(datetime.now(UTC)),
            "message_count": int(self._store.state(source)["message_count"]),
        }
        self._store.fork(source, target, metadata)
        return ChatSession(self._store, target)

    def delete(self, address: SessionAddress) -> None:
        self._store.delete(address)

    async def archive(self, address: SessionAddress) -> None:
        async with self.write_lock(address):
            await _run_session_io(self._store.archive, address)

    def restore(self, address: SessionAddress) -> None:
        self._store.restore(address)

    def retarget_identity_agent_sessions(self, old_agent_id: str, new_agent_id: str) -> None:
        self._store.retarget_identity_agent(old_agent_id, new_agent_id)

    def archive_identity_agent_sessions(self, agent_id: str) -> None:
        self._store.archive_identity_agent_sessions(agent_id)

    def archive_project_sessions(self, project_id: str) -> None:
        self._store.archive_project_sessions(project_id)

    @staticmethod
    def _notify_callbacks(
        callbacks: Sequence[Callable[[SessionAddress], None]], address: SessionAddress
    ) -> None:
        for callback in list(callbacks):
            try:
                callback(address)
            except Exception:
                logging.getLogger(__name__).exception("Session callback failed")
