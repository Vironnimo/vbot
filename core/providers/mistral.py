"""Mistral provider adapter."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, override

from core.models.models import (
    Capabilities,
    Model,
    ReasoningCapabilities,
    text_embedding_capabilities,
)
from core.providers._chat_completions_catalog import (
    _parse_optional_int,
    _read_optional_non_empty_string,
    _read_string,
)
from core.providers.errors import CatalogEntrySkipped
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
)

# Mistral's catalog has no embedding capability flag; its embedding Models are
# the non-chat ``*-embed`` ids (``mistral-embed``, ``codestral-embed-2505``).
_EMBEDDING_MODEL_ID = re.compile(r"(?:^|-)embed(?:-|$)")


class MistralAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter with Mistral's catalog and content-chunk responses.

    The request wire (binary reasoning effort, nine-character Tool-call ids,
    chunk-only replay) is described by ``resources/wire/mistral.json``.
    """

    @classmethod
    @override
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

        if raw.get("archived") is True:
            raise CatalogEntrySkipped(f"Skipped archived model: {model_id}")
        # Absent → ``None`` (honest "unknown"), filled by the read-side default
        # chain at use time — no fake ``0`` written into the catalog.
        context_window = _parse_optional_int(raw.get("max_context_length"))
        if capabilities_raw.get("completion_chat") is not True:
            if not _EMBEDDING_MODEL_ID.search(model_id):
                raise CatalogEntrySkipped(f"Skipped non-chat model: {model_id}")
            return Model(
                model_id=model_id,
                name=name,
                capabilities=text_embedding_capabilities(),
                context_window=context_window,
                max_output_tokens=None,
            )

        reported_reasoning = capabilities_raw.get("reasoning")
        reasoning_supported = reported_reasoning if isinstance(reported_reasoning, bool) else None
        vision_supported = capabilities_raw.get("vision", False) is True
        tools_supported = capabilities_raw.get("function_calling", False) is True
        audio_transcription_supported = capabilities_raw.get("audio_transcription", False) is True
        input_modalities = ["text"]
        if vision_supported:
            input_modalities.append("image")
        if audio_transcription_supported:
            input_modalities.append("audio")
        supported_parameters = ["response_format"]
        if tools_supported:
            supported_parameters.append("tools")
        if reasoning_supported:
            supported_parameters.append("reasoning")
        if audio_transcription_supported:
            supported_parameters.append("audio_transcription")

        return Model(
            model_id=model_id,
            name=name,
            capabilities=Capabilities(
                vision=vision_supported,
                tools=tools_supported,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=reasoning_supported),
                input_modalities=tuple(input_modalities),
                output_modalities=("text",),
                supported_parameters=tuple(supported_parameters),
            ),
            context_window=context_window,
            max_output_tokens=None,
        )
