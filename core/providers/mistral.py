"""Mistral provider adapter."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Any, override
from urllib.parse import urlencode

from core.models.models import (
    Capabilities,
    Model,
    ReasoningCapabilities,
    derive_model_task_types,
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
            for flag, task in (
                ("audio_transcription", "speech_to_text"),
                ("audio_speech", "text_to_speech"),
            ):
                details = raw.get(f"{flag}_capabilities")
                details = details if isinstance(details, Mapping) else {}
                modes = details.get("modes")
                if capabilities_raw.get(flag) is True and (
                    not isinstance(modes, list) or "offline" in modes
                ):
                    return Model(
                        model_id=model_id,
                        name=name,
                        capabilities=_speech_capabilities(task, details),
                        context_window=context_window,
                        max_output_tokens=None,
                        metadata={
                            "mistral": {
                                "billing_model_name": _read_optional_non_empty_string(
                                    raw, "billing_model_name"
                                )
                                or model_id
                            }
                        },
                    )
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
        if capabilities_raw.get("audio") is True or audio_transcription_supported:
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
                # Audio chat is not the separate transcription endpoint.
                task_types=tuple(
                    task
                    for task in derive_model_task_types(input_modalities, ("text",))
                    if task != "speech_to_text" or audio_transcription_supported
                ),
            ),
            context_window=context_window,
            max_output_tokens=None,
        )

    @classmethod
    async def discover_task_models(
        cls,
        normalized_models: Mapping[str, Model],
        fetch_json: Callable[[str], Awaitable[Any]],
    ) -> dict[str, Model]:
        """Enrich TTS Models with public preset slugs, never Account-owned voices."""
        speech_models = {
            key: model
            for key, model in normalized_models.items()
            if "text_to_speech" in model.capabilities.task_types
        }
        if not speech_models:
            return {}
        voices: set[str] = set()
        seen_tokens: set[str] = set()
        params = {"type": "preset", "page_size": "100"}
        while True:
            # The speech API base is /v1; voice search uses /v2 on that same host.
            payload = await fetch_json("../v2/audio/voices?" + urlencode(params))
            if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
                raise ValueError("Mistral voices response must contain a data list")
            for voice in payload["data"]:
                if isinstance(voice, Mapping) and voice.get("type") == "preset":
                    slug = voice.get("slug")
                    if isinstance(slug, str) and slug.strip():
                        voices.add(slug.strip())
            token = payload.get("next_page_token")
            if not token:
                break
            if not isinstance(token, str) or token in seen_tokens or len(seen_tokens) >= 99:
                raise ValueError("Mistral voices returned invalid pagination")
            seen_tokens.add(token)
            params["page_token"] = token
        return {
            key: replace(
                model,
                capabilities=replace(
                    model.capabilities,
                    supported_voices=tuple(
                        voice
                        for voice in voices
                        if model.metadata.get("mistral", {}).get("billing_model_name")
                        != "voxtral-mini-tts-2603"
                        or not voice.startswith("voice_")
                    ),
                ),
            )
            for key, model in speech_models.items()
        }


def _speech_capabilities(task: str, details: Mapping[str, Any]) -> Capabilities:
    """Project the endpoint contract and /models audio capability details."""
    parameters: dict[str, Any] = {}
    if task == "speech_to_text":
        parameters["language"] = {"type": "string", "default": "auto"}
        languages = _strings(details.get("languages"))
        if languages:
            parameters["language"] = {
                "type": "enum",
                "values": ["auto", *languages],
                "default": "auto",
            }
        parameters["temperature"] = {"type": "range", "min": 0, "max": 1, "default": 0}
        if details.get("context_bias") is True:
            parameters["context_bias"] = {"type": "string"}
        granularities = _strings(details.get("timestamp_granularities"))
        if granularities:
            parameters["timestamp_granularities"] = {"type": "enum", "values": granularities}
        if details.get("diarization") is True:
            parameters["diarize"] = {"type": "boolean", "default": False}
        inputs, outputs = ("audio",), ("transcription",)
    else:
        parameters["voice"] = {"type": "string", "default": "gb_jane_neutral"}
        formats = _strings(details.get("response_formats")) or ["mp3"]
        parameters["response_format"] = {
            "type": "enum",
            "values": formats,
            "default": "mp3" if "mp3" in formats else formats[0],
        }
        if details.get("instruction") is True:
            parameters["instructions"] = {"type": "string"}
        inputs, outputs = ("text",), ("speech",)
    return Capabilities(
        vision=False,
        tools=False,
        json_mode=False,
        reasoning=ReasoningCapabilities(supported=False),
        input_modalities=inputs,
        output_modalities=outputs,
        supported_parameters=tuple(parameters),
        task_options={task: {"parameters": parameters}},
    )


def _strings(value: Any) -> list[str]:
    return (
        [item for item in value if isinstance(item, str) and item]
        if isinstance(value, list)
        else []
    )
