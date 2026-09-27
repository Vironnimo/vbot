"""Shared fixtures and fakes for compaction behavior tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any

from core.chat import ChatMessage
from core.compaction import CompactionService, CompactionSettings
from core.compaction.compaction import COMPACTION_USER_QUOTE_PREFIX
from core.sessions import SessionAddress
from core.utils.tokens import estimate_request_input_tokens

TIMESTAMP = "2026-05-19T12:00:00+00:00"
PROMPT_FRAGMENT = "Preserve decisions and unfinished work."
SESSION = SessionAddress(project_id=None, agent_id="coder", session_id="session")


def _tail_token_span(messages):
    return estimate_request_input_tokens([item.to_dict() for item in messages])[0]


class StubStorage:
    def __init__(self) -> None:
        self.read_names: list[str] = []

    def read_prompt_fragment(self, name: str) -> str:
        self.read_names.append(name)
        assert name in {
            "compaction.md",
            "compaction-manual.md",
            "compaction-continuation.md",
            "compaction-continuation-manual.md",
        }
        return PROMPT_FRAGMENT


class StubAdapter:
    def __init__(self, text: str = "COMPACTED") -> None:
        self.text = text
        self.requests: list[dict[str, Any]] = []

    async def stream(self, messages: list[dict], **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.requests.append({"messages": messages, **kwargs})
        yield {"type": "content_delta", "text": self.text}
        yield {"type": "finish", "reason": "stop"}


async def compact(
    messages: list[ChatMessage], *, service: CompactionService | None = None, **overrides: Any
) -> ChatMessage:
    """Run one Compaction for the test Session; keyword overrides replace the defaults."""
    arguments: dict[str, Any] = {
        "session_address": SESSION,
        "prompt_cache_affinity_id": "test-affinity",
        "summary_adapter": StubAdapter(),
        "summary_model_id": "openai/summary",
        "storage": StubStorage(),
        "settings": CompactionSettings(tail_tokens=1),
        **overrides,
    }
    return await (service or CompactionService()).compact(messages, **arguments)


def message(message_id: str, role: str, content: str, **extra: Any) -> ChatMessage:
    return ChatMessage.from_dict(
        {"id": message_id, "timestamp": TIMESTAMP, "role": role, "content": content, **extra}
    )


def user(message_id: str, content: str) -> ChatMessage:
    return message(message_id, "user", content)


def assistant(message_id: str, content: str) -> ChatMessage:
    return message(message_id, "assistant", content, model="openai/gpt-5")


def tool_step(
    step: str, output: str, *, name: str = "read", arguments: dict[str, Any] | None = None
) -> list[ChatMessage]:
    """One Assistant Tool Call (`a-<step>`) and its correlated Tool Result (`t-<step>`)."""
    call_id = f"call-{step}"
    call = {"id": call_id, "name": name, "arguments": arguments or {"path": step}}
    return [
        message(f"a-{step}", "assistant", "", model="openai/gpt-5", tool_calls=[call]),
        message(f"t-{step}", "tool", output, tool_call_id=call_id, name=name),
    ]


def checkpoint(projection: list[ChatMessage], count: int = 10) -> ChatMessage:
    return ChatMessage.compaction_checkpoint(
        summary="PRIOR",
        projection=projection,
        compacted_token_count=count,
        policy="summary_tail",
        strategy="summary_tail",
    )


def provider_request(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    return [
        {"id": "system-1", "role": "system", "content": "system"},
        *(item.to_dict() for item in messages),
    ]


def user_quotes(effective: Iterable[ChatMessage]) -> list[ChatMessage]:
    """The historical User quote notes of an effective Context."""
    return [
        item for item in effective if str(item.content).startswith(COMPACTION_USER_QUOTE_PREFIX)
    ]


def quote_payload(quote: ChatMessage) -> str:
    return str(quote.content).removeprefix(COMPACTION_USER_QUOTE_PREFIX)


def quoted_user(quote: ChatMessage) -> dict[str, Any]:
    quoted: dict[str, Any] = json.loads(quote_payload(quote))
    return quoted
