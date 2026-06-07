"""Backend-owned option schemas for specialized task models.

Schemas are model-aware: ``option_schema_for`` accepts the resolved
:class:`core.models.Model` (when available) and adapts fields to what
that model advertises — TTS voices come from ``supported_voices``,
``seed`` appears only for image models that list it in
``supported_parameters``, ``response_format`` appears for STT models
that list it, and Recraft/Sourceful family-specific image options are
added by family. The image render hints that the provider API does not
expose (Recraft/Sourceful field lists, aspect-ratio/image-size
exceptions) are authored here, consistent with the convention that
options are backend-owned render hints.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.model_tasks.constants import (
    TASK_IMAGE_GENERATION,
    TASK_SPEECH_TO_TEXT,
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


class TaskModelOptionValidationError(ValueError):
    """Raised when a task-model option field is malformed."""


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

# Base image aspect ratios. Models with restricted or extended ratios
# override the choices via :func:`_aspect_ratio_choices_for_model`.
BASE_ASPECT_RATIOS: tuple[tuple[str, str], ...] = (
    ("1:1", "1:1 (1024×1024)"),
    ("2:3", "2:3 (832×1248)"),
    ("3:2", "3:2 (1248×832)"),
    ("3:4", "3:4 (864×1184)"),
    ("4:3", "4:3 (1184×864)"),
    ("4:5", "4:5 (896×1152)"),
    ("5:4", "5:4 (1152×896)"),
    ("9:16", "9:16 (768×1344)"),
    ("16:9", "16:9 (1344×768)"),
    ("21:9", "21:9 (1536×672)"),
)

# microsoft/mai-image-2.5 advertises a reduced aspect-ratio set.
MAI_IMAGE_2_5_ASPECT_RATIOS: tuple[tuple[str, str], ...] = (
    ("1:1", "1:1 (1024×1024)"),
    ("4:3", "4:3 (1184×864)"),
    ("3:4", "3:4 (864×1184)"),
    ("16:9", "16:9 (1344×768)"),
    ("9:16", "9:16 (768×1344)"),
    ("3:2", "3:2 (1248×832)"),
    ("2:3", "2:3 (832×1248)"),
)

# google/gemini-3.1-flash-image-preview extends the base set with very
# narrow/wide ratios that the other image models do not accept.
GEMINI_3_1_FLASH_IMAGE_EXTRA_ASPECT_RATIOS: tuple[tuple[str, str], ...] = (
    ("1:4", "1:4"),
    ("4:1", "4:1"),
    ("1:8", "1:8"),
    ("8:1", "8:1"),
)

# google/gemini-3.1-flash-image-preview is the only model that advertises
# 0.5K as an image_size choice.
HALF_K_IMAGE_SIZE = ("0.5K", "0.5K")

BASE_IMAGE_SIZES: tuple[tuple[str, str], ...] = (
    ("1K", "1K (standard)"),
    ("2K", "2K"),
    ("4K", "4K"),
)

# Model IDs that get profile-specific overrides.
MAI_IMAGE_2_5_MODEL_ID = "microsoft/mai-image-2.5"
GEMINI_3_1_FLASH_IMAGE_MODEL_ID = "google/gemini-3.1-flash-image-preview"

# ---------------------------------------------------------------------------
# OpenAI image option profiles
#
# OpenAI's ``/v1/images/generations`` endpoint accepts a different set of
# fields than OpenRouter's chat/completions path. The fields are the union
# of what ``gpt-image-1`` and ``dall-e-3`` accept; each model's
# ``supported_parameters`` decides which subset is actually exposed.
# ---------------------------------------------------------------------------

# gpt-image-1 size choices (square + portrait + landscape + auto).
GPT_IMAGE_SIZE_CHOICES: tuple[tuple[str, str], ...] = (
    ("1024x1024", "1024×1024 (square)"),
    ("1024x1536", "1024×1536 (portrait)"),
    ("1536x1024", "1536×1024 (landscape)"),
    ("auto", "Auto"),
)

# dall-e-3 size choices.
DALL_E_3_SIZE_CHOICES: tuple[tuple[str, str], ...] = (
    ("1024x1024", "1024×1024 (square)"),
    ("1792x1024", "1792×1024 (landscape)"),
    ("1024x1792", "1024×1792 (portrait)"),
)

# gpt-image-1 quality choices.
GPT_IMAGE_QUALITY_CHOICES: tuple[tuple[str, str], ...] = (
    ("auto", "Auto"),
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
)

# dall-e-3 quality choices.
DALL_E_3_QUALITY_CHOICES: tuple[tuple[str, str], ...] = (
    ("standard", "Standard"),
    ("hd", "HD"),
)

# gpt-image-1 background choices.
GPT_IMAGE_BACKGROUND_CHOICES: tuple[tuple[str, str], ...] = (
    ("opaque", "Opaque"),
    ("transparent", "Transparent"),
    ("auto", "Auto"),
)

# gpt-image-1 output_format choices.
GPT_IMAGE_OUTPUT_FORMAT_CHOICES: tuple[tuple[str, str], ...] = (
    ("png", "PNG"),
    ("jpeg", "JPEG"),
    ("webp", "WebP"),
)

# dall-e-3 style choices.
DALL_E_3_STYLE_CHOICES: tuple[tuple[str, str], ...] = (
    ("vivid", "Vivid"),
    ("natural", "Natural"),
)

# dall-e-3 response_format choices.
OPENAI_IMAGE_RESPONSE_FORMAT_CHOICES: tuple[tuple[str, str], ...] = (
    ("b64_json", "Base64 JSON"),
    ("url", "URL"),
)

# Model-id prefixes that mark a model as an OpenAI gpt-image or dall-e-3
# variant — used to disambiguate option shapes within the OpenAI branch.
GPT_IMAGE_MODEL_PREFIX = "gpt-image-"
DALL_E_3_MODEL_PREFIX = "dall-e-"

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
    conservative defaults that the model-aware branches extend.
    """

    if task_type == TASK_SPEECH_TO_TEXT:
        return TaskModelOptionSchema(
            task_type=task_type,
            target=target,
            fields=_speech_to_text_fields(provider_id, model),
        )
    if task_type == TASK_TEXT_TO_SPEECH:
        return TaskModelOptionSchema(
            task_type=task_type,
            target=target,
            fields=_text_to_speech_fields(provider_id, model),
        )
    if task_type == TASK_IMAGE_GENERATION:
        return TaskModelOptionSchema(
            task_type=task_type,
            target=target,
            fields=_image_generation_fields(provider_id, model),
        )
    return TaskModelOptionSchema(task_type=task_type, target=target)


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
    # advertises support for it. The override file (Phase 5) flags it in
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
    if provider_id == "openai":
        return _openai_image_fields(model)
    if provider_id != "openrouter":
        # Non-OpenAI, non-OpenAI-native providers have no image option
        # profile authored here. Execution domains that know how to talk
        # to those providers map their own fields; we keep the schema
        # empty so the UI does not invent unsupported inputs.
        return ()

    fields: list[TaskModelOptionField] = [
        _aspect_ratio_field(model),
        _image_size_field(model),
    ]

    if model is not None and "seed" in model.capabilities.supported_parameters:
        fields.append(_seed_field())

    if model is not None:
        if model.model_id.startswith("recraft/"):
            fields.extend(_recraft_fields(model.model_id))
        elif model.model_id.startswith("sourceful/"):
            fields.extend(_sourceful_fields(model.model_id))

    return tuple(fields)


def _aspect_ratio_choices_for_model(model: Model | None) -> tuple[tuple[str, str], ...]:
    if model is not None and model.model_id == MAI_IMAGE_2_5_MODEL_ID:
        return MAI_IMAGE_2_5_ASPECT_RATIOS
    if model is not None and model.model_id == GEMINI_3_1_FLASH_IMAGE_MODEL_ID:
        return BASE_ASPECT_RATIOS + GEMINI_3_1_FLASH_IMAGE_EXTRA_ASPECT_RATIOS
    return BASE_ASPECT_RATIOS


def _image_size_choices_for_model(model: Model | None) -> tuple[tuple[str, str], ...]:
    if model is not None and model.model_id == GEMINI_3_1_FLASH_IMAGE_MODEL_ID:
        return (HALF_K_IMAGE_SIZE,) + BASE_IMAGE_SIZES
    return BASE_IMAGE_SIZES


def _aspect_ratio_field(model: Model | None) -> TaskModelOptionField:
    return TaskModelOptionField(
        name="aspect_ratio",
        type="select",
        label="Aspect ratio",
        default="1:1",
        options=_to_choices(_aspect_ratio_choices_for_model(model)),
    )


def _image_size_field(model: Model | None) -> TaskModelOptionField:
    return TaskModelOptionField(
        name="image_size",
        type="select",
        label="Image size",
        default="1K",
        options=_to_choices(_image_size_choices_for_model(model)),
    )


def _seed_field() -> TaskModelOptionField:
    return TaskModelOptionField(
        name="seed",
        type="number",
        label="Seed",
        default=None,
        step=1,
        description="Reproducible generation seed. Provider-specific support.",
    )


# ---------------------------------------------------------------------------
# OpenAI image profile
# ---------------------------------------------------------------------------


def _openai_image_fields(model: Model | None) -> tuple[TaskModelOptionField, ...]:
    """OpenAI native image options.

    The OpenAI ``/v1/images/generations`` endpoint accepts a union of
    fields across ``gpt-image-1`` (size / quality / background / n /
    output_format) and ``dall-e-3`` (size / quality / style / n /
    response_format). Each model's ``supported_parameters`` decides which
    fields are exposed in the Settings UI.

    If *model* is ``None`` (catalog not refreshed yet) every field that
    belongs to the union is exposed so the user can still configure the
    target. The wire layer is responsible for sending only the fields the
    selected model accepts.
    """

    supported: frozenset[str] | None = (
        frozenset(model.capabilities.supported_parameters) if model is not None else None
    )
    is_gpt_image = model is not None and model.model_id.startswith(GPT_IMAGE_MODEL_PREFIX)
    is_dall_e = model is not None and model.model_id.startswith(DALL_E_3_MODEL_PREFIX)

    def has(field_name: str) -> bool:
        return supported is None or field_name in supported

    fields: list[TaskModelOptionField] = []
    if has("size"):
        fields.append(_openai_size_field(is_gpt_image, is_dall_e))
    if has("quality"):
        fields.append(_openai_quality_field(is_gpt_image, is_dall_e))
    if has("background"):
        fields.append(_openai_background_field())
    if has("n"):
        fields.append(_openai_n_field(is_dall_e))
    if has("output_format"):
        fields.append(_openai_output_format_field())
    if has("style"):
        fields.append(_openai_style_field())
    if has("response_format"):
        fields.append(_openai_response_format_field())
    return tuple(fields)


def _openai_size_field(is_gpt_image: bool, is_dall_e: bool) -> TaskModelOptionField:
    # When both flags are True the model is ambiguous — gpt-image-1's set
    # is the safer default because gpt-image-1 is the newer model and its
    # size set covers ``auto``. The model=None case also lands here.
    choices = DALL_E_3_SIZE_CHOICES if is_dall_e and not is_gpt_image else GPT_IMAGE_SIZE_CHOICES
    return TaskModelOptionField(
        name="size",
        type="select",
        label="Size",
        default="1024x1024",
        options=_to_choices(choices),
        description="Image dimensions; provider-specific choices.",
    )


def _openai_quality_field(is_gpt_image: bool, is_dall_e: bool) -> TaskModelOptionField:
    if is_dall_e and not is_gpt_image:
        choices = DALL_E_3_QUALITY_CHOICES
        default = "standard"
    else:
        choices = GPT_IMAGE_QUALITY_CHOICES
        default = "auto"
    return TaskModelOptionField(
        name="quality",
        type="select",
        label="Quality",
        default=default,
        options=_to_choices(choices),
        description="Render quality; provider-specific choices.",
    )


def _openai_background_field() -> TaskModelOptionField:
    return TaskModelOptionField(
        name="background",
        type="select",
        label="Background",
        default="opaque",
        options=_to_choices(GPT_IMAGE_BACKGROUND_CHOICES),
        description="Background transparency (gpt-image-1).",
    )


def _openai_n_field(is_dall_e: bool) -> TaskModelOptionField:
    # dall-e-3 only supports n=1 per OpenAI docs.
    max_value = 1.0 if is_dall_e else 10.0
    return TaskModelOptionField(
        name="n",
        type="number",
        label="Number of images",
        default=1,
        min_value=1,
        max_value=max_value,
        step=1,
        description="How many images to generate. dall-e-3 supports n=1 only.",
    )


def _openai_output_format_field() -> TaskModelOptionField:
    return TaskModelOptionField(
        name="output_format",
        type="select",
        label="Output format",
        default="png",
        options=_to_choices(GPT_IMAGE_OUTPUT_FORMAT_CHOICES),
        description="Image file format (gpt-image-1).",
    )


def _openai_style_field() -> TaskModelOptionField:
    return TaskModelOptionField(
        name="style",
        type="select",
        label="Style",
        default="vivid",
        options=_to_choices(DALL_E_3_STYLE_CHOICES),
        description="Visual style (dall-e-3).",
    )


def _openai_response_format_field() -> TaskModelOptionField:
    return TaskModelOptionField(
        name="response_format",
        type="select",
        label="Response format",
        default="b64_json",
        options=_to_choices(OPENAI_IMAGE_RESPONSE_FORMAT_CHOICES),
        description="How OpenAI returns the generated image (dall-e-3).",
    )


# ---------------------------------------------------------------------------
# Recraft family
# ---------------------------------------------------------------------------


def _recraft_fields(model_id: str) -> tuple[TaskModelOptionField, ...]:
    """Recraft family image options, gated by model variant.

    v3, v4, v4-pro, and conservatively v4.1 share the strength / rgb_colors
    / background_rgb_color fields. v3 also exposes ``text_layout`` and
    ``style`` (vector styles are explicitly unsupported there per the
    provider docs).
    """

    is_v3 = "recraft-v3" in model_id
    is_v4_family = "recraft-v4" in model_id

    fields: list[TaskModelOptionField] = []
    if is_v3 or is_v4_family:
        fields.append(
            TaskModelOptionField(
                name="strength",
                type="number",
                label="Strength",
                default=0.2,
                min_value=0.0,
                max_value=1.0,
                step=0.05,
                description="Image-to-image strength (Recraft).",
            )
        )
        fields.append(
            TaskModelOptionField(
                name="rgb_colors",
                type="json",
                label="RGB colors",
                default=[],
                description="Array of [r,g,b] (0-255). Example: [[0,128,255],[255,0,0]].",
            )
        )
        fields.append(
            TaskModelOptionField(
                name="background_rgb_color",
                type="json",
                label="Background RGB color",
                default=None,
                description="[r,g,b] (0-255). Example: [255,255,255].",
            )
        )
    if is_v3:
        fields.append(
            TaskModelOptionField(
                name="text_layout",
                type="json",
                label="Text layout",
                default=[],
                description='Array of {"text": str, "bbox": [[x,y]×4] (0-1)}. Recraft-v3 only.',
            )
        )
        fields.append(
            TaskModelOptionField(
                name="style",
                type="text",
                label="Style",
                default="",
                description="Recraft-v3 style id. Vector styles are unsupported.",
            )
        )
    return tuple(fields)


# ---------------------------------------------------------------------------
# Sourceful family
# ---------------------------------------------------------------------------


def _sourceful_fields(model_id: str) -> tuple[TaskModelOptionField, ...]:
    """Sourceful Riverflow family image options, gated by model variant.

    v2.5 must be detected before v2 because ``v2.5`` contains ``v2`` as
    a substring. v2 exposes ``font_inputs`` and ``super_resolution_references``
    (img2img-only); v2.5 exposes ``font_inputs``, ``scoring_prompt``,
    ``scoring_rubric``, and the background controls.
    """

    is_v2_5 = "riverflow-v2.5" in model_id
    is_v2 = "riverflow-v2" in model_id and not is_v2_5

    fields: list[TaskModelOptionField] = []
    if is_v2 or is_v2_5:
        fields.append(_sourceful_font_inputs())
    if is_v2:
        fields.append(_sourceful_super_resolution_references())
    if is_v2_5:
        fields.append(_sourceful_scoring_prompt())
        fields.append(_sourceful_scoring_rubric())
        fields.append(_sourceful_background_mode())
        fields.append(_sourceful_background_hex_color())
    return tuple(fields)


def _sourceful_font_inputs() -> TaskModelOptionField:
    return TaskModelOptionField(
        name="font_inputs",
        type="json",
        label="Font inputs",
        default=[],
        description='Array of {"font_url": str, "text": str}, max 2. Sourceful v2 / v2.5.',
    )


def _sourceful_super_resolution_references() -> TaskModelOptionField:
    return TaskModelOptionField(
        name="super_resolution_references",
        type="json",
        label="Super-resolution references",
        default=[],
        description="Array of image URL strings, max 4. Sourceful v2 (img2img only).",
    )


def _sourceful_scoring_prompt() -> TaskModelOptionField:
    return TaskModelOptionField(
        name="scoring_prompt",
        type="textarea",
        label="Scoring prompt",
        default="",
        description="Scoring prompt for the rubric. Sourceful v2.5 only.",
    )


def _sourceful_scoring_rubric() -> TaskModelOptionField:
    return TaskModelOptionField(
        name="scoring_rubric",
        type="json",
        label="Scoring rubric",
        default=[],
        description=(
            "Array of rubric entries (1-8). Each entry has key, label, "
            "description, weight, optional passing_score and score_guidance. "
            "Sourceful v2.5 only."
        ),
    )


def _sourceful_background_mode() -> TaskModelOptionField:
    return TaskModelOptionField(
        name="background_mode",
        type="select",
        label="Background mode",
        default="original",
        options=_to_choices(
            (
                ("original", "Original"),
                ("transparent", "Transparent"),
                ("solid", "Solid"),
            )
        ),
        description="Sourceful v2.5 only.",
    )


def _sourceful_background_hex_color() -> TaskModelOptionField:
    return TaskModelOptionField(
        name="background_hex_color",
        type="text",
        label="Background hex color",
        default="",
        description="#RRGGBB. Required when background_mode is 'solid'. Sourceful v2.5 only.",
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
