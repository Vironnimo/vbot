"""Chat message, JSONL session primitives, and chat loop execution."""

from __future__ import annotations

import inspect
import json
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
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
)
from core.utils.errors import ProviderError, VBotError

MessageRole = Literal["system", "user", "assistant", "tool"]
JsonObject = dict[str, Any]

TIMESTAMP_SUFFIX = "+00:00"
UTC_Z_SUFFIX = "Z"
SESSION_FILE_EXTENSION = ".jsonl"
SESSION_LINE_ENDING = "\n"
MAX_TOOL_ITERATIONS = 8
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class ChatError(VBotError):
    """Base error for chat domain failures."""


class ChatMessageValidationError(ChatError):
    """Raised when a canonical chat message is invalid."""


class ChatSessionError(ChatError):
    """Raised when a chat session file operation cannot be completed."""


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
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

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
    def assistant(
        cls,
        *,
        model: str,
        content: str | None,
        reasoning: str | None = None,
        reasoning_meta: JsonObject | None = None,
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
        if self.tool_calls is not None:
            message["tool_calls"] = [tool_call.to_dict() for tool_call in self.tool_calls]
        _add_if_not_none(message, "tool_call_id", self.tool_call_id)
        _add_if_not_none(message, "name", self.name)
        return message

    @classmethod
    def from_dict(cls, data: JsonObject) -> ChatMessage:
        """Build a chat message from a canonical JSON object."""
        role = _require_role(data)
        tool_calls = _parse_tool_calls(data.get("tool_calls"))
        reasoning_meta = data.get("reasoning_meta")
        if reasoning_meta is not None and not isinstance(reasoning_meta, dict):
            raise ChatMessageValidationError("reasoning_meta must be an object")

        message = cls(
            id=_require_string(data, "id"),
            timestamp=_require_string(data, "timestamp"),
            role=role,
            content=_optional_string(data, "content"),
            model=_optional_string(data, "model"),
            reasoning=_optional_string(data, "reasoning"),
            reasoning_meta=dict(reasoning_meta) if reasoning_meta is not None else None,
            tool_calls=tool_calls,
            tool_call_id=_optional_string(data, "tool_call_id"),
            name=_optional_string(data, "name"),
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


class ChatSession:
    """Append-only UTF-8 JSONL session file."""

    def __init__(self, path: Path) -> None:
        if path.suffix != SESSION_FILE_EXTENSION:
            raise ChatSessionError("session path must end with .jsonl")
        self.path = path

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
        provider_id, _model_id = _split_agent_model(agent.model)
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
        provider_id, model_id = _split_agent_model(agent.model)
        _ensure_provider_exists(self._runtime.providers, provider_id)
        adapter = self._runtime.get_adapter(provider_id)
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
        history = [_message_to_request_dict(message) for message in session.load()]
        return [system_message.to_dict(), *history]

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
            assistant_message = await self._send_assistant_request(
                agent,
                adapter,
                model_id,
                messages,
                tools,
                run,
            )
            run.raise_if_cancelled()
            session.append(assistant_message)
            if not self._streaming:
                _emit_assistant_events(run, assistant_message)
            messages.append(assistant_message.to_dict())

            if not assistant_message.tool_calls:
                return assistant_message

            if self._tool_iterations_exhausted(messages):
                raise ChatError("maximum tool iterations exceeded")

            tool_messages = await self._dispatch_tool_calls(
                agent, assistant_message.tool_calls, run
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
                emit_hook=lambda event_type, payload: _emit_tool_context_event(
                    run,
                    event_type,
                    payload,
                ),
                cancellation_hook=lambda: run.cancel_requested,
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
    return data


def _assistant_message_from_response(model: str, response: JsonObject) -> ChatMessage:
    tool_calls = _parse_tool_calls(response.get("tool_calls"))
    return ChatMessage.assistant(
        model=model,
        content=_nullable_response_string(response, "content"),
        reasoning=_nullable_response_string(response, "reasoning"),
        reasoning_meta=_response_reasoning_meta(response),
        tool_calls=tool_calls,
    )


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
    if role not in ("system", "user", "assistant", "tool"):
        raise ChatMessageValidationError("role must be system, user, assistant, or tool")
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
    _reject_fields(message, "reasoning", "reasoning_meta", "tool_calls", "tool_call_id", "name")


def _validate_user_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("user messages require content")
    _reject_fields(
        message, "model", "reasoning", "reasoning_meta", "tool_calls", "tool_call_id", "name"
    )


def _validate_assistant_message(message: ChatMessage) -> None:
    if message.model is None:
        raise ChatMessageValidationError("assistant messages require model")
    _reject_fields(message, "tool_call_id", "name")
    if message.reasoning_meta is not None and not isinstance(message.reasoning_meta, dict):
        raise ChatMessageValidationError("reasoning_meta must be an object")


def _validate_tool_message(message: ChatMessage) -> None:
    if message.content is None:
        raise ChatMessageValidationError("tool messages require content")
    if message.tool_call_id is None:
        raise ChatMessageValidationError("tool messages require tool_call_id")
    if message.name is None:
        raise ChatMessageValidationError("tool messages require name")
    _reject_fields(message, "model", "reasoning", "reasoning_meta", "tool_calls")


def _reject_fields(message: ChatMessage, *fields: str) -> None:
    for field_name in fields:
        if getattr(message, field_name) is not None:
            raise ChatMessageValidationError(f"{message.role} messages cannot include {field_name}")
