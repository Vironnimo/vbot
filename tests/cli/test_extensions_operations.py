"""Dynamic Extension CLI flags retain exact argument and credential boundaries."""

import io
import json
from types import SimpleNamespace

import pytest

from cli import extensions_management
from cli.extensions_management import _operation_arguments
from cli.parser import parse_args


def test_dynamic_arguments_and_target_survive_the_cli_parser():
    args = parse_args(
        [
            "extensions",
            "mcp",
            "invoke",
            "blender",
            "--agent",
            "alice@project",
            "--operation",
            "tools/call",
            "--arguments",
            '{"name":"a b"}',
            "--port",
            "8422",
        ]
    )
    assert args.port == 8422
    assert args.rest == [
        "invoke",
        "blender",
        "--agent",
        "alice@project",
        "--operation",
        "tools/call",
        "--arguments",
        '{"name":"a b"}',
    ]


def test_operation_schema_parses_string_enums_without_json_quotes():
    result = _operation_arguments(
        {"parameters": {"properties": {"operation": {"enum": ["tools/call"]}}}},
        ["--operation", "tools/call"],
    )
    assert result == {"operation": "tools/call"}


def test_secret_operation_requires_standard_input():
    with pytest.raises(ValueError):
        _operation_arguments({"secret": True, "parameters": {}}, ["--value", "do-not-print"])


def test_standard_input_decodes_utf8_without_echo(monkeypatch):
    monkeypatch.setattr("cli.extensions_management.sys.stdin", io.StringIO('{"value":"ä-secret"}'))
    assert _operation_arguments({"secret": True, "parameters": {}}, ["--stdin"]) == {
        "value": "ä-secret"
    }


def test_dynamic_help_is_forwarded_to_the_extension():
    args = parse_args(["extensions", "mcp", "save", "--help"])
    assert args.rest == ["save", "--help"]


def test_catalog_is_bounded_and_exact_help_keeps_the_complete_schema(monkeypatch):
    operation = {
        "name": "save",
        "description": "Replace the saved connection.",
        "secret": False,
        "parameters": {"type": "object", "properties": {"connection": {"type": "object"}}},
    }
    calls = []

    def rpc(instance, method, params):
        calls.append(params)
        return SimpleNamespace(ok=True, data={"operations": [operation]})

    monkeypatch.setattr(extensions_management, "_rpc_call", rpc)
    catalog = extensions_management.extensions_operation(None, "mcp", ["operations"])
    detail = extensions_management.extensions_operation(None, "mcp", ["save", "--help"])
    assert catalog.ok and detail.ok
    summary = json.loads(catalog.message)
    assert "parameters" not in summary["operations"][0]
    assert summary["operations"][0]["name"] == "save"
    assert json.loads(detail.message) == operation
    assert all(call["operation"] == "describe" for call in calls)


def test_unknown_operation_suggests_only_catalog_names_without_invoking(monkeypatch):
    calls = []

    def rpc(instance, method, params):
        calls.append(params)
        return SimpleNamespace(
            ok=True, data={"operations": [{"name": "inspect"}, {"name": "invoke"}]}
        )

    monkeypatch.setattr(extensions_management, "_rpc_call", rpc)
    result = extensions_management.extensions_operation(None, "mcp", ["insepct"])
    assert not result.ok
    assert json.loads(result.message)["suggestions"] == ["inspect"]
    assert [call["operation"] for call in calls] == ["describe"]


@pytest.mark.parametrize(
    "tokens", [["--requset-id", "secret-sentinel"], ["--request-id=secret-sentinel"]]
)
def test_unknown_operation_argument_suggests_schema_flag_without_echo(tokens):
    operation = {"parameters": {"properties": {"request_id": {"type": "string"}}}}
    with pytest.raises(ValueError) as error:
        _operation_arguments(operation, tokens)
    assert "--request-id" in str(error.value)
    assert "secret-sentinel" not in str(error.value)


def test_dynamic_operation_target_named_fields_remain_opaque():
    args = parse_args(["extensions", "run", "demo", "export", "--port", "payload-port"])
    assert args.rest == ["export", "--port", "payload-port"]


def test_bad_enum_does_not_invoke_extension(monkeypatch):
    calls = []

    def rpc(instance, method, params):
        calls.append(params)
        return SimpleNamespace(
            ok=True,
            data={
                "operations": [
                    {
                        "name": "set",
                        "parameters": {"properties": {"mode": {"enum": ["active", "paused"]}}},
                    }
                ]
            },
        )

    monkeypatch.setattr(extensions_management, "_rpc_call", rpc)
    result = extensions_management.extensions_operation(None, "demo", ["set", "--mode", "acitve"])
    assert not result.ok
    assert "active" in result.message  # Allowed schema value.
    assert [call["operation"] for call in calls] == ["describe"]
