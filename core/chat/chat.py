"""Chat message and JSONL session primitives."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from core.utils.errors import VBotError

MessageRole = Literal["system", "user", "assistant", "tool"]
JsonObject = dict[str, Any]

TIMESTAMP_SUFFIX = "+00:00"
SESSION_FILE_EXTENSION = ".jsonl"
SESSION_LINE_ENDING = "\n"


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
        session_identifier = session_id or str(uuid.uuid4())
        if not session_identifier:
            raise ChatSessionError("session id must not be empty")
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
        if not session_id:
            raise ChatSessionError("session id must not be empty")
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
            ChatSession(path) for path in sorted(sessions_dir.glob(f"*{SESSION_FILE_EXTENSION}"))
        ]

    def delete(self, agent_id: str, session_id: str) -> None:
        """Delete one agent session file."""
        self.get(agent_id, session_id).delete()


def _new_message_id() -> str:
    return str(uuid.uuid4())


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
    if TIMESTAMP_SUFFIX not in message.timestamp:
        raise ChatMessageValidationError("timestamp must include explicit UTC offset")


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
