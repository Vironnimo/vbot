"""OpenCode Go provider adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from core.providers.anthropic import AnthropicAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter, _to_openai_assistant_message
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.token_getter import TokenGetter

_MINIMAX_ANTHROPIC_MODELS: frozenset[str] = frozenset({"minimax-m2.7"})


class OpenCodeGoAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter for the OpenCode Go gateway.

    Models with reasoning capability (DeepSeek, Kimi, GLM, ...) return
    ``reasoning_content`` in the assistant message and require it to be
    echoed back in every subsequent request of the same conversation.
    """

    def __init__(
        self,
        config: ProviderConfig,
        token_getter: TokenGetter | str,
    ) -> None:
        super().__init__(config, token_getter)
        self._anthropic = AnthropicAdapter(
            config,
            token_getter,
            auth_config=AuthConfig(
                header="x-api-key",
                prefix="",
                credential_key=config.connections[0].auth.credential_key,
            ),
        )

    async def aclose(self) -> None:
        await super().aclose()
        await self._anthropic.aclose()

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if model_id in _MINIMAX_ANTHROPIC_MODELS:
            return await self._anthropic.send(messages, model_id=model_id, **kwargs)
        return await super().send(messages, model_id=model_id, **kwargs)

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        if model_id in _MINIMAX_ANTHROPIC_MODELS:
            return self._anthropic.stream(messages, model_id=model_id, **kwargs)
        return super().stream(messages, model_id=model_id, **kwargs)

    def normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        if "choices" in response:
            return super().normalize_response(response)
        return self._anthropic.normalize_response(response)

    def _format_assistant_message(self, message: dict[str, Any]) -> dict[str, Any]:
        wire = _to_openai_assistant_message(message)
        reasoning = message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            wire["reasoning_content"] = reasoning
        return wire
