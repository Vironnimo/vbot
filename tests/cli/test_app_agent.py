"""The App Agent uses the full local CLI, with the owning server as its fixed target."""

from pathlib import Path

import pytest

from cli._server_target import resolve_instance
from cli.app_agent import bind_target, changes_agent_access
from cli.parser import parse_args
from core.tools.shell_environment import RunIdentity
from core.tools.vbot_cli_runner import VBotAccessReviewRequiredError, run_vbot_cli


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
        (["agent", "create", "new", "New", "--allow-rule", "Read(/docs)"], True),
        (["agent", "update", "other", "--allowed-skills"], True),
        (["agent", "update", "other", "--subagent-allow", "helper"], True),
        (["project", "clear-override", "proj", "member", "tool_access"], True),
        (["project", "set-override", "proj", "member", "permission_rules", "work-dir"], True),
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
            ["agent", "update", "other", "--permissions", "full-access"],
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
