"""Tests for the backend-owned task-model option schemas."""

from __future__ import annotations

import pytest

from core.model_tasks.constants import (
    TASK_IMAGE_GENERATION,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_TO_SPEECH,
)
from core.model_tasks.options import (
    ALLOWED_OPTION_TYPES,
    TaskModelOptionField,
    TaskModelOptionValidationError,
    option_schema_for,
)


def test_allowed_option_types_includes_json() -> None:
    """The new ``json`` field type is part of the supported set so the
    Settings UI can render generic array/object options like Recraft's
    ``text_layout`` or Sourceful's ``scoring_rubric``."""

    assert "json" in ALLOWED_OPTION_TYPES
    # Existing renderable types remain in the whitelist.
    for known in ("text", "textarea", "select", "number", "boolean"):
        assert known in ALLOWED_OPTION_TYPES


def test_task_model_option_field_accepts_json_type() -> None:
    """A field declared as ``json`` is constructed and serialized as ``json``.
    The default value is passed through untouched so the frontend receives
    the raw array/object the provider expects."""

    field = TaskModelOptionField(
        name="text_layout",
        type="json",
        label="Text layout",
        default=[{"text": "hi", "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]]}],
        description="Array of {text, bbox} entries (recraft-v3).",
    )

    assert field.type == "json"
    payload = field.to_dict()
    assert payload["type"] == "json"
    assert payload["name"] == "text_layout"
    # Default is preserved as-is — backend does not transform JSON values.
    assert payload["default"] == [{"text": "hi", "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]]}]
    assert payload["description"].startswith("Array of ")


def test_task_model_option_field_rejects_unknown_type() -> None:
    """Unknown field types are rejected up front so the renderer never sees
    them as silent fallbacks. ``json`` is allowed; ``totally-unknown`` is not."""

    with pytest.raises(TaskModelOptionValidationError, match="totally-unknown"):
        TaskModelOptionField(
            name="x",
            type="totally-unknown",
            label="X",
        )

    # Sanity: the constructor's error names the actual offending type.
    with pytest.raises(TaskModelOptionValidationError, match="json-list"):
        TaskModelOptionField(
            name="x",
            type="json-list",  # plausible typo
            label="X",
        )


def test_task_model_option_field_rejects_empty_name() -> None:
    """An empty name would render as an unkeyed option in the binding — reject it."""

    with pytest.raises(TaskModelOptionValidationError, match="name"):
        TaskModelOptionField(name="", type="json", label="X")


def test_existing_field_types_still_validate() -> None:
    """Sanity: the four pre-existing field types still construct without
    error, and each reports the correct ``type`` in its serialized form."""

    cases = [
        ("text", "language"),
        ("textarea", "instructions"),
        ("number", "temperature"),
        ("boolean", "enabled"),
    ]
    for field_type, name in cases:
        field = TaskModelOptionField(name=name, type=field_type, label=name.title())
        assert field.type == field_type
        assert field.to_dict()["type"] == field_type


def test_option_schema_for_unchanged_for_existing_tasks() -> None:
    """The existing task branches (TTS, STT, image) still produce the
    pre-M2 schemas — adding the ``json`` type does not silently alter the
    recognized task types."""

    tts_schema = option_schema_for(TASK_TEXT_TO_SPEECH, "openai", "openai/tts-1::api-key")
    assert [field.name for field in tts_schema.fields[:1]] == ["voice"]

    stt_schema = option_schema_for(TASK_SPEECH_TO_TEXT, "openai", "openai/whisper-1::api-key")
    assert "language" in [field.name for field in stt_schema.fields]

    image_schema = option_schema_for(
        TASK_IMAGE_GENERATION, "openrouter", "openrouter/flux.2-pro::api-key"
    )
    assert "aspect_ratio" in [field.name for field in image_schema.fields]


def test_task_model_option_field_choices_serialize_with_json_default() -> None:
    """A ``select`` field with a JSON-serializable default serializes
    correctly. This guards the to_dict path used by the renderer."""

    field = TaskModelOptionField(
        name="voice",
        type="select",
        label="Voice",
        default="alloy",
    )

    payload = field.to_dict()
    assert payload["type"] == "select"
    assert payload["default"] == "alloy"


def test_task_model_option_field_json_default_passthrough_complex_types() -> None:
    """JSON fields can carry nested arrays, objects, and primitives. The
    backend must not transform the value — the frontend stringifies it for
    display and parses it back on input."""

    items: list[dict[str, object]] = [
        {"key": "clarity", "weight": 0.6, "passing_score": 0.5},
        {"key": "style", "weight": 0.4},
    ]
    nested: dict[str, object] = {"items": items, "background": None}
    field = TaskModelOptionField(
        name="scoring_rubric",
        type="json",
        label="Scoring rubric",
        default=nested,
    )

    payload = field.to_dict()
    assert payload["default"] is nested
    nested_default = payload["default"]
    assert nested_default is not None
    assert isinstance(nested_default, dict)
    nested_items = nested_default["items"]
    assert isinstance(nested_items, list)
    first_item = nested_items[0]
    assert isinstance(first_item, dict)
    assert first_item["passing_score"] == 0.5
