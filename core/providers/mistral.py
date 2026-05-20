"""Mistral provider adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import CatalogEntrySkipped
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
    _parse_optional_int,
    _provider_default_max_tokens,
    _read_optional_non_empty_string,
    _read_string,
)

MISTRAL_HIGH_REASONING_EFFORTS = {"medium", "high", "xhigh", "max"}


class MistralAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter with Mistral-specific catalog and reasoning behavior."""

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one Mistral ``/models`` entry into a vBot ``Model``."""

        model_id = _read_string(raw, "id")
        name = _read_optional_non_empty_string(raw, "name") or model_id

        capabilities_raw = raw.get("capabilities", {})
        if not isinstance(capabilities_raw, dict):
            capabilities_raw = {}

        if raw.get("archived") is True or capabilities_raw.get("completion_chat") is not True:
            raise CatalogEntrySkipped(f"Skipped non-chat model: {raw.get('id')}")

        context_window = _parse_optional_int(raw.get("max_context_length")) or 0
        reasoning_supported = capabilities_raw.get("reasoning", False) is True

        return Model(
            model_id=model_id,
            name=name,
            capabilities=Capabilities(
                vision=capabilities_raw.get("vision", False) is True,
                tools=capabilities_raw.get("function_calling", False) is True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=reasoning_supported),
            ),
            context_window=context_window,
            max_output_tokens=_provider_default_max_tokens(defaults),
        )

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build a Mistral payload with Mistral-specific reasoning effort mapping."""

        thinking_effort = kwargs.pop("thinking_effort", "")
        payload = super()._build_payload(messages, model_id, **kwargs)
        if thinking_effort in MISTRAL_HIGH_REASONING_EFFORTS:
            payload["reasoning_effort"] = "high"
        elif thinking_effort == "none":
            payload["reasoning_effort"] = "none"
        return payload
