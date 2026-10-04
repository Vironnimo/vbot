"""MiniMax provider adapter.

The API-key Connections speak Chat Completions and the subscription Connection
speaks Anthropic Messages; ``resources/wire/minimax.json`` selects the protocol
per Connection and owns the reasoning, sampling, prompt-cache and media rules;
``resources/models/minimax.overrides.json`` owns the per-Model catalog facts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, override

from core.providers.adapter import ModelLookup
from core.providers.anthropic_compatible import AnthropicCompatibleAdapter
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
)
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.token_getter import TokenGetter
from core.providers.wire_profile import Protocol
from core.providers.wire_profiles import WireBinding

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder


class MiniMaxAdapter(OpenAICompatibleAdapter):
    """MiniMax adapter for direct OpenAI and subscription Messages wires.

    Requests whose wire profile selects the ``messages`` protocol go through an
    inner Messages Adapter that shares this Adapter's HTTP client and wire
    profiles.
    """

    WIRE_PROTOCOLS: ClassVar[tuple[Protocol, ...]] = ("chat_completions", "messages")

    def __init__(
        self,
        config: ProviderConfig,
        token_getter: TokenGetter | str,
        base_url: str | None = None,
        auth_config: AuthConfig | None = None,
        model_lookup: ModelLookup | None = None,
        debug_recorder: ProviderDebugRecorder | None = None,
        *,
        connection_mode: str | None = None,
    ) -> None:
        super().__init__(
            config,
            token_getter,
            base_url,
            auth_config,
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
            connection_mode=connection_mode,
        )
        selected_auth_config = auth_config or config.connections[0].auth
        self._messages = AnthropicCompatibleAdapter(
            config,
            self._token_getter,
            base_url=base_url,
            auth_config=selected_auth_config,
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
            client=self._client,
        )
        self._messages.bind_wire_profiles(self.wire)

    @override
    def bind_wire_profiles(self, binding: WireBinding) -> None:
        super().bind_wire_profiles(binding)
        self._messages.bind_wire_profiles(binding)

    @override
    async def aclose(self) -> None:
        await self._messages.aclose()
        await super().aclose()

    @override
    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self._uses_messages(model_id):
            request_kwargs = dict(kwargs)
            self._apply_model_output_limit(request_kwargs, model_id, messages)
            return await self._messages.send(messages, model_id=model_id, **request_kwargs)
        return await super().send(messages, model_id=model_id, **kwargs)

    @override
    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        if self._uses_messages(model_id):
            request_kwargs = dict(kwargs)
            self._apply_model_output_limit(request_kwargs, model_id, messages)
            return self._messages.stream(messages, model_id=model_id, **request_kwargs)
        return super().stream(messages, model_id=model_id, **kwargs)

    @override
    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int:
        """Estimate the rendered request of the wire the Model's profile selects."""

        if self._uses_messages(model_id):
            return self._messages.estimate_request_input_tokens(
                messages, model_id=model_id, tools=tools
            )
        return super().estimate_request_input_tokens(messages, model_id=model_id, tools=tools)

    @override
    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        if self._uses_messages(model_id):
            return self._messages.normalize_response(response, model_id=model_id)
        normalized = super().normalize_response(response, model_id=model_id)
        if normalized.get("reasoning") is None:
            reasoning = _extract_reasoning_details_text(normalized.get("reasoning_meta"))
            if reasoning:
                normalized["reasoning"] = reasoning
        return normalized

    def _uses_messages(self, model_id: str | None) -> bool:
        return self.wire_profile(model_id or "").protocol == "messages"


def _extract_reasoning_details_text(reasoning_meta: Any) -> str | None:
    if not isinstance(reasoning_meta, dict):
        return None

    reasoning_details = reasoning_meta.get("reasoning_details")
    if not isinstance(reasoning_details, list):
        return None

    parts: list[str] = []
    for detail in reasoning_details:
        if not isinstance(detail, dict):
            continue
        text = detail.get("text")
        if isinstance(text, str):
            parts.append(text)

    return "".join(parts) or None
