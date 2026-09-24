"""Backend-owned option schemas for specialized task models.

Schemas are model-aware and data-driven: ``option_schema_for`` accepts the
resolved :class:`core.models.Model` (when available) and builds fields from
what the model data advertises. Image options render generically from the
typed parameter schema in ``capabilities.task_options`` (projected at refresh
from the OpenRouter image API, or hand-authored in the override layer for
providers whose APIs publish nothing); TTS voices come from
``supported_voices``; STT/TTS/embedding extras are gated by
``supported_parameters``. This module owns only render hints (labels,
descriptions, control types) — the per-model facts live in the model DB.

Every provider target also gets an ``extra_options`` JSON escape hatch merged
into the provider request by the wire layer, so an option vBot does not
surface is usable without a code change. Video fields come from OpenRouter's
dedicated Video catalog; Music exposes only sampling controls published by its
Model. Live voice renders its voice and delegating backend Model from its
``live_voice`` facts; backend choices depend on the Model registry and the
target's Connection, so its schema carries no ``extra_options``."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from core.model_tasks._image_options import (
    _image_generation_fields,
)
from core.model_tasks._live_options import (
    ModelCatalog,
    _live_voice_fields,
    live_backend_candidates,
)
from core.model_tasks._media_options import (
    _music_generation_fields,
    _speech_to_text_fields,
    _text_to_speech_fields,
    _video_generation_fields,
)
from core.model_tasks._option_types import (
    ALLOWED_OPTION_TYPES,
    DALL_E_STYLE_CHOICES,
    EMBEDDING_RESERVED_PAYLOAD_FIELDS,
    FALLBACK_ASPECT_RATIOS,
    FALLBACK_RESOLUTIONS,
    GPT_IMAGE_BACKGROUND_CHOICES,
    GPT_IMAGE_OUTPUT_FORMAT_CHOICES,
    GPT_IMAGE_QUALITY_CHOICES,
    GPT_IMAGE_SIZE_CHOICES,
    IMAGE_CHOICE_LABELS,
    IMAGE_ENUM_FORCED_DEFAULTS,
    IMAGE_NUMBER_VALUED_PARAMETERS,
    IMAGE_PARAMETER_DESCRIPTIONS,
    IMAGE_PARAMETER_LABELS,
    IMAGE_PARAMETER_ORDER,
    IMAGE_PARAMETER_SKIP,
    IMAGE_SIZE_SHORTHAND_CONFLICTS,
    OPENAI_IMAGE_RESPONSE_FORMAT_CHOICES,
    OPENAI_TTS_FORMAT_CHOICES,
    OPENAI_TTS_VOICES,
    OPENROUTER_TTS_FORMAT_CHOICES,
    PROVIDER_DEFAULT_CHOICE_LABEL,
    STT_RESPONSE_FORMAT_CHOICES,
    JsonObject,
    TaskModelOptionChoice,
    TaskModelOptionField,
    TaskModelOptionSchema,
    TaskModelOptionValidationError,
    _extra_options_field,
)
from core.model_tasks.constants import (
    TASK_IMAGE_GENERATION,
    TASK_LIVE_VOICE,
    TASK_MUSIC_GENERATION,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_EMBEDDING,
    TASK_TEXT_TO_SPEECH,
    TASK_VIDEO_GENERATION,
)
from core.models import Model

__all__ = [
    "ALLOWED_OPTION_TYPES",
    "DALL_E_STYLE_CHOICES",
    "EMBEDDING_RESERVED_PAYLOAD_FIELDS",
    "FALLBACK_ASPECT_RATIOS",
    "FALLBACK_RESOLUTIONS",
    "GPT_IMAGE_BACKGROUND_CHOICES",
    "GPT_IMAGE_OUTPUT_FORMAT_CHOICES",
    "GPT_IMAGE_QUALITY_CHOICES",
    "GPT_IMAGE_SIZE_CHOICES",
    "IMAGE_CHOICE_LABELS",
    "IMAGE_ENUM_FORCED_DEFAULTS",
    "IMAGE_NUMBER_VALUED_PARAMETERS",
    "IMAGE_PARAMETER_DESCRIPTIONS",
    "IMAGE_PARAMETER_LABELS",
    "IMAGE_PARAMETER_ORDER",
    "IMAGE_PARAMETER_SKIP",
    "IMAGE_SIZE_SHORTHAND_CONFLICTS",
    "JsonObject",
    "ModelCatalog",
    "OPENAI_IMAGE_RESPONSE_FORMAT_CHOICES",
    "OPENAI_TTS_FORMAT_CHOICES",
    "OPENAI_TTS_VOICES",
    "OPENROUTER_TTS_FORMAT_CHOICES",
    "PROVIDER_DEFAULT_CHOICE_LABEL",
    "STT_RESPONSE_FORMAT_CHOICES",
    "TaskModelOptionChoice",
    "TaskModelOptionField",
    "TaskModelOptionSchema",
    "TaskModelOptionValidationError",
    "live_backend_candidates",
    "option_schema_for",
    "validate_task_model_options",
    "validate_text_embedding_options",
]


def validate_text_embedding_options(options: Mapping[str, Any]) -> None:
    """Validate values that define the embedding request and vector space."""

    dimensions = options.get("dimensions")
    if dimensions is not None and (
        not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions <= 0
    ):
        raise TaskModelOptionValidationError(
            "text_embedding dimensions must be a positive integer or null"
        )
    extra_options = options.get("extra_options")
    if extra_options is None:
        return
    if not isinstance(extra_options, Mapping):
        raise TaskModelOptionValidationError("text_embedding extra_options must be an object")
    reserved = sorted(EMBEDDING_RESERVED_PAYLOAD_FIELDS.intersection(extra_options))
    if reserved:
        raise TaskModelOptionValidationError(
            "text_embedding extra_options cannot override reserved fields: " + ", ".join(reserved)
        )


def validate_task_model_options(
    schema: TaskModelOptionSchema,
    options: Mapping[str, Any],
) -> None:
    """Validate persisted options against one target's public option schema."""

    if not isinstance(options, Mapping):
        raise TaskModelOptionValidationError(f"{schema.task_type} options must be an object")
    fields = {field.name: field for field in schema.fields}
    unknown = sorted(set(options) - set(fields))
    if unknown:
        available = ", ".join(sorted(fields)) or "none"
        raise TaskModelOptionValidationError(
            f"{schema.task_type} options are not supported: {', '.join(unknown)}; "
            f"available: {available}"
        )

    for field in schema.fields:
        value = options.get(field.name, field.default)
        if value is None or value == "":
            if field.required:
                raise TaskModelOptionValidationError(
                    f"{schema.task_type} option {field.name!r} is required"
                )
            continue
        _validate_task_model_option_value(schema.task_type, field, value)


def _validate_task_model_option_value(
    task_type: str,
    field: TaskModelOptionField,
    value: Any,
) -> None:
    label = f"{task_type} option {field.name!r}"
    if field.type in {"text", "textarea", "select"}:
        if not isinstance(value, str):
            raise TaskModelOptionValidationError(f"{label} must be a string")
        if field.type == "select":
            choices = tuple(option.value for option in field.options)
            if not choices:
                raise TaskModelOptionValidationError(f"{label} has no available choices")
            if value not in choices:
                raise TaskModelOptionValidationError(
                    f"{label} must be one of: {', '.join(choices)}"
                )
        return
    if field.type == "number":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TaskModelOptionValidationError(f"{label} must be a finite number")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise TaskModelOptionValidationError(f"{label} must be a finite number")
        if field.min_value is not None and value < field.min_value:
            raise TaskModelOptionValidationError(f"{label} must be at least {field.min_value}")
        if field.max_value is not None and value > field.max_value:
            raise TaskModelOptionValidationError(f"{label} must be at most {field.max_value}")
        return
    if field.type == "boolean":
        if not isinstance(value, bool):
            raise TaskModelOptionValidationError(f"{label} must be a boolean")
        return
    if (
        field.type == "json"
        and field.name in {"extra_options", "provider_options"}
        and not isinstance(value, Mapping)
    ):
        raise TaskModelOptionValidationError(f"{label} must be an object")


def option_schema_for(
    task_type: str,
    provider_id: str,
    target: str,
    *,
    model: Model | None = None,
    models: ModelCatalog | None = None,
    connection_id: str = "",
) -> TaskModelOptionSchema:
    """Return a model-aware option schema for *task_type* and *provider_id*.

    *model* is the resolved :class:`core.models.Model` for the target when
    available. Without it, the schema falls back to the provider-level
    conservative defaults that the model-aware branches extend. Media task
    types additionally carry the ``extra_options`` escape hatch (provider
    targets only — local targets never reach this builder).

    *models* is the Model registry and *connection_id* the target's local
    Connection id; live voice needs both to offer backend Model choices and
    offers none without the registry.
    """

    if task_type == TASK_LIVE_VOICE:
        return TaskModelOptionSchema(
            task_type=task_type,
            target=target,
            fields=_live_voice_fields(
                provider_id, model, models=models, connection_id=connection_id
            ),
        )
    if task_type == TASK_SPEECH_TO_TEXT:
        fields = _speech_to_text_fields(provider_id, model)
    elif task_type == TASK_TEXT_TO_SPEECH:
        fields = _text_to_speech_fields(provider_id, model)
    elif task_type == TASK_IMAGE_GENERATION:
        fields = _image_generation_fields(provider_id, model)
    elif task_type == TASK_VIDEO_GENERATION:
        fields = _video_generation_fields(provider_id, model)
    elif task_type == TASK_MUSIC_GENERATION:
        fields = _music_generation_fields(provider_id, model)
    elif task_type == TASK_TEXT_EMBEDDING:
        fields = _text_embedding_fields(provider_id, model)
    else:
        return TaskModelOptionSchema(task_type=task_type, target=target)
    return TaskModelOptionSchema(
        task_type=task_type,
        target=target,
        fields=(*fields, _extra_options_field()),
    )


def _text_embedding_fields(
    provider_id: str,
    model: Model | None,
) -> tuple[TaskModelOptionField, ...]:
    """Embedding task-model option schema.

    The first iteration only ships ``dimensions`` — the Matryoshka
    truncation knob for models that advertise it. The OpenRouter
    `/api/v1/embeddings` endpoint accepts a single optional
    ``dimensions`` integer; non-Matryoshka models reject it. The
    backend emits an empty default for optional ``number`` fields, and
    the wire layer drops empties — so this field is harmless for
    models that ignore it. Future embedding fields (e.g. ``input_type``
    for asymmetric query/document embedding) belong here too, gated
    by ``model.capabilities.supported_parameters`` like the rest of
    the model-aware schema builders.
    """

    supported: frozenset[str] | None = (
        frozenset(model.capabilities.supported_parameters) if model is not None else None
    )

    def has(field_name: str) -> bool:
        return supported is None or field_name in supported

    fields: list[TaskModelOptionField] = []
    if has("dimensions"):
        fields.append(_dimensions_field())
    return tuple(fields)


def _dimensions_field() -> TaskModelOptionField:
    return TaskModelOptionField(
        name="dimensions",
        type="number",
        label="Output dimensions",
        default=None,
        min_value=1,
        step=1,
        description=(
            "Matryoshka truncation. Optional — leave empty to use the "
            "model's native dimension. Non-Matryoshka models reject this "
            "value."
        ),
    )
