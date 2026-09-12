"""Media options."""

from __future__ import annotations

from collections.abc import Mapping

from core.model_tasks._option_types import (
    OPENAI_TTS_FORMAT_CHOICES,
    OPENAI_TTS_VOICES,
    OPENROUTER_TTS_FORMAT_CHOICES,
    PROVIDER_DEFAULT_CHOICE_LABEL,
    STT_RESPONSE_FORMAT_CHOICES,
    TaskModelOptionChoice,
    TaskModelOptionField,
    _parameter_is_supported,
    _string_values,
    _task_options,
    _to_choices,
)
from core.model_tasks.constants import (
    TASK_VIDEO_GENERATION,
)
from core.models import Model


def _text_to_speech_fields(
    provider_id: str,
    model: Model | None,
) -> tuple[TaskModelOptionField, ...]:
    format_choices = (
        OPENAI_TTS_FORMAT_CHOICES if provider_id == "openai" else OPENROUTER_TTS_FORMAT_CHOICES
    )
    fields: list[TaskModelOptionField] = [
        _tts_voice_field(model, provider_id),
        TaskModelOptionField(
            name="response_format",
            type="select",
            label="Format",
            default="mp3",
            options=_to_choices(format_choices),
        ),
        TaskModelOptionField(
            name="speed",
            type="number",
            label="Speed",
            default=1.0,
            min_value=0.25,
            max_value=4.0,
            step=0.05,
        ),
    ]
    # ``instructions`` is model-specific — only OpenAI's ``gpt-4o-mini-tts``
    # advertises support for it. The override file flags it in
    # ``supported_parameters``; we surface it exactly when present.
    if model is not None and "instructions" in model.capabilities.supported_parameters:
        fields.append(
            TaskModelOptionField(
                name="instructions",
                type="textarea",
                label="Instructions",
                default="",
                description="Optional speaking style instructions for the selected model.",
            )
        )
    return tuple(fields)


def _tts_voice_field(model: Model | None, provider_id: str) -> TaskModelOptionField:
    """Build the TTS ``voice`` field.

    The shape depends on what we know about the model:

    * ``model.capabilities.supported_voices`` non-empty → ``select`` with
      those voices (the only authoritative list — published by the
      provider per model).
    * Otherwise, ``provider_id == "openai"`` → ``select`` with the OpenAI
      canonical voice list (kokoro, gemini-tts, voxtral, … may also
      accept these names, but we do not invent them as a default).
    * Otherwise → ``text`` field with no default. The model is unknown
      to us and the user is expected to provide a voice id the provider
      accepts; this replaces the previous bug that always sent the
      OpenAI list to every provider.
    """

    if model is not None and model.capabilities.supported_voices:
        choices = tuple(
            TaskModelOptionChoice(value=voice_id, label=voice_id)
            for voice_id in model.capabilities.supported_voices
        )
        return TaskModelOptionField(
            name="voice",
            type="select",
            label="Voice",
            required=True,
            options=choices,
        )
    if provider_id == "openai":
        return TaskModelOptionField(
            name="voice",
            type="select",
            label="Voice",
            default="alloy",
            required=True,
            options=_to_choices(OPENAI_TTS_VOICES),
        )
    return TaskModelOptionField(
        name="voice",
        type="text",
        label="Voice",
        default="",
        description="Voice id supported by the selected model.",
    )


def _speech_to_text_fields(
    provider_id: str,
    model: Model | None,
) -> tuple[TaskModelOptionField, ...]:
    fields: list[TaskModelOptionField] = [
        TaskModelOptionField(
            name="language",
            type="text",
            label="Language",
            default="auto",
            description="ISO-639-1 code, or auto to let the provider detect it.",
        ),
        TaskModelOptionField(
            name="temperature",
            type="number",
            label="Temperature",
            default=0,
            min_value=0,
            max_value=1,
            step=0.1,
        ),
    ]
    if provider_id != "openrouter":
        fields.insert(
            1,
            TaskModelOptionField(
                name="prompt",
                type="textarea",
                label="Prompt",
                default="",
                description="Optional vocabulary or context bias for the transcription.",
            ),
        )
    if (
        provider_id != "openrouter"
        and model is not None
        and "response_format" in model.capabilities.supported_parameters
    ):
        fields.append(
            TaskModelOptionField(
                name="response_format",
                type="select",
                label="Response format",
                default="json",
                options=_to_choices(STT_RESPONSE_FORMAT_CHOICES),
                description="Format the provider returns the transcription in.",
            )
        )
    return tuple(fields)


def _video_generation_fields(
    provider_id: str,
    model: Model | None,
) -> tuple[TaskModelOptionField, ...]:
    if provider_id != "openrouter":
        return ()
    task_options = _task_options(model, TASK_VIDEO_GENERATION)
    parameters = task_options.get("parameters")
    if not isinstance(parameters, Mapping):
        parameters = {}

    fields: list[TaskModelOptionField] = []
    for name in ("resolution", "aspect_ratio", "size", "duration"):
        if name == "size" and any(
            conflict in parameters for conflict in ("resolution", "aspect_ratio")
        ):
            continue
        values = _string_values(parameters.get(name))
        if len(values) < 2:
            continue
        labels = (
            tuple(TaskModelOptionChoice(value=value, label=f"{value} seconds") for value in values)
            if name == "duration"
            else tuple(TaskModelOptionChoice(value=value, label=value) for value in values)
        )
        fields.append(
            TaskModelOptionField(
                name=name,
                type="select",
                label={
                    "resolution": "Resolution",
                    "aspect_ratio": "Aspect ratio",
                    "size": "Size",
                    "duration": "Duration",
                }[name],
                default="",
                options=(
                    TaskModelOptionChoice(value="", label=PROVIDER_DEFAULT_CHOICE_LABEL),
                    *labels,
                ),
            )
        )
    if _parameter_is_supported(parameters.get("generate_audio")):
        fields.append(
            TaskModelOptionField(
                name="generate_audio",
                type="boolean",
                label="Generate audio",
                default=None,
            )
        )
    if _parameter_is_supported(parameters.get("seed")):
        fields.append(
            TaskModelOptionField(
                name="seed",
                type="number",
                label="Seed",
                default=None,
                step=1,
                description="Reproducible generation seed.",
            )
        )

    passthrough = task_options.get("passthrough_parameters")
    if isinstance(passthrough, list | tuple) and passthrough:
        allowed = ", ".join(str(value) for value in passthrough)
        fields.append(
            TaskModelOptionField(
                name="provider_options",
                type="json",
                label="Provider options",
                default={},
                description=(
                    "Provider-specific options sent as provider.options, keyed by upstream "
                    f"provider slug. Catalog-advertised passthrough keys: {allowed}."
                ),
            )
        )
    return tuple(fields)


def _music_generation_fields(
    provider_id: str,
    model: Model | None,
) -> tuple[TaskModelOptionField, ...]:
    if provider_id != "openrouter":
        return ()
    supported = set(model.capabilities.supported_parameters) if model is not None else set()
    fields: list[TaskModelOptionField] = []
    if not supported or "temperature" in supported:
        fields.append(
            TaskModelOptionField(
                name="temperature",
                type="number",
                label="Temperature",
                default=None,
                min_value=0,
                max_value=2,
                step=0.1,
            )
        )
    if not supported or "top_p" in supported:
        fields.append(
            TaskModelOptionField(
                name="top_p",
                type="number",
                label="Top P",
                default=None,
                min_value=0,
                max_value=1,
                step=0.05,
            )
        )
    if not supported or "seed" in supported:
        fields.append(
            TaskModelOptionField(
                name="seed",
                type="number",
                label="Seed",
                default=None,
                step=1,
                description="Reproducible generation seed.",
            )
        )
    return tuple(fields)
