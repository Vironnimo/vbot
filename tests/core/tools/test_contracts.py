"""Tests for canonical Tool contract compilation and enforcement."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.tools import (
    InvalidToolResultError,
    ToolContext,
    ToolContractError,
    ToolRegistry,
    action_schema,
    compile_tool_contract,
    discriminated_union_schema,
    tool_success,
)

JsonObject = dict[str, Any]


def _context(name: str) -> ToolContext:
    return ToolContext(
        agent_id="agent",
        session_id="session",
        run_id="run",
        tool_call_id="call",
        tool_name=name,
        tool_call_index=0,
        workspace=Path("workspace"),
        vbot_root=Path("app"),
        data_root=Path("data"),
    )


def _input_schema() -> JsonObject:
    return {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "minimum": 1},
            "label": {"type": "string", "minLength": 1},
        },
        "required": ["count"],
        "additionalProperties": False,
    }


def test_action_schema_builds_closed_required_branches_without_field_leakage() -> None:
    schema = action_schema(
        {
            "add": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                },
                "required": ["content"],
            },
            "remove": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "integer"},
                },
                "required": ["entry_id"],
            },
        },
        description="Choose an action.",
    )

    contract = compile_tool_contract(name="sample", input_schema=schema)
    branches = {branch["properties"]["action"]["enum"][0]: branch for branch in schema["oneOf"]}

    assert set(branches["add"]["properties"]) == {"action", "content"}
    assert branches["add"]["required"] == ["action", "content"]
    assert set(branches["remove"]["properties"]) == {"action", "entry_id"}
    assert branches["remove"]["required"] == ["action", "entry_id"]
    assert all(branch["additionalProperties"] is False for branch in branches.values())
    contract.validate_arguments({"action": "add", "content": "fact"})
    contract.validate_arguments({"action": "remove", "entry_id": 1})
    with pytest.raises(ToolContractError, match="entry_id"):
        contract.validate_arguments({"action": "add", "content": "fact", "entry_id": 1})


def test_discriminated_union_schema_supports_non_action_discriminators() -> None:
    schema = discriminated_union_schema(
        "mode",
        {
            "foreground": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            "auto": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "yield_after": {"type": "number"},
                },
                "required": ["command"],
            },
        },
        description="Choose an execution mode.",
        discriminator_description="Execution mode.",
    )

    contract = compile_tool_contract(name="sample", input_schema=schema)
    branches = {branch["properties"]["mode"]["enum"][0]: branch for branch in schema["oneOf"]}

    assert set(branches["foreground"]["properties"]) == {"mode", "command"}
    assert branches["foreground"]["required"] == ["mode", "command"]
    assert set(branches["auto"]["properties"]) == {"mode", "command", "yield_after"}
    assert branches["auto"]["required"] == ["mode", "command"]
    contract.validate_arguments({"mode": "foreground", "command": "echo ok"})
    contract.validate_arguments({"mode": "auto", "command": "echo ok", "yield_after": 30})
    with pytest.raises(ToolContractError, match="yield_after"):
        contract.validate_arguments({"mode": "foreground", "command": "echo ok", "yield_after": 30})


def test_compile_accepts_explicitly_open_model_facing_object() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        require_closed_input=False,
    )

    contract.validate_arguments({"value": "ok", "extra": True})


def test_compile_rejects_open_object_by_default() -> None:
    with pytest.raises(ToolContractError, match="additionalProperties"):
        compile_tool_contract(
            name="sample",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        )


def test_compile_rejects_external_reference() -> None:
    with pytest.raises(ToolContractError, match=r"external \$ref"):
        compile_tool_contract(
            name="sample",
            input_schema={
                "type": "object",
                "properties": {"value": {"$ref": "https://example.test/schema.json"}},
                "additionalProperties": False,
            },
        )


@pytest.mark.parametrize("name", ("1tool", "tool-name", "tool name", "a" * 65))
def test_compile_rejects_nonportable_tool_names(name: str) -> None:
    with pytest.raises(ToolContractError):
        compile_tool_contract(name=name, input_schema=_input_schema())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "arguments: 'count' is a required property [required]"),
        (
            {"count": "1.5"},
            'arguments/count: expected JSON integer, received JSON string "1.5" [type]',
        ),
        (
            {"count": 1, "extra": True},
            "arguments: Additional properties are not allowed ('extra' was unexpected) "
            "[additionalProperties]",
        ),
    ],
)
async def test_dispatch_rejects_invalid_arguments_before_handler(
    arguments: JsonObject,
    message: str,
) -> None:
    calls = 0

    def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        nonlocal calls
        calls += 1
        return tool_success({"value": "ok"})

    registry = ToolRegistry()
    registry.register("sample", "Sample.", _input_schema(), handler)

    with pytest.raises(ToolContractError) as exc_info:
        await registry.dispatch(_context("sample"), arguments, ["sample"])

    assert str(exc_info.value) == message
    assert calls == 0


@pytest.mark.asyncio
async def test_dispatch_normalizes_unambiguous_model_encodings_without_mutating_call() -> None:
    received: JsonObject | None = None

    def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        nonlocal received
        received = arguments
        return tool_success({"value": "ok"})

    schema: JsonObject = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "minimum": 1},
            "ratio": {"type": "number"},
            "enabled": {"type": "boolean"},
            "items": {"type": "array", "items": {"type": "integer"}},
            "config": {
                "type": "object",
                "properties": {"retries": {"type": "integer"}},
                "required": ["retries"],
                "additionalProperties": False,
            },
            "optional": {"type": ["object", "null"]},
        },
        "required": ["count", "ratio", "enabled", "items", "config", "optional"],
        "additionalProperties": False,
    }
    registry = ToolRegistry()
    registry.register("sample", "Sample.", schema, handler)
    original = {
        "count": "72",
        "ratio": "0.7",
        "enabled": " TRUE ",
        "items": '["1", "2"]',
        "config": '{"retries":"3"}',
        "optional": "null",
    }

    result = await registry.dispatch(_context("sample"), original, ["sample"])

    assert result == tool_success({"value": "ok"})
    assert received == {
        "count": 72,
        "ratio": 0.7,
        "enabled": True,
        "items": [1, 2],
        "config": {"retries": 3},
        "optional": None,
    }
    assert original == {
        "count": "72",
        "ratio": "0.7",
        "enabled": " TRUE ",
        "items": '["1", "2"]',
        "config": '{"retries":"3"}',
        "optional": "null",
    }


@pytest.mark.asyncio
async def test_dispatch_wraps_one_array_item_and_uses_active_input_contract() -> None:
    received: JsonObject | None = None

    def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        nonlocal received
        received = arguments
        return tool_success({"value": "ok"})

    registry = ToolRegistry()
    registry.register(
        "sample",
        "Sample.",
        {
            "type": "object",
            "properties": {"items": {"type": "string"}},
            "required": ["items"],
            "additionalProperties": False,
        },
        handler,
    )
    active_contract = compile_tool_contract(
        name="sample",
        input_schema={
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "string"}}},
            "required": ["items"],
            "additionalProperties": False,
        },
    )
    context = replace(_context("sample"), input_contract=active_contract)

    await registry.dispatch(context, {"items": "one"}, ["sample"])

    assert received == {"items": ["one"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"count": "0"}, "0 is less than the minimum of 1 [minimum]"),
        (
            {"count": "3.5"},
            'expected JSON integer, received JSON string "3.5" [type]',
        ),
        (
            {"count": "abc"},
            'expected JSON integer, received JSON string "abc" [type]',
        ),
        (
            {"count": "1e1000"},
            'expected JSON integer, received JSON string "1e1000" [type]',
        ),
        (
            {"count": "2", "extra": True},
            "Additional properties are not allowed ('extra' was unexpected)",
        ),
    ],
)
async def test_dispatch_keeps_semantic_and_shape_validation_after_normalization(
    arguments: JsonObject,
    message: str,
) -> None:
    calls = 0

    def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        nonlocal calls
        calls += 1
        return tool_success({"value": "ok"})

    registry = ToolRegistry()
    registry.register("sample", "Sample.", _input_schema(), handler)

    with pytest.raises(ToolContractError) as exc_info:
        await registry.dispatch(_context("sample"), arguments, ["sample"])

    assert message in str(exc_info.value)
    assert calls == 0


def test_normalization_preserves_a_string_when_the_schema_accepts_it() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": ["integer", "string"]}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )

    assert contract.normalize_arguments({"value": "72"}) == {"value": "72"}


@pytest.mark.parametrize("value", ("yes", "1", ""))
def test_normalization_does_not_invent_boolean_aliases(value: str) -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
            "additionalProperties": False,
        },
    )

    normalized = contract.normalize_arguments({"enabled": value})

    assert normalized == {"enabled": value}
    with pytest.raises(ToolContractError):
        contract.validate_arguments(normalized)


def test_normalization_selects_the_matching_discriminated_union_branch() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema=action_schema(
            {
                "add": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                },
                "remove": {
                    "type": "object",
                    "properties": {"entry_id": {"type": "integer"}},
                    "required": ["entry_id"],
                },
            },
            description="Choose an action.",
        ),
    )

    normalized = contract.normalize_arguments({"action": "remove", "entry_id": "72"})

    assert normalized == {"action": "remove", "entry_id": 72}
    contract.validate_arguments(normalized)


def test_type_error_explains_optional_default_without_coercion() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={
            "type": "object",
            "properties": {
                "include_links": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
    )

    with pytest.raises(ToolContractError) as exc_info:
        contract.validate_arguments({"include_links": "false"})

    assert "arguments/include_links" in str(exc_info.value)
    assert "boolean" in str(exc_info.value)


def test_type_error_does_not_recommend_omitting_a_required_default() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean", "default": True},
            },
            "required": ["enabled"],
            "additionalProperties": False,
        },
    )

    with pytest.raises(ToolContractError) as exc_info:
        contract.validate_arguments({"enabled": "true"})

    message = str(exc_info.value)
    assert 'expected JSON boolean, received JSON string "true"' in message
    assert "omit" not in message


@pytest.mark.asyncio
async def test_dispatch_validates_success_data_after_handler() -> None:
    registry = ToolRegistry()
    registry.register(
        "sample",
        "Sample.",
        _input_schema(),
        lambda _context, _arguments: tool_success({"wrong": True}),
        result_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )

    with pytest.raises(InvalidToolResultError):
        await registry.dispatch(_context("sample"), {"count": 1}, ["sample"])


def test_fingerprint_is_deterministic_and_covers_result_and_scheduling_contracts() -> None:
    base = compile_tool_contract(
        name="sample",
        input_schema=_input_schema(),
        result_schema={"type": "object", "required": ["value"]},
    )
    reordered = compile_tool_contract(
        name="sample",
        input_schema={
            "additionalProperties": False,
            "required": ["count"],
            "properties": {
                "label": {"minLength": 1, "type": "string"},
                "count": {"minimum": 1, "type": "integer"},
            },
            "type": "object",
        },
        result_schema={"required": ["value"], "type": "object"},
    )
    changed_result = compile_tool_contract(
        name="sample",
        input_schema=_input_schema(),
        result_schema={"type": "object", "required": ["other"]},
    )
    serial = compile_tool_contract(
        name="sample",
        input_schema=_input_schema(),
        result_schema={"type": "object", "required": ["value"]},
        parallel_safe=False,
    )

    assert base.schema_fingerprint == reordered.schema_fingerprint
    assert changed_result.schema_fingerprint != base.schema_fingerprint
    assert serial.schema_fingerprint != base.schema_fingerprint
