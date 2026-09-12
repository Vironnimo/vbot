"""Option types."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.models import Model

JsonObject = dict[str, Any]

ALLOWED_OPTION_TYPES: frozenset[str] = frozenset(
    {"text", "textarea", "select", "number", "boolean", "json"}
)

EMBEDDING_RESERVED_PAYLOAD_FIELDS: frozenset[str] = frozenset(
    {"model", "input", "encoding_format", "dimensions", "input_type"}
)


class TaskModelOptionValidationError(ValueError):
    """Raised when a task-model option field is malformed."""


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

STT_RESPONSE_FORMAT_CHOICES: tuple[tuple[str, str], ...] = (
    ("json", "JSON"),
    ("text", "Text"),
    ("srt", "SRT"),
    ("verbose_json", "Verbose JSON"),
    ("vtt", "VTT"),
)

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

GPT_IMAGE_SIZE_CHOICES: tuple[str, ...] = ("1024x1024", "1024x1536", "1536x1024", "auto")

GPT_IMAGE_QUALITY_CHOICES: tuple[str, ...] = ("auto", "low", "medium", "high")

GPT_IMAGE_BACKGROUND_CHOICES: tuple[str, ...] = ("opaque", "transparent", "auto")

GPT_IMAGE_OUTPUT_FORMAT_CHOICES: tuple[str, ...] = ("png", "jpeg", "webp")

DALL_E_STYLE_CHOICES: tuple[str, ...] = ("vivid", "natural")

OPENAI_IMAGE_RESPONSE_FORMAT_CHOICES: tuple[str, ...] = ("b64_json", "url")

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

IMAGE_PARAMETER_SKIP: frozenset[str] = frozenset({"input_references", "stream"})

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

IMAGE_NUMBER_VALUED_PARAMETERS: frozenset[str] = frozenset({"seed"})

IMAGE_ENUM_FORCED_DEFAULTS: Mapping[str, str] = {"response_format": "b64_json"}

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


def _task_options(model: Model | None, task_type: str) -> Mapping[str, Any]:
    if model is None:
        return {}
    options = model.capabilities.task_options.get(task_type)
    return options if isinstance(options, Mapping) else {}


def _string_values(spec: Any) -> tuple[str, ...]:
    if not isinstance(spec, Mapping):
        return ()
    values = spec.get("values")
    if not isinstance(values, list | tuple):
        return ()
    return tuple(value for value in values if isinstance(value, str) and value)


def _parameter_is_supported(spec: Any) -> bool:
    return isinstance(spec, Mapping) and spec.get("type") == "boolean"


def _choice(value: str, label: str) -> TaskModelOptionChoice:
    return TaskModelOptionChoice(value=value, label=label)


def _to_choices(
    pairs: tuple[tuple[str, str], ...],
) -> tuple[TaskModelOptionChoice, ...]:
    return tuple(_choice(value, label) for value, label in pairs)
