"""OpenCode Go provider adapter."""

from __future__ import annotations

from typing import Any

from core.providers.openai_compatible import OpenAICompatibleAdapter, _to_openai_assistant_message


class OpenCodeGoAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter for the OpenCode Go gateway.

    Models with reasoning capability (DeepSeek, Kimi, GLM, ...) return
    ``reasoning_content`` in the assistant message and require it to be
    echoed back in every subsequent request of the same conversation.
    """

    def _format_assistant_message(self, message: dict[str, Any]) -> dict[str, Any]:
        wire = _to_openai_assistant_message(message)
        reasoning = message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            wire["reasoning_content"] = reasoning
        return wire
