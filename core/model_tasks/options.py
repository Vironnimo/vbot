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
surface is usable without a code change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.model_tasks.constants import (
    TASK_IMAGE_GENERATION,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_EMBEDDING,
    TASK_TEXT_TO_SPEECH,
)
from core.models import Model

JsonObject = dict[str, Any]

# Whitelist of field types the Settings UI knows how to render. The frontend
# renderer treats anything outside this set as a plain text input, so we
# validate the type here to catch typos and unknown additions before they
# reach the wire as misleading options.
ALLOWED_OPTION_TYPES: frozenset[str] = frozenset(
    {"text", "textarea", "select", "number", "boolean", "json"}
)
EMBEDDING_RESERVED_PAYLOAD_FIELDS: frozenset[str] = frozenset(
    {"model", "input", "encoding_format", "dimensions", "input_type"}
)


class TaskModelOptionValidationError(ValueError):
    """Raised when a task-model option field is malformed."""


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


# ---------------------------------------------------------------------------
# OpenAI fallback tables
#
# OpenRouter does not publish the OpenAI voice list (or the full OpenAI
# response_format set) for every model. When a target has no model-specific
# ``supported_voices`` we still want the OpenAI provider to expose the
# canonical voice/format lists; for every other provider we fall back to
# free-text inputs because the list of valid voices is model-specific.
# ---------------------------------------------------------------------------

OPENAI_TTS_VOICES: tuple[tuple[str, str], ...] = (
    ("alloy", "Alloy"),
    ("ash", "Ash"),
    ("ballad", "Ballad"),
    ("coral", "Coral"),
    ("echo", "Echo"),
    ("fable", "Fable"),
    ("nova", "Nova"),
    ("onyx", "Onyx"),
    ("sage", "Sage"),
    ("shimmer", "Shimmer"),
    ("verse", "Verse"),
)

OPENAI_TTS_FORMAT_CHOICES: tuple[tuple[str, str], ...] = (
    ("mp3", "MP3"),
    ("opus", "Opus"),
    ("aac", "AAC"),
    ("flac", "FLAC"),
    ("wav", "WAV"),
    ("pcm", "PCM"),
)

OPENROUTER_TTS_FORMAT_CHOICES: tuple[tuple[str, str], ...] = (
    ("mp3", "MP3"),
    ("pcm", "PCM"),
)

# OpenAI Whisper-style STT response formats — used when a model advertises
# ``response_format`` support. Other providers using the OpenAI-compatible
# audio endpoint also accept this set.
STT_RESPONSE_FORMAT_CHOICES: tuple[tuple[str, str], ...] = (
    ("json", "JSON"),
    ("text", "Text"),
    ("srt", "SRT"),
    ("verbose_json", "Verbose JSON"),
    ("vtt", "VTT"),
)

# ---------------------------------------------------------------------------
# Image fallback tables
#
# Used only when a model carries no typed ``task_options`` parameter schema
# (catalog not refreshed yet, or a provider without a task-capability feed).
# The values mirror the documented OpenRouter unified image parameters and
# the OpenAI gpt-image union.
# ---------------------------------------------------------------------------

FALLBACK_ASPECT_RATIOS: tuple[str, ...] = (
    "1:1",
    "2:3",
    "3:2",
    "3:4",
    "4:3",
    "4:5",
    "5:4",
    "9:16",
    "16:9",
    "21:9",
)

FALLBACK_RESOLUTIONS: tuple[str, ...] = ("512", "1K", "2K", "4K")

# OpenAI ``/v1/images/generations`` fallback choice sets (gpt-image shaped —
# the union's safer default; dall-e models carry explicit override data).
GPT_IMAGE_SIZE_CHOICES: tuple[str, ...] = ("1024x1024", "1024x1536", "1536x1024", "auto")
GPT_IMAGE_QUALITY_CHOICES: tuple[str, ...] = ("auto", "low", "medium", "high")
GPT_IMAGE_BACKGROUND_CHOICES: tuple[str, ...] = ("opaque", "transparent", "auto")
GPT_IMAGE_OUTPUT_FORMAT_CHOICES: tuple[str, ...] = ("png", "jpeg", "webp")
DALL_E_STYLE_CHOICES: tuple[str, ...] = ("vivid", "natural")
OPENAI_IMAGE_RESPONSE_FORMAT_CHOICES: tuple[str, ...] = ("b64_json", "url")

# ---------------------------------------------------------------------------
# Image parameter render hints
#
# The typed parameter schema in ``capabilities.task_options`` carries the
# facts (which parameters, which values/bounds); this table carries only the
# presentation: labels, descriptions, ordering, and per-name control
# overrides. Unknown parameter names render generically from their spec.
# ---------------------------------------------------------------------------

# Presentation order for known image parameters; unknown names follow sorted.
IMAGE_PARAMETER_ORDER: tuple[str, ...] = (
    "aspect_ratio",
    "resolution",
    "size",
    "quality",
    "background",
    "style",
    "output_format",
    "output_compression",
    "moderation",
    "response_format",
    "n",
    "seed",
)

# Parameters that are runtime inputs rather than persisted settings: image
# references arrive with the request, streaming is a transport concern.
IMAGE_PARAMETER_SKIP: frozenset[str] = frozenset({"input_references", "stream"})

# ``size`` is OpenRouter's convenience shorthand for resolution + aspect
# ratio and conflicts with them; it is skipped whenever either is present
# (OpenAI native models publish only ``size``, which stays rendered).
IMAGE_SIZE_SHORTHAND_CONFLICTS: tuple[str, ...] = ("resolution", "aspect_ratio")

IMAGE_PARAMETER_LABELS: Mapping[str, str] = {
    "aspect_ratio": "Aspect ratio",
    "resolution": "Resolution",
    "size": "Size",
    "quality": "Quality",
    "background": "Background",
    "style": "Style",
    "output_format": "Output format",
    "output_compression": "Output compression",
    "response_format": "Response format",
    "n": "Number of images",
    "seed": "Seed",
    "moderation": "Moderation",
}

IMAGE_PARAMETER_DESCRIPTIONS: Mapping[str, str] = {
    "seed": "Reproducible generation seed.",
    "n": "How many images to generate per request.",
    "output_compression": "Compression level (webp/jpeg only).",
    "moderation": "Content-moderation strictness.",
    "response_format": "How the provider returns the image; vBot decodes b64_json.",
}

# Feed parameters typed ``boolean`` mean "supported, value free-form"; these
# names take a number instead of a toggle.
IMAGE_NUMBER_VALUED_PARAMETERS: frozenset[str] = frozenset({"seed"})

# Enum parameters that must not fall back to the provider default because the
# wire layer depends on a specific value.
IMAGE_ENUM_FORCED_DEFAULTS: Mapping[str, str] = {"response_format": "b64_json"}

# Label for the empty enum choice that leaves the parameter unsent so the
# provider's own default applies (the wire layer drops empty placeholders).
PROVIDER_DEFAULT_CHOICE_LABEL = "Provider default"

IMAGE_CHOICE_LABELS: Mapping[str, str] = {
    "auto": "Auto",
    "png": "PNG",
    "jpeg": "JPEG",
    "webp": "WebP",
    "b64_json": "Base64 JSON",
    "url": "URL",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "standard": "Standard",
    "hd": "HD",
    "vivid": "Vivid",
    "natural": "Natural",
    "opaque": "Opaque",
    "transparent": "Transparent",
    "original": "Original",
    "solid": "Solid",
}

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskModelOptionChoice:
    """One choice for a select-style task-model option."""

    value: str
    label: str

    def to_dict(self) -> JsonObject:
        return {"value": self.value, "label": self.label}


@dataclass(frozen=True)
class TaskModelOptionField:
    """One renderable task-model option field."""

    name: str
    type: str
    label: str
    default: Any = None
    required: bool = False
    description: str = ""
    options: tuple[TaskModelOptionChoice, ...] = ()
    min_value: float | None = None
    max_value: float | None = None
    step: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise TaskModelOptionValidationError(
                "TaskModelOptionField.name must be a non-empty string"
            )
        if not isinstance(self.type, str) or self.type not in ALLOWED_OPTION_TYPES:
            allowed = ", ".join(sorted(ALLOWED_OPTION_TYPES))
            raise TaskModelOptionValidationError(
                f"Unsupported option type '{self.type}' for field '{self.name}'. Allowed: {allowed}"
            )

    def to_dict(self) -> JsonObject:
        payload: JsonObject = {
            "name": self.name,
            "type": self.type,
            "label": self.label,
            "default": self.default,
            "required": self.required,
        }
        if self.description:
            payload["description"] = self.description
        if self.options:
            payload["options"] = [option.to_dict() for option in self.options]
        if self.min_value is not None:
            payload["min"] = self.min_value
        if self.max_value is not None:
            payload["max"] = self.max_value
        if self.step is not None:
            payload["step"] = self.step
        return payload


@dataclass(frozen=True)
class TaskModelOptionSchema:
    """Option schema for one task target."""

    task_type: str
    target: str
    fields: tuple[TaskModelOptionField, ...] = ()

    def default_options(self) -> JsonObject:
        """Return defaults for fields that define one."""

        defaults: JsonObject = {}
        for field in self.fields:
            if field.default is not None:
                defaults[field.name] = field.default
        return defaults

    def to_dict(self) -> JsonObject:
        return {
            "task_type": self.task_type,
            "target": self.target,
            "fields": [field.to_dict() for field in self.fields],
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def option_schema_for(
    task_type: str,
    provider_id: str,
    target: str,
    *,
    model: Model | None = None,
) -> TaskModelOptionSchema:
    """Return a model-aware option schema for *task_type* and *provider_id*.

    *model* is the resolved :class:`core.models.Model` for the target when
    available. Without it, the schema falls back to the provider-level
    conservative defaults that the model-aware branches extend. Every
    supported task type additionally carries the ``extra_options`` escape
    hatch (provider targets only — local targets never reach this builder).
    """

    if task_type == TASK_SPEECH_TO_TEXT:
        fields = _speech_to_text_fields(provider_id, model)
    elif task_type == TASK_TEXT_TO_SPEECH:
        fields = _text_to_speech_fields(provider_id, model)
    elif task_type == TASK_IMAGE_GENERATION:
        fields = _image_generation_fields(provider_id, model)
    elif task_type == TASK_TEXT_EMBEDDING:
        fields = _text_embedding_fields(provider_id, model)
    else:
        return TaskModelOptionSchema(task_type=task_type, target=target)
    return TaskModelOptionSchema(
        task_type=task_type,
        target=target,
        fields=(*fields, _extra_options_field()),
    )


def _extra_options_field() -> TaskModelOptionField:
    return TaskModelOptionField(
        name="extra_options",
        type="json",
        label="Extra options",
        default={},
        description=(
            "Additional request fields (JSON object) merged into the provider "
            "request. Escape hatch for options vBot does not surface."
        ),
    )


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------


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
    if model is not None and "response_format" in model.capabilities.supported_parameters:
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


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------


def _image_generation_fields(
    provider_id: str,
    model: Model | None,
) -> tuple[TaskModelOptionField, ...]:
    """Image option fields, driven by the model's typed parameter schema.

    When the model carries ``capabilities.task_options.image_generation``
    (projected at refresh from the OpenRouter image API, or hand-authored in
    the override layer), fields render generically from that data. Without
    it, a conservative provider-level fallback applies. Providers with no
    image execution path get no fields — the UI must not invent inputs.
    """

    image_options = _image_task_options(model)
    parameters = image_options.get("parameters")
    fields: list[TaskModelOptionField] = []
    if isinstance(parameters, Mapping) and parameters:
        fields.extend(_fields_from_image_parameters(parameters))
    elif provider_id == "openrouter":
        fields.extend(_openrouter_image_fallback_fields(model))
    elif provider_id == "openai":
        fields.extend(_openai_image_fallback_fields(model))

    passthrough = image_options.get("passthrough")
    if isinstance(passthrough, Mapping) and passthrough:
        fields.append(_provider_options_field(passthrough))
    return tuple(fields)


def _image_task_options(model: Model | None) -> Mapping[str, Any]:
    if model is None:
        return {}
    image_options = model.capabilities.task_options.get(TASK_IMAGE_GENERATION)
    return image_options if isinstance(image_options, Mapping) else {}


def _fields_from_image_parameters(
    parameters: Mapping[str, Any],
) -> list[TaskModelOptionField]:
    """Render typed parameter specs into option fields.

    ``enum`` → select (with a "Provider default" empty choice unless the wire
    layer forces a value), ``range`` → bounded number (skipped when the range
    collapses to a single value — nothing to configure), ``boolean`` → number
    for free-value names like ``seed``, toggle otherwise, ``string`` → free
    text (hand-authored specs for parameters with open value spaces, e.g.
    gpt-image-2 arbitrary sizes). Unknown spec types are skipped fail-soft;
    the raw catalog still carries them.
    """

    known_order = [name for name in IMAGE_PARAMETER_ORDER if name in parameters]
    remaining = sorted(name for name in parameters if name not in IMAGE_PARAMETER_ORDER)
    fields: list[TaskModelOptionField] = []
    for name in (*known_order, *remaining):
        if name in IMAGE_PARAMETER_SKIP:
            continue
        if name == "size" and any(
            conflict in parameters for conflict in IMAGE_SIZE_SHORTHAND_CONFLICTS
        ):
            continue
        spec = parameters.get(name)
        if not isinstance(spec, Mapping):
            continue
        field = _field_from_image_parameter(name, spec)
        if field is not None:
            fields.append(field)
    return fields


def _field_from_image_parameter(
    name: str,
    spec: Mapping[str, Any],
) -> TaskModelOptionField | None:
    spec_type = spec.get("type")
    if spec_type == "enum":
        return _enum_image_field(name, spec)
    if spec_type == "range":
        return _range_image_field(name, spec)
    if spec_type == "string":
        return TaskModelOptionField(
            name=name,
            type="text",
            label=_image_parameter_label(name),
            default="",
            description=_image_parameter_description(name, spec),
        )
    if spec_type == "boolean":
        if name in IMAGE_NUMBER_VALUED_PARAMETERS:
            return TaskModelOptionField(
                name=name,
                type="number",
                label=_image_parameter_label(name),
                default=None,
                step=1,
                description=_image_parameter_description(name, spec),
            )
        return TaskModelOptionField(
            name=name,
            type="boolean",
            label=_image_parameter_label(name),
            default=None,
            description=_image_parameter_description(name, spec),
        )
    return None


def _enum_image_field(name: str, spec: Mapping[str, Any]) -> TaskModelOptionField | None:
    # Loaded ``task_options`` are frozen (lists become tuples), while
    # fallback specs are built inline as lists — accept both sequence forms.
    raw_values = spec.get("values")
    if not isinstance(raw_values, list | tuple):
        return None
    values = [value for value in raw_values if isinstance(value, str) and value]
    forced_default = IMAGE_ENUM_FORCED_DEFAULTS.get(name)
    if forced_default is not None and forced_default in values:
        choices = tuple(_image_choice(value) for value in values)
        default: str = forced_default
    else:
        if len(values) < 2:
            # A single published value offers no choice; leaving the
            # parameter unsent keeps the provider's fixed value
            # authoritative (mirrors the collapsed-range rule).
            return None
        choices = (
            TaskModelOptionChoice(value="", label=PROVIDER_DEFAULT_CHOICE_LABEL),
            *(_image_choice(value) for value in values),
        )
        default = ""
    return TaskModelOptionField(
        name=name,
        type="select",
        label=_image_parameter_label(name),
        default=default,
        options=choices,
        description=_image_parameter_description(name, spec),
    )


def _range_image_field(name: str, spec: Mapping[str, Any]) -> TaskModelOptionField | None:
    minimum = spec.get("min")
    maximum = spec.get("max")
    if not isinstance(minimum, int | float) or not isinstance(maximum, int | float):
        return None
    if minimum == maximum:
        # A collapsed range offers no choice; leaving the parameter unsent
        # keeps the provider's fixed value authoritative.
        return None
    return TaskModelOptionField(
        name=name,
        type="number",
        label=_image_parameter_label(name),
        default=None,
        min_value=float(minimum),
        max_value=float(maximum),
        step=1,
        description=_image_parameter_description(name, spec),
    )


def _image_parameter_label(name: str) -> str:
    label = IMAGE_PARAMETER_LABELS.get(name)
    if label is not None:
        return label
    return name.replace("_", " ").capitalize()


def _image_parameter_description(name: str, spec: Mapping[str, Any]) -> str:
    """Per-spec description wins over the code hint.

    Hand-authored override specs may carry a model-specific ``description``
    (e.g. gpt-image-2's arbitrary-size constraints) that a generic per-name
    hint cannot express.
    """

    description = spec.get("description")
    if isinstance(description, str) and description:
        return description
    return IMAGE_PARAMETER_DESCRIPTIONS.get(name, "")


def _image_choice(value: str) -> TaskModelOptionChoice:
    return TaskModelOptionChoice(value=value, label=IMAGE_CHOICE_LABELS.get(value, value))


def _provider_options_field(passthrough: Mapping[str, Any]) -> TaskModelOptionField:
    allowed_parts: list[str] = []
    for slug in sorted(str(key) for key in passthrough):
        keys = passthrough.get(slug)
        if isinstance(keys, list | tuple) and keys:
            allowed_parts.append(f"{slug}: {', '.join(str(key) for key in keys)}")
    allowed = "; ".join(allowed_parts)
    description = (
        "Provider-specific options (JSON object) sent as provider.options, keyed by provider slug."
    )
    if allowed:
        description = f"{description} Allowed keys — {allowed}."
    return TaskModelOptionField(
        name="provider_options",
        type="json",
        label="Provider options",
        default={},
        description=description,
    )


def _openrouter_image_fallback_fields(model: Model | None) -> list[TaskModelOptionField]:
    fields = [
        _enum_image_field("aspect_ratio", {"type": "enum", "values": list(FALLBACK_ASPECT_RATIOS)}),
        _enum_image_field("resolution", {"type": "enum", "values": list(FALLBACK_RESOLUTIONS)}),
    ]
    if model is not None and "seed" in model.capabilities.supported_parameters:
        fields.append(_field_from_image_parameter("seed", {"type": "boolean"}))
    return [field for field in fields if field is not None]


def _openai_image_fallback_fields(model: Model | None) -> list[TaskModelOptionField]:
    """OpenAI native image fallback (gpt-image-shaped union).

    Applies only when the model carries no ``task_options`` data — e.g. an
    unrefreshed catalog. Each field is gated by ``supported_parameters`` when
    the model is known; with no model at all the whole union renders so the
    target stays configurable.
    """

    supported: frozenset[str] | None = (
        frozenset(model.capabilities.supported_parameters) if model is not None else None
    )

    def has(field_name: str) -> bool:
        return supported is None or field_name in supported

    union: tuple[tuple[str, dict[str, Any]], ...] = (
        ("size", {"type": "enum", "values": list(GPT_IMAGE_SIZE_CHOICES)}),
        ("quality", {"type": "enum", "values": list(GPT_IMAGE_QUALITY_CHOICES)}),
        ("background", {"type": "enum", "values": list(GPT_IMAGE_BACKGROUND_CHOICES)}),
        ("n", {"type": "range", "min": 1, "max": 10}),
        ("output_format", {"type": "enum", "values": list(GPT_IMAGE_OUTPUT_FORMAT_CHOICES)}),
        ("style", {"type": "enum", "values": list(DALL_E_STYLE_CHOICES)}),
        (
            "response_format",
            {"type": "enum", "values": list(OPENAI_IMAGE_RESPONSE_FORMAT_CHOICES)},
        ),
    )
    fields: list[TaskModelOptionField] = []
    for name, spec in union:
        if not has(name):
            continue
        field = _field_from_image_parameter(name, spec)
        if field is not None:
            fields.append(field)
    return fields


# ---------------------------------------------------------------------------
# Text embedding
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _choice(value: str, label: str) -> TaskModelOptionChoice:
    return TaskModelOptionChoice(value=value, label=label)


def _to_choices(
    pairs: tuple[tuple[str, str], ...],
) -> tuple[TaskModelOptionChoice, ...]:
    return tuple(_choice(value, label) for value, label in pairs)
