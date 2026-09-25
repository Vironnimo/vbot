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
    compile_tool_contract,
    tool_success,
)
from core.tools._argument_repair import normalize_call_arguments

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


def test_model_facing_object_compiles_open_but_its_properties_are_the_parameter_list() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={"type": "object", "properties": {"value": {"type": "string"}}},
        require_closed_input=False,
    )

    contract.validate_arguments({"value": "ok"})
    with pytest.raises(ToolContractError) as exc_info:
        contract.validate_arguments({"value": "ok", "extra": True})

    assert str(exc_info.value) == (
        'sample was not run:\n- "extra" is not a parameter.\nsample parameters: value.'
    )


@pytest.mark.parametrize(
    "open_keywords",
    [{"additionalProperties": True}, {"patternProperties": {"^x_": {"type": "string"}}}],
)
def test_explicitly_open_root_accepts_and_keeps_unknown_arguments(
    open_keywords: JsonObject,
) -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            **open_keywords,
        },
        require_closed_input=False,
    )
    arguments = {"value": "ok", "x_extra": "kept", "x_empty": ""}

    normalized = contract.normalize_arguments(arguments)

    assert normalized == arguments
    contract.validate_arguments(normalized)


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
        (
            {},
            'sample was not run:\n- "count" is required.\n'
            "sample parameters: count (required), label.",
        ),
        (
            {"count": "1.5"},
            'sample was not run: "count" must be an integer; received "1.5".',
        ),
        (
            {"count": 1, "extra": True},
            'sample was not run:\n- "extra" is not a parameter.\n'
            "sample parameters: count (required), label.",
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
async def test_unadvertised_parameters_are_accepted_and_validated_but_never_offered() -> None:
    received: list[JsonObject] = []

    def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        received.append(arguments)
        return tool_success({"value": "ok"})

    registry = ToolRegistry()
    registry.register(
        "sample",
        "Sample.",
        {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]},
        handler,
        open_input_schema=True,
        unadvertised_parameters={"legacy": {"type": "string", "enum": ["raw"]}},
    )

    await registry.dispatch(_context("sample"), {"count": 1, "legacy": "raw"})
    with pytest.raises(ToolContractError) as exc_info:
        await registry.dispatch(_context("sample"), {"legacy": "other"})

    assert received == [{"count": 1, "legacy": "raw"}]
    assert "legacy" not in registry.provider_definitions()[0]["parameters"]["properties"]
    assert str(exc_info.value) == (
        "sample was not run:\n"
        '- "count" is required.\n'
        '- "legacy" must be one of "raw"; received "other".\n'
        "sample parameters: count (required)."
    )


def test_argument_error_names_every_problem_with_a_suggestion_and_the_parameters() -> None:
    contract = compile_tool_contract(
        name="read",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (relative to the working "
                    "directory, or absolute). Line ranges use offset and limit.",
                },
                "offset": {"type": "integer"},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
        },
        require_closed_input=False,
    )

    with pytest.raises(ToolContractError) as exc_info:
        contract.validate_arguments({"file_path": "notes.txt", "limit": 0})

    assert str(exc_info.value) == (
        "read was not run:\n"
        '- "file_path" is not a parameter. Did you mean "path"?\n'
        '- "limit" must be at least 1; received 0.\n'
        '- "path" is required: Path to the file to read (relative to the working '
        "directory, or absolute).\n"
        "read parameters: path (required), offset, limit."
    )


@pytest.mark.parametrize(
    ("unknown", "hint"),
    [
        ("limti", ' Did you mean "limit"?'),
        ("counts", ' Did you mean "count"?'),
        ("max_count", ' Did you mean "count"?'),
        ("country", ""),
    ],
)
def test_unknown_parameter_hint_suggests_only_a_likely_misspelling(unknown: str, hint: str) -> None:
    contract = compile_tool_contract(
        name="web_search",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "count": {}, "limit": {}},
        },
        require_closed_input=False,
    )

    with pytest.raises(ToolContractError) as exc_info:
        contract.validate_arguments({"query": "news", unknown: "x"})

    assert str(exc_info.value).splitlines()[1] == f'- "{unknown}" is not a parameter.{hint}'


def test_nested_argument_problems_name_the_field_by_its_path() -> None:
    contract = compile_tool_contract(
        name="edit",
        input_schema={
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"old_text": {"type": "string", "minLength": 1}},
                        "required": ["old_text"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["edits"],
            "additionalProperties": False,
        },
    )

    with pytest.raises(ToolContractError) as exc_info:
        contract.validate_arguments({"edits": [{"old_text": ""}, {"old_text": "a", "new": "b"}]})

    assert str(exc_info.value) == (
        "edit was not run:\n"
        '- "edits[0].old_text" must not be empty.\n'
        '- "edits[1].new" is not a known field of "edits[1]".'
    )


def test_normalization_drops_empty_unknown_arguments_but_keeps_meaningful_ones() -> None:
    contract = compile_tool_contract(name="sample", input_schema=_input_schema())

    normalized = contract.normalize_arguments(
        {
            "count": 1,
            "label": "",
            "none": None,
            "blank": "",
            "items": [],
            "mapping": {},
            "zero": 0,
            "flag": False,
        }
    )

    assert normalized == {"count": 1, "label": "", "zero": 0, "flag": False}
    with pytest.raises(ToolContractError) as exc_info:
        contract.validate_arguments(normalized)
    message = str(exc_info.value)
    assert '"zero" is not a parameter' in message
    assert '"flag" is not a parameter' in message
    assert '"label" must not be empty' in message


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


def test_normalization_omits_exact_empty_optional_string_properties() -> None:
    schema: JsonObject = {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "optional": {"type": "string", "minLength": 1},
            "items": {"type": "array", "items": {"type": "string", "minLength": 1}},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    contract = compile_tool_contract(name="sample", input_schema=schema)

    normalized = normalize_call_arguments(
        contract, {"action": "input", "optional": "", "items": [""]}, empty_as_omitted=("optional",)
    )

    assert normalized == {"action": "input", "items": [""]}
    with pytest.raises(ToolContractError, match=r'"items\[0\]" must not be empty'):
        contract.validate_arguments(normalized)


def test_normalization_keeps_exact_empty_required_string_properties() -> None:
    schema: JsonObject = {
        "type": "object",
        "properties": {
            "value": {"type": "string", "minLength": 1},
            "optional": {"type": "string", "minLength": 1},
        },
        "required": ["value"],
        "additionalProperties": False,
    }
    contract = compile_tool_contract(name="sample", input_schema=schema)

    normalized = contract.normalize_arguments({"value": "", "optional": ""})

    assert normalized == {"value": "", "optional": ""}
    with pytest.raises(ToolContractError, match='"value" must not be empty'):
        contract.validate_arguments(normalized)


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
        ({"count": "0"}, '"count" must be at least 1; received 0.'),
        ({"count": "3.5"}, '"count" must be an integer; received "3.5".'),
        ({"count": "abc"}, '"count" must be an integer; received "abc".'),
        ({"count": "1e1000"}, '"count" must be an integer; received "1e1000".'),
        ({"count": "2", "extra": True}, '"extra" is not a parameter.'),
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


@pytest.mark.parametrize("value", ("maybe", "2", ""))
def test_normalization_rejects_ambiguous_boolean_values(value: str) -> None:
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
        input_schema={
            "type": "object",
            "description": "Choose an action.",
            "oneOf": [
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add"]},
                        "content": {"type": "string"},
                    },
                    "required": ["action", "content"],
                    "additionalProperties": False,
                },
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["remove"]},
                        "entry_id": {"type": "integer"},
                    },
                    "required": ["action", "entry_id"],
                    "additionalProperties": False,
                },
            ],
        },
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

    assert str(exc_info.value) == (
        'sample was not run: "include_links" must be a boolean; received "false"; '
        "omit this optional field to use its default true."
    )


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
    assert message == 'sample was not run: "enabled" must be a boolean; received "true".'
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


def test_identical_inputs_reuse_one_contract_until_their_content_changes() -> None:
    # Arrange
    schema = _input_schema()
    first = compile_tool_contract(name="sample", input_schema=schema)

    # Act
    again = compile_tool_contract(name="sample", input_schema=_input_schema())
    open_variant = compile_tool_contract(
        name="sample", input_schema=_input_schema(), require_closed_input=False
    )
    schema["properties"]["count"]["minimum"] = 5
    changed = compile_tool_contract(name="sample", input_schema=schema)

    # Assert: equal content shares one contract; any input change compiles afresh.
    assert again is first
    assert open_variant is not first
    assert changed is not first
    assert first.input_schema["properties"]["count"]["minimum"] == 1
    first.validate_arguments({"count": 1})
    with pytest.raises(ToolContractError, match='"count" must be at least 5; received 1'):
        changed.validate_arguments({"count": 1})


def test_a_tuple_schema_stays_rejected_after_its_json_twin_compiled() -> None:
    compile_tool_contract(name="sample", input_schema=_input_schema())

    with pytest.raises(ToolContractError, match="is not of type 'array'"):
        compile_tool_contract(
            name="sample", input_schema={**_input_schema(), "required": ("count",)}
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("YES", True), (1, True), ("1", True), ("off", False), (0, False), ("No", False)],
)
def test_boolean_aliases_preserve_intent(value: object, expected: bool) -> None:
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
    contract.validate_arguments(normalized)
    assert normalized["enabled"] is expected


@pytest.mark.parametrize(
    "arguments",
    [
        {"request": {"operation": " WRITE-FILE ", "filePath": "notes.txt", "content": ""}},
        {"write_file": {"file_pth": "notes.txt", "content": ""}},
        {"action": "WRITE-FILE", "file_path": "notes.txt", "content": ""},
    ],
)
def test_open_contract_repairs_spelling_and_wrappers_preserving_empty_payload(
    arguments: dict[str, Any],
) -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["write_file", "delete"]},
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["action"],
        },
        require_closed_input=False,
    )
    assert normalize_call_arguments(
        contract, arguments, enum_fields=("action",), field_aliases={"file_pth": "file_path"}
    ) == {
        "action": "write_file",
        "file_path": "notes.txt",
        "content": "",
    }


def test_repairs_optional_typo_even_when_open_schema_already_valid() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={"type": "object", "properties": {"include_links": {"type": "boolean"}}},
        require_closed_input=False,
    )
    assert normalize_call_arguments(contract, {"includeLinkS": "false"}) == {"include_links": False}


def test_duplicate_conflicting_target_is_not_silently_overwritten() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={"type": "object", "properties": {"file_path": {"type": "string"}}},
        require_closed_input=False,
    )
    with pytest.raises(ToolContractError):
        normalize_call_arguments(contract, {"file_path": "one.txt", "filePath": "two.txt"})
    assert normalize_call_arguments(contract, {"file_path": "one.txt", "filePath": "one.txt"}) == {
        "file_path": "one.txt"
    }


def test_arbitrary_payload_keys_and_whitespace_are_preserved() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={
            "type": "object",
            "properties": {"payload": {"type": "object"}, "text": {"type": "string"}},
        },
        require_closed_input=False,
    )
    arguments = {
        "payload": {"request": {"operation": "DELETE"}, "CamelCase": " YES "},
        "text": "  ",
    }
    assert contract.normalize_arguments(arguments) == arguments


def test_integral_json_float_reaches_integer_handler_as_int() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={"type": "object", "properties": {"count": {"type": "integer"}}},
        require_closed_input=False,
    )
    assert type(contract.normalize_arguments({"count": 3.0})["count"]) is int


def test_large_integer_text_representation_does_not_overflow_float() -> None:
    contract = compile_tool_contract(
        name="write",
        input_schema={
            "type": "object",
            "properties": {"content": {"type": "string"}},
            "required": ["content"],
        },
        require_closed_input=False,
    )
    assert contract.normalize_arguments({"content": 10**309}) == {"content": str(10**309)}


@pytest.mark.parametrize("identifier", ["tg-team-b", "TG-TEAM-A", "tg_team_a", " tg-team-a "])
def test_generic_repair_never_selects_a_similar_enum_identifier(identifier: str) -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={
            "type": "object",
            "properties": {"target": {"type": "string", "enum": ["tg-team-a"]}},
        },
        require_closed_input=False,
    )
    original = {"target": identifier}
    assert contract.normalize_arguments(original) == original
    with pytest.raises(ToolContractError):
        contract.validate_arguments(original)


@pytest.mark.parametrize("additional", [True, {"type": "string"}])
def test_valid_open_application_fields_and_wrappers_remain_literal(additional: Any) -> None:
    payload = {"color": "red", "colors": "blue", "COLOR": "green", "operation": "replace"}
    contract = compile_tool_contract(
        name="sample",
        input_schema={
            "type": "object",
            "properties": {
                "payload": {
                    "type": "object",
                    "properties": {"color": {"type": "string"}},
                    "additionalProperties": additional,
                },
                "request": {"type": "object"},
            },
        },
        require_closed_input=False,
    )
    original = {"payload": payload, "request": {"action": "delete"}}
    contract.validate_arguments(original)
    assert contract.normalize_arguments(original) == original
    assert normalize_call_arguments(contract, original) == original


def test_generic_repair_keeps_explicit_empty_values_for_the_owner() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "minLength": 1},
                "limit": {"type": "integer"},
                "enabled": {"type": "boolean"},
            },
        },
        require_closed_input=False,
    )
    original = {"target": None, "limit": "", "enabled": None}
    assert contract.normalize_arguments(original) == original
    with pytest.raises(ToolContractError):
        contract.validate_arguments(original)


def test_owner_alias_conflicts_are_checked_before_omission() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={"type": "object", "properties": {"file_path": {"type": "string"}}},
        require_closed_input=False,
    )
    with pytest.raises(ToolContractError):
        normalize_call_arguments(
            contract, {"file_path": "one", "filePath": None}, empty_as_omitted=("file_path",)
        )


@pytest.mark.parametrize(
    "branches",
    [
        [{"type": "boolean"}, {"type": "integer"}],
        [{"type": "integer"}, {"type": "boolean"}],
    ],
)
def test_ambiguous_union_repair_does_not_depend_on_branch_order(branches: list[Any]) -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={"type": "object", "properties": {"value": {"oneOf": branches}}},
        require_closed_input=False,
    )
    with pytest.raises(ToolContractError):
        contract.normalize_arguments({"value": "1"})


def test_encoded_duplicate_fields_cannot_silently_choose_a_target() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={"type": "object", "properties": {"target": {"type": "string"}}},
        require_closed_input=False,
    )
    original = '{"target":"one","target":"two"}'
    with pytest.raises(ToolContractError):
        contract.normalize_arguments(original)
    with pytest.raises(ToolContractError):
        normalize_call_arguments(contract, {"request": original})
