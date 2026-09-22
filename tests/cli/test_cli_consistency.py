"""Cross-area navigation, output preservation, and operation status contracts."""

import argparse
import shlex

import pytest

from cli import _commands, _output, main, rpc_client
from cli._parser_common import AREA_HELP, COMMAND_PATHS
from cli._progress import ProgressPrinter, current_progress
from cli.formatting import output_mode, record_fields
from cli.parser import build_parser, parse_args
from cli.server_management import CommandResult, ServerInstance


def children(parser):
    return next(
        (a.choices for a in parser._actions if isinstance(a, argparse._SubParsersAction)), {}
    )


def test_every_area_and_command_has_discoverable_help_and_output_mode(capsys):
    root = build_parser()
    assert set(children(root)) == {*AREA_HELP, "help"}
    seen = set()

    def visit(parser):
        if id(parser) in seen:
            return
        seen.add(id(parser))
        assert "--output" in parser._option_string_actions
        with pytest.raises(SystemExit) as done:
            parser.parse_args(["--help"])
        assert done.value.code == 0
        for child in children(parser).values():
            visit(child)

    visit(root)
    assert len(seen) > 150


@pytest.mark.parametrize(
    "area,legacy,path",
    [
        (area, legacy, path)
        for area, paths in COMMAND_PATHS.items()
        for legacy, path in paths.items()
    ],
)
def test_nested_commands_and_legacy_spellings_preserve_every_argument(area, legacy, path):
    leaf = children(children(build_parser())[area])[legacy]
    example = leaf.description.split("Example: ", 1)[1]
    tokens = shlex.split(example.removeprefix("vbot "))
    # Examples may still be overridden by a builder. Both public paths must
    # reach exactly the same action, defaults, payload inputs and target.
    prefix = 2 if tokens[1] == legacy else 1 + len(path)
    arguments = [*tokens[prefix:], "--host", "192.0.2.8", "--port", "9123"]
    assert vars(parse_args([area, legacy, *arguments])) == vars(
        parse_args([area, *path, *arguments])
    )


@pytest.mark.parametrize(
    "tokens",
    [
        [],
        ["agent"],
        ["provider", "custom"],
        ["session-store", "snapshot"],
        ["help", "skill", "file"],
    ],
)
def test_bare_groups_show_help_without_dispatch(tokens, capsys):
    with pytest.raises(SystemExit) as done:
        main.run(tokens, resolve=lambda **kw: pytest.fail("discovery must not resolve a target"))
    assert done.value.code == 0
    assert not capsys.readouterr().err


AREAS = [
    ("agent", ["list"]),
    ("project", ["list"]),
    ("session", ["list", "a"]),
    ("session-store", ["status"]),
    ("channel", ["list"]),
    ("tool", ["list"]),
    ("prompt", ["list"]),
    ("log", ["list"]),
    ("provider", ["list"]),
    ("model", ["list"]),
    ("task-model", ["list"]),
    ("skill", ["list"]),
    ("memory", ["list", "a"]),
    ("extensions", ["list"]),
    ("cron", ["list"]),
    ("bootstrap", ["list"]),
    ("statistics", ["overview"]),
    ("config", ["list"]),
    ("debug", ["status"]),
    ("autostart", ["status"]),
]


@pytest.mark.parametrize("area,tokens", AREAS)
@pytest.mark.parametrize(
    "ok,attention,marker",
    [
        (True, (), "[OK]"),
        (False, (), "[ERROR]"),
        (True, ("Saved; further action required",), "[WARN]"),
    ],
)
def test_all_management_areas_report_outcomes_without_changing_payload(
    area, tokens, ok, attention, marker, tmp_path, monkeypatch, capsys
):
    instance = ServerInstance(
        "127.0.0.1", 9123, tmp_path, "http://127.0.0.1:9123", tmp_path / "test.log"
    )
    payload = '{"content": "warning: user text is data", "id": "exact-id"}'
    result = CommandResult(ok, payload, instance, attention=attention)
    monkeypatch.setattr(
        _commands, f"dispatch_{area.replace('-', '_')}_command", lambda *a, **kw: result
    )
    code = main.run([area, *tokens], resolve=lambda **kw: instance)
    out, err = capsys.readouterr()
    assert code == (0 if ok else 1)
    assert payload in out
    assert marker in err
    assert "exact-id" not in err
    assert "\x1b" not in out + err
    assert not current_progress.get()


def test_plain_output_preserves_json_and_suppresses_status(tmp_path, monkeypatch, capsys):
    instance = ServerInstance(
        "127.0.0.1", 9123, tmp_path, "http://127.0.0.1:9123", tmp_path / "test.log"
    )
    message = '{"id": "a", "values": [1, 2]}'
    code = main.run(
        ["config", "raw", "--output", "plain"],
        resolve=lambda **kw: instance,
        raw_config_fn=lambda instance: CommandResult(True, message, instance),
    )
    assert code == 0
    out, err = capsys.readouterr()
    assert out == message + "\n"
    assert err == ""
    assert output_mode.get() == "auto"


def test_human_records_keep_exact_values_and_plain_layout():
    values = ["- id=one", " name=Two words name=three", " text=  spaces\nnew line"]
    token = output_mode.set("human")
    try:
        assert (
            record_fields(values, separator="")
            == "- id=one\n  name=Two words name=three\n  text=  spaces\nnew line"
        )
    finally:
        output_mode.reset(token)
    token = output_mode.set("plain")
    try:
        assert record_fields(values, separator="") == "".join(values)
    finally:
        output_mode.reset(token)


def test_real_long_rpc_announces_before_response_and_stops_heartbeat(tmp_path, monkeypatch, capsys):
    instance = ServerInstance(
        "127.0.0.1", 9123, tmp_path, "http://127.0.0.1:9123", tmp_path / "test.log"
    )
    printers = []

    def printer(**kwargs):
        item = ProgressPrinter(interval=0.01, **kwargs)
        printers.append(item)
        return item

    monkeypatch.setattr(_output, "ProgressPrinter", printer)

    def post(*args, **kwargs):
        assert "[WORK]" in capsys.readouterr().err
        assert kwargs["json"]["method"] == "model.refresh_db"

        class Response:
            status_code = 200

            def json(self):
                return {"ok": True, "result": {"refreshed_count": 1, "model_count": 4}}

        return Response()

    monkeypatch.setattr(rpc_client.httpx, "post", post)
    assert main.run(["model", "refresh"], resolve=lambda **kw: instance) == 0
    out, err = capsys.readouterr()
    assert "4 models" in out and "[OK]" in err
    assert all(not printer._thread.is_alive() for printer in printers)


def test_interrupted_command_returns_130_and_cleans_up(tmp_path, capsys):
    instance = ServerInstance(
        "127.0.0.1", 9123, tmp_path, "http://127.0.0.1:9123", tmp_path / "test.log"
    )

    def interrupted(instance):
        raise KeyboardInterrupt

    assert (
        main.run(["agent", "list"], resolve=lambda **kw: instance, list_agents=interrupted) == 130
    )
    assert "[WARN]" in capsys.readouterr().err
    assert current_progress.get() is None


@pytest.mark.parametrize(
    "modern,legacy",
    [
        (["show", "demo"], ["demo"]),
        (["set", "demo", "key", "value"], ["demo", "set", "key", "value"]),
        (["set", "demo", "key", "--stdin"], ["demo", "set", "key", "--stdin"]),
        (["operations", "demo"], ["demo", "operations"]),
        (
            ["run", "demo", "do-work", "--custom-flag", "exact value"],
            ["demo", "do-work", "--custom-flag", "exact value"],
        ),
    ],
)
def test_extensions_action_first_retains_owner_arguments(modern, legacy):
    left = parse_args(["extensions", *modern])
    right = parse_args(["extensions", *legacy])
    for field in ("selector", "rest", "host", "port", "data_dir", "stdin"):
        assert getattr(left, field) == getattr(right, field)


@pytest.mark.parametrize("prefix", [["--output", "plain"], ["--output=plain"]])
def test_root_output_option_keeps_aliases_and_dynamic_extension_arguments(prefix):
    assert parse_args([*prefix, "providers", "custom", "list"]).output == "plain"
    args = parse_args([*prefix, "extensions", "demo", "export", "--output", "json"])
    assert args.output == "plain"
    assert args.rest == ["export", "--output", "json"]


def test_extension_run_separates_target_options_from_operation_options():
    args = parse_args(
        [
            "extensions",
            "run",
            "demo",
            "--host",
            "192.0.2.8",
            "--port",
            "9123",
            "--output",
            "plain",
            "export",
            "--host",
            "operation-target",
            "--output",
            "json",
        ]
    )
    assert (args.host, args.port, args.output) == ("192.0.2.8", 9123, "plain")
    assert args.rest == ["export", "--host", "operation-target", "--output", "json"]
