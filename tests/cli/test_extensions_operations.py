"""Dynamic Extension CLI flags retain exact argument and credential boundaries."""

import io

import pytest

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
