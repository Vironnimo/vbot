"""The shell Tool under Permission Rules: refusals launch nothing; a restricted shell is confined.

The command analysis is faked (``command_check_support``); the boundary is real.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

import core.tools.shell_environment as shell_environment
from core.permissions.commands.commands import UnresolvedElement
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.update_handoff import UpdateHandoffs
from tests.core.permissions.command_check_support import (
    PORT,
    UNAVAILABLE,
    FakeAnalyzer,
    plan,
    restricted,
)
from tests.core.tools.shell_test_support import (
    Shell,
    data,
    launched,
    started_shell,
)


@pytest_asyncio.fixture
async def shell(tmp_path: Path) -> AsyncIterator[Shell]:
    async with started_shell(tmp_path) as harness:
        yield harness


@pytest.mark.parametrize(
    ("rules", "analysis", "fragment"),
    [
        pytest.param(
            {"deny": ["Shell(git push *)"]},
            plan("git push origin main"),
            "the rule Shell(git push *) denies it",
            id="denied-command",
        ),
        pytest.param(
            {"commands": "listed", "allow": ["Shell(git status)"]},
            plan("git status", "rm notes.txt"),
            'running "rm notes.txt", because only commands that match an allow rule can run',
            id="unlisted-command",
        ),
        pytest.param(
            {},
            plan("cp a b", writes=[Path("/elsewhere/b")]),
            "outside the folders you can change",
            id="write-outside",
        ),
        pytest.param(
            {"deny": ["Read({workdir}/secrets/**)"]},
            plan("cat secrets/key", reads=[Path("secrets/key")]),
            "denies it",
            id="denied-read",
        ),
        pytest.param(
            {"deny": ["Read({workdir}/secrets/**)"]},
            plan("rm -r .", subtree_writes=[Path(".")]),
            "/secrets, which the rule Read(",
            id="subtree-write-over-denied-folder",
        ),
        pytest.param(
            {"deny": ["Read({workdir}/secrets/**)"]},
            plan("rg x", subtree_reads=[Path(".")]),
            "/secrets/**) denies. Use search_files, "
            "which leaves denied folders out, or name only the folders the command needs.",
            id="recursive-read-over-denied-folder",
        ),
        pytest.param(
            {"web": "listed", "allow": ["Web(example.com)"]},
            plan("ssh git.example.org", hosts=["git.example.org"]),
            "the permission rules refuse connecting to git.example.org",
            id="unlisted-host",
        ),
        pytest.param(
            {},
            plan(
                "cd",
                unresolved=[UnresolvedElement("cwd", "$d", "the folder is computed when it runs")],
            ),
            "Change into folders by their literal path, or pass the folder as workdir.",
            id="unresolved-folder",
        ),
        pytest.param(
            {},
            plan(
                "iwr",
                unresolved=[UnresolvedElement("url", "$u", "the address is computed when it runs")],
            ),
            "so the check cannot tell which address it reaches. "
            "Write each address literally in the command.",
            id="unresolved-address",
        ),
        pytest.param({}, plan("vbot agent list"), "vBot's own command line", id="vbot-cli"),
        pytest.param(
            {},
            plan("curl", urls=[f"http://127.0.0.1:{PORT}/api/agents"]),
            "it is the vBot server itself",
            id="loopback-url",
        ),
        pytest.param(
            {},
            plan(f"nc localhost {PORT}"),
            "it is the vBot server itself",
            id="loopback-host-and-port",
        ),
        pytest.param(
            {},
            plan(
                "cat",
                unresolved=[
                    UnresolvedElement("path_read", "$f", "the path is computed when it runs")
                ],
            ),
            "the path is computed when it runs ($f), so the check cannot tell what it reads. "
            "Write each path literally in the command.",
            id="unresolved",
        ),
        pytest.param(
            {}, UNAVAILABLE, "vBot could not analyze it to check it", id="analysis-unavailable"
        ),
    ],
)
@pytest.mark.asyncio
async def test_restricted_calls_the_rules_refuse_launch_nothing(
    tmp_path: Path, rules: dict[str, object], analysis: object, fragment: str
) -> None:
    analyzer = FakeAnalyzer()
    analyzer.result = analysis  # type: ignore[assignment]
    async with started_shell(
        tmp_path, analyzer=analyzer, permissions=restricted(tmp_path, **rules)
    ) as shell:
        result = await shell.call({"command": "the command", "run_in_background": True})

    assert result["error"]["code"] == "permission_denied", result
    # Only a failed analysis can pass when the same call is sent again.
    assert result["error"]["retryable"] is (analysis is UNAVAILABLE)
    assert fragment in result["error"]["message"]
    assert result["error"]["message"].startswith(f"{SHELL_MODEL_NAME} was not run: ")
    assert shell.launcher.argv == []
    # The analysis saw the call's command in its working directory.
    assert [(line, cwd) for line, _shell, cwd, _env in analyzer.lines] == [
        ("the command", tmp_path)
    ]


@pytest.mark.asyncio
async def test_a_restricted_shell_starts_only_in_its_area_without_vbot_control_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VBOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VBOT_SERVER_PORT", str(PORT))
    # The server's environment carries credentials vBot knows and a variable of the user's.
    login = shell_environment._login_environment
    server = {"OPENAI_API_KEY": "sk-server", "API_TOKEN": "server-token", "FLAVOR": "release"}
    monkeypatch.setattr(shell_environment, "_login_environment", lambda: {**login(), **server})
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    handoffs = UpdateHandoffs(tmp_path / "data")
    analyzer = FakeAnalyzer(plan("build"))
    async with started_shell(
        tmp_path,
        analyzer=analyzer,
        permissions=restricted(tmp_path),
        update_handoffs=handoffs,
        credential_names=frozenset({"OPENAI_API_KEY", "API_TOKEN"}),
    ) as shell:
        refused = await shell.call({"command": "build", "workdir": str(outside)})
        # Not granted, so not inherited either: the call says so instead of running without it.
        ungranted = await shell.returned(
            shell.call({"command": "build", "env_keys": ["OPENAI_API_KEY"]})
        )
        call = shell.call({"command": "build", "env": {"MODE": "ci"}, "env_keys": ["API_TOKEN"]})
        (await shell.started()).exit(0)
        assert data(await shell.returned(call))["status"] == "exited"

    assert refused["error"]["code"] == "permission_denied"
    assert "working in" in refused["error"]["message"]
    assert ungranted["error"]["code"] == "invalid_arguments"
    assert len(shell.launcher.processes) == 1
    command, env = launched(shell.launcher)
    assert (command, env["MODE"], env["FLAVOR"]) == ("build", "ci", "release")
    # A granted credential arrives as resolved for the call; the other one not at all.
    assert env["API_TOKEN"] == "value-of-API_TOKEN"
    assert not {
        "OPENAI_API_KEY",
        "VBOT_DATA_DIR",
        "VBOT_SERVER_PORT",
        "VBOT_RUN_AGENT_ID",
        "VBOT_RUN_SESSION_ID",
        "VBOT_RUN_PROJECT_ID",
        "VBOT_UPDATE_HANDOFF",
    } & set(env)
    # The analysis expanded the call's own variables, in the same confined environment.
    assert analyzer.lines[-1][3]["MODE"] == "ci"
    assert not {"VBOT_DATA_DIR", "OPENAI_API_KEY"} & set(analyzer.lines[-1][3])


@pytest.mark.asyncio
async def test_full_access_runs_commands_without_analyzing_them(shell: Shell) -> None:
    call = shell.call({"command": "vbot agent list"})
    (await shell.started()).exit(0)
    assert data(await shell.returned(call))["status"] == "exited"

    assert shell.checker.analyzer_started is False
