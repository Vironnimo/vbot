"""The App Agent uses the full local CLI, with the owning server as its fixed target."""

import io
import json
import sys
from pathlib import Path

import pytest

from cli._server_target import resolve_instance
from cli.app_agent import bind_target, changes_agent_access, main
from cli.parser import parse_args
from core.tools.shell_environment import RunIdentity
from core.tools.vbot_cli_runner import (
    REVIEW_ENV,
    REVIEW_EXIT,
    REVIEWED_INPUTS_ENV,
    VBotAccessReviewRequiredError,
    run_vbot_cli,
)
from tests.cli.cli_test_support import FakeRpc


@pytest.mark.parametrize(
    "argv",
    [
        ["--port=99", "config", "list"],
        ["server", "status", "--host", "another-server"],
        ["home", "--data-dir", "another-instance"],
        ["devices", "logout", "https://another-server"],
    ],
)
def test_app_agent_rejects_retargeting_before_dispatch(tmp_path: Path, argv: list[str]) -> None:
    instance = resolve_instance(host="::1", port=22000, data_dir=tmp_path)
    with pytest.raises(ValueError, match="current vBot instance"):
        bind_target(parse_args(argv), instance)


def test_app_agent_binds_only_instance_selectors(tmp_path: Path) -> None:
    instance = resolve_instance(host="::1", port=22000, data_dir=tmp_path)
    command = parse_args(["application", "access", "set", "lan", "--host", "0.0.0.0"])
    bind_target(command, instance)
    assert command.bind_host == "0.0.0.0"
    desktop = parse_args(["desktop"])
    bind_target(desktop, instance)
    assert (desktop.host, desktop.port) == ("::1", 22000)


@pytest.mark.parametrize(
    ("argv", "review"),
    [
        (["agents", "update", "self", "--clear-permissions"], True),
        (["agent", "update", "other", "--permissions=full-access"], True),
        (["agent", "update", "other", "--permission-profile=restricted"], True),
        (["agent", "create", "new", "New", "--allow-rule", "Read(/docs)"], True),
        (["agent", "update", "other", "--allowed-skills"], True),
        (["agent", "update", "other", "--subagent-allow", "helper"], True),
        (["project", "clear-override", "proj", "member", "tool_access"], True),
        (["project", "set-override", "proj", "member", "permission_rules", "work-dir"], True),
        (["project", "override", "set", "proj", "member", "permission_profile", "custom"], True),
        (["project", "override", "clear", "proj", "member", "permission_profile"], True),
        (["permission-profiles", "save", "custom", "--stdin"], True),
        (["permission-profiles", "delete", "custom"], True),
        (["permission-profiles", "list"], False),
        (["permission-profiles", "show", "custom"], False),
        (
            [
                "config",
                "patch",
                "--set",
                "server.timezone",
                "UTC",
                "--unset",
                "defaults.agent.permission_rules",
            ],
            True,
        ),
        (["config", "set", "defaults.agent.permission_rules", "--stdin"], True),
        (["agent", "show", "other"], False),
        (["agent", "update", "other", "--name", "permission_rules"], False),
        (["config", "get", "defaults.agent.permission_rules"], False),
        (["project", "clear-override", "proj", "member", "temperature"], False),
    ],
)
def test_agent_access_review_follows_the_parsed_operation(argv: list[str], review: bool) -> None:
    assert changes_agent_access(parse_args(argv)) is review


def test_app_agent_requires_profile_contents_in_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rpc: FakeRpc,
) -> None:
    rules = {"deny": ["Read(~/Verträge/**)"]}
    body = json.dumps(rules, ensure_ascii=False)
    monkeypatch.setenv("VBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VBOT_SERVER_PORT", "22000")
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "manager")
    command = ["vbot", "permission-profiles", "save", "custom"]
    monkeypatch.setattr(sys, "argv", [*command, "--file", "absent.json"])
    monkeypatch.setenv("VBOT_APP_CLI_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv(REVIEW_ENV, "marker")
    assert main() == 2
    assert "Use --stdin" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", [*command, "--stdin"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    rpc.reply("permissions.profiles", {"profiles": []})
    monkeypatch.setenv("VBOT_APP_CLI_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv(REVIEW_ENV, "marker")
    assert main() == REVIEW_EXIT
    marker, review = capsys.readouterr().out.strip().splitlines()
    preview = json.loads(review)
    assert marker == "marker" and preview["rights"] is True and not preview["reads"]
    snapshot = preview["profile"]
    assert snapshot == {
        "action": "save",
        "name": "custom",
        "base": None,
        "rules": {
            "read": "anywhere",
            "write": "work_area",
            "commands": "any",
            "web": "listed",
            "allow": [],
            **rules,
        },
    }
    assert rpc.methods == ["permissions.profiles"]

    rpc.reply(
        "permissions.save_profile",
        {"profiles": [{"name": "custom", "builtin": False, "rules": rules}]},
    )
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"profile": snapshot, "files": {}}) + "\n" + body)
    )
    monkeypatch.setenv(REVIEWED_INPUTS_ENV, "1")
    monkeypatch.setenv("VBOT_APP_CLI_LISTEN_HOST", "127.0.0.1")
    assert main() == 0
    assert rpc.params("permissions.save_profile") == {
        "name": "custom",
        "permission_rules": rules,
        "base": None,
    }


@pytest.mark.parametrize(
    ("argv", "action", "write", "expected"),
    [
        (
            ["agent", "create", "new", "New", "--permission-profile", "custom"],
            "assign",
            "agent.create",
            "catalog",
        ),
        (
            ["agent", "update", "old", "--permission-profile", "custom"],
            "assign",
            "agent.update",
            "catalog",
        ),
        (["agent", "update", "old", "--clear-permissions"], "inherit", "agent.update", "inherited"),
        (
            ["agent", "update", "old", "--permissions", "full-access"],
            "inline",
            "agent.update",
            "inline",
        ),
        (
            ["project", "override", "set", "p", "a", "permission_profile", "custom"],
            "assign",
            "project.set_override",
            "catalog",
        ),
        (
            ["project", "override", "clear", "p", "a", "permission_profile"],
            "inherit",
            "project.clear_override",
            "inherited",
        ),
        (
            ["project", "override", "clear", "p", "unbound", "permission_profile"],
            "inherit",
            "project.clear_override",
            "current",
        ),
        (
            ["project", "override", "clear", "p", "a", "permission_rules"],
            "inherit",
            "project.clear_override",
            "current",
        ),
        (
            ["project", "override", "set", "p", "a", "permission_rules", "work-dir"],
            "inline",
            "project.set_override",
            "inline",
        ),
        (
            ["permission-profiles", "delete", "custom"],
            "delete",
            "permissions.delete_profile",
            "catalog",
        ),
    ],
)
def test_bound_profile_change_writes_with_approved_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rpc: FakeRpc,
    argv: list[str],
    action: str,
    write: str,
    expected: str,
) -> None:
    values: dict[str, dict[str, object] | None] = {
        "catalog": {"read": "work_area"},
        "current": {"web": "listed"},
        "inherited": None,
        "inline": {"write": "scratch"},
    }
    rpc.reply(
        "permissions.profiles",
        {
            "profiles": [
                {"name": "custom", "builtin": False, "rules": values["catalog"]},
            ]
        },
    )
    rpc.reply(
        "agent.get",
        {
            "permission_rules_default": values["inherited"],
            "effective": {"permission_rules": {"value": values["current"]}},
        },
    )
    member = {
        "permission_rules_default": values["inherited"],
        "effective": {"permission_rules": {"value": values["current"]}},
    }
    rpc.reply(
        "project.show",
        {
            "scan": {
                "team": [
                    {**member, "agent_id": "a", "overrides": {"permission_profile": "custom"}},
                    {**member, "agent_id": "unbound", "overrides": None},
                ]
            }
        },
    )
    rpc.reply("permissions.validate", {"valid": True, "effective": values["inline"]})
    rpc.reply(write, {"profiles": []} if action == "delete" else {})
    monkeypatch.setenv("VBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VBOT_SERVER_PORT", "22000")
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "manager")
    monkeypatch.setattr(sys, "argv", ["vbot", *argv])
    monkeypatch.setenv("VBOT_APP_CLI_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv(REVIEW_ENV, "marker")
    assert main() == REVIEW_EXIT
    _, message = capsys.readouterr().out.strip().splitlines()
    snapshot = json.loads(message)["profile"]
    key = "base" if action == "delete" else "rules"
    target_rules = values[expected]
    if expected in {"current", "inline"}:
        assert target_rules is not None
        target_rules = {
            "read": "anywhere",
            "write": "anywhere",
            "commands": "any",
            "web": "any",
            "allow": [],
            "deny": [],
            **target_rules,
        }
    assert snapshot["action"] == action and snapshot[key] == target_rules
    assert write not in rpc.methods
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"profile": snapshot, "files": {}}) + "\n")
    )
    monkeypatch.setenv(REVIEWED_INPUTS_ENV, "1")
    monkeypatch.setenv("VBOT_APP_CLI_LISTEN_HOST", "127.0.0.1")
    assert main() == 0
    assert (
        rpc.params(write)["base" if action == "delete" else "expected_profile_rules"]
        == target_rules
    )


@pytest.mark.asyncio
async def test_app_agent_runs_own_cli_with_run_identity(tmp_path: Path) -> None:
    # The real process boundary covers interpreter selection, literal argv, stdio and Run identity.
    data_dir = tmp_path / "session & settings"
    run = RunIdentity("app-assistant", "local-cli-test", None)
    home = await run_vbot_cli(["home"], data_dir=data_dir, stdin=None, port=22000, run=run)
    assert home["exit_code"] == 0 and str(data_dir) in home["stdout"]
    refused = await run_vbot_cli(
        ["devices", "approve", "test-only", "--yes"],
        data_dir=data_dir,
        stdin=None,
        port=22000,
        run=run,
    )
    assert refused["exit_code"] and "does not run inside an Agent's Run" in refused["stdout"]
    with pytest.raises(VBotAccessReviewRequiredError) as pending:
        await run_vbot_cli(
            ["agent", "update", "other", "--allowed-skills"],
            data_dir=data_dir,
            stdin=None,
            port=22000,
            run=run,
            check_agent_access=True,
        )
    assert pending.value.rights and not pending.value.reads
    source = tmp_path / "transcripts"
    with pytest.raises(VBotAccessReviewRequiredError) as pending:
        await run_vbot_cli(
            ["external", "list", "codex", "--path", str(source)],
            data_dir=data_dir,
            stdin=None,
            port=22000,
            run=run,
            check_agent_access=True,
        )
    assert not pending.value.rights and pending.value.reads == ((str(source), True),)
