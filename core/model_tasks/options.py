"""Backend-owned option schemas for specialized task models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.model_tasks.constants import (
    TASK_IMAGE_GENERATION,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_TO_SPEECH,
)

JsonObject = dict[str, Any]


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


def option_schema_for(task_type: str, provider_id: str, target: str) -> TaskModelOptionSchema:
    """Return a conservative option schema for *task_type* and provider."""

    if task_type == TASK_SPEECH_TO_TEXT:
        return TaskModelOptionSchema(
            task_type=task_type,
            target=target,
            fields=_speech_to_text_fields(provider_id),
        )
    if task_type == TASK_TEXT_TO_SPEECH:
        return TaskModelOptionSchema(
            task_type=task_type,
            target=target,
            fields=_text_to_speech_fields(provider_id),
        )
    if task_type == TASK_IMAGE_GENERATION:
        return TaskModelOptionSchema(
            task_type=task_type,
            target=target,
            fields=_image_generation_fields(provider_id),
        )
    return TaskModelOptionSchema(task_type=task_type, target=target)


def _speech_to_text_fields(provider_id: str) -> tuple[TaskModelOptionField, ...]:
    language_default = "auto"
    fields = [
        TaskModelOptionField(
            name="language",
            type="text",
            label="Language",
            default=language_default,
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
    return tuple(fields)


def _text_to_speech_fields(provider_id: str) -> tuple[TaskModelOptionField, ...]:
    format_choices: tuple[TaskModelOptionChoice, ...] = (
        _choice("mp3", "MP3"),
        _choice("pcm", "PCM"),
    )
    if provider_id == "openai":
        format_choices = (
            _choice("mp3", "MP3"),
            _choice("opus", "Opus"),
            _choice("aac", "AAC"),
            _choice("flac", "FLAC"),
            _choice("wav", "WAV"),
            _choice("pcm", "PCM"),
        )

    return (
        TaskModelOptionField(
            name="voice",
            type="select",
            label="Voice",
            default="alloy",
            required=True,
            options=(
                _choice("alloy", "Alloy"),
                _choice("ash", "Ash"),
                _choice("ballad", "Ballad"),
                _choice("coral", "Coral"),
                _choice("echo", "Echo"),
                _choice("fable", "Fable"),
                _choice("nova", "Nova"),
                _choice("onyx", "Onyx"),
                _choice("sage", "Sage"),
                _choice("shimmer", "Shimmer"),
                _choice("verse", "Verse"),
            ),
        ),
        TaskModelOptionField(
            name="response_format",
            type="select",
            label="Format",
            default="mp3",
            options=format_choices,
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
        TaskModelOptionField(
            name="instructions",
            type="textarea",
            label="Instructions",
            default="",
            description="Optional speaking style instructions when the selected model supports it.",
        ),
    )


def _image_generation_fields(provider_id: str) -> tuple[TaskModelOptionField, ...]:
    if provider_id != "openrouter":
        return ()

    return (
        TaskModelOptionField(
            name="aspect_ratio",
            type="select",
            label="Aspect ratio",
            default="1:1",
            options=(
                _choice("1:1", "1:1 (1024×1024)"),
                _choice("2:3", "2:3 (832×1248)"),
                _choice("3:2", "3:2 (1248×832)"),
                _choice("3:4", "3:4 (864×1184)"),
                _choice("4:3", "4:3 (1184×864)"),
                _choice("4:5", "4:5 (896×1152)"),
                _choice("5:4", "5:4 (1152×896)"),
                _choice("9:16", "9:16 (768×1344)"),
                _choice("16:9", "16:9 (1344×768)"),
                _choice("21:9", "21:9 (1536×672)"),
            ),
        ),
        TaskModelOptionField(
            name="image_size",
            type="select",
            label="Image size",
            default="1K",
            options=(
                _choice("1K", "1K (standard)"),
                _choice("2K", "2K"),
                _choice("4K", "4K"),
            ),
        ),
    )


def _choice(value: str, label: str) -> TaskModelOptionChoice:
    return TaskModelOptionChoice(value=value, label=label)
