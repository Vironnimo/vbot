"""OpenCode Go provider adapter."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from core.providers.adapter import ModelLookup
from core.providers.anthropic import AnthropicAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter, _to_openai_assistant_message
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.token_getter import TokenGetter

_ANTHROPIC_MESSAGES_MODELS: frozenset[str] = frozenset(
    {
        "minimax-m2.7",
        "minimax-m2.5",
        "qwen3.6-plus",
        "qwen3.5-plus",
    }
)
_SYSTEM_REMINDER_BLOCKS_PATTERN = re.compile(
    r"^<system-reminder>\n[\s\S]*?\n</system-reminder>(?:\n<system-reminder>\n[\s\S]*?\n</system-reminder>)*$"
)


class OpenCodeGoAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter for the OpenCode Go gateway.

    Models with reasoning capability (DeepSeek, Kimi, GLM, ...) return
    ``reasoning_content`` in assistant messages.

    For OpenCode Go models routed through the Anthropic ``/messages`` path,
    this adapter replays reasoning only for the active assistant continuation
    turn (assistant tool call followed by tool results) to avoid stale reasoning
    from older completed turns and unbounded prompt growth.

    For OpenCode Go models routed through OpenAI ``/chat/completions``, the
    adapter replays assistant reasoning for every historical assistant message
    because the provider expects full ``reasoning_content`` round-tripping.
    """

    def __init__(
        self,
        config: ProviderConfig,
        token_getter: TokenGetter | str,
        base_url: str | None = None,
        auth_config: AuthConfig | None = None,
        model_lookup: ModelLookup | None = None,
    ) -> None:
        super().__init__(
            config,
            token_getter,
            base_url,
            auth_config,
            model_lookup=model_lookup,
        )
        selected_auth_config = auth_config or config.connections[0].auth
        self._anthropic = AnthropicAdapter(
            config,
            token_getter,
            base_url=base_url,
            auth_config=AuthConfig(
                header="x-api-key",
                prefix="",
                credential_key=selected_auth_config.credential_key,
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
        if _uses_anthropic_messages_path(model_id):
            bounded_messages = _bound_assistant_reasoning_replay(messages)
            return await self._anthropic.send(bounded_messages, model_id=model_id, **kwargs)
        return await super().send(messages, model_id=model_id, **kwargs)

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        if _uses_anthropic_messages_path(model_id):
            bounded_messages = _bound_assistant_reasoning_replay(messages)
            return self._anthropic.stream(bounded_messages, model_id=model_id, **kwargs)
        return super().stream(messages, model_id=model_id, **kwargs)

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload_messages = messages
        if _uses_anthropic_messages_path(model_id):
            payload_messages = _bound_assistant_reasoning_replay(messages)
        return super()._build_payload(payload_messages, model_id, **kwargs)

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


def _bound_assistant_reasoning_replay(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keep_index = _active_assistant_continuation_index(messages)

    sanitized_messages: list[dict[str, Any]] = []
    changed = False
    for index, message in enumerate(messages):
        if message.get("role") != "assistant" or index == keep_index:
            sanitized_messages.append(message)
            continue
        if "reasoning" not in message and "reasoning_meta" not in message:
            sanitized_messages.append(message)
            continue

        sanitized_message = dict(message)
        sanitized_message.pop("reasoning", None)
        sanitized_message.pop("reasoning_meta", None)
        sanitized_messages.append(sanitized_message)
        changed = True

    return sanitized_messages if changed else messages


def _active_assistant_continuation_index(messages: list[dict[str, Any]]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "assistant":
            continue
        if not message.get("tool_calls"):
            return None
        continuation_suffix = messages[index + 1 :]
        if _is_active_continuation_suffix(continuation_suffix):
            return index
        return None
    return None


def _is_active_continuation_suffix(continuation_suffix: list[dict[str, Any]]) -> bool:
    if not continuation_suffix:
        return False

    saw_tool_result = False
    saw_synthetic_user_note = False
    for candidate in continuation_suffix:
        if candidate.get("role") == "tool":
            if saw_synthetic_user_note:
                return False
            saw_tool_result = True
            continue
        if candidate.get("role") == "user" and _is_synthetic_system_reminder_message(candidate):
            if not saw_tool_result:
                return False
            saw_synthetic_user_note = True
            continue
        return False
    return saw_tool_result


def _is_synthetic_system_reminder_message(message: dict[str, Any]) -> bool:
    if "id" in message or "timestamp" in message:
        return False

    content = message.get("content")
    if not isinstance(content, str):
        return False

    return bool(_SYSTEM_REMINDER_BLOCKS_PATTERN.fullmatch(content.strip()))


def _uses_anthropic_messages_path(model_id: str) -> bool:
    return model_id in _ANTHROPIC_MESSAGES_MODELS
