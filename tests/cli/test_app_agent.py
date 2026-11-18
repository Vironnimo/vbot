"""The App Agent uses the full local CLI, with the owning server as its fixed target."""

from pathlib import Path

import pytest

from cli._server_target import resolve_instance
from cli.app_agent import bind_target
from cli.parser import parse_args
from core.tools.shell_environment import RunIdentity
from core.tools.vbot_cli_runner import run_vbot_cli


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
