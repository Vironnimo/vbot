"""Tests for live voice option schemas and backend Model candidates."""

from __future__ import annotations

from typing import Any

import pytest

from core.model_tasks.constants import TASK_LIVE_VOICE
from core.model_tasks.options import (
    TaskModelOptionSchema,
    TaskModelOptionValidationError,
    live_backend_candidates,
    option_schema_for,
    validate_task_model_options,
)
from core.models import ModelQuery, ModelRegistry
from tests.core.model_tasks.model_tasks_test_support import (
    _live_voice_registry,
    _registry_model,
)


def _live_schema(
    model_id: str,
    connection_id: str,
    *,
    registry: ModelRegistry | None = None,
    use_registry: bool = True,
) -> TaskModelOptionSchema:
    registry = registry or _live_voice_registry()
    return option_schema_for(
        TASK_LIVE_VOICE,
        "openai",
        f"openai/{model_id}::{connection_id}",
        model=registry.get("openai", model_id),
        models=registry if use_registry else None,
        connection_id=connection_id,
    )


def _registry_with_live_facts(parameters: dict[str, Any]) -> ModelRegistry:
    base = _live_voice_registry()
    entries = {
        (provider_id, model.model_id): model for provider_id, model in base.query(ModelQuery())
    }
    entries[("openai", "live-custom")] = _registry_model(
        "live-custom",
        "Live Custom",
        task_types=(TASK_LIVE_VOICE,),
        connections=("subscription",),
        task_options={TASK_LIVE_VOICE: {"parameters": parameters}},
    )
    return ModelRegistry(entries)


def test_live_voice_schema_offers_declared_voices_and_backend_models() -> None:
    schema = _live_schema("live-sub", "subscription")

    assert [field.name for field in schema.fields] == ["voice", "backend_model"]
    voice, backend = schema.fields
    assert voice.type == "select"
    assert voice.required is True
    assert voice.default == "juniper"
    assert [(choice.value, choice.label) for choice in voice.options] == [
        ("cove", "Cove"),
        ("juniper", "Juniper"),
        ("maple", "Maple"),
    ]
    assert backend.type == "select"
    assert backend.required is True
    assert backend.default == "terra"
    assert [(choice.value, choice.label) for choice in backend.options] == [
        ("astra", "Astra"),
        ("terra", "Terra"),
    ]
    assert schema.default_options() == {"voice": "juniper", "backend_model": "terra"}


def test_backend_choices_follow_the_target_connection() -> None:
    schema = _live_schema("live-key", "api-key")

    backend = {field.name: field for field in schema.fields}["backend_model"]
    assert [choice.value for choice in backend.options] == ["platform", "terra"]


def test_live_backend_candidates_are_tool_capable_chat_models_on_the_connection() -> None:
    registry = _live_voice_registry()

    subscription = live_backend_candidates(registry, "openai", "subscription")
    api_key = live_backend_candidates(registry, "openai", "api-key")

    assert [model.model_id for model in subscription] == ["astra", "terra"]
    assert [model.model_id for model in api_key] == ["platform", "terra"]
    assert live_backend_candidates(registry, "openrouter", "api-key")[0].model_id == "foreign"
    assert live_backend_candidates(registry, "anthropic", "api-key") == ()


def test_live_backend_candidates_sort_by_name() -> None:
    registry = ModelRegistry(
        {
            ("openai", "a-id"): _registry_model("a-id", "zeta", task_types=("chat",), tools=True),
            ("openai", "z-id"): _registry_model("z-id", "Alpha", task_types=("chat",), tools=True),
        }
    )

    candidates = live_backend_candidates(registry, "openai", "api-key")

    assert [model.name for model in candidates] == ["Alpha", "zeta"]


def test_undeclared_or_unavailable_defaults_fall_back_to_the_first_choice() -> None:
    registry = _registry_with_live_facts(
        {
            "voice": {"type": "enum", "values": ["maple", "cove"], "default": "missing"},
            "backend_model": {"type": "model", "default": "platform"},
        }
    )

    schema = _live_schema("live-custom", "subscription", registry=registry)

    voice, backend = schema.fields
    assert voice.default == "maple"
    # ``platform`` is not allowed on the subscription Connection.
    assert backend.default == "astra"


def test_live_voice_fields_are_omitted_without_matching_facts() -> None:
    registry = _registry_with_live_facts(
        {"backend_model": {"type": "enum", "values": ["terra"]}, "voice": {"type": "boolean"}}
    )

    assert _live_schema("live-custom", "subscription", registry=registry).fields == ()
    assert option_schema_for(TASK_LIVE_VOICE, "openai", "openai/unknown::api-key").fields == ()


def test_voice_only_facts_yield_no_backend_model_field() -> None:
    registry = _registry_with_live_facts({"voice": {"type": "enum", "values": ["cove"]}})

    schema = _live_schema("live-custom", "subscription", registry=registry)

    assert [field.name for field in schema.fields] == ["voice"]


def test_validation_uses_the_same_backend_choices() -> None:
    schema = _live_schema("live-sub", "subscription")

    validate_task_model_options(schema, {})
    validate_task_model_options(schema, {"voice": "maple", "backend_model": "astra"})
    rejected_options: tuple[dict[str, Any], ...] = (
        {"backend_model": "platform"},
        {"backend_model": "quiet"},
        {"backend_model": "foreign"},
        {"voice": "marin"},
        {"extra_options": {}},
    )
    for rejected in rejected_options:
        with pytest.raises(TaskModelOptionValidationError):
            validate_task_model_options(schema, rejected)


def test_backend_model_without_registry_offers_no_valid_choice() -> None:
    schema = _live_schema("live-sub", "subscription", use_registry=False)

    backend = {field.name: field for field in schema.fields}["backend_model"]
    assert backend.options == ()
    assert backend.default is None
    for options in ({}, {"backend_model": "terra"}):
        with pytest.raises(TaskModelOptionValidationError):
            validate_task_model_options(schema, options)
