"""MiniMax provider adapter.

The API-key Connections speak Chat Completions and the subscription Connection
speaks Anthropic Messages; ``resources/wire/minimax.json`` selects the protocol
per Connection and owns the reasoning, sampling, prompt-cache and media rules.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, override

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._chat_completions_catalog import (
    _read_optional_non_empty_string,
    _read_string,
)
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


MINIMAX_M3_MODEL_ID = "MiniMax-M3"
MINIMAX_M2_SUPPORTED_PARAMETERS = (
    "max_tokens",
    "reasoning_split",
    "temperature",
    "tools",
    "top_p",
)
MINIMAX_M3_SUPPORTED_PARAMETERS = (
    "max_completion_tokens",
    "max_tokens",
    "reasoning_split",
    "stream_options",
    "temperature",
    "thinking",
    "tools",
    "top_p",
)
# MiniMax publishes a *recommended* and a *hard-max* output allowance per model
# (https://platform.minimax.io/docs/api-reference/text-chat-openai). vBot pins
# the output ceiling to the RECOMMENDED value, not the hard max: for the M2.x
# series the hard max (204,800) equals the context window, so defaulting the
# output allowance to it would collide with any non-trivial prompt and 400. The
# recommended value is a safe, non-truncating default an order of magnitude above
# the old flat 8,192 config cap; a caller can still request more explicitly (M3
# up to 524,288, M2.x up to 204,800). This ceiling is what the OpenAI-compatible
# base defaults ``max_tokens`` to when the caller sends none.
MINIMAX_M2_RECOMMENDED_MAX_OUTPUT = 65536
MINIMAX_M3_RECOMMENDED_MAX_OUTPUT = 131072

MINIMAX_MODEL_FACTS: dict[str, dict[str, Any]] = {
    "MiniMax-M2": {
        "name": "MiniMax M2",
        "context_window": 204800,
        "max_output_tokens": MINIMAX_M2_RECOMMENDED_MAX_OUTPUT,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    "MiniMax-M2.1": {
        "name": "MiniMax M2.1",
        "context_window": 204800,
        "max_output_tokens": MINIMAX_M2_RECOMMENDED_MAX_OUTPUT,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    "MiniMax-M2.1-highspeed": {
        "name": "MiniMax M2.1 Highspeed",
        "context_window": 204800,
        "max_output_tokens": MINIMAX_M2_RECOMMENDED_MAX_OUTPUT,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    "MiniMax-M2.5": {
        "name": "MiniMax M2.5",
        "context_window": 204800,
        "max_output_tokens": MINIMAX_M2_RECOMMENDED_MAX_OUTPUT,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    "MiniMax-M2.5-highspeed": {
        "name": "MiniMax M2.5 Highspeed",
        "context_window": 204800,
        "max_output_tokens": MINIMAX_M2_RECOMMENDED_MAX_OUTPUT,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    "MiniMax-M2.7": {
        "name": "MiniMax M2.7",
        "context_window": 204800,
        "max_output_tokens": MINIMAX_M2_RECOMMENDED_MAX_OUTPUT,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    "MiniMax-M2.7-highspeed": {
        "name": "MiniMax M2.7 Highspeed",
        "context_window": 204800,
        "max_output_tokens": MINIMAX_M2_RECOMMENDED_MAX_OUTPUT,
        "input_modalities": ("text",),
        "supported_parameters": MINIMAX_M2_SUPPORTED_PARAMETERS,
    },
    MINIMAX_M3_MODEL_ID: {
        "name": "MiniMax M3",
        "context_window": 1000000,
        "max_output_tokens": MINIMAX_M3_RECOMMENDED_MAX_OUTPUT,
        "input_modalities": ("text", "image", "video"),
        "supported_parameters": MINIMAX_M3_SUPPORTED_PARAMETERS,
    },
}


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

    @classmethod
    @override
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one MiniMax ``/models`` entry into a vBot ``Model``."""

        model_id = _read_string(raw, "id")
        facts = MINIMAX_MODEL_FACTS.get(model_id)
        if facts is None:
            return super().normalize_catalog_entry(raw, defaults)

        name = (
            _read_optional_non_empty_string(raw, "name")
            or _read_optional_non_empty_string(raw, "display_name")
            or str(facts["name"])
        )
        input_modalities = tuple(facts["input_modalities"])

        return Model(
            model_id=model_id,
            name=name,
            capabilities=Capabilities(
                vision="image" in input_modalities,
                tools=True,
                json_mode=False,
                reasoning=ReasoningCapabilities(supported=True),
                input_modalities=input_modalities,
                output_modalities=("text",),
                supported_parameters=tuple(facts["supported_parameters"]),
            ),
            context_window=int(facts["context_window"]),
            max_output_tokens=int(facts["max_output_tokens"]),
        )

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
