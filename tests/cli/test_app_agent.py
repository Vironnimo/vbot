"""The App Agent uses the full local CLI, with the owning server as its fixed target."""

import io
import json
import sys
from pathlib import Path

import pytest

from cli import main as cli_main
from cli._server_target import CommandResult, resolve_instance
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
        (["archive", "show", "arc_test"], False),
    ],
)
def test_agent_access_review_follows_the_parsed_operation(
    argv: list[str], review: bool, tmp_path: Path
) -> None:
    target = resolve_instance(host="127.0.0.1", port=22000, data_dir=tmp_path)
    assert changes_agent_access(parse_args(argv), target) is review


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
            ["project", "override", "clear", "p", "broken", "permission_profile"],
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
        "inherited": {"commands": "listed"},
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
    member = {
        "effective": {"permission_rules": {"value": values["current"]}},
    }
    rpc.reply(
        "project.show",
        {
            "scan": {
                "team": [
                    {**member, "agent_id": "a", "overrides": {"permission_profile": "custom"}},
                    {**member, "agent_id": "unbound", "overrides": None},
                    {
                        "agent_id": "broken",
                        "overrides": {"permission_profile": "missing"},
                        "effective": {"permission_rules": {"value": None, "error": "missing"}},
                    },
                ]
            }
        },
    )
    rpc.reply("permissions.validate", {"valid": True, "effective": values[expected]})
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
    if expected == "inherited":
        assert rpc.params("permissions.validate") == {
            "agent_id": snapshot["target"],
            "permission_rules": None,
        }
    if expected in {"current", "inline", "inherited"}:
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


@pytest.fixture
def restricted_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # The entrypoint normally runs in its own process.
    for name in (
        "VBOT_DATA_DIR",
        "VBOT_SERVER_PORT",
        "VBOT_SERVER_URL",
        "VBOT_RUN_AGENT_ID",
        "VBOT_RUN_SESSION_ID",
        "VBOT_RUN_PROJECT_ID",
        "VBOT_UPDATE_HANDOFF",
    ):
        monkeypatch.setenv(name, "")
    scope = {
        "agent_id": "coder",
        "session_id": "shell",
        "token": "call",
        "data_dir": str(tmp_path),
        "host": "127.0.0.1",
        "port": 22000,
    }

    def invoke(*argv: str) -> int:
        monkeypatch.setenv("VBOT_SHELL_CLI", json.dumps(scope))
        with pytest.raises(SystemExit) as done:
            cli_main.main(argv)
        assert isinstance(done.value.code, int)
        return done.value.code

    return invoke


@pytest.mark.parametrize("source", ["--stdin", "--file"])
def test_shell_cli_reviews_and_writes_the_same_profile_input(
    restricted_cli, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rpc: FakeRpc, source: str
) -> None:
    rules = {"deny": ["Read(~/Verträge/**)"]}
    body = json.dumps(rules)
    path = tmp_path / "permissions file.json"
    path.write_text(body, encoding="utf-16")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    files = {str(path): str(path.resolve())} if source == "--file" else {}
    rpc.reply("permissions.review_cli", {"files": files})
    rpc.reply("permissions.review_cli", {"files": {}})
    rpc.reply("permissions.profiles", {"profiles": []})
    if source == "--file":
        # The file changes while the user reviews the first version.
        import cli._shell_cli as shell_cli

        call = shell_cli.rpc_call

        def decide(instance, method, params):
            if params["rights"]:
                path.write_text("{}", encoding="utf-8")
            return call(instance, method, params)

        monkeypatch.setattr(shell_cli, "rpc_call", decide)
    rpc.reply(
        "permissions.save_profile",
        {"profiles": [{"name": "test", "rules": rules, "builtin": False}]},
    )
    argv = ["permission-profiles", "save", "test", source, *([path.name] if files else [])]
    assert restricted_cli(*argv) == 0
    assert rpc.methods == [
        "permissions.review_cli",
        "permissions.profiles",
        "permissions.review_cli",
        "permissions.save_profile",
    ]
    assert rpc.calls[0][1]["reads"] == ([(str(path), False)] if files else [])
    preview = rpc.calls[2][1]
    assert preview["argv"] == argv and preview["rights"] is True
    assert preview["profile"]["rules"]["deny"] == rules["deny"]
    assert rpc.params("permissions.save_profile") == {
        "name": "test",
        "base": None,
        "permission_rules": rules,
    }


@pytest.mark.parametrize("approved", [True, False])
def test_shell_cli_reviews_the_actual_stdin_before_a_shared_permissions_change(
    restricted_cli, monkeypatch: pytest.MonkeyPatch, rpc: FakeRpc, approved: bool
) -> None:
    path = "defaults.agent.permission_rules"
    rules = {"read": "work_area"}
    stdin = json.dumps(rules)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rpc.reply("permissions.review_cli", {"files": {}})
    if approved:
        rpc.reply("permissions.review_cli", {"files": {}})
        rpc.reply("settings.patch", {"changes": [], "changed": []})
    else:
        rpc.fail("permissions.review_cli", "permission_denied", "The change was declined.")
    code = restricted_cli("config", "set", path, "--stdin")
    assert (code == 0) == approved
    assert rpc.calls[1][1]["stdin"] == stdin and rpc.calls[1][1]["rights"] is True
    if approved:
        assert rpc.params("settings.patch") == {
            "operations": [{"op": "set", "path": path, "value": rules}]
        }
    else:
        assert "settings.patch" not in rpc.methods


def test_shell_cli_refuses_an_expired_call_before_an_ordinary_command(
    restricted_cli, rpc: FakeRpc
) -> None:
    rpc.fail("permissions.review_cli", "permission_denied", "This shell command has ended.")
    assert restricted_cli("agent", "list") != 0
    assert rpc.methods == ["permissions.review_cli"]


@pytest.mark.parametrize(
    ("kind", "session_access", "review"),
    [
        ("agent", False, True),
        ("project", False, True),
        ("session", True, True),
        ("session", None, True),
        ("session", False, False),
        ("owner_group", False, False),
        ("files", False, False),
    ],
)
def test_restricted_archive_restore_checks_the_stored_entry_before_mutation(
    restricted_cli, rpc: FakeRpc, kind: str, session_access: bool | None, review: bool
) -> None:
    rpc.reply("permissions.review_cli", {"files": {}})
    rpc.reply(
        "archive.show",
        {
            "entry": {"kind": kind},
            **({"restores_access": session_access} if session_access is not None else {}),
        },
    )
    if review:
        rpc.fail("permissions.review_cli", "permission_denied", "The change was declined.")
    else:
        rpc.fail("archive.restore", "archive_restore_conflict", "The entry cannot be restored.")
    assert restricted_cli("archive", "restore", "arc_test", "--as", "other") != 0
    assert rpc.params("archive.show") == {"entry_id": "arc_test", "session_limit": 1}
    assert rpc.methods == [
        "permissions.review_cli",
        "archive.show",
        "permissions.review_cli" if review else "archive.restore",
    ]
    if review:
        assert rpc.calls[-1][1]["rights"] is True


@pytest.mark.parametrize("approved", [True, False])
def test_shell_cli_reviews_remote_skill_source_before_install(
    restricted_cli, rpc: FakeRpc, approved: bool
) -> None:
    url = "https://github.com/example/skill"
    rpc.reply("permissions.review_cli", {"files": {}})
    if approved:
        rpc.reply("skill.install_cli", {"operation": "installed", "name": "example"})
    else:
        rpc.fail("skill.install_cli", "permission_denied", "Access to this URL was declined.")
    assert (restricted_cli("skill", "install", url, "--scope", "own") == 0) is approved
    assert rpc.methods == ["permissions.review_cli", "skill.install_cli"]
    params = rpc.params("skill.install_cli")
    assert (params["token"], params["source"], params["scope"]) == ("call", url, "agent:coder")


@pytest.mark.parametrize(
    ("argv", "review"),
    [
        (["snapshot", "verify", "copy"], False),
        (["snapshot", "restore", "copy", "--documents"], False),
        (["snapshot", "restore", "copy", "--yes"], True),
        (["snapshot", "restore", "copy", "--all", "--yes"], True),
        (["snapshot", "restore", "copy", "--documents", "--yes"], True),
        (["snapshot", "restore", "copy", "--database", "sessions", "--yes"], True),
        (["snapshot", "restore", "copy", "--database", "model_usage", "--yes"], False),
        (["config-backup", "show", "copy"], False),
        (["config-backup", "restore", "copy", "--all", "--yes"], True),
        (["config-backup", "restore", "copy", "--file", "settings.json", "--yes"], True),
        (["config-backup", "restore", "copy", "--file", r"agents\me\agent.json", "--yes"], True),
        (["config-backup", "restore", "copy", "--file", "projects/x/project.json", "--yes"], True),
        (["config-backup", "restore", "copy", "--file", "calendar/events.json", "--yes"], False),
    ],
)
def test_restricted_data_restore_reviews_rights_before_maintenance(
    restricted_cli, rpc: FakeRpc, monkeypatch: pytest.MonkeyPatch, argv: list[str], review: bool
) -> None:
    dispatched = []

    def fake_maintenance(args, instance):
        dispatched.append(args.command)
        return CommandResult(ok=True, message="maintenance completed", instance=instance)

    monkeypatch.setattr("cli._dispatch_agents.dispatch_data_store_command", fake_maintenance)
    rpc.reply("permissions.review_cli", {"files": {}})
    rpc.fail("permissions.review_cli", "permission_denied", "The restore was declined.")
    assert (restricted_cli("data-store", *argv) != 0) is review
    assert dispatched == ([] if review else [argv[0]])
    assert [params["rights"] for _, params in rpc.calls] == ([False, True] if review else [False])
