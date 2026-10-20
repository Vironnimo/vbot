"""Tools: canonical contract compilation, argument errors and argument repair."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

import pytest

from core.tools import (
    ToolContext,
    ToolContractError,
    ToolRegistry,
    compile_tool_contract,
    tool_failure,
    tool_success,
)
from core.tools.call_syntax import normalize_call_arguments
from tests.core.tools.tools_test_support import JsonObject, make_context


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


_IGNORED = '"transition" had no effect: sample has no such parameter.'


def _open_contract(properties: JsonObject, **schema: Any) -> Any:
    """A model-facing contract: root ``properties`` without ``additionalProperties``."""
    return compile_tool_contract(
        name="sample",
        input_schema={"type": "object", "properties": properties, **schema},
        require_closed_input=False,
    )


# --- Compilation ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "input_schema", "message"),
    [
        pytest.param(
            "sample",
            {"type": "object", "properties": {"value": {"type": "string"}}},
            "must set additionalProperties to false",
            id="open-object",
        ),
        pytest.param(
            "sample",
            {
                "type": "object",
                "properties": {"value": {"$ref": "https://example.test/schema.json"}},
                "additionalProperties": False,
            },
            r"external \$ref",
            id="external-reference",
        ),
        *(
            pytest.param(name, _input_schema(), "Tool name must start with a letter", id=name)
            for name in ("1tool", "tool-name", "tool name", "a" * 65)
        ),
    ],
)
def test_compile_rejects_an_unportable_contract(
    name: str, input_schema: JsonObject, message: str
) -> None:
    with pytest.raises(ToolContractError, match=message):
        compile_tool_contract(name=name, input_schema=input_schema)


def test_model_facing_object_compiles_open_but_its_properties_are_the_parameter_list() -> None:
    contract = _open_contract({"value": {"type": "string"}})

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
    contract = _open_contract({"value": {"type": "string"}}, **open_keywords)
    arguments = {"value": "ok", "x_extra": "kept", "x_empty": ""}

    normalized = contract.normalize_arguments(arguments)

    assert normalized == arguments
    contract.validate_arguments(normalized)


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
    schema = _input_schema()
    first = compile_tool_contract(name="sample", input_schema=schema)

    again = compile_tool_contract(name="sample", input_schema=_input_schema())
    open_variant = compile_tool_contract(
        name="sample", input_schema=_input_schema(), require_closed_input=False
    )
    schema["properties"]["count"]["minimum"] = 5
    changed = compile_tool_contract(name="sample", input_schema=schema)

    # Equal content shares one contract; any input change compiles afresh.
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


# --- Argument errors --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"label": "x"},
            'sample was not run:\n- "count" is required.\n'
            "sample parameters: count (required), label.",
        ),
        ({"count": "1.5"}, 'sample was not run: "count" must be an integer; received "1.5".'),
        ({"count": "abc"}, 'sample was not run: "count" must be an integer; received "abc".'),
        (
            {"count": "1e1000"},
            'sample was not run: "count" must be an integer; received "1e1000".',
        ),
        ({"count": "0"}, 'sample was not run: "count" must be at least 1; received 0.'),
        # Unknown fields the call cannot run without: a likely misspelled parameter, a
        # mode or safety switch, a target or a content of its own.
        (
            {"count": "2", "labels": "x"},
            'sample was not run:\n- "labels" is not a parameter. Did you mean "label"?\n'
            "sample parameters: count (required), label.",
        ),
        (
            {"count": "2", "dryRun": True},
            'sample was not run:\n- "dryRun" is not a parameter.\n'
            "sample parameters: count (required), label.",
        ),
        (
            {"count": "2", "thread_id": "42", "message": "Hi"},
            'sample was not run:\n- "thread_id" is not a parameter.\n'
            '- "message" is not a parameter.\nsample parameters: count (required), label.',
        ),
        *[
            ({"count": "2", field: "important extra content"}, {field})
            for field in ("subject", "title", "caption", "description", "heading", "summary")
        ],
        # Content or call fields under any name: text with a line break, an object.
        (
            {"count": "2", "retext": "## Correction\nThe parser stays."},
            'sample was not run:\n- "retext" is not a parameter.\n'
            "sample parameters: count (required), label.",
        ),
        (
            {"count": "2", "parameters": {"label": "x"}},
            'sample was not run:\n- "parameters" is not a parameter.\n'
            "sample parameters: count (required), label.",
        ),
        # A refused call names its other unknown fields too; commentary requests nothing.
        (
            {"label": "x", "mood": "calm", "commentary": "analysis"},
            'sample was not run:\n- "mood" is not a parameter.\n- "count" is required.\n'
            "sample parameters: count (required), label.",
        ),
    ],
)
async def test_dispatch_rejects_invalid_arguments_before_handler(
    arguments: JsonObject,
    message: str | set[str],
) -> None:
    calls = 0

    def handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        nonlocal calls
        calls += 1
        return tool_success({"value": "ok"})

    registry = ToolRegistry()
    registry.register("sample", "Sample.", _input_schema(), handler)

    with pytest.raises(ToolContractError) as exc_info:
        await registry.dispatch(make_context("sample"), arguments, ["sample"])

    if isinstance(message, set):
        assert all(f'"{field}"' in str(exc_info.value) for field in message)
    else:
        assert str(exc_info.value) == message
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(
            tool_success({"value": "ok"}),
            tool_success({"value": "ok", "note": _IGNORED}),
            id="success",
        ),
        pytest.param(
            tool_success({"value": "ok", "note": "Read as a list."}),
            tool_success({"value": "ok", "note": f"{_IGNORED} Read as a list."}),
            id="success-with-a-note",
        ),
        pytest.param(
            tool_failure("not_found", "No such item."),
            tool_failure("not_found", f"No such item.\n{_IGNORED}"),
            id="failure",
        ),
        pytest.param(
            ToolContractError("sample was not run: the label is taken."),
            ToolContractError(f"sample was not run: the label is taken.\n{_IGNORED}"),
            id="refusal",
        ),
    ],
)
async def test_an_unknown_field_without_effect_is_left_out_and_the_result_says_so(
    outcome: Any, expected: Any
) -> None:
    received: list[JsonObject] = []

    def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        received.append(arguments)
        if isinstance(outcome, Exception):
            raise outcome
        return dict(outcome)

    registry = ToolRegistry()
    # A closed result schema still admits the note.
    result_schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "additionalProperties": False,
    }
    registry.register("sample", "Sample.", _input_schema(), handler, result_schema=result_schema)
    arguments = {"count": 2, "transition": "support wiring", "commentary": "analysis"}

    if isinstance(expected, Exception):
        with pytest.raises(ToolContractError) as exc_info:
            await registry.dispatch(make_context("sample"), arguments, ["sample"])
        assert str(exc_info.value) == str(expected)
    else:
        assert await registry.dispatch(make_context("sample"), arguments, ["sample"]) == expected
    assert received == [{"count": 2}]


@pytest.mark.asyncio
async def test_a_parameter_the_model_request_left_out_is_never_ignored() -> None:
    registry = ToolRegistry()
    registry.register(
        "sample", "Sample.", _input_schema(), lambda _c, _a: tool_success({"value": "ok"})
    )
    offered = _open_contract({"count": {"type": "integer"}}, required=["count"])
    context = replace(make_context("sample"), input_contract=offered)

    with pytest.raises(ToolContractError, match='"label" is not a parameter'):
        await registry.dispatch(context, {"count": 2, "label": "x"}, ["sample"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "arrived_empty"),
    [
        pytest.param({}, True, id="no-arguments"),
        # The owner's repair empties the call, but the Model did send arguments.
        pytest.param({"label": ""}, False, id="emptied-by-repair"),
    ],
)
async def test_a_call_that_arrived_without_arguments_is_asked_for_again_complete(
    arguments: JsonObject, arrived_empty: bool
) -> None:
    registry = ToolRegistry()
    registry.register(
        "sample",
        "Sample.",
        _input_schema(),
        lambda _context, _arguments: tool_success({"value": "ok"}),
        argument_normalizer=lambda call: {key: value for key, value in call.items() if value},
    )

    with pytest.raises(ToolContractError) as exc_info:
        await registry.dispatch(make_context("sample"), arguments, ["sample"])

    no_arguments = registry.get("sample").contract.no_arguments_message()
    assert (str(exc_info.value) == no_arguments) is arrived_empty
    assert "count" in str(exc_info.value)


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

    await registry.dispatch(make_context("sample"), {"count": 1, "legacy": "raw"})
    with pytest.raises(ToolContractError) as exc_info:
        await registry.dispatch(make_context("sample"), {"legacy": "other"})

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
                "mode": {
                    "type": "string",
                    "enum": ["text", "bytes"],
                    "description": "text reads decoded lines; bytes reads raw bytes.",
                },
            },
            "required": ["path", "mode"],
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
        '- "mode" is required: one of "text", "bytes".\n'
        "read parameters: path (required), offset, limit, mode (required)."
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
    contract = _open_contract({"query": {"type": "string"}, "count": {}, "limit": {}})

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


@pytest.mark.parametrize(
    ("required", "value", "message"),
    [
        pytest.param(
            [],
            "false",
            'sample was not run: "enabled" must be a boolean; received "false"; '
            "omit this optional field to use its default true.",
            id="optional-default",
        ),
        pytest.param(
            ["enabled"],
            "true",
            'sample was not run: "enabled" must be a boolean; received "true".',
            id="required-default",
        ),
    ],
)
def test_type_error_recommends_omitting_only_an_optional_default(
    required: list[str], value: str, message: str
) -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={
            "type": "object",
            "properties": {"enabled": {"type": "boolean", "default": True}},
            "required": required,
            "additionalProperties": False,
        },
    )

    with pytest.raises(ToolContractError) as exc_info:
        contract.validate_arguments({"enabled": value})

    assert str(exc_info.value) == message


# --- Shared argument repair -------------------------------------------------------


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
    call = {**original}

    result = await registry.dispatch(make_context("sample"), call, ["sample"])

    assert result == tool_success({"value": "ok"})
    assert received == {
        "count": 72,
        "ratio": 0.7,
        "enabled": True,
        "items": [1, 2],
        "config": {"retries": 3},
        "optional": None,
    }
    assert call == original


_NAMED_FIELDS: JsonObject = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("schema", "arguments", "owner_sees"),
    [
        # Swarm run 2026-10-12: a shell command arrived as {"name": "command", "value": ...}.
        pytest.param(_input_schema(), {"name": "count", "value": "2"}, {"count": "2"}, id="pair"),
        pytest.param(
            _input_schema(),
            {"name": "label", "value": "x", "count": 2, "commentary": "analysis"},
            {"label": "x", "count": 2, "commentary": "analysis"},
            id="pair-beside-parameters",
        ),
        pytest.param(
            _input_schema(),
            {"name": "count", "value": 2, "count": 2},
            {"count": 2},
            id="pair-repeating-a-parameter",
        ),
        # Anything else stays as sent.
        pytest.param(_input_schema(), {"name": "counter", "value": 2}, None, id="no-parameter"),
        pytest.param(
            _input_schema(), {"name": "count", "value": 2, "count": 3}, None, id="conflict"
        ),
        pytest.param(
            _input_schema(), {"name": "count", "value": 2, "dryRun": True}, None, id="unknown-field"
        ),
        pytest.param(
            _NAMED_FIELDS,
            {"name": "value", "value": "x"},
            {"name": "value", "value": "x"},
            id="real-name-and-value-parameters",
        ),
    ],
)
async def test_a_call_sent_as_one_name_value_pair_runs_as_that_parameter(
    schema: JsonObject, arguments: JsonObject, owner_sees: JsonObject | None
) -> None:
    owner_saw: list[JsonObject] = []
    handled: list[JsonObject] = []

    def owner(call: JsonObject) -> JsonObject:
        owner_saw.append(copy.deepcopy(call))
        return call

    def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        handled.append(arguments)
        return tool_success({"value": "ok"})

    registry = ToolRegistry()
    registry.register(
        "sample", "Sample.", schema, handler, open_input_schema=True, argument_normalizer=owner
    )

    if owner_sees is None:
        # The pair stays as sent, so the call is refused for its unknown fields.
        with pytest.raises(ToolContractError, match='"name" is not a parameter'):
            await registry.dispatch(make_context("sample"), arguments, ["sample"])
        assert owner_saw == [arguments]
        assert handled == []
    else:
        await registry.dispatch(make_context("sample"), arguments, ["sample"])
        # The owner's repair reads the pair as the parameter it names.
        assert owner_saw == [owner_sees]
        assert len(handled) == 1


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
    context = replace(make_context("sample"), input_contract=active_contract)

    await registry.dispatch(context, {"items": "one"}, ["sample"])

    assert received == {"items": ["one"]}


@pytest.mark.parametrize(
    ("value_schema", "value", "expected", "valid"),
    [
        *(
            pytest.param({"type": "boolean"}, value, expected, True, id=f"boolean-{value!r}")
            for value, expected in (
                ("YES", True),
                (1, True),
                ("1", True),
                ("off", False),
                (0, False),
                ("No", False),
            )
        ),
        *(
            pytest.param({"type": "boolean"}, value, value, False, id=f"ambiguous-{value!r}")
            for value in ("maybe", "2", "")
        ),
        pytest.param({"type": "integer"}, 3.0, 3, True, id="integral-float"),
        pytest.param({"type": "string"}, 10**309, str(10**309), True, id="huge-integer-text"),
        pytest.param({"type": ["integer", "string"]}, "72", "72", True, id="accepted-string"),
    ],
)
def test_scalar_encodings_convert_only_when_unambiguous(
    value_schema: JsonObject, value: Any, expected: Any, valid: bool
) -> None:
    contract = _open_contract({"value": value_schema})

    normalized = contract.normalize_arguments({"value": value})

    assert normalized == {"value": expected}
    assert type(normalized["value"]) is type(expected)
    if valid:
        contract.validate_arguments(normalized)
    else:
        with pytest.raises(ToolContractError):
            contract.validate_arguments(normalized)


@pytest.mark.parametrize(
    ("properties", "arguments", "valid"),
    [
        pytest.param(
            {"payload": {"type": "object"}, "text": {"type": "string"}},
            {"payload": {"request": {"operation": "DELETE"}, "CamelCase": " YES "}, "text": "  "},
            True,
            id="arbitrary-payload-and-whitespace",
        ),
        *(
            pytest.param(
                {
                    "payload": {
                        "type": "object",
                        "properties": {"color": {"type": "string"}},
                        "additionalProperties": additional,
                    },
                    "request": {"type": "object"},
                },
                {
                    "payload": {
                        "color": "red",
                        "colors": "blue",
                        "COLOR": "green",
                        "operation": "replace",
                    },
                    "request": {"action": "delete"},
                },
                True,
                id=f"open-application-fields-{label}",
            )
            for label, additional in (("any", True), ("typed", {"type": "string"}))
        ),
        pytest.param(
            {
                "target": {"type": "string", "minLength": 1},
                "limit": {"type": "integer"},
                "enabled": {"type": "boolean"},
            },
            {"target": None, "limit": "", "enabled": None},
            False,
            id="explicit-empty-values",
        ),
        *(
            pytest.param(
                {"target": {"type": "string", "enum": ["tg-team-a"]}},
                {"target": identifier},
                False,
                id=f"similar-enum-identifier-{identifier.strip()}",
            )
            for identifier in ("tg-team-b", "TG-TEAM-A", "tg_team_a", " tg-team-a ")
        ),
    ],
)
def test_generic_repair_leaves_application_values_to_the_owner(
    properties: JsonObject, arguments: JsonObject, valid: bool
) -> None:
    contract = _open_contract(properties)

    assert contract.normalize_arguments(arguments) == arguments
    if valid:
        contract.validate_arguments(arguments)
    else:
        with pytest.raises(ToolContractError):
            contract.validate_arguments(arguments)


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
            "commentary": "analysis",
        }
    )

    # commentary labels a Model's output channel; it requests nothing either.
    assert normalized == {"count": 1, "label": "", "zero": 0, "flag": False}
    with pytest.raises(ToolContractError) as exc_info:
        contract.validate_arguments(normalized)
    message = str(exc_info.value)
    assert '"zero" is not a parameter' in message
    assert '"flag" is not a parameter' in message
    assert '"label" must not be empty' in message

    # A commentary that carries the call's fields is no label: refused, never run without them.
    carried = contract.normalize_arguments({"count": 1, "commentary": {"label": "x"}})
    kept, ignored = contract.drop_inert_fields(carried)
    assert (kept, ignored) == ({"count": 1, "commentary": {"label": "x"}}, ())
    with pytest.raises(ToolContractError, match='"commentary" is not a parameter'):
        contract.validate_arguments(kept)


def test_normalization_keeps_exact_empty_declared_string_properties() -> None:
    contract = compile_tool_contract(
        name="sample",
        input_schema={
            "type": "object",
            "properties": {
                "value": {"type": "string", "minLength": 1},
                "optional": {"type": "string", "minLength": 1},
            },
            "required": ["value"],
            "additionalProperties": False,
        },
    )

    normalized = contract.normalize_arguments({"value": "", "optional": ""})

    assert normalized == {"value": "", "optional": ""}
    with pytest.raises(ToolContractError, match='"value" must not be empty'):
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


@pytest.mark.parametrize(
    ("value_schema", "arguments"),
    [
        pytest.param(
            {"oneOf": [{"type": "boolean"}, {"type": "integer"}]},
            {"value": "1"},
            id="union-boolean-first",
        ),
        pytest.param(
            {"oneOf": [{"type": "integer"}, {"type": "boolean"}]},
            {"value": "1"},
            id="union-integer-first",
        ),
        pytest.param({"type": "string"}, '{"value":"one","value":"two"}', id="duplicate-json-key"),
    ],
)
def test_ambiguous_encoding_fails_instead_of_choosing_a_value(
    value_schema: JsonObject, arguments: Any
) -> None:
    contract = _open_contract({"value": value_schema})

    with pytest.raises(ToolContractError):
        contract.normalize_arguments(arguments)


_CHANGE: JsonObject = {
    "type": "object",
    "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
    "required": ["old", "new"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    ("field", "text", "expected"),
    [
        pytest.param(
            "changes",
            '[{"old": "x = 1", "new": "x = f("a")"}, {"old": "say "hi"", "new": ""}]',
            [{"old": "x = 1", "new": 'x = f("a")'}, {"old": 'say "hi"', "new": ""}],
            id="array",
        ),
        pytest.param(
            "change",
            '{"old": "a "b" c", "new": "d"}',
            {"old": 'a "b" c', "new": "d"},
            id="object",
        ),
        # A raw line break or tab inside a text stands for itself.
        pytest.param(
            "change",
            '{"old": "a = 1\nb = 2", "new": "\tc"}',
            {"old": "a = 1\nb = 2", "new": "\tc"},
            id="raw-control-characters",
        ),
        # Each string chooses its own representation. Mixing inside one string
        # can still be a slip of the output, such as a break inside a word.
        pytest.param(
            "change",
            '{"old": "a\\nb", "new": "c\nd"}',
            {"old": "a\nb", "new": "c\nd"},
            id="different-field-line-breaks",
        ),
        pytest.param(
            "changes",
            '[{"old":"a", "new":"b\nc"}, {"old":"d", "new":"e\\nf"}]',
            [{"old": "a", "new": "b\nc"}, {"old": "d", "new": "e\nf"}],
            id="different-item-line-breaks",
        ),
        pytest.param(
            "change",
            '{"old":"a", "new":"b\\nc\nd"}',
            None,
            id="mixed-line-breaks",
        ),
        pytest.param(
            "change",
            '{"old":"a", "new":"b\\rc\rd"}',
            None,
            id="mixed-cr-breaks",
        ),
        pytest.param(
            "change",
            '{"old":"a", "new":"b\\\\nc\nd"}',
            {"old": "a", "new": "b\\nc\nd"},
            id="literal-backslash-n",
        ),
        pytest.param(
            "change",
            '{"old":"a", "new":"b\\"c\nd"}',
            {"old": "a", "new": 'b"c\nd'},
            id="escaped-quote-before-raw-break",
        ),
        # A quote before ":" ends the text only where a value follows the colon.
        pytest.param(
            "change",
            '{"old": "got "x": not a number", "new": "x"}',
            {"old": 'got "x": not a number', "new": "x"},
            id="quote-colon-text",
        ),
        pytest.param("change", '{"old": "x = {"a": 2}", "new": "x"}', None, id="quote-colon-value"),
        # A comma and a quote after a quote are structure: no reading fits the schema.
        pytest.param("change", '{"old": "f("a", "b")", "new": "x"}', None, id="comma-quote"),
        pytest.param(
            "change", '{"old": "a "b" c", "new": "d", "line": 2}', None, id="undeclared-field"
        ),
        pytest.param("change", '{"old": "a "b" c", "old": "d"}', None, id="repeated-field"),
        pytest.param("change", '{"old": "a "b" \\_", "new": "d"}', None, id="other-break"),
    ],
)
def test_slips_in_json_text_are_read_only_into_a_value_the_schema_accepts(
    field: str, text: str, expected: Any
) -> None:
    contract = _open_contract({"changes": {"type": "array", "items": _CHANGE}, "change": _CHANGE})

    if expected is not None:
        assert contract.normalize_arguments({field: text}) == {field: expected}
        return
    with pytest.raises(ToolContractError):
        contract.validate_arguments(contract.normalize_arguments({field: text}))


# --- Owner-selected call repair ---------------------------------------------------


@pytest.mark.parametrize(
    ("properties", "arguments", "options", "expected"),
    [
        *(
            pytest.param(
                {
                    "action": {"type": "string", "enum": ["write_file", "delete"]},
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                arguments,
                {"enum_fields": ("action",), "field_aliases": {"file_pth": "file_path"}},
                {"action": "write_file", "file_path": "notes.txt", "content": ""},
                id=label,
            )
            for label, arguments in (
                (
                    "request-wrapper",
                    {
                        "request": {
                            "operation": " WRITE-FILE ",
                            "filePath": "notes.txt",
                            "content": "",
                        }
                    },
                ),
                (
                    "action-wrapper-with-alias",
                    {"write_file": {"file_pth": "notes.txt", "content": ""}},
                ),
                (
                    "enum-and-field-spelling",
                    {"action": "WRITE-FILE", "file_path": "notes.txt", "content": ""},
                ),
            )
        ),
        pytest.param(
            {"include_links": {"type": "boolean"}},
            {"includeLinkS": "false"},
            {},
            {"include_links": False},
            id="optional-field-spelling",
        ),
        pytest.param(
            {"file_path": {"type": "string"}},
            {"file_path": "one.txt", "filePath": "one.txt"},
            {},
            {"file_path": "one.txt"},
            id="agreeing-duplicate-spellings",
        ),
        pytest.param(
            {
                "action": {"type": "string"},
                "optional": {"type": "string", "minLength": 1},
                "items": {"type": "array", "items": {"type": "string", "minLength": 1}},
            },
            {"action": "input", "optional": "", "items": [""]},
            {"empty_as_omitted": ("optional",)},
            {"action": "input", "items": [""]},
            id="selected-empty-field-omitted",
        ),
        pytest.param(
            {"payload": {"type": "object", "additionalProperties": True}},
            {"payload": {"color": "red", "COLOR": "green", "operation": "replace"}},
            {},
            {"payload": {"color": "red", "COLOR": "green", "operation": "replace"}},
            id="application-payload-literal",
        ),
    ],
)
def test_owner_repair_reads_the_call_the_owner_selected(
    properties: JsonObject, arguments: JsonObject, options: JsonObject, expected: JsonObject
) -> None:
    contract = _open_contract(properties)

    assert normalize_call_arguments(contract, arguments, **options) == expected


@pytest.mark.parametrize(
    ("properties", "arguments", "options"),
    [
        pytest.param(
            {"file_path": {"type": "string"}},
            {"file_path": "one.txt", "filePath": "two.txt"},
            {},
            id="conflicting-spellings",
        ),
        pytest.param(
            {"file_path": {"type": "string"}},
            {"file_path": "one", "filePath": None},
            {"empty_as_omitted": ("file_path",)},
            id="conflict-checked-before-omission",
        ),
        pytest.param(
            {"target": {"type": "string"}},
            {"request": '{"target":"one","target":"two"}'},
            {},
            id="duplicate-key-in-encoded-wrapper",
        ),
    ],
)
def test_owner_repair_never_resolves_conflicting_values(
    properties: JsonObject, arguments: JsonObject, options: JsonObject
) -> None:
    with pytest.raises(ToolContractError):
        normalize_call_arguments(_open_contract(properties), arguments, **options)
