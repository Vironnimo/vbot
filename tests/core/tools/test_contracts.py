"""Tests for canonical Tool contract compilation and enforcement."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.tools import (
    InvalidToolResultError,
    ToolContext,
    ToolContractError,
    ToolRegistry,
    compile_tool_contract,
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


def test_compile_rejects_open_fixed_object() -> None:
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
    with pytest.raises(ToolContractError, match="Tool name must start with a letter"):
        compile_tool_contract(name=name, input_schema=_input_schema())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({}, "arguments: 'count' is a required property [required]"),
        ({"count": "1"}, "arguments/count: '1' is not of type 'integer' [type]"),
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

    with pytest.raises(InvalidToolResultError, match="Tool result violates its contract"):
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
