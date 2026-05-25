"""OpenRouter provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
    _provider_default_max_tokens,
    _read_int,
    _read_mapping,
    _read_string,
    _read_string_list,
)
from core.providers.reasoning import closest_supported_effort

OPENROUTER_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max"}


class OpenRouterAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter with OpenRouter-specific behavior."""

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one OpenRouter ``/models`` entry into a vBot ``Model``."""

        architecture = _read_mapping(raw, "architecture")
        top_provider = _read_mapping(raw, "top_provider")
        supported_parameters = _read_string_list(raw, "supported_parameters")
        input_modalities = _read_string_list(architecture, "input_modalities")

        max_completion_tokens = top_provider.get("max_completion_tokens")
        if max_completion_tokens is None:
            max_completion_tokens = _provider_default_max_tokens(defaults)

        return Model(
            model_id=_read_string(raw, "id"),
            name=_read_string(raw, "name"),
            capabilities=Capabilities(
                vision="image" in input_modalities,
                tools="tools" in supported_parameters,
                json_mode=(
                    "response_format" in supported_parameters
                    or "structured_outputs" in supported_parameters
                ),
                reasoning=ReasoningCapabilities(
                    supported=(
                        "reasoning" in supported_parameters
                        or "include_reasoning" in supported_parameters
                    ),
                ),
            ),
            context_window=_read_int(raw, "context_length"),
            max_output_tokens=int(max_completion_tokens),
        )

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build an OpenRouter payload with OpenRouter reasoning parameters."""

        thinking_effort = kwargs.pop("thinking_effort", "")
        reasoning_effort = kwargs.pop("reasoning_effort", "")
        payload = super()._build_payload(messages, model_id, **kwargs)
        if self._model_reasoning_supported(model_id) is False:
            payload.pop("reasoning", None)
            payload.pop("include_reasoning", None)
            return payload

        supported_effort = closest_supported_effort(
            thinking_effort or reasoning_effort,
            OPENROUTER_REASONING_EFFORTS,
        )
        if supported_effort is not None:
            payload["reasoning"] = {"effort": supported_effort}
            payload["include_reasoning"] = True
        return payload
