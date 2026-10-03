"""Dynamic Extension operations keep exact argument, target and credential boundaries."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from cli import main as cli_main
from cli.server_management import ServerInstance
from tests.cli.cli_test_support import FakeRpc, RunCli


def _operation(name: str, properties: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"Run {name}.",
        "secret": False,
        "parameters": {"type": "object", "properties": properties},
    } | fields


def _operation_calls(rpc: FakeRpc) -> list[str]:
    return [params["operation"] for method, params in rpc.calls if method == "extensions.operation"]


def test_dynamic_arguments_reach_the_operation_while_target_options_select_the_server(
    rpc: FakeRpc, instance: ServerInstance, capsys: pytest.CaptureFixture[str]
) -> None:
    invoke = _operation(
        "invoke",
        {
            "id": {"type": "string"},
            "agent": {"type": "string"},
            "operation": {"enum": ["tools/call", "tools/list"]},
            "arguments": {"type": "object"},
        },
    )
    rpc.reply("extensions.operation", {"operations": [invoke]})
    rpc.reply("extensions.operation", {"state": "completed"})
    targets: list[dict[str, Any]] = []

    def resolve(**target: Any) -> ServerInstance:
        targets.append(target)
        return instance

    code = cli_main.run(
        [
            "extensions", "mcp", "invoke", "blender",
            "--agent", "alice@project",
            "--operation", "tools/call",
            "--arguments", '{"name":"a b"}',
            "--port", "8422",
        ],
        resolve=resolve,
    )  # fmt: skip

    assert code == 0
    assert [target["port"] for target in targets] == [8422]
    assert rpc.calls == [
        ("extensions.operation", {"name": "mcp", "operation": "describe"}),
        (
            "extensions.operation",
            {
                "name": "mcp",
                "operation": "invoke",
                # A string enum is taken verbatim, without JSON quotes.
                "arguments": {
                    "id": "blender",
                    "agent": "alice@project",
                    "operation": "tools/call",
                    "arguments": {"name": "a b"},
                },
            },
        ),
    ]
    assert json.loads(capsys.readouterr().out) == {"state": "completed"}


def test_explicit_run_keeps_target_named_operation_fields_opaque(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "extensions.operation", {"operations": [_operation("export", {"port": {"type": "string"}})]}
    )
    rpc.reply("extensions.operation", {"state": "completed"})

    code, _out, _err = run_cli("extensions", "run", "demo", "export", "--port", "payload-port")

    assert code == 0
    assert rpc.calls[-1] == (
        "extensions.operation",
        {"name": "demo", "operation": "export", "arguments": {"port": "payload-port"}},
    )


def test_catalog_is_bounded_and_operation_help_keeps_the_complete_schema(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    save = _operation(
        "save", {"connection": {"type": "object"}}, description="Replace the saved connection."
    )
    rpc.reply("extensions.operation", {"operations": [save]})

    catalog = run_cli("extensions", "mcp", "operations")
    detail = run_cli("extensions", "mcp", "save", "--help")

    assert (catalog[0], detail[0]) == (0, 0)
    summary = json.loads(catalog[1])
    assert summary["operations"] == [
        {"name": "save", "description": "Replace the saved connection.", "secret": False}
    ]
    assert summary["next"] == (
        "vbot extensions run mcp <operation> --help for the complete argument schema; "
        "keep the same target options"
    )
    assert json.loads(detail[1]) == save
    assert _operation_calls(rpc) == ["describe", "describe"]


@pytest.mark.parametrize(
    ("tokens", "shown"),
    [
        pytest.param(("insepct",), '"suggestions": ["inspect"]', id="unknown-operation"),
        pytest.param(("configure", "--mode", "acitve"), "active", id="invalid-enum-value"),
        pytest.param(("inspect", "--value", "do-not-print"), "--stdin", id="secret-in-arguments"),
    ],
)
def test_invalid_operation_input_never_invokes_the_extension(
    rpc: FakeRpc, run_cli: RunCli, tokens: tuple[str, ...], shown: str
) -> None:
    rpc.reply(
        "extensions.operation",
        {
            "operations": [
                _operation("inspect", {"value": {"type": "string"}}, secret=True),
                _operation("invoke", {}),
                _operation("configure", {"mode": {"enum": ["active", "paused"]}}),
            ]
        },
    )

    code, out, err = run_cli("extensions", "demo", *tokens)

    assert code == 1
    assert _operation_calls(rpc) == ["describe"]
    assert shown in out
    assert "do-not-print" not in out + err


@pytest.mark.parametrize(
    "tokens",
    [
        pytest.param(("--requset-id", "secret-sentinel"), id="separate-value"),
        pytest.param(("--request-id=secret-sentinel",), id="inline-value"),
    ],
)
def test_unknown_argument_suggests_the_schema_flag_without_echoing_the_value(
    rpc: FakeRpc, run_cli: RunCli, tokens: tuple[str, ...]
) -> None:
    rpc.reply(
        "extensions.operation",
        {"operations": [_operation("cancel", {"request_id": {"type": "string"}})]},
    )

    code, out, err = run_cli("extensions", "demo", "cancel", *tokens)

    assert code == 1
    assert _operation_calls(rpc) == ["describe"]
    assert "--request-id" in out
    assert "secret-sentinel" not in out + err


def test_secret_operation_reads_utf8_json_from_stdin_without_echo(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"value":"ä-secret"}'))
    login = _operation("login", {"value": {"type": "string"}}, secret=True)
    rpc.reply("extensions.operation", {"operations": [login]})
    rpc.reply("extensions.operation", {"state": "completed"})

    code, out, err = run_cli("extensions", "mcp", "login", "--stdin")

    assert code == 0
    assert rpc.calls[-1][1]["arguments"] == {"value": "ä-secret"}
    assert "ä-secret" not in out + err


@pytest.mark.parametrize("source", ["file", "stdin"])
def test_document_argument_is_read_from_a_file_or_standard_input(
    rpc: FakeRpc,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source: str,
) -> None:
    text = '{"mcpServers": {"a": {"command": "ä"}}}'
    document = {"type": "string", "contentMediaType": "text/plain"}
    load = _operation("import", {"source": document, "apply": {"type": "boolean"}})
    rpc.reply("extensions.operation", {"operations": [load]})
    rpc.reply("extensions.operation", {"servers": []})
    if source == "file":
        path = tmp_path / "setup.json"
        path.write_text(text, encoding="utf-8-sig")
        tokens: tuple[str, ...] = (str(path),)
    else:
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))
        tokens = ("--source", "-")

    code, _out, _err = run_cli("extensions", "run", "mcp", "import", *tokens, "--apply", "true")

    assert code == 0
    # The text is read without a byte order mark and never passes the shell.
    assert rpc.calls[-1][1]["arguments"] == {"source": text, "apply": True}


def test_unreadable_document_argument_is_not_echoed(rpc: FakeRpc, run_cli: RunCli) -> None:
    document = {"type": "string", "contentMediaType": "text/plain"}
    rpc.reply("extensions.operation", {"operations": [_operation("import", {"source": document})]})

    # Setup text pasted where a path belongs can hold credentials.
    code, out, err = run_cli("extensions", "mcp", "import", "TOKEN=secret-sentinel npx server")

    assert code == 1
    assert _operation_calls(rpc) == ["describe"]
    assert "- to read standard input" in out
    assert "secret-sentinel" not in out + err
