"""CLI failures offer valid, target-preserving recovery without replaying writes."""

import argparse
import shlex

import httpx
import pytest

from cli import _output, main, rpc_client
from cli._recovery import recovery_guidance
from cli._server_target import CommandResult, RpcFailure, ServerInstance
from cli.parser import build_parser, parse_args


def instance(tmp_path, host="192.0.2.8"):
    return ServerInstance(host, 9123, tmp_path, f"http://{host}:9123", tmp_path / "test.log")


def leaves(parser):
    children = next(
        (a.choices for a in parser._actions if isinstance(a, argparse._SubParsersAction)), {}
    )
    if not children:
        yield parser
    else:
        for child in {id(p): p for p in children.values()}.values():
            yield from leaves(child)


def test_every_published_leaf_example_has_valid_read_or_help_recovery(tmp_path, capsys):
    target = instance(tmp_path)
    count = 0
    for leaf in leaves(build_parser()):
        if not leaf.description or "Example: " not in leaf.description:
            continue
        tokens = shlex.split(leaf.description.split("Example: ", 1)[1].removeprefix("vbot "))
        args = parse_args(tokens)
        result = CommandResult(False, "test sentinel", target)
        guidance = recovery_guidance(args, result)
        assert guidance.commands
        for command in guidance.commands:
            if command[-1] == "--help":
                with pytest.raises(SystemExit) as done:
                    parse_args(command[1:])
                assert done.value.code == 0
            else:
                followup = parse_args(command[1:])
                assert followup.host == target.host
                assert followup.port == target.port
                assert followup.data_dir == str(target.data_dir)
                assert followup.command in {
                    "list",
                    "status",
                    "inventory",
                    "read",
                    "describe",
                    "show",
                    "operations",
                    "options",
                }
        count += 1
        capsys.readouterr()
    assert count > 120


@pytest.mark.parametrize(
    "tokens,expected",
    [
        (["server", "restart", "--prot", "9000"], "--port"),
        (["channel", "add", "demo", "--platfrom", "telegram"], "--platform"),
        (["provider", "key", "set", "openai", "--stdni"], "--stdin"),
        (["project", "override", "set", "demo", "a", "model", "p/m", "--hots", "remote"], "--host"),
    ],
)
def test_bad_flags_suggest_existing_options_without_dispatch(tokens, expected, capsys):
    with pytest.raises(SystemExit) as done:
        main.run(tokens, resolve=lambda **kw: pytest.fail("syntax errors must not dispatch"))
    assert done.value.code == 2
    error = capsys.readouterr().err
    assert expected in error  # Public option token, not editable prose.


def test_unknown_flag_does_not_echo_its_value_or_other_positional_secrets(capsys):
    secret = "credential-sentinel-$()"
    with pytest.raises(SystemExit) as done:
        main.run(["provider", "key", "set", "openai", secret, "--prot=" + secret])
    assert done.value.code == 2
    assert secret not in capsys.readouterr().err


@pytest.mark.parametrize(
    "tokens,choices",
    [
        (["channel", "add", "demo", "--platform", "telegarm"], ["telegram", "discord"]),
        (["agent", "update", "a", "--thinking-effort", "hihg"], ["high"]),
        (["update", "--output", "humna"], ["human", "auto", "plain"]),
    ],
)
def test_invalid_choices_retain_the_allowed_tokens(tokens, choices, capsys):
    with pytest.raises(SystemExit) as done:
        main.run(tokens, resolve=lambda **kw: pytest.fail("invalid choices must not dispatch"))
    assert done.value.code == 2
    error = capsys.readouterr().err
    assert all(choice in error for choice in choices)


@pytest.mark.parametrize(
    "tokens,scope",
    [
        (["prompt", "reset", "system", "--scope", "agent:demo"], "agent:demo"),
        (["skill", "delete", "demo", "--scope", "agent:a", "--yes"], "agent:a"),
        (["memory", "remove", "a", "1", "--scope", "user", "--yes"], "user"),
    ],
)
def test_inspection_keeps_content_scope(tmp_path, tokens, scope):
    args = parse_args(tokens)
    guidance = recovery_guidance(args, CommandResult(False, "test sentinel", instance(tmp_path)))
    assert parse_args(guidance.commands[0][1:]).scope == scope


def test_inspection_keeps_project_agent_address_and_resolved_target(tmp_path):
    args = parse_args(["session", "delete", "alice@project", "s", "--yes"])
    result = CommandResult(False, "test sentinel", instance(tmp_path))
    followup = parse_args(recovery_guidance(args, result).commands[0][1:])
    assert (followup.agent, followup.host, followup.port) == ("alice@project", "192.0.2.8", 9123)


@pytest.mark.parametrize("host,area", [("127.0.0.1", "server"), ("192.0.2.8", "agent")])
def test_connection_failure_never_proposes_remote_lifecycle(tmp_path, host, area):
    result = CommandResult(
        False,
        "test sentinel",
        instance(tmp_path, host),
        failure=RpcFailure("agent.create", "not_sent"),
    )
    guidance = recovery_guidance(parse_args(["agent", "create", "a", "A"]), result)
    assert guidance.commands[0][1] == area


@pytest.mark.parametrize(
    "code,area",
    [
        ("project_not_found", "project"),
        ("channel_not_found", "channel"),
        ("oauth_not_supported", "provider"),
        ("agent_order_conflict", "agent"),
    ],
)
def test_server_code_selects_the_actual_failed_resource(tmp_path, code, area):
    result = CommandResult(
        False,
        "test sentinel",
        instance(tmp_path),
        failure=RpcFailure("agent.update", "responded", code),
    )
    guidance = recovery_guidance(parse_args(["agent", "update", "a", "--name", "A"]), result)
    assert guidance.commands[0][1] == area


@pytest.mark.parametrize("mode", ["auto", "plain", "human"])
def test_real_rpc_error_keeps_stdout_and_adds_recovery_in_every_mode(
    tmp_path, monkeypatch, capsys, mode
):
    requests = []

    def post(url, **kwargs):
        requests.append(kwargs["json"])
        return httpx.Response(
            200,
            json={"ok": False, "error": {"code": "project_not_found", "message": "test sentinel"}},
        )

    monkeypatch.setattr(rpc_client.httpx, "post", post)
    code = main.run(
        ["project", "show", "absent", "--output", mode], resolve=lambda **kw: instance(tmp_path)
    )
    out, err = capsys.readouterr()
    assert code == 1
    assert out == "project_not_found: test sentinel\n"
    assert len(requests) == 1
    assert "vbot project list" in err  # Public command path.
    assert "9123" in err


def test_saved_key_then_failed_refresh_preserves_failure_and_never_resends_secret(
    tmp_path, monkeypatch, capsys
):
    calls = []
    evidence = []
    original = _output.recovery_guidance

    def observe(args, result):
        evidence.append(result.failure)
        return original(args, result)

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        if kwargs["json"]["method"] == "provider.set_key":
            return httpx.Response(
                200, json={"ok": True, "result": {"connection_id": "openai:api-key"}}
            )
        raise httpx.ConnectError("secret-sentinel", request=httpx.Request("POST", url))

    monkeypatch.setattr(rpc_client.httpx, "post", post)
    monkeypatch.setattr(_output, "recovery_guidance", observe)
    code = main.run(
        ["provider", "key", "set", "openai", "secret-sentinel", "--refresh-models"],
        resolve=lambda **kw: instance(tmp_path),
    )
    assert code == 1
    assert [call["method"] for call in calls] == ["provider.set_key", "model.refresh_db"]
    assert evidence[0].request_state == "not_sent"
    assert evidence[0].method == "model.refresh_db"
    out, err = capsys.readouterr()
    assert "secret-sentinel" not in out + err


def test_local_resolution_failure_is_a_normal_error_exit(capsys):
    def resolve(**kwargs):
        raise OSError("test sentinel")

    assert main.run(["agent", "list"], resolve=resolve) == 1
    out, err = capsys.readouterr()
    assert not out
    assert "OSError" in err
    assert "vbot agent list --help" in err
