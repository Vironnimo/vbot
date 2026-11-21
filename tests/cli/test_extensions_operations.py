"""Dynamic Extension operations keep exact argument, target and credential boundaries."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from cli import app_agent
from cli import main as cli_main
from cli.server_management import ServerInstance
from core.tools.vbot_cli_runner import REVIEW_ENV, REVIEW_EXIT, REVIEWED_INPUTS_ENV
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
            "extensions", "mcp", "--port", "8422", "invoke", "blender",
            "--agent", "alice@project",
            "--operation", "tools/call",
            "--arguments", '{"name":"a b"}',
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


@pytest.mark.parametrize("command", [("run", "demo"), ("demo",)])
def test_operation_keeps_target_named_fields_opaque(
    rpc: FakeRpc, run_cli: RunCli, command: tuple[str, ...]
) -> None:
    rpc.reply(
        "extensions.operation", {"operations": [_operation("export", {"port": {"type": "string"}})]}
    )
    rpc.reply("extensions.operation", {"state": "completed"})

    code, _out, _err = run_cli("extensions", *command, "export", "--port", "payload-port")

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

    assert code == 2
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

    assert code == 2
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


@pytest.mark.parametrize(
    "source", ["utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "stdin", "tool-stdin"]
)
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
    if source not in {"stdin", "tool-stdin"}:
        path = tmp_path / "setup.json"
        bom = {"utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}.get(source, b"")
        path.write_bytes(bom + text.encode(source))
        tokens: tuple[str, ...] = (str(path),)
    else:
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))
        tokens = ("--source", "-")

    command = ["extensions", "run", "mcp", "import", *tokens, "--apply", "true"]
    if source == "tool-stdin":
        monkeypatch.setattr(sys, "argv", ["vbot", *command])
        monkeypatch.setenv("VBOT_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("VBOT_SERVER_PORT", "22000")
        monkeypatch.setenv("VBOT_APP_CLI_LISTEN_HOST", "127.0.0.1")
        monkeypatch.setenv(REVIEWED_INPUTS_ENV, "1")
        frame = json.dumps({"profile": None, "files": {}})
        monkeypatch.setattr(sys, "stdin", io.StringIO(frame + "\n" + text))
        code = app_agent.main()
    else:
        code, _out, _err = run_cli(*command)

    assert code == 0
    # The text is read without a byte order mark and never passes the shell.
    assert rpc.calls[-1][1]["arguments"] == {"source": text, "apply": True}


@pytest.mark.parametrize("change", ["none", "schema", "link"])
def test_bound_tool_reviews_all_document_files_before_opening_any(
    rpc: FakeRpc,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    change: str,
) -> None:
    document = {"type": "string", "contentMediaType": "text/plain"}
    plain = {"type": "string"}
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    extra = tmp_path / "extra.txt"
    monkeypatch.chdir(tmp_path)
    command = [
        "vbot",
        "extensions",
        "run",
        "demo",
        "import",
        first.name,
        "--second",
        str(second),
        "--extra",
        str(extra),
        "--stdin",
    ]
    monkeypatch.setattr(sys, "argv", command)
    monkeypatch.setenv("VBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VBOT_SERVER_PORT", "22000")
    monkeypatch.setenv("VBOT_APP_CLI_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv(REVIEW_ENV, "marker")
    body = json.dumps({"inline": "a path-shaped value from stdin"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    properties = {"first": document, "second": document, "extra": plain, "inline": document}
    rpc.reply("extensions.operation", {"operations": [_operation("import", properties)]})
    next_properties = {**properties, "extra": document} if change == "schema" else properties
    rpc.reply("extensions.operation", {"operations": [_operation("import", next_properties)]})
    rpc.reply("extensions.operation", {"state": "completed"})
    # Neither file exists yet. The entire operand list reaches review before any read.
    assert app_agent.main() == REVIEW_EXIT
    marker, packet = capsys.readouterr().out.strip().splitlines()
    assert marker == "marker"
    assert json.loads(packet) == {
        "rights": True,
        "reads": [[str(first), False], [str(second), False]],
    }
    assert _operation_calls(rpc) == ["describe"]

    frame = {"profile": None, "files": {str(p): str(p.resolve()) for p in (first, second)}}
    if change == "none":
        first.write_text("first", encoding="utf8")
        second.write_text("second", encoding="utf8")
    elif change == "link":
        extra.write_text("different target", encoding="utf8")
        try:
            second.symlink_to(extra)
        except OSError:
            pytest.skip("symlink privilege unavailable")
    monkeypatch.setenv("VBOT_APP_CLI_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv(REVIEWED_INPUTS_ENV, "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(frame) + "\n" + body))
    code = app_agent.main()
    if change != "none":
        assert code == 2
        assert "paths could not be confirmed" in capsys.readouterr().out
        assert _operation_calls(rpc) == ["describe", "describe"]
    else:
        assert code == 0
        assert rpc.calls[-1][1]["arguments"] == {
            "first": "first",
            "second": "second",
            "extra": str(extra),
            "inline": "a path-shaped value from stdin",
        }


def test_unreadable_document_argument_is_not_echoed(rpc: FakeRpc, run_cli: RunCli) -> None:
    document = {"type": "string", "contentMediaType": "text/plain"}
    rpc.reply("extensions.operation", {"operations": [_operation("import", {"source": document})]})

    # Setup text pasted where a path belongs can hold credentials.
    code, out, err = run_cli("extensions", "mcp", "import", "TOKEN=secret-sentinel npx server")

    assert code == 2
    assert _operation_calls(rpc) == ["describe"]
    assert "- to read standard input" in out
    assert "secret-sentinel" not in out + err
