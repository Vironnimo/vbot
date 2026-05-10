"""Chat message, JSONL session primitives, and chat loop execution."""

from __future__ import annotations

import inspect
import json
import re
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from core.chat.runs import (
    ASSISTANT_OUTPUT_EVENT,
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
    iter_with_chunk_timeout,
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
from core.utils.errors import ProviderError, VBotError
from core.utils.tokens import estimate_tokens

MessageRole = Literal["system", "user", "assistant", "tool", "note", "error"]
JsonObject = dict[str, Any]

TIMESTAMP_SUFFIX = "+00:00"
UTC_Z_SUFFIX = "Z"
SESSION_FILE_EXTENSION = ".jsonl"
SESSION_LINE_ENDING = "\n"
MAX_TOOL_ITERATIONS = 8
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
SYSTEM_REMINDER_OPEN_TAG = "<system-reminder>"
SYSTEM_REMINDER_CLOSE_TAG = "</system-reminder>"
SKILL_SLASH_TRIGGER_PATTERN = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?=\s|$)")
SKILL_INLINE_TRIGGER_PATTERN = re.compile(r"\$([A-Za-z0-9][A-Za-z0-9_-]{0,63})")
SKILL_CONTEXT_NOTE_PREFIX = "[skill-context] "
ERROR_KIND_RATE_LIMIT = "rate_limit"
ERROR_KIND_TIMEOUT = "timeout"
ERROR_KIND_PROVIDER_OVERLOAD = "provider_overloaded"
ERROR_KIND_TOOL_ITERATIONS = "tool_iterations_exceeded"
ERROR_KIND_AUTH = "auth_error"
ERROR_KIND_PROVIDER_FATAL = "provider_fatal"
ERROR_KIND_CONFIG = "config_error"
ERROR_KIND_PROVIDER_ERROR = "provider_error"
ERROR_KIND_LLM_VISIBLE: dict[str, bool] = {
    ERROR_KIND_RATE_LIMIT: True,
    ERROR_KIND_TIMEOUT: True,
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
    content: str | None = None
    model: str | None = None
    reasoning: str | None = None
    reasoning_meta: JsonObject | None = None
    usage: JsonObject | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    error_kind: str | None = None

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
    def user(cls, content: str, *, timestamp: datetime | None = None) -> ChatMessage:
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

    def to_dict(self) -> JsonObject:
        """Return a canonical JSON-serializable message dictionary."""
        self.validate()
        message: JsonObject = {
            "id": self.id,
            "timestamp": self.timestamp,
            "role": self.role,
        }
        _add_if_not_none(message, "model", self.model)
        _add_if_not_none(message, "content", self.content)
        _add_if_not_none(message, "reasoning", self.reasoning)
        _add_if_not_none(message, "reasoning_meta", self.reasoning_meta)
        _add_if_not_none(message, "usage", self.usage)
        if self.tool_calls is not None:
            message["tool_calls"] = [tool_call.to_dict() for tool_call in self.tool_calls]
        _add_if_not_none(message, "tool_call_id", self.tool_call_id)
        _add_if_not_none(message, "name", self.name)
        _add_if_not_none(message, "error_kind", self.error_kind)
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
            content=_optional_string(data, "content"),
            model=_optional_string(data, "model"),
            reasoning=_optional_string(data, "reasoning"),
            reasoning_meta=dict(reasoning_meta) if reasoning_meta is not None else None,
            usage=dict(usage) if usage is not None else None,
            tool_calls=tool_calls,
            tool_call_id=_optional_string(data, "tool_call_id"),
            name=_optional_string(data, "name"),
            error_kind=_optional_string(data, "error_kind"),
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


class ChatSession:
    """Append-only UTF-8 JSONL session file."""

    def __init__(self, path: Path) -> None:
        if path.suffix != SESSION_FILE_EXTENSION:
            raise ChatSessionError("session path must end with .jsonl")
        self.path = path
        self._pending_notes: deque[ChatMessage] = deque()
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

    def append(self, message: ChatMessage) -> None:
        """Append one canonical message as a single JSONL line."""
        payload = json.dumps(message.to_dict(), ensure_ascii=False, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="") as session_file:
            session_file.write(payload + SESSION_LINE_ENDING)

    def add_note(self, content: str) -> None:
        """Persist a kernel-internal note and enqueue it for provider-request injection."""
        note = ChatMessage.note(content)
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

    def get(self, agent_id: str, session_id: str) -> ChatSession:
        """Return a session handle for an existing agent session."""
        _validate_session_id(session_id)
        session_path = self.sessions_dir(agent_id) / f"{session_id}{SESSION_FILE_EXTENSION}"
        if not session_path.exists():
            raise ChatSessionError(f"session does not exist: {session_id}")
        return ChatSession(session_path)

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

    def delete(self, agent_id: str, session_id: str) -> None:
        """Delete one agent session file."""
        self.get(agent_id, session_id).delete()


class _EmittingToolRegistry(ToolRegistry):
    """Adapter that emits public lifecycle events around registry dispatch."""

    def __init__(self, registry: Any, run: Run) -> None:
        self._registry = registry
        self._run = run

    async def dispatch(
        self,
        context: ToolContext,
        arguments: JsonObject,
        allowed_tools: Sequence[str] | None = None,
    ) -> JsonObject:
        self._run.raise_if_cancelled()
        self._run.emit(
            TOOL_CALL_STARTED_EVENT,
            {
                "tool_call": {
                    "id": context.tool_call_id,
                    "index": context.tool_call_index,
                    "name": context.tool_name,
                    "arguments": dict(arguments),
                }
            },
        )
        result = await self._dispatch_with_failure_envelope(context, arguments, allowed_tools)
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
    ) -> None:
        if max_tool_iterations < 0:
            raise ChatError("max tool iterations must not be negative")
        self._runtime = runtime
        self._max_tool_iterations = max_tool_iterations
        self._streaming = streaming

    async def send(
        self,
        agent_id: str,
        content: str,
        *,
        session_id: str | None = None,
    ) -> ChatMessage:
        """Run one persisted non-streaming chat turn and return the final assistant message."""
        run = await self._start_run(agent_id, content, session_id=session_id, create_missing=True)
        return cast(ChatMessage, await run.wait())

    async def start_run(
        self,
        agent_id: str,
        content: str,
        *,
        session_id: str,
    ) -> Run:
        """Start one chat run against an existing session for server-facing callers."""
        return await self._start_run(agent_id, content, session_id=session_id, create_missing=False)

    async def _start_run(
        self,
        agent_id: str,
        content: str,
        *,
        session_id: str | None,
        create_missing: bool,
    ) -> Run:
        agent = self._runtime.agents.get(agent_id)
        provider_id, _connection_id = _resolve_agent_connection(self._runtime, agent)
        _ensure_provider_exists(self._runtime.providers, provider_id)
        session = self._get_session(agent_id, session_id, create_missing=create_missing)
        manager = _runtime_run_manager(self._runtime)
        return await manager.start(
            agent_id=agent_id,
            session_id=session.id,
            executor=lambda run: self._execute_run(run, content),
        )

    async def _execute_run(self, run: Run, content: str) -> ChatMessage:
        agent = self._runtime.agents.get(run.agent_id)
        _model_provider_id, model_id = _split_agent_model(agent.model)
        provider_id, connection_id = _resolve_agent_connection(self._runtime, agent)
        _ensure_provider_exists(self._runtime.providers, provider_id)
        adapter = self._runtime.get_adapter(provider_id, connection_id)
        run.add_cancel_callback(lambda: _close_adapter(adapter))
        session = cast(ChatSessionManager, self._runtime.chat_sessions).get(
            run.agent_id,
            run.session_id,
        )

        try:
            run.raise_if_cancelled()
            user_message = ChatMessage.user(content)
            session.append(user_message)
            _emit_message_event(run, USER_MESSAGE_EVENT, user_message)
            self._activate_triggered_skills(agent, session, content)
            run.raise_if_cancelled()
            messages = self._build_request_messages(agent, session)
            tools = self._runtime.system_prompts.provider_tool_definitions(agent)

            return await self._send_until_final(
                agent,
                adapter,
                model_id,
                session,
                messages,
                tools,
                run,
            )
        finally:
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
        history = _embed_notes_into_request(session.load())
        session.drain_pending_notes()
        return [system_message.to_dict(), *session.skill_context_messages(), *history]

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
        for _ in range(self._max_tool_iterations + 1):
            run.raise_if_cancelled()
            pending_notes = session.drain_pending_notes()
            if pending_notes:
                messages.append(_notes_to_synthetic_user_message(pending_notes))
            _sync_skill_context_messages(messages, session)
            assistant_message = await self._send_assistant_request(
                agent,
                adapter,
                model_id,
                messages,
                tools,
                run,
            )
            run.raise_if_cancelled()
            if assistant_message.usage is None:
                assistant_message = _apply_usage_estimation(assistant_message, messages)
            session.append(assistant_message)
            if not self._streaming:
                _emit_assistant_events(run, assistant_message)
            messages.append(assistant_message.to_dict())

            if not assistant_message.tool_calls:
                return assistant_message

            if self._tool_iterations_exhausted(messages):
                raise ChatError("maximum tool iterations exceeded")

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

        raise ChatError("maximum tool iterations exceeded")

    async def _send_assistant_request(
        self,
        agent: Any,
        adapter: Any,
        model_id: str,
        messages: list[JsonObject],
        tools: list[JsonObject],
        run: Run,
    ) -> ChatMessage:
        if self._streaming:
            return await self._send_streaming_assistant_request(
                agent,
                adapter,
                model_id,
                messages,
                tools,
                run,
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
        executor = ToolExecutor(_EmittingToolRegistry(self._runtime.tools, run))
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

    def _tool_iterations_exhausted(self, messages: list[JsonObject]) -> bool:
        assistant_tool_messages = [
            message
            for message in messages
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        return len(assistant_tool_messages) > self._max_tool_iterations

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
                session.add_note(
                    f"Skill trigger '{skill_name}' did not match an allowed loadable skill."
                )
                continue
            try:
                data = load_skill_content(skill.name, skill.path)
            except OSError as error:
                session.add_note(f"Skill trigger '{skill_name}' could not be loaded: {error}")
                continue
            except ValueError as error:
                session.add_note(f"Skill trigger '{skill_name}' could not be loaded: {error}")
                continue
            session.activate_skill_context(skill.name, data)


def _runtime_run_manager(runtime: Any) -> ChatRunManager:
    run_manager = getattr(runtime, "chat_runs", None)
    if isinstance(run_manager, ChatRunManager):
        return run_manager
    run_manager = ChatRunManager()
    runtime.chat_runs = run_manager
    return run_manager


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


def _split_agent_model(model: str) -> tuple[str, str]:
    if not model:
        raise ChatError("agent has no model set")
    provider_id, separator, model_id = model.partition("/")
    if not separator or not provider_id or not model_id:
        raise ChatError("agent model must use <provider>/<model-id>")
    return provider_id, model_id


def _resolve_agent_connection(runtime: Any, agent: Any) -> tuple[str, str]:
    model_provider_id, _model_id = _split_agent_model(agent.model)
    connection_id = getattr(agent, "connection", "")
    if connection_id:
        provider_id, separator, local_id = connection_id.partition(":")
        if not separator or not provider_id or not local_id:
            raise ChatError("agent connection must use <provider>:<connection-id>")
        return provider_id, connection_id

    return model_provider_id, _first_usable_connection_id(runtime, model_provider_id)


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


def _embed_notes_into_request(messages: list[ChatMessage]) -> list[JsonObject]:
    request_messages: list[JsonObject] = []
    pending_notes: list[ChatMessage] = []

    for message in messages:
        if message.role == "note":
            if _is_skill_context_note(message):
                continue
            pending_notes.append(message)
            continue

        if pending_notes:
            request_messages.append(_notes_to_synthetic_user_message(pending_notes))
            pending_notes = []
        request_messages.append(_message_to_request_dict(message))

    if pending_notes:
        request_messages.append(_notes_to_synthetic_user_message(pending_notes))

    return request_messages


def _notes_to_synthetic_user_message(notes: list[ChatMessage]) -> JsonObject:
    return {
        "role": "user",
        "content": "\n".join(_system_reminder_block(note) for note in notes),
    }


def _system_reminder_block(note: ChatMessage) -> str:
    if note.role != "note":
        raise ChatMessageValidationError("system reminders can only be built from notes")
    note.validate()
    return f"{SYSTEM_REMINDER_OPEN_TAG}\n{note.content}\n{SYSTEM_REMINDER_CLOSE_TAG}"


def _skill_context_note_content(name: str, content: str) -> str:
    return SKILL_CONTEXT_NOTE_PREFIX + json.dumps(
        {"name": name, "content": content},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _is_skill_context_note(message: ChatMessage) -> bool:
    return message.role == "note" and bool(
        message.content and message.content.startswith(SKILL_CONTEXT_NOTE_PREFIX)
    )


def _skill_contexts_from_messages(messages: list[ChatMessage]) -> dict[str, str]:
    contexts: dict[str, str] = {}
    for message in messages:
        if not _is_skill_context_note(message):
            continue
        assert message.content is not None
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
    input_text = "".join(msg.get("content", "") or "" for msg in request_messages)
    estimated_input, _ = estimate_tokens(input_text)
    estimated_output, _ = estimate_tokens(message.content or "")
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


def _require_role(data: JsonObject) -> MessageRole:
    role = data.get("role")
    if role not in ("system", "user", "assistant", "tool", "note", "error"):
        raise ChatMessageValidationError(
            "role must be system, user, assistant, tool, note, or error"
        )
    return cast(MessageRole, role)


def _parse_tool_calls(value: Any) -> list[ToolCall] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ChatMessageValidationError("tool_calls must be an array")
    return [ToolCall.from_dict(item) for item in value if _is_tool_call_object(item)]


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
    _reject_fields(
        message,
        "reasoning",
        "reasoning_meta",
        "usage",
        "tool_calls",
        "tool_call_id",
        "name",
        "error_kind",
    )


def _validate_user_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("user messages require content")
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
    )


def _validate_assistant_message(message: ChatMessage) -> None:
    if message.model is None:
        raise ChatMessageValidationError("assistant messages require model")
    _reject_fields(message, "tool_call_id", "name", "error_kind")
    if message.reasoning_meta is not None and not isinstance(message.reasoning_meta, dict):
        raise ChatMessageValidationError("reasoning_meta must be an object")
    if message.usage is not None and not isinstance(message.usage, dict):
        raise ChatMessageValidationError("usage must be an object")


def _validate_tool_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("tool messages require content")
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
    )


def _validate_note_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("note messages require content")
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
    )


def _validate_error_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("error messages require content")
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
    )


def _reject_fields(message: ChatMessage, *fields: str) -> None:
    for field_name in fields:
        if getattr(message, field_name) is not None:
            raise ChatMessageValidationError(f"{message.role} messages cannot include {field_name}")
