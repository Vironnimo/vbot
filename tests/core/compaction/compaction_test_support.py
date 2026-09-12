"""Shared fixtures and fakes for compaction behavior tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.chat import ChatMessage
from core.utils.tokens import estimate_request_input_tokens

TIMESTAMP = "2026-05-19T12:00:00+00:00"


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
        return "Preserve decisions and unfinished work."


class StubAdapter:
    def __init__(self, text: str = "COMPACTED") -> None:
        self.text = text
        self.requests: list[dict[str, Any]] = []

    async def stream(self, messages: list[dict], **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        self.requests.append({"messages": messages, **kwargs})
        yield {"type": "content_delta", "text": self.text}
        yield {"type": "finish", "reason": "stop"}


def message(message_id: str, role: str, content: str, **extra: Any) -> ChatMessage:
    return ChatMessage.from_dict(
        {"id": message_id, "timestamp": TIMESTAMP, "role": role, "content": content, **extra}
    )


def user(message_id: str, content: str) -> ChatMessage:
    return message(message_id, "user", content)


def assistant(message_id: str, content: str) -> ChatMessage:
    return message(message_id, "assistant", content, model="openai/gpt-5")


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
