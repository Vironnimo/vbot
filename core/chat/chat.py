"""Chat message, JSONL session primitives, and chat loop execution."""

from __future__ import annotations

import builtins
import inspect
import json
import os
import re
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from core.chat.content_blocks import (
    ContentBlock,
    ContentBlockError,
    FileBlock,
    MediaBlock,
    TextBlock,
    content_block_from_dict,
    content_block_to_dict,
)
from core.chat.runs import (
    ASSISTANT_OUTPUT_EVENT,
    COMPACTION_COMPLETED_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT,
    REASONING_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    USER_MESSAGE_EVENT,
    ChatRunManager,
    Run,
)
from core.chat.streaming import (
    STREAM_CHUNK_TIMEOUT_SECONDS,
    StreamingAccumulator,
    StreamingChunkTimeoutError,
    iter_with_chunk_timeout,
)
from core.extensions import ExtensionRegistry, HookContext
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderTimeoutError,
)
from core.tools import ToolCall as ScheduledToolCall
from core.tools import (
    ToolContext,
    ToolExecutionConfig,
    ToolExecutor,
    ToolNotAllowedError,
    ToolNotFoundError,
    ToolRegistry,
    is_tool_result_envelope,
    tool_failure,
    tool_success,
)
from core.tools.skill import load_skill_content
from core.utils.errors import ConfigError, ProviderError, VBotError
from core.utils.logging import get_logger
from core.utils.tokens import estimate_tokens

if TYPE_CHECKING:
    from core.chat.block_resolver import ContentBlockResolver
    from core.chat.compaction import CompactionService

MessageRole = Literal[
    "system",
    "user",
    "assistant",
    "tool",
    "note",
    "error",
    "compaction_checkpoint",
]
JsonObject = dict[str, Any]

_LOGGER = get_logger("chat")

TIMESTAMP_SUFFIX = "+00:00"
UTC_Z_SUFFIX = "Z"
SESSION_FILE_EXTENSION = ".jsonl"
SESSION_LINE_ENDING = "\n"
MAX_TOOL_ITERATIONS = 1000
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SYSTEM_REMINDER_OPEN_TAG = "<system-reminder>"
SYSTEM_REMINDER_CLOSE_TAG = "</system-reminder>"
SKILL_SLASH_TRIGGER_PATTERN = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?=\s|$)")
SKILL_INLINE_TRIGGER_PATTERN = re.compile(r"\$([A-Za-z0-9][A-Za-z0-9_-]{0,63})")
SKILL_CONTEXT_NOTE_PREFIX = "[skill-context] "
ERROR_KIND_RATE_LIMIT = "rate_limit"
ERROR_KIND_TIMEOUT = "timeout"
ERROR_KIND_NETWORK = "network_error"
ERROR_KIND_PROVIDER_OVERLOAD = "provider_overloaded"
ERROR_KIND_TOOL_ITERATIONS = "tool_iterations_exceeded"
ERROR_KIND_AUTH = "auth_error"
ERROR_KIND_PROVIDER_FATAL = "provider_fatal"
ERROR_KIND_CONFIG = "config_error"
ERROR_KIND_PROVIDER_ERROR = "provider_error"
ERROR_KIND_LLM_VISIBLE: dict[str, bool] = {
    ERROR_KIND_RATE_LIMIT: True,
    ERROR_KIND_TIMEOUT: True,
    ERROR_KIND_NETWORK: True,
    ERROR_KIND_PROVIDER_OVERLOAD: True,
    ERROR_KIND_TOOL_ITERATIONS: True,
    ERROR_KIND_AUTH: False,
    ERROR_KIND_PROVIDER_FATAL: False,
    ERROR_KIND_CONFIG: False,
    ERROR_KIND_PROVIDER_ERROR: True,
}


class ChatError(VBotError):
    """Base error for chat domain failures."""


class ChatMessageValidationError(ChatError):
    """Raised when a canonical chat message is invalid."""


class ChatSessionError(ChatError):
    """Raised when a chat session file operation cannot be completed."""


class ToolIterationLimitError(ChatError):
    """Raised when a chat run exceeds its configured tool-iteration limit."""


@dataclass(frozen=True)
class ToolCall:
    """A canonical assistant-requested tool call."""

    id: str
    name: str
    arguments: JsonObject = field(default_factory=dict)

    def to_dict(self) -> JsonObject:
        """Return a JSON-serializable tool call dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "arguments": dict(self.arguments),
        }

    @classmethod
    def from_dict(cls, data: JsonObject) -> ToolCall:
        """Build a tool call from a JSON object."""
        tool_call_id = _require_string(data, "id")
        name = _require_string(data, "name")
        arguments = data.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ChatMessageValidationError("tool call arguments must be an object")
        return cls(id=tool_call_id, name=name, arguments=dict(arguments))


@dataclass(frozen=True)
class ChatMessage:
    """Canonical message persisted to session JSONL files."""

    id: str
    timestamp: str
    role: MessageRole
    content: str | list[ContentBlock] | None = None
    model: str | None = None
    reasoning: str | None = None
    reasoning_meta: JsonObject | None = None
    usage: JsonObject | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    error_kind: str | None = None
    tail_boundary_id: str | None = None

    @classmethod
    def system(cls, content: str, model: str, *, timestamp: datetime | None = None) -> ChatMessage:
        """Create a system message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="system",
            model=model,
            content=content,
        )

    @classmethod
    def user(
        cls,
        content: str | list[ContentBlock],
        *,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a user message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="user",
            content=content,
        )

    @classmethod
    def note(cls, content: str, *, timestamp: datetime | None = None) -> ChatMessage:
        """Create a kernel-internal note message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="note",
            content=content,
        )

    @classmethod
    def error(
        cls,
        error_kind: str,
        content: str,
        *,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a persisted error message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="error",
            content=content,
            error_kind=error_kind,
        )

    @classmethod
    def assistant(
        cls,
        *,
        model: str,
        content: str | None,
        reasoning: str | None = None,
        reasoning_meta: JsonObject | None = None,
        usage: JsonObject | None = None,
        tool_calls: list[ToolCall] | None = None,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create an assistant message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="assistant",
            model=model,
            content=content,
            reasoning=reasoning,
            reasoning_meta=dict(reasoning_meta) if reasoning_meta is not None else None,
            usage=dict(usage) if usage is not None else None,
            tool_calls=list(tool_calls) if tool_calls is not None else None,
        )

    @classmethod
    def tool(
        cls,
        *,
        tool_call_id: str,
        name: str,
        content: str,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a tool result message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="tool",
            content=content,
            tool_call_id=tool_call_id,
            name=name,
        )

    @classmethod
    def compaction_checkpoint(
        cls,
        *,
        summary: str,
        tail_boundary_id: str,
        compacted_token_count: int,
        timestamp: datetime | None = None,
    ) -> ChatMessage:
        """Create a compaction checkpoint message."""
        return cls(
            id=_new_message_id(),
            timestamp=_format_timestamp(timestamp),
            role="compaction_checkpoint",
            content=summary,
            usage={"compacted_token_count": compacted_token_count},
            tail_boundary_id=tail_boundary_id,
        )

    def to_dict(self) -> JsonObject:
        """Return a canonical JSON-serializable message dictionary."""
        self.validate()
        message: JsonObject = {
            "id": self.id,
            "timestamp": self.timestamp,
            "role": self.role,
        }
        _add_if_not_none(message, "model", self.model)
        if self.content is not None:
            if isinstance(self.content, list):
                message["content"] = [content_block_to_dict(block) for block in self.content]
            else:
                message["content"] = self.content
        _add_if_not_none(message, "reasoning", self.reasoning)
        _add_if_not_none(message, "reasoning_meta", self.reasoning_meta)
        _add_if_not_none(message, "usage", self.usage)
        if self.tool_calls is not None:
            message["tool_calls"] = [tool_call.to_dict() for tool_call in self.tool_calls]
        _add_if_not_none(message, "tool_call_id", self.tool_call_id)
        _add_if_not_none(message, "name", self.name)
        _add_if_not_none(message, "error_kind", self.error_kind)
        _add_if_not_none(message, "tail_boundary_id", self.tail_boundary_id)
        return message

    @classmethod
    def from_dict(cls, data: JsonObject) -> ChatMessage:
        """Build a chat message from a canonical JSON object."""
        role = _require_role(data)
        tool_calls = _parse_tool_calls(data.get("tool_calls"))
        reasoning_meta = data.get("reasoning_meta")
        if reasoning_meta is not None and not isinstance(reasoning_meta, dict):
            raise ChatMessageValidationError("reasoning_meta must be an object")
        usage = data.get("usage")
        if usage is not None and not isinstance(usage, dict):
            raise ChatMessageValidationError("usage must be an object")

        message = cls(
            id=_require_string(data, "id"),
            timestamp=_require_string(data, "timestamp"),
            role=role,
            content=_parse_content(data),
            model=_optional_string(data, "model"),
            reasoning=_optional_string(data, "reasoning"),
            reasoning_meta=dict(reasoning_meta) if reasoning_meta is not None else None,
            usage=dict(usage) if usage is not None else None,
            tool_calls=tool_calls,
            tool_call_id=_optional_string(data, "tool_call_id"),
            name=_optional_string(data, "name"),
            error_kind=_optional_string(data, "error_kind"),
            tail_boundary_id=_optional_string(data, "tail_boundary_id"),
        )
        message.validate()
        return message

    def validate(self) -> None:
        """Validate this message against the role-specific canonical schema."""
        _validate_core_fields(self)
        match self.role:
            case "system":
                _validate_system_message(self)
            case "user":
                _validate_user_message(self)
            case "assistant":
                _validate_assistant_message(self)
            case "tool":
                _validate_tool_message(self)
            case "note":
                _validate_note_message(self)
            case "error":
                _validate_error_message(self)
            case "compaction_checkpoint":
                _validate_compaction_checkpoint_message(self)


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="") as session_file:
            session_file.write(payload + SESSION_LINE_ENDING)

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

    def skill_context_messages(self) -> list[JsonObject]:
        """Return currently activated skill context as provider request messages."""
        activated_contents = self._load_activated_skill_contents()
        return [
            {"role": "user", "content": content}
            for _name, content in sorted(activated_contents.items())
        ]

    def _load_activated_skill_contents(self) -> dict[str, str]:
        if self._activated_skill_contents:
            return dict(self._activated_skill_contents)

        activated_contents = _skill_contexts_from_messages(self.load())
        self._activated_skill_names = set(activated_contents)
        self._activated_skill_contents = dict(activated_contents)
        return activated_contents

    def _persist_skill_context_note(self, name: str, content: str) -> None:
        self.append(ChatMessage.note(_skill_context_note_content(name, content)))

    def load(self) -> list[ChatMessage]:
        """Load all valid JSONL messages from this session file."""
        if not self.path.exists():
            raise ChatSessionError(f"session does not exist: {self.path}")

        messages: list[ChatMessage] = []
        with self.path.open("r", encoding="utf-8") as session_file:
            for line_number, line in enumerate(session_file, start=1):
                if not line.strip():
                    continue
                messages.append(self._parse_line(line, line_number))
        return messages

    def delete(self) -> None:
        """Delete the session file if it exists."""
        self.path.unlink(missing_ok=True)

    @staticmethod
    def _parse_line(line: str, line_number: int) -> ChatMessage:
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ChatSessionError(f"invalid JSON at line {line_number}") from exc

        if not isinstance(data, dict):
            raise ChatSessionError(f"message at line {line_number} must be an object")

        try:
            return ChatMessage.from_dict(data)
        except ChatMessageValidationError as exc:
            raise ChatSessionError(f"invalid message at line {line_number}: {exc}") from exc


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


class _EmittingToolRegistry(ToolRegistry):
    """Adapter that emits public lifecycle events around registry dispatch."""

    def __init__(
        self,
        registry: Any,
        run: Run,
        extension_registry: ExtensionRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._run = run
        self._extension_registry = extension_registry

    async def dispatch(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None = None,
    ) -> JsonObject:
        self._run.raise_if_cancelled()
        original_arguments = deepcopy(arguments)
        self._run.emit(
            TOOL_CALL_STARTED_EVENT,
            {
                "tool_call": {
                    "id": context.tool_call_id,
                    "index": context.tool_call_index,
                    "name": context.tool_name,
                    "arguments": original_arguments,
                }
            },
        )
        result: JsonObject | None = None
        if self._extension_registry is not None:
            ctx = HookContext(session_id=self._run.session_id, agent_id=self._run.agent_id)
            for extension_name, handler in self._extension_registry._handlers.get(
                "tool_call",
                [],
            ):
                try:
                    hook_result = handler(
                        ctx,
                        tool_name=context.tool_name,
                        tool_call_id=context.tool_call_id,
                        input=arguments,
                    )
                    if inspect.isawaitable(hook_result):
                        hook_result = await hook_result
                except Exception as exc:
                    _LOGGER.warning(
                        "Extension %r tool_call handler raised: %s",
                        extension_name,
                        exc,
                    )
                    continue
                if isinstance(hook_result, dict):
                    validated_override = _validated_extension_tool_hook_result(
                        tool_name=context.tool_name,
                        extension_name=extension_name,
                        hook_name="tool_call",
                        result=hook_result,
                    )
                    if validated_override is None:
                        continue
                    result = validated_override
                    break

        if result is None:
            result = await self._dispatch_with_failure_envelope(context, arguments, allowed_tools)

        if self._extension_registry is not None:
            ctx = HookContext(session_id=self._run.session_id, agent_id=self._run.agent_id)
            for extension_name, handler in self._extension_registry._handlers.get(
                "tool_result",
                [],
            ):
                try:
                    hook_result = handler(
                        ctx,
                        tool_name=context.tool_name,
                        tool_call_id=context.tool_call_id,
                        input=arguments,
                        result=result,
                    )
                    if inspect.isawaitable(hook_result):
                        hook_result = await hook_result
                except Exception as exc:
                    _LOGGER.warning(
                        "Extension %r tool_result handler raised: %s",
                        extension_name,
                        exc,
                    )
                    continue
                if isinstance(hook_result, dict):
                    patched_result = dict(result)
                    patched_result.update(hook_result)
                    validated_patch = _validated_extension_tool_hook_result(
                        tool_name=context.tool_name,
                        extension_name=extension_name,
                        hook_name="tool_result",
                        result=patched_result,
                    )
                    if validated_patch is not None:
                        result = validated_patch

        self._run.raise_if_cancelled()
        self._run.emit(
            TOOL_CALL_RESULT_EVENT,
            {
                "tool_call": {
                    "id": context.tool_call_id,
                    "index": context.tool_call_index,
                    "name": context.tool_name,
                },
                "result": result,
            },
        )
        return result

    async def _dispatch_with_failure_envelope(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None,
    ) -> JsonObject:
        try:
            return await self._dispatch_with_current_registry_signature(
                context,
                arguments,
                allowed_tools,
            )
        except ToolNotFoundError as error:
            return tool_failure("tool_not_found", str(error))
        except ToolNotAllowedError as error:
            return tool_failure("tool_not_allowed", str(error))
        except ValueError as error:
            return tool_failure(
                "invalid_tool_result" if "return" in str(error) else "invalid_arguments",
                str(error),
            )
        except Exception as error:
            return tool_failure("tool_execution_error", str(error))

    async def _dispatch_with_current_registry_signature(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None,
    ) -> JsonObject:
        try:
            result = await self._registry.dispatch(context, arguments, allowed_tools)
            return _validated_tool_result(context.tool_name, result)
        except TypeError as error:
            if not _looks_like_legacy_dispatch_type_error(error):
                raise
            return await self._dispatch_legacy(context, arguments, allowed_tools)

    async def _dispatch_legacy(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None,
    ) -> JsonObject:
        try:
            result = self._registry.dispatch(context.tool_name, arguments, allowed_tools)
            if inspect.isawaitable(result):
                result = await result
            return _validated_tool_result(context.tool_name, result)
        except ToolNotFoundError as error:
            return tool_failure("tool_not_found", str(error))
        except ToolNotAllowedError as error:
            return tool_failure("tool_not_allowed", str(error))
        except ValueError as error:
            return tool_failure(
                "invalid_tool_result" if "return" in str(error) else "invalid_arguments",
                str(error),
            )
        except Exception as error:
            return tool_failure("tool_execution_error", str(error))


class ChatLoop:
    """Minimal agentic chat loop."""

    def __init__(
        self,
        runtime: Any,
        *,
        max_tool_iterations: int = MAX_TOOL_ITERATIONS,
        streaming: bool = False,
        attachment_resolver: ContentBlockResolver | None = None,
        compaction_service: CompactionService | None = None,
    ) -> None:
        if max_tool_iterations < 0:
            raise ChatError("max tool iterations must not be negative")
        self._runtime = runtime
        self._max_tool_iterations = max_tool_iterations
        self._streaming = streaming
        self._attachment_resolver = attachment_resolver
        self._compaction_service = compaction_service
        self._nesting_depth = 0

    async def send(
        self,
        agent_id: str,
        content: str | list[ContentBlock],
        *,
        session_id: str | None = None,
    ) -> ChatMessage:
        """Run one persisted non-streaming chat turn and return the final assistant message."""
        run = await self._start_run(agent_id, content, session_id=session_id, create_missing=True)
        return cast(ChatMessage, await run.wait())

    async def start_run(
        self,
        agent_id: str,
        content: str | list[ContentBlock],
        *,
        session_id: str,
        internal: bool = False,
    ) -> Run:
        """Start one chat run against an existing session for server-facing callers."""
        return await self._start_run(
            agent_id,
            content,
            session_id=session_id,
            create_missing=False,
            internal=internal,
        )

    async def retry_run(self, agent_id: str, session_id: str) -> Run:
        """Retry the last user turn without adding a new user message.

        Only valid when the session already contains at least one user message.
        """
        session = self._get_session(agent_id, session_id, create_missing=False)
        messages = session.load()
        if not any(message.role == "user" for message in messages):
            raise ChatSessionError("no user message in session to retry")
        manager = _runtime_run_manager(self._runtime)
        return await manager.start(
            agent_id=agent_id,
            session_id=session.id,
            executor=lambda run: self._execute_run(run, content=None, retry=True),
        )

    async def _start_run(
        self,
        agent_id: str,
        content: str | list[ContentBlock] | None = None,
        *,
        session_id: str | None,
        create_missing: bool,
        internal: bool = False,
    ) -> Run:
        agent = self._runtime.agents.get(agent_id)
        provider_id, _connection_id = _resolve_agent_connection(self._runtime, agent)
        _ensure_provider_exists(self._runtime.providers, provider_id)
        session = self._get_session(agent_id, session_id, create_missing=create_missing)
        manager = _runtime_run_manager(self._runtime)
        return await manager.start(
            agent_id=agent_id,
            session_id=session.id,
            executor=lambda run: self._execute_run(run, content, internal=internal),
        )

    async def _execute_run(
        self,
        run: Run,
        content: str | list[ContentBlock] | None = None,
        *,
        internal: bool = False,
        retry: bool = False,
    ) -> ChatMessage:
        agent = self._runtime.agents.get(run.agent_id)
        _model_provider_id, model_id = _split_agent_model(agent.model)
        provider_id, connection_id = _resolve_agent_connection(self._runtime, agent)
        _ensure_provider_exists(self._runtime.providers, provider_id)
        adapter = self._runtime.get_adapter(provider_id, connection_id)
        run.add_cancel_callback(lambda: _close_adapter(adapter))
        process_manager = getattr(self._runtime, "process_manager", None)
        if process_manager is not None:
            run.add_cancel_callback(lambda: process_manager.cancel_scope(run.id))
        session = cast(ChatSessionManager, self._runtime.chat_sessions).get(
            run.agent_id,
            run.session_id,
        )
        _run_succeeded = True

        try:
            extension_registry = _runtime_extensions(self._runtime)
            if extension_registry is not None:
                extension_ctx = HookContext(session_id=run.session_id, agent_id=run.agent_id)
                for extension_name, handler in extension_registry._handlers.get(
                    "run_start",
                    [],
                ):
                    try:
                        hook_result = handler(
                            extension_ctx,
                            session_id=run.session_id,
                            agent_id=run.agent_id,
                        )
                        if inspect.isawaitable(hook_result):
                            await hook_result
                    except Exception as exc:
                        _LOGGER.warning(
                            "Extension %r run_start handler raised: %s",
                            extension_name,
                            exc,
                        )

            run.raise_if_cancelled()
            if retry:
                pass
            elif internal:
                if not isinstance(content, str):
                    raise ChatError("internal runs require string content")
                session.add_note(content)
            else:
                if content is None:
                    raise ChatError("content is required for non-retry runs")
                user_message = ChatMessage.user(content)
                session.append(user_message)
                _emit_message_event(run, USER_MESSAGE_EVENT, user_message)
                if isinstance(content, str):
                    self._activate_triggered_skills(agent, session, content)
            run.raise_if_cancelled()
            messages = self._build_request_messages(agent, session)
            tools = self._runtime.system_prompts.provider_tool_definitions(agent)

            extension_registry = _runtime_extensions(self._runtime)
            if extension_registry is not None:
                extension_ctx = HookContext(session_id=run.session_id, agent_id=run.agent_id)
                prompt_appends: list[str] = []
                for extension_name, handler in extension_registry._handlers.get(
                    "before_agent_start",
                    [],
                ):
                    try:
                        hook_result = handler(
                            extension_ctx,
                            agent=agent,
                            session=session,
                            messages=messages,
                            run=run,
                        )
                        if inspect.isawaitable(hook_result):
                            hook_result = await hook_result
                    except Exception as exc:
                        _LOGGER.warning(
                            "Extension %r before_agent_start handler raised: %s",
                            extension_name,
                            exc,
                        )
                        continue
                    if isinstance(hook_result, dict) and isinstance(
                        hook_result.get("system_prompt_append"),
                        str,
                    ):
                        prompt_appends.append(hook_result["system_prompt_append"])

                if prompt_appends and messages:
                    system_content = messages[0].get("content")
                    if isinstance(system_content, str):
                        messages[0] = dict(messages[0])
                        messages[0]["content"] = system_content + "\n" + "\n".join(prompt_appends)
                    else:
                        _LOGGER.debug(
                            "before_agent_start: system message content is not a string; "
                            "skipping append"
                        )

            try:
                return await self._send_until_final(
                    agent,
                    adapter,
                    model_id,
                    session,
                    messages,
                    tools,
                    run,
                )
            except ProviderError as primary_exc:
                if _is_model_fallback_trigger(primary_exc):
                    fallback = _resolve_fallback(self._runtime, agent)
                    if fallback is not None:
                        fallback_model_str, fb_provider_id, fb_connection_id = fallback
                        _, fallback_model_id = _split_agent_model(fallback_model_str)
                        try:
                            fallback_adapter = self._runtime.get_adapter(
                                fb_provider_id,
                                fb_connection_id,
                            )
                        except (ConfigError, VBotError) as construction_exc:
                            _run_succeeded = False
                            _persist_run_error(run, session, construction_exc)
                            raise
                        run.add_cancel_callback(lambda: _close_adapter(fallback_adapter))
                        run.emit(
                            MODEL_FALLBACK_ACTIVATED_EVENT,
                            {"from_model": agent.model, "to_model": fallback_model_str},
                        )
                        session.add_note(
                            "Primary model unavailable. Switched to "
                            f"{fallback_model_str} for this run."
                        )
                        try:
                            return await self._send_until_final(
                                agent,
                                fallback_adapter,
                                fallback_model_id,
                                session,
                                messages,
                                tools,
                                run,
                            )
                        except (ProviderError, ChatError, ConfigError, VBotError) as fallback_exc:
                            _run_succeeded = False
                            _persist_run_error(run, session, fallback_exc)
                            raise fallback_exc
                        finally:
                            await _close_adapter(fallback_adapter)

                _run_succeeded = False
                _persist_run_error(run, session, primary_exc)
                raise
            except (ChatError, ConfigError, VBotError) as exc:
                _run_succeeded = False
                _persist_run_error(run, session, exc)
                raise
        finally:
            outcome: Literal["success", "error", "cancelled"]
            if run.cancel_requested:
                outcome = "cancelled"
            elif _run_succeeded:
                outcome = "success"
            else:
                outcome = "error"

            extension_registry = _runtime_extensions(self._runtime)
            if extension_registry is not None:
                extension_ctx = HookContext(session_id=run.session_id, agent_id=run.agent_id)
                for extension_name, handler in extension_registry._handlers.get(
                    "run_end",
                    [],
                ):
                    try:
                        hook_result = handler(
                            extension_ctx,
                            session_id=run.session_id,
                            agent_id=run.agent_id,
                            outcome=outcome,
                        )
                        if inspect.isawaitable(hook_result):
                            await hook_result
                    except Exception as exc:
                        _LOGGER.warning(
                            "Extension %r run_end handler raised: %s",
                            extension_name,
                            exc,
                        )

            await _close_adapter(adapter)

    def _get_session(
        self,
        agent_id: str,
        session_id: str | None,
        *,
        create_missing: bool,
    ) -> ChatSession:
        session_manager = cast(ChatSessionManager, self._runtime.chat_sessions)
        if session_id is None:
            if not create_missing:
                raise ChatSessionError("session id is required")
            return session_manager.create(agent_id)
        try:
            return session_manager.get(agent_id, session_id)
        except ChatSessionError:
            if not create_missing:
                raise
            return session_manager.create(agent_id, session_id=session_id)

    def _build_request_messages(self, agent: Any, session: ChatSession) -> list[JsonObject]:
        system_prompt = self._runtime.system_prompts.build_system_prompt(agent)
        system_message = ChatMessage.system(system_prompt, agent.model)
        session_messages = session.load()
        checkpoint = _latest_compaction_checkpoint(session_messages)

        if checkpoint is None:
            history = _embed_notes_into_request(session_messages)
            request_messages = [
                system_message.to_dict(),
                *session.skill_context_messages(),
                *history,
            ]
        else:
            if checkpoint.tail_boundary_id is None:
                raise ChatError("compaction checkpoint is missing tail boundary")

            tail_messages = [
                message
                for message in _messages_from_boundary(
                    session_messages,
                    checkpoint.tail_boundary_id,
                )
                if message.role != "compaction_checkpoint"
            ]
            summary_text = checkpoint.content if isinstance(checkpoint.content, str) else ""
            summary_synthetic_message: JsonObject = {
                "role": "user",
                "content": (
                    f"{SYSTEM_REMINDER_OPEN_TAG}\n{summary_text}\n{SYSTEM_REMINDER_CLOSE_TAG}"
                ),
            }
            history = _embed_notes_into_request(tail_messages)
            request_messages = [
                system_message.to_dict(),
                *session.skill_context_messages(),
                summary_synthetic_message,
                *history,
            ]

        session.drain_pending_notes()

        if self._attachment_resolver is None:
            return request_messages
        if not _session_has_any_content_blocks(session_messages):
            return request_messages

        # Use the most recently appended user turn as the current-turn marker.
        # If that turn is plain text, all content blocks resolve as historical.
        current_user_message = _last_user_message_with_content_blocks(
            session_messages
        ) or _last_user_message(session_messages)
        if current_user_message is None:
            return request_messages

        return self._attachment_resolver.resolve_messages(
            request_messages,
            current_user_message_id=current_user_message.id,
            vision_supported=_model_has_vision(self._runtime, agent),
        )

    async def _send_until_final(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        session: ChatSession,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
    ) -> ChatMessage:
        tool_iteration_count = 0
        for _ in range(self._max_tool_iterations + 1):
            run.raise_if_cancelled()
            pending_notes = session.drain_pending_notes()
            if pending_notes:
                messages.append(_notes_to_synthetic_user_message(pending_notes))
            _sync_skill_context_messages(messages, session)
            extension_registry = _runtime_extensions(self._runtime)
            messages_for_request = [dict(message) for message in messages]
            if extension_registry is not None:
                extension_ctx = HookContext(session_id=run.session_id, agent_id=run.agent_id)
                for extension_name, handler in extension_registry._handlers.get(
                    "context",
                    [],
                ):
                    try:
                        hook_result = handler(extension_ctx, messages=messages_for_request)
                        if inspect.isawaitable(hook_result):
                            hook_result = await hook_result
                    except Exception as exc:
                        _LOGGER.warning(
                            "Extension %r context handler raised: %s",
                            extension_name,
                            exc,
                        )
                        continue
                    if isinstance(hook_result, list):
                        messages_for_request = hook_result
                        break

            assistant_message = await self._send_assistant_request(
                agent,
                adapter,
                model_id,
                messages_for_request,
                tools,
                run,
                note_hook=session.add_note,
            )
            run.raise_if_cancelled()
            if assistant_message.usage is None:
                assistant_message = _apply_usage_estimation(assistant_message, messages)
            session.append(assistant_message)
            if not self._streaming:
                _emit_assistant_events(run, assistant_message)
            messages.append(assistant_message.to_dict())

            if not assistant_message.tool_calls:
                if self._compaction_service is not None:
                    messages = await self._maybe_auto_compact(
                        agent,
                        adapter,
                        model_id,
                        session,
                        messages,
                        usage=assistant_message.usage,
                        run=run,
                    )
                return assistant_message

            if tool_iteration_count >= self._max_tool_iterations:
                raise ToolIterationLimitError("maximum tool iterations exceeded")
            tool_iteration_count += 1

            session.begin_defer_notes()
            try:
                tool_messages = await self._dispatch_tool_calls(
                    agent,
                    assistant_message.tool_calls,
                    session,
                    run,
                )
                for tool_message in tool_messages:
                    run.raise_if_cancelled()
                    session.append(tool_message)
                    messages.append(tool_message.to_dict())
            finally:
                session.flush_deferred_notes()

            if self._compaction_service is not None:
                messages = await self._maybe_auto_compact(
                    agent,
                    adapter,
                    model_id,
                    session,
                    messages,
                    usage=None,
                    run=run,
                )

        raise ToolIterationLimitError("maximum tool iterations exceeded")

    async def _maybe_auto_compact(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        session: ChatSession,
        messages: list[JsonObject],
        usage: JsonObject | None,
        *,
        run: Run,
    ) -> list[JsonObject]:
        """Auto-compact when configured token thresholds are exceeded."""
        if self._compaction_service is None:
            return messages

        storage = getattr(self._runtime, "storage", None)
        if storage is None:
            return messages

        load_compaction_settings = getattr(storage, "load_compaction_settings", None)
        if not callable(load_compaction_settings):
            return messages

        from core.chat.compaction import CompactionSettings

        raw_settings = load_compaction_settings()
        settings = CompactionSettings(
            auto=bool(raw_settings["auto"]),
            threshold=float(raw_settings["threshold"]),
            tail_tokens=int(raw_settings["tail_tokens"]),
            summary_model=raw_settings["summary_model"],
        )
        if not settings.auto:
            return messages

        context_window = self._resolve_context_window(agent)
        if context_window is None:
            return messages

        if isinstance(usage, dict):
            input_tokens_raw = usage.get("input_tokens")
            input_tokens = (
                input_tokens_raw
                if isinstance(input_tokens_raw, int) and not isinstance(input_tokens_raw, bool)
                else 0
            )
        else:
            input_tokens = self._compaction_service.estimate_messages_tokens(messages)

        if not self._compaction_service.should_auto_compact(
            input_tokens,
            context_window,
            settings.threshold,
        ):
            return messages

        summary_adapter, summary_model_id = self._resolve_summary_adapter(
            agent,
            adapter,
            model_id,
            settings,
        )
        close_summary_adapter = summary_adapter is not adapter
        try:
            checkpoint = await self._compaction_service.compact(
                session.load(),
                agent=agent,
                summary_adapter=summary_adapter,
                summary_model_id=summary_model_id,
                storage=storage,
                settings=settings,
            )
        except Exception:
            _LOGGER.warning("Compaction failed; continuing without compaction", exc_info=True)
            return messages
        finally:
            if close_summary_adapter:
                await _close_adapter(summary_adapter)

        session.append(checkpoint)
        run.emit(COMPACTION_COMPLETED_EVENT, {"message": checkpoint.to_dict()})
        return self._build_request_messages(agent, session)

    def _resolve_context_window(self, agent: Any) -> int | None:
        """Resolve context window for the active agent model from model registry."""
        models = getattr(self._runtime, "models", None)
        if models is None:
            return None

        bare_model = parse_bare_model(agent.model)
        if "/" not in bare_model:
            return None

        provider_id, _, resolved_model_id = bare_model.partition("/")
        if not provider_id or not resolved_model_id:
            return None

        try:
            model_entry = models.get(provider_id, resolved_model_id)
        except (KeyError, AttributeError):
            return None

        try:
            return int(model_entry.context_window)
        except (TypeError, ValueError, AttributeError):
            return None

    def _resolve_summary_adapter(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        settings: Any,
    ) -> tuple[Any, str]:
        """Resolve compaction summary adapter/model, defaulting to active run target."""
        del agent

        summary_model = settings.summary_model
        if not isinstance(summary_model, str) or not summary_model:
            return adapter, model_id

        try:
            provider_id, summary_model_id, connection_suffix = parse_model_with_connection(
                summary_model
            )
            if connection_suffix:
                connection_id = f"{provider_id}:{connection_suffix}"
            else:
                connection_id = _first_usable_connection_id(self._runtime, provider_id)
            summary_adapter = self._runtime.get_adapter(provider_id, connection_id)
        except (ChatError, ConfigError, VBotError):
            _LOGGER.warning(
                "Invalid compaction summary model %r; using active run model instead.",
                summary_model,
                exc_info=True,
            )
            return adapter, model_id

        return summary_adapter, summary_model_id

    async def _send_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
        note_hook: Callable[[str], None] | None = None,
    ) -> ChatMessage:
        if self._streaming:
            return await self._send_streaming_assistant_request(
                agent,
                adapter,
                model_id,
                messages,
                tools,
                run,
                note_hook=note_hook,
            )

        return await self._send_non_streaming_assistant_request(
            agent, adapter, model_id, messages, tools
        )

    async def _send_non_streaming_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
    ) -> ChatMessage:
        response = await adapter.send(
            messages,
            model_id=model_id,
            temperature=agent.temperature,
            thinking_effort=agent.thinking_effort,
            tools=tools,
        )
        normalized = adapter.normalize_response(response)
        return _assistant_message_from_response(agent.model, normalized)

    async def _send_streaming_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
        note_hook: Callable[[str], None] | None = None,
    ) -> ChatMessage:
        accumulator = StreamingAccumulator()
        emitted_visible_delta = False
        stream = adapter.stream(
            messages,
            model_id=model_id,
            temperature=agent.temperature,
            thinking_effort=agent.thinking_effort,
            tools=tools,
        )

        try:
            async for delta in iter_with_chunk_timeout(
                stream,
                timeout_seconds=STREAM_CHUNK_TIMEOUT_SECONDS,
            ):
                run.raise_if_cancelled()
                visible_deltas = accumulator.add_delta(delta)
                for visible_delta in visible_deltas:
                    run.emit(visible_delta.event_type, visible_delta.payload)
                    emitted_visible_delta = True
                run.raise_if_cancelled()
        except ProviderError as exc:
            if emitted_visible_delta or not _is_streaming_fallback_error(exc):
                _maybe_persist_partial_thinking(accumulator, note_hook)
                raise
            assistant_message = await self._send_non_streaming_assistant_request(
                agent,
                adapter,
                model_id,
                messages,
                tools,
            )
            _emit_assistant_events(run, assistant_message)
            return assistant_message
        except BaseException:
            _maybe_persist_partial_thinking(accumulator, note_hook)
            raise

        assistant_message = _assistant_message_from_response(
            agent.model,
            accumulator.finalize_assistant_fields().to_response_dict(),
        )
        _emit_streaming_assistant_events(run, assistant_message)
        return assistant_message

    async def _dispatch_tool_calls(
        self,
        agent: Any,
        tool_calls: list[ToolCall],
        session: ChatSession,
        run: Run,
    ) -> list[ChatMessage]:
        run.raise_if_cancelled()
        executor = ToolExecutor(
            _EmittingToolRegistry(
                self._runtime.tools,
                run,
                _runtime_extensions(self._runtime),
            )
        )
        results = await executor.execute_many(
            [
                ScheduledToolCall(
                    id=tool_call.id,
                    name=tool_call.name,
                    arguments=tool_call.arguments,
                )
                for tool_call in tool_calls
            ],
            ToolExecutionConfig(
                agent_id=run.agent_id,
                session_id=run.session_id,
                run_id=run.id,
                workspace=_agent_workspace(agent, _runtime_data_root(self._runtime)),
                app_root=_runtime_app_root(self._runtime),
                data_root=_runtime_data_root(self._runtime),
                allowed_tools=agent.allowed_tools,
                allowed_skills=getattr(agent, "allowed_skills", ["*"]),
                emit_hook=lambda event_type, payload: _emit_tool_context_event(
                    run,
                    event_type,
                    payload,
                ),
                cancellation_hook=lambda: run.cancel_requested,
                note_hook=session.add_note,
                skill_activation_hook=session.activate_skill_context,
                nesting_depth=self._nesting_depth,
            ),
        )
        run.raise_if_cancelled()
        return [
            ChatMessage.tool(
                tool_call_id=tool_call.id,
                name=tool_call.name,
                content=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            )
            for tool_call, result in zip(tool_calls, results, strict=True)
        ]

    def _activate_triggered_skills(self, agent: Any, session: ChatSession, content: str) -> None:
        skill_registry = getattr(self._runtime, "skills", None)
        if skill_registry is None:
            return

        if not _triggered_skill_names(content):
            return

        filter_allowed = getattr(skill_registry, "filter_allowed", None)
        if not callable(filter_allowed):
            return

        allowed_skills = getattr(agent, "allowed_skills", None)
        if allowed_skills is None:
            allowed_skills = ["*"]
        allowed_by_name = {skill.name: skill for skill in filter_allowed(allowed_skills)}
        for skill_name in _triggered_skill_names(content):
            skill = allowed_by_name.get(skill_name)
            if skill is None:
                _LOGGER.warning(
                    "Ignored skill trigger '%s' for agent=%s session=%s "
                    "because it is not allowed or loadable",
                    skill_name,
                    agent.id,
                    session.id,
                )
                session.add_note(
                    f"Skill trigger '{skill_name}' did not match an allowed loadable skill."
                )
                continue
            try:
                data = load_skill_content(skill.name, skill.path)
            except OSError as error:
                _LOGGER.warning(
                    "Failed to load triggered skill '%s' for agent=%s session=%s: %s",
                    skill_name,
                    agent.id,
                    session.id,
                    error,
                )
                session.add_note(f"Skill trigger '{skill_name}' could not be loaded: {error}")
                continue
            except ValueError as error:
                _LOGGER.warning(
                    "Failed to parse triggered skill '%s' for agent=%s session=%s: %s",
                    skill_name,
                    agent.id,
                    session.id,
                    error,
                )
                session.add_note(f"Skill trigger '{skill_name}' could not be loaded: {error}")
                continue
            session.activate_skill_context(skill.name, data)
            _LOGGER.info(
                "Activated triggered skill '%s' for agent=%s session=%s",
                skill.name,
                agent.id,
                session.id,
            )


def _runtime_run_manager(runtime: Any) -> ChatRunManager:
    run_manager = getattr(runtime, "chat_runs", None)
    if isinstance(run_manager, ChatRunManager):
        return run_manager
    run_manager = ChatRunManager()
    runtime.chat_runs = run_manager
    return run_manager


def _runtime_extensions(runtime: Any) -> ExtensionRegistry | None:
    return getattr(runtime, "extensions", None)


def _runtime_data_root(runtime: Any) -> Path:
    storage = getattr(runtime, "storage", None)
    data_dir = getattr(storage, "data_dir", None)
    if data_dir is not None:
        return Path(data_dir)

    chat_sessions = getattr(runtime, "chat_sessions", None)
    session_data_dir = getattr(chat_sessions, "data_dir", None)
    if session_data_dir is not None:
        return Path(session_data_dir)

    return Path.cwd()


def _runtime_app_root(runtime: Any) -> Path:
    system_prompts = getattr(runtime, "system_prompts", None)
    app_root = getattr(system_prompts, "_app_dir", None)
    if app_root is not None:
        return Path(app_root)

    return Path.cwd()


def _agent_workspace(agent: Any, data_root: Path) -> Path:
    workspace = getattr(agent, "workspace", None)
    if workspace is not None:
        return Path(workspace)

    return data_root / f"workspace-{agent.id}"


def _emit_assistant_events(run: Run, message: ChatMessage) -> None:
    if message.reasoning:
        run.emit(REASONING_EVENT, {"message": _visible_message_payload(message)})
    if message.content:
        _emit_message_event(run, ASSISTANT_OUTPUT_EVENT, message)


def _emit_streaming_assistant_events(run: Run, message: ChatMessage) -> None:
    if message.reasoning:
        run.emit(REASONING_EVENT, {"message": _visible_message_payload(message)})
    _emit_message_event(run, ASSISTANT_OUTPUT_EVENT, message)


def _emit_message_event(run: Run, event_type: str, message: ChatMessage) -> None:
    run.emit(event_type, {"message": _visible_message_payload(message)})


def _is_streaming_fallback_error(error: ProviderError) -> bool:
    if error.retryable:
        return False
    message = str(error).lower()
    return all(token in message for token in ("stream", "support"))


def _maybe_persist_partial_thinking(
    accumulator: StreamingAccumulator,
    note_hook: Callable[[str], None] | None,
) -> None:
    if note_hook is None:
        return
    partial = accumulator.partial_reasoning
    if partial:
        note_hook(f"Partial thinking before interruption:\n{partial}")


def _visible_message_payload(message: ChatMessage) -> JsonObject:
    data = message.to_dict()
    data.pop("reasoning_meta", None)
    return data


def _emit_tool_context_event(run: Run, event_type: str, payload: JsonObject) -> None:
    run.emit(event_type, payload)


def _looks_like_legacy_dispatch_type_error(error: TypeError) -> bool:
    message = str(error)
    return "positional" in message or "argument" in message


def _validated_tool_result(tool_name: str, result: Any) -> JsonObject:
    if not isinstance(result, dict):
        raise ValueError(f"Tool handler must return a JSON object: {tool_name}")
    if not is_tool_result_envelope(result):
        raise ValueError(f"Tool handler must return a valid result envelope: {tool_name}")
    return result


def _validated_extension_tool_hook_result(
    *,
    tool_name: str,
    extension_name: str,
    hook_name: str,
    result: Any,
) -> JsonObject | None:
    try:
        validated = _validated_tool_result(tool_name, result)
        json.dumps(validated, ensure_ascii=False, separators=(",", ":"))
        return validated
    except (TypeError, ValueError) as error:
        _LOGGER.warning(
            "Extension %r %s handler returned invalid tool result for %r: %s",
            extension_name,
            hook_name,
            tool_name,
            error,
        )
        return None


def _triggered_skill_names(content: str) -> list[str]:
    names: list[str] = []
    slash_match = SKILL_SLASH_TRIGGER_PATTERN.search(content)
    if slash_match:
        names.append(slash_match.group(1))

    for inline_match in SKILL_INLINE_TRIGGER_PATTERN.finditer(content):
        name = inline_match.group(1)
        if name not in names:
            names.append(name)
    return names


def _sync_skill_context_messages(messages: list[JsonObject], session: ChatSession) -> None:
    existing = {
        message.get("content")
        for message in messages
        if message.get("role") == "user"
        and isinstance(message.get("content"), str)
        and str(message.get("content", "")).startswith("<skill_content ")
    }
    for skill_message in session.skill_context_messages():
        if skill_message["content"] not in existing:
            messages.insert(1, skill_message)
            existing.add(skill_message["content"])


def error_kind_llm_visible(kind: str) -> bool:
    """Return whether an error kind should be included in later provider context."""
    return ERROR_KIND_LLM_VISIBLE.get(kind, False)


def _is_model_fallback_trigger(exc: Exception) -> bool:
    return isinstance(exc, ProviderError) and exc.retryable


def _exception_to_error_kind(exc: Exception) -> str:
    if isinstance(exc, ProviderRateLimitError):
        return ERROR_KIND_RATE_LIMIT
    if isinstance(exc, ProviderTimeoutError):
        return ERROR_KIND_TIMEOUT
    if isinstance(exc, StreamingChunkTimeoutError):
        return ERROR_KIND_TIMEOUT
    if isinstance(exc, NetworkError):
        return ERROR_KIND_NETWORK
    if isinstance(exc, ProviderAuthError):
        return ERROR_KIND_AUTH
    if isinstance(exc, ProviderError):
        if exc.retryable:
            return ERROR_KIND_PROVIDER_OVERLOAD
        return ERROR_KIND_PROVIDER_FATAL
    if isinstance(exc, ToolIterationLimitError):
        return ERROR_KIND_TOOL_ITERATIONS
    if isinstance(exc, (ChatError, ConfigError, VBotError)):
        return ERROR_KIND_CONFIG
    return ERROR_KIND_PROVIDER_ERROR


def _persist_run_error(run: Run, session: ChatSession, exc: Exception) -> None:
    kind = _exception_to_error_kind(exc)
    error_message = ChatMessage.error(error_kind=kind, content=str(exc))
    session.append(error_message)
    _emit_message_event(run, ERROR_MESSAGE_PERSISTED_EVENT, error_message)
    _LOGGER.error(
        "Persisted run error for agent=%s session=%s kind=%s: %s",
        run.agent_id,
        run.session_id,
        kind,
        exc,
    )


def _new_message_id() -> str:
    return str(uuid.uuid4())


def _validate_session_id(session_id: str) -> None:
    if not _is_valid_session_id(session_id):
        raise ChatSessionError(
            "session id must be 1-128 characters using only letters, numbers, hyphen, or underscore"
        )


def _is_valid_session_id(session_id: str) -> bool:
    return bool(SESSION_ID_PATTERN.fullmatch(session_id))


async def _close_adapter(adapter: Any) -> None:
    close_method = getattr(adapter, "aclose", None)
    if not callable(close_method):
        return
    result = close_method()
    if inspect.isawaitable(result):
        await result


def _last_user_message_with_content_blocks(messages: list[ChatMessage]) -> ChatMessage | None:
    for message in reversed(messages):
        if message.role != "user":
            continue
        if isinstance(message.content, list):
            return message
        return None
    return None


def _last_user_message(messages: list[ChatMessage]) -> ChatMessage | None:
    """Return the most recently appended user message regardless of content type."""
    for message in reversed(messages):
        if message.role == "user":
            return message
    return None


def _session_has_any_content_blocks(messages: list[ChatMessage]) -> bool:
    """Return True if any user message in the session carries list content."""
    return any(message.role == "user" and isinstance(message.content, list) for message in messages)


def parse_bare_model(model: str) -> str:
    """Return a model string without an optional ``::connection-suffix`` part."""
    before, separator, _suffix = model.rpartition("::")
    if not separator:
        return model
    return before


def parse_model_with_connection(model: str) -> tuple[str, str, str]:
    """Parse ``<provider>/<model-id>[::connection-id]`` into provider/model/suffix parts."""
    before, suffix_separator, connection_suffix = model.rpartition("::")
    if suffix_separator and not connection_suffix:
        raise ChatError("agent model connection suffix must not be empty")

    bare_model = before if suffix_separator else model
    if not bare_model:
        raise ChatError("agent has no model set")

    provider_id, separator, model_id = bare_model.partition("/")
    if not separator or not provider_id or not model_id:
        raise ChatError("agent model must use <provider>/<model-id>")

    if not suffix_separator:
        connection_suffix = ""
    return provider_id, model_id, connection_suffix


def _split_agent_model(model: str) -> tuple[str, str]:
    provider_id, model_id, _connection_suffix = parse_model_with_connection(model)
    return provider_id, model_id


def _model_has_vision(runtime: Any, agent: Any) -> bool:
    try:
        provider_id, model_id = _split_agent_model(agent.model)
        model = runtime.models.get(provider_id, model_id)
    except Exception:
        return False

    capabilities = getattr(model, "capabilities", None)
    return bool(getattr(capabilities, "vision", False))


def _resolve_agent_connection(runtime: Any, agent: Any) -> tuple[str, str]:
    model_provider_id, _model_id, connection_suffix = parse_model_with_connection(agent.model)
    if connection_suffix:
        return model_provider_id, f"{model_provider_id}:{connection_suffix}"

    return model_provider_id, _first_usable_connection_id(runtime, model_provider_id)


def _resolve_fallback(runtime: Any, agent: Any) -> tuple[str, str, str] | None:
    fallback_model = getattr(agent, "fallback_model", "")
    if not fallback_model:
        return None

    try:
        fallback_provider_id, _fallback_model_id, fallback_connection_suffix = (
            parse_model_with_connection(fallback_model)
        )
    except ChatError:
        return None

    if fallback_connection_suffix:
        return (
            fallback_model,
            fallback_provider_id,
            f"{fallback_provider_id}:{fallback_connection_suffix}",
        )

    try:
        fallback_connection_id = _first_usable_connection_id(runtime, fallback_provider_id)
    except ChatError:
        return None

    return fallback_model, fallback_provider_id, fallback_connection_id


def _first_usable_connection_id(runtime: Any, provider_id: str) -> str:
    try:
        provider_config = runtime.providers.get(provider_id)
    except KeyError as exc:
        raise ChatError(f"provider not found: {provider_id}") from exc

    credential_resolver = getattr(runtime, "provider_credentials", None)
    if credential_resolver is None:
        raise ChatError(f"agent has no connection set for provider: {provider_id}")

    for connection in provider_config.connections:
        connection_id = f"{provider_id}:{connection.id}"
        if credential_resolver.has_credentials(provider_id, connection_id):
            return connection_id

    raise ChatError(f"provider has no usable connections: {provider_id}")


def _ensure_provider_exists(providers: Any, provider_id: str) -> None:
    try:
        providers.get(provider_id)
    except KeyError as exc:
        raise ChatError(f"provider not found: {provider_id}") from exc


def _message_to_request_dict(message: ChatMessage) -> JsonObject:
    data = message.to_dict()
    if data.get("role") == "assistant":
        data.pop("reasoning", None)
        data.pop("reasoning_meta", None)
        data.pop("usage", None)
    return data


def _latest_compaction_checkpoint(messages: list[ChatMessage]) -> ChatMessage | None:
    for message in reversed(messages):
        if message.role == "compaction_checkpoint":
            return message
    return None


def _messages_from_boundary(messages: list[ChatMessage], boundary_id: str) -> list[ChatMessage]:
    for index, message in enumerate(messages):
        if message.id == boundary_id:
            return messages[index:]
    raise ChatError(f"compaction boundary id not found: {boundary_id}")


def _embed_notes_into_request(messages: list[ChatMessage]) -> list[JsonObject]:
    request_messages: list[JsonObject] = []
    pending_notes: list[ChatMessage] = []
    deferred_until_after_tools: list[ChatMessage] = []

    for message in messages:
        if message.role == "note":
            if _is_skill_context_note(message):
                continue
            pending_notes.append(message)
            continue

        if message.role == "error":
            if message.error_kind is not None and error_kind_llm_visible(message.error_kind):
                pending_notes.append(message)
            continue

        if message.role == "tool":
            if pending_notes:
                deferred_until_after_tools.extend(pending_notes)
                pending_notes = []
            request_messages.append(_message_to_request_dict(message))
            continue

        if deferred_until_after_tools:
            request_messages.append(_notes_to_synthetic_user_message(deferred_until_after_tools))
            deferred_until_after_tools = []

        if pending_notes:
            request_messages.append(_notes_to_synthetic_user_message(pending_notes))
            pending_notes = []
        request_messages.append(_message_to_request_dict(message))

    if deferred_until_after_tools:
        request_messages.append(_notes_to_synthetic_user_message(deferred_until_after_tools))

    if pending_notes:
        request_messages.append(_notes_to_synthetic_user_message(pending_notes))

    return request_messages


def _notes_to_synthetic_user_message(notes: list[ChatMessage]) -> JsonObject:
    return {
        "role": "user",
        "content": "\n".join(_system_reminder_block(note) for note in notes),
    }


def _system_reminder_block(message: ChatMessage) -> str:
    message.validate()
    return f"{SYSTEM_REMINDER_OPEN_TAG}\n{message.content}\n{SYSTEM_REMINDER_CLOSE_TAG}"


def _skill_context_note_content(name: str, content: str) -> str:
    return SKILL_CONTEXT_NOTE_PREFIX + json.dumps(
        {"name": name, "content": content},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _is_skill_context_note(message: ChatMessage) -> bool:
    return (
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith(SKILL_CONTEXT_NOTE_PREFIX)
    )


def _skill_contexts_from_messages(messages: list[ChatMessage]) -> dict[str, str]:
    contexts: dict[str, str] = {}
    for message in messages:
        if not _is_skill_context_note(message):
            continue
        assert isinstance(message.content, str)
        try:
            payload = json.loads(message.content.removeprefix(SKILL_CONTEXT_NOTE_PREFIX))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        name = payload.get("name")
        content = payload.get("content")
        if isinstance(name, str) and isinstance(content, str):
            contexts[name] = content
    return contexts


def _assistant_message_from_response(model: str, response: JsonObject) -> ChatMessage:
    tool_calls = _parse_tool_calls(response.get("tool_calls"))
    return ChatMessage.assistant(
        model=model,
        content=_nullable_response_string(response, "content"),
        reasoning=_nullable_response_string(response, "reasoning"),
        reasoning_meta=_response_reasoning_meta(response),
        usage=response.get("usage"),
        tool_calls=tool_calls,
    )


def _apply_usage_estimation(
    message: ChatMessage,
    request_messages: list[JsonObject],
) -> ChatMessage:
    """Estimate token usage when the provider doesn't supply usage data.

    Uses the 4-chars/token heuristic from estimate_tokens to compute
    approximate input and output token counts.  Marks the result with
    ``"estimated": True`` so the frontend can display a tilde prefix.
    """
    input_chunks: list[str] = []
    for request_message in request_messages:
        content = request_message.get("content")
        if isinstance(content, str):
            input_chunks.append(content)
    input_text = "".join(input_chunks)
    estimated_input, _ = estimate_tokens(input_text)
    output_text = message.content if isinstance(message.content, str) else ""
    estimated_output, _ = estimate_tokens(output_text)
    usage: JsonObject = {
        "input_tokens": estimated_input,
        "output_tokens": estimated_output,
        "estimated": True,
    }
    return replace(message, usage=usage)


def _nullable_response_string(response: JsonObject, key: str) -> str | None:
    value = response.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ChatMessageValidationError(f"assistant response {key} must be a string or null")


def _response_reasoning_meta(response: JsonObject) -> JsonObject | None:
    reasoning_meta = response.get("reasoning_meta")
    if reasoning_meta is None:
        return None
    if not isinstance(reasoning_meta, dict):
        raise ChatMessageValidationError("assistant response reasoning_meta must be an object")
    return dict(reasoning_meta)


def _format_timestamp(timestamp: datetime | None) -> str:
    value = timestamp or datetime.now(UTC)
    if value.tzinfo is None:
        raise ChatMessageValidationError("timestamp must include timezone information")
    return value.astimezone(UTC).isoformat()


def _add_if_not_none(message: JsonObject, key: str, value: Any) -> None:
    if value is not None:
        message[key] = value


def _require_string(data: JsonObject, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ChatMessageValidationError(f"{key} must be a non-empty string")
    return value


def _optional_string(data: JsonObject, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ChatMessageValidationError(f"{key} must be a string")
    return value


def _parse_content(data: JsonObject) -> str | list[ContentBlock] | None:
    value = data.get("content")
    if value is None or isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ChatMessageValidationError(
            "content must be a string, an array of content blocks, or null"
        )

    blocks: list[ContentBlock] = []
    for item in value:
        if not isinstance(item, dict):
            raise ChatMessageValidationError("content list entries must be objects")
        try:
            blocks.append(content_block_from_dict(item))
        except ContentBlockError as exc:
            raise ChatMessageValidationError(f"invalid content block: {exc}") from exc
    return blocks


def _require_role(data: JsonObject) -> MessageRole:
    role = data.get("role")
    if role not in (
        "system",
        "user",
        "assistant",
        "tool",
        "note",
        "error",
        "compaction_checkpoint",
    ):
        raise ChatMessageValidationError(
            "role must be system, user, assistant, tool, note, error, or compaction_checkpoint"
        )
    return cast(MessageRole, role)


def _parse_tool_calls(value: Any) -> list[ToolCall] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ChatMessageValidationError("tool_calls must be an array")
    return [ToolCall.from_dict(item) for item in value if _is_tool_call_object(item)]


def _is_content_block(value: Any) -> bool:
    return isinstance(value, (TextBlock, MediaBlock, FileBlock))


def _is_tool_call_object(value: Any) -> JsonObject:
    if not isinstance(value, dict):
        raise ChatMessageValidationError("tool_calls entries must be objects")
    return value


def _validate_core_fields(message: ChatMessage) -> None:
    if not message.id:
        raise ChatMessageValidationError("id must be a non-empty string")
    if not message.timestamp:
        raise ChatMessageValidationError("timestamp must be a non-empty string")
    if not _has_explicit_utc_offset(message.timestamp):
        raise ChatMessageValidationError("timestamp must include explicit UTC offset")


def _has_explicit_utc_offset(timestamp: str) -> bool:
    if timestamp.endswith(UTC_Z_SUFFIX):
        return _is_valid_iso_utc_timestamp(timestamp[:-1] + TIMESTAMP_SUFFIX)
    if TIMESTAMP_SUFFIX in timestamp:
        return _is_valid_iso_utc_timestamp(timestamp)
    return False


def _is_valid_iso_utc_timestamp(timestamp: str) -> bool:
    try:
        value = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(value)


def _validate_system_message(message: ChatMessage) -> None:
    if message.model is None:
        raise ChatMessageValidationError("system messages require model")
    if message.content is None:
        raise ChatMessageValidationError("system messages require content")
    if not isinstance(message.content, str):
        raise ChatMessageValidationError("system messages content must be a string")
    _reject_fields(
        message,
        "reasoning",
        "reasoning_meta",
        "usage",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "tail_boundary_id",
    )


def _validate_user_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("user messages require content")
    if isinstance(message.content, list):
        if not message.content:
            raise ChatMessageValidationError("user content block lists must not be empty")
        if not all(_is_content_block(block) for block in message.content):
            raise ChatMessageValidationError(
                "user content block lists must contain only content blocks"
            )
    elif not isinstance(message.content, str):
        raise ChatMessageValidationError("user messages content must be a string")
    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "usage",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "tail_boundary_id",
    )


def _validate_assistant_message(message: ChatMessage) -> None:
    if message.model is None:
        raise ChatMessageValidationError("assistant messages require model")
    if message.content is not None and not isinstance(message.content, str):
        raise ChatMessageValidationError("assistant messages content must be a string")
    _reject_fields(message, "tool_call_id", "name", "error_kind", "tail_boundary_id")
    if message.reasoning_meta is not None and not isinstance(message.reasoning_meta, dict):
        raise ChatMessageValidationError("reasoning_meta must be an object")
    if message.usage is not None and not isinstance(message.usage, dict):
        raise ChatMessageValidationError("usage must be an object")


def _validate_tool_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("tool messages require content")
    if not isinstance(message.content, str):
        raise ChatMessageValidationError("tool messages content must be a string")
    if message.tool_call_id is None:
        raise ChatMessageValidationError("tool messages require tool_call_id")
    if message.name is None:
        raise ChatMessageValidationError("tool messages require name")
    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "usage",
        "tool_calls",
        "error_kind",
        "tail_boundary_id",
    )


def _validate_note_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("note messages require content")
    if not isinstance(message.content, str):
        raise ChatMessageValidationError("note messages content must be a string")
    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "usage",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
        "tail_boundary_id",
    )


def _validate_error_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("error messages require content")
    if not isinstance(message.content, str):
        raise ChatMessageValidationError("error messages content must be a string")
    if not message.error_kind:
        raise ChatMessageValidationError("error messages require error_kind")
    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "usage",
        "tool_calls",
        "tool_call_id",
        "name",
        "tail_boundary_id",
    )


def _validate_compaction_checkpoint_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("compaction checkpoints require content")
    if not isinstance(message.content, str):
        raise ChatMessageValidationError("compaction checkpoints content must be a string")
    if message.tail_boundary_id is None:
        raise ChatMessageValidationError("compaction checkpoints require tail_boundary_id")
    if not message.tail_boundary_id:
        raise ChatMessageValidationError(
            "compaction checkpoints tail_boundary_id must be a non-empty string"
        )

    if message.usage is not None:
        compacted_count = message.usage.get("compacted_token_count")
        if (
            isinstance(compacted_count, bool)
            or not isinstance(compacted_count, int)
            or compacted_count < 0
        ):
            raise ChatMessageValidationError(
                "compaction checkpoints usage.compacted_token_count must be a non-negative integer"
            )

    _reject_fields(
        message,
        "model",
        "reasoning",
        "reasoning_meta",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
    )


def _reject_fields(message: ChatMessage, *fields: str) -> None:
    for field_name in fields:
        if getattr(message, field_name) is not None:
            raise ChatMessageValidationError(f"{message.role} messages cannot include {field_name}")
