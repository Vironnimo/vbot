"""Tests for live voice option schemas: the voice and who answers the voice model."""

from __future__ import annotations

from typing import Any

import pytest

from core.model_tasks.constants import TASK_LIVE_VOICE
from core.model_tasks.options import (
    TaskModelOptionField,
    TaskModelOptionSchema,
    TaskModelOptionValidationError,
    live_backend_choices,
    option_schema_for,
    validate_task_model_options,
)
from core.models import Model
from tests.core.model_tasks.model_tasks_test_support import (
    LIVE_VOICE_TASK_OPTIONS,
    _registry_model,
)


def _live_model(parameters: dict[str, Any] | None = None) -> Model:
    return _registry_model(
        "live-custom",
        "Live Custom",
        task_types=(TASK_LIVE_VOICE,),
        connections=("subscription",),
        task_options=(
            {TASK_LIVE_VOICE: {"parameters": parameters}}
            if parameters is not None
            else LIVE_VOICE_TASK_OPTIONS
        ),
    )


def _schema(parameters: dict[str, Any] | None = None) -> TaskModelOptionSchema:
    return option_schema_for(
        TASK_LIVE_VOICE,
        "openai",
        "openai/live-custom::subscription",
        model=_live_model(parameters),
    )


def _fields(schema: TaskModelOptionSchema) -> dict[str, TaskModelOptionField]:
    return {field.name: field for field in schema.fields}


def test_live_voice_schema_offers_voices_backends_and_openai_backend_models() -> None:
    schema = _schema()

    voice, backend, hosted = schema.fields
    assert (voice.name, voice.type, voice.required, voice.default) == (
        "voice",
        "select",
        True,
        "juniper",
    )
    assert [(choice.value, choice.label) for choice in voice.options] == [
        ("cove", "Cove"),
        ("juniper", "Juniper"),
        ("maple", "Maple"),
    ]
    assert (backend.name, backend.required, backend.default) == ("backend", True, "vbot")
    assert [choice.value for choice in backend.options] == ["vbot", "openai"]
    # OpenAI's backend Model matters only when OpenAI answers the voice model.
    assert hosted.name == "openai_backend_model"
    assert [choice.value for choice in hosted.options] == ["luna", "sol"]
    assert hosted.to_dict()["options_by"] == {"field": "backend", "values": {"vbot": []}}
    assert schema.default_options() == {
        "voice": "juniper",
        "backend": "vbot",
        "openai_backend_model": "luna",
    }


@pytest.mark.parametrize(
    ("backend", "choices"),
    [
        ({"type": "enum", "values": ["vbot", "openai"], "default": "openai"}, ("openai", "vbot")),
        ({"type": "enum", "values": ["none", "vbot"]}, ("none", "vbot")),
        ({"type": "enum", "values": ["vbot", "elsewhere"], "default": "elsewhere"}, ("vbot",)),
        ({"type": "enum", "values": ["elsewhere"]}, ()),
        (None, ()),
    ],
    ids=["declared-default-first", "first-is-default", "unknown-dropped", "none-known", "absent"],
)
def test_backend_choices_put_the_default_first_and_keep_only_known_backends(
    backend: dict[str, Any] | None, choices: tuple[str, ...]
) -> None:
    parameters: dict[str, Any] = {"voice": {"type": "enum", "values": ["cove"]}}
    if backend is not None:
        parameters["backend"] = backend

    assert live_backend_choices(_live_model(parameters)) == choices
    assert live_backend_choices(None) == ()


def test_an_xai_like_model_offers_no_openai_backend_model() -> None:
    schema = _schema(
        {
            "voice": {"type": "enum", "values": ["eve"]},
            "backend": {"type": "enum", "values": ["none", "vbot"], "default": "none"},
            "openai_backend_model": {"type": "enum", "values": ["luna"]},
        }
    )

    assert list(_fields(schema)) == ["voice", "backend"]
    assert schema.default_options() == {"voice": "eve", "backend": "none"}


def test_undeclared_defaults_fall_back_to_the_first_choice() -> None:
    schema = _schema(
        {
            "voice": {"type": "enum", "values": ["maple", "cove"], "default": "missing"},
            "backend": {"type": "enum", "values": ["openai", "vbot"], "default": "missing"},
            "openai_backend_model": {"type": "enum", "values": ["sol", "luna"], "default": "x"},
        }
    )

    assert schema.default_options() == {
        "voice": "maple",
        "backend": "openai",
        "openai_backend_model": "sol",
    }


def test_live_voice_fields_are_omitted_without_matching_facts() -> None:
    schema = _schema({"backend": {"type": "boolean"}, "voice": {"type": "boolean"}})

    assert schema.fields == ()
    assert option_schema_for(TASK_LIVE_VOICE, "openai", "openai/unknown::api-key").fields == ()
    assert [
        field.name for field in _schema({"voice": {"type": "enum", "values": ["cove"]}}).fields
    ] == ["voice"]


def test_validation_uses_the_same_choices() -> None:
    schema = _schema()

    validate_task_model_options(schema, {})
    validate_task_model_options(
        schema, {"voice": "maple", "backend": "openai", "openai_backend_model": "sol"}
    )
    rejected_options: tuple[dict[str, Any], ...] = (
        {"backend": "none"},
        {"backend": ""},
        {"openai_backend_model": "terra"},
        {"voice": "marin"},
        {"extra_options": {}},
    )
    for rejected in rejected_options:
        with pytest.raises(TaskModelOptionValidationError):
            validate_task_model_options(schema, rejected)
