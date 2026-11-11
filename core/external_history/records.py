"""Lossless source records and their inert, portable conversation projection."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from core.chat.messages import ChatMessage
from core.utils.ids import new_id
from core.utils.timestamps import format_canonical_timestamp

SOURCES = {
    "codex": "Codex",
    "claude-code": "Claude Code",
    "opencode": "OpenCode",
    "hermes": "Hermes",
    "openclaw": "OpenClaw",
}


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def timestamp(value: Any, fallback: str) -> str:
    try:
        if isinstance(value, (int, float)):
            date = datetime.fromtimestamp(value / 1000 if value > 10**11 else value, UTC)
        elif isinstance(value, str):
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                return fallback
        else:
            return fallback
        return format_canonical_timestamp(date)
    except ValueError, OverflowError, OSError:
        return fallback


@dataclass(frozen=True)
class ExternalEntry:
    role: str
    content: str
    timestamp: str
    raw: dict[str, Any]
    kind: str = "message"

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "kind": self.kind,
            "content": self.content,
            "timestamp": self.timestamp,
            "raw": self.raw,
        }

    def message(self, source: str, session_id: str, ordinal: int) -> ChatMessage:
        # Foreign calls are ordinary Assistant text: no call id, ToolCall, Run,
        # executable Tool grant, opaque reasoning, Usage or System role is imported.
        role: Literal["user", "assistant"] = "user" if self.role == "user" else "assistant"
        content = self.content
        if self.kind != "message" or self.role not in {"user", "assistant"}:
            content = (
                f"Historical {self.kind.replace('_', ' ')} from {SOURCES[source]} "
                f"({self.role}):\n{content}"
            )
        return ChatMessage(
            id=new_id("msg"),
            timestamp=self.timestamp,
            role=role,
            content=content,
            external_history={
                "source": source,
                "session_id": session_id,
                "ordinal": ordinal,
                "role": self.role,
                "kind": self.kind,
                "raw": self.raw,
            },
        )


@dataclass
class ExternalSession:
    source: str
    id: str
    path: str
    title: str
    updated_at: str
    cwd: str = ""
    entries: list[ExternalEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "id": self.id,
            "path": self.path,
            "title": self.title,
            "updated_at": self.updated_at,
            "cwd": self.cwd,
            "entry_count": len(self.entries),
            "warnings": list(self.warnings),
        }

    def messages(self) -> list[ChatMessage]:
        return [entry.message(self.source, self.id, i) for i, entry in enumerate(self.entries)]


def conversation_entry(raw: dict[str, Any], fallback: str) -> ExternalEntry:
    """Read the common role/content envelope used by Claude, Hermes and OpenClaw."""
    nested = raw.get("message")
    message = nested if isinstance(nested, dict) else raw
    role = str(message.get("role", raw.get("type", "assistant")))
    content = message.get("content", "")
    fragments: list[str] = []
    kinds: set[str] = set()
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                fragments.append(text(block))
                continue
            kind = block.get("type", "")
            if kind in {"text", "input_text", "output_text"}:
                fragments.append(text(block.get("text")))
            elif kind in {"tool_use", "toolCall"}:
                kinds.add("tool_call")
                fragments.append("Tool call: " + text(block))
            elif kind in {"tool_result", "toolResult"}:
                kinds.add("tool_result")
                fragments.append("Tool result: " + text(block))
            elif kind in {"thinking", "reasoning"}:
                fragments.append(text(block.get("thinking", block.get("text", ""))))
            else:
                fragments.append("Source content block: " + text(block))
    else:
        fragments.append(text(content))
    calls = message.get("tool_calls")
    if calls:
        kinds.add("tool_call")
        fragments.append("Tool calls: " + text(calls))
    if role in {"tool", "toolResult"}:
        kinds.add("tool_result")
        fragments.insert(
            0,
            "Tool: "
            + text(message.get("name", message.get("tool_name", message.get("toolName", "")))),
        )
        role = "tool"
    # Claude puts tool results in user envelopes. They are foreign observations,
    # not a new human instruction in the continuing conversation.
    if role == "user" and "tool_result" in kinds:
        role = "tool"
    kind = "tool_activity" if kinds else "message"
    if role not in {"user", "assistant", "tool"}:
        kind = "context"
    return ExternalEntry(
        role,
        "\n\n".join(fragments),
        timestamp(raw.get("timestamp", message.get("timestamp")), fallback),
        raw,
        kind,
    )
