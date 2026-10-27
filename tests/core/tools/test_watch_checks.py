"""Tests for Cron watch checks: a command in the host shell, or a GET of a URL."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import curl_cffi.requests
import httpx
import pytest

from core.automation.cron import CronWatch, WatchCheckError
from core.permissions import BoundaryContext, PermissionBoundary, PermissionCeiling
from core.sessions import AGENT_DEFAULT_PROJECT, SessionAddress, WorkingProjectChoice
from core.tools import _public_http as public_http
from core.tools import shell_environment, watch_checks
from core.tools._powershell import PowerShellRequest
from core.tools.shell import HostShellStart
from core.tools.shell_environment import CommandEnvironment
from core.tools.watch_checks import COMMAND_OUTPUT_MAX_BYTES, URL_MAX_BYTES, WatchCheckRunner
from tests.core.permissions.command_check_support import (
    FakeAnalyzer,
    fake_checker,
    plan,
    restricted,
)
from tests.core.tools.command_manager_helpers import FakeProcess
from tests.core.tools.web_fetch_test_support import StreamingResponse, StreamingSession

_NOT_RUN = 990


class _Shell(FakeProcess):
    """A host shell the test scripts; its kill only records that the check ended it."""

    def __init__(self) -> None:
        super().__init__(pid=4242)
        self.requests: list[bytes] = []
        self.killed = False

    def send(self, request: bytes) -> None:
        self.requests.append(request)

    def kill(self) -> None:
        self.killed = True


@dataclass
class _Launches:
    """Starts a scripted shell per check and records how it was started."""

    script: Callable[[_Shell], None]
    calls: list[tuple[Sequence[str], Path, bool]] = field(default_factory=list)
    shells: list[_Shell] = field(default_factory=list)

    async def __call__(
        self, argv: Sequence[str], cwd: Path, _env: Any, _tracker: Any, *, request_pipe: bool
    ) -> _Shell:
        shell = _Shell()
        self.calls.append((argv, cwd, request_pipe))
        self.shells.append(shell)
        self.script(shell)
        return shell


def _runner(
    tmp_path: Path,
    launches: _Launches | None = None,
    *,
    default_project: str | None = None,
    **kwargs: Any,
) -> WatchCheckRunner:
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)

    async def agent(_project_id: str | None, agent_id: str, **_kwargs: Any) -> Any:
        if agent_id == "gone":
            raise LookupError("Agent not found: gone")
        return SimpleNamespace(workspace=str(workspace))

    async def working_project(
        project_id: str | None,
        _agent: Any,
        *,
        session_id: str | None,
        requested: WorkingProjectChoice,
    ) -> str | None:
        if session_id == "missing-project":
            raise LookupError("The Session's Working Project is unavailable")
        if project_id is not None:
            assert requested is AGENT_DEFAULT_PROJECT
            return project_id
        if session_id == "pinned":
            return "project-a"
        return default_project if requested is AGENT_DEFAULT_PROJECT else requested

    async def session(address: SessionAddress) -> Any:
        if address.session_id == "missing-session":
            raise LookupError("Session is unavailable")
        return SimpleNamespace(address=address)

    return WatchCheckRunner(
        agent_resolver=cast(
            Any,
            SimpleNamespace(
                resolve_agent_async=agent, resolve_working_project_async=working_project
            ),
        ),
        sessions=cast(Any, SimpleNamespace(get_async=session)),
        projects=SimpleNamespace(get=lambda _project_id: SimpleNamespace(cwd=str(project_dir))),
        data_root=tmp_path / "data",
        launch=cast(Any, launches if launches is not None else _Launches(lambda _shell: None)),
        tracker=cast(Any, None),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _host_shell(monkeypatch: pytest.MonkeyPatch) -> list[CommandEnvironment]:
    """A PowerShell-like host shell that takes its command as a request.

    Records the environment each check's shell starts with.
    """
    environments: list[CommandEnvironment] = []

    def start(command: str, workdir: Path, environment: CommandEnvironment) -> HostShellStart:
        environments.append(environment)
        request = PowerShellRequest(
            f"{workdir}|{command}".encode(),
            {_NOT_RUN: "request_unreadable", _NOT_RUN + 1: "workdir_unavailable"},
        )
        return HostShellStart(["pwsh", "-Command", "bootstrap"], {}, request)

    monkeypatch.setattr(watch_checks, "host_shell_start", start)
    return environments


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project_id", "working_project_id", "session_id", "default_project", "directory"),
    [
        pytest.param("project-a", AGENT_DEFAULT_PROJECT, None, None, "project", id="project"),
        pytest.param(None, AGENT_DEFAULT_PROJECT, None, None, "workspace", id="agent"),
        pytest.param(None, "project-a", None, None, "project", id="working-project"),
        pytest.param(None, None, None, "project-a", "workspace", id="explicit-workspace"),
        pytest.param(None, AGENT_DEFAULT_PROJECT, "pinned", None, "project", id="pinned"),
        pytest.param(None, AGENT_DEFAULT_PROJECT, None, "project-a", "project", id="default"),
    ],
)
async def test_a_check_command_runs_in_the_jobs_directory_and_keeps_the_start_of_its_output(
    tmp_path: Path,
    project_id: str | None,
    working_project_id: WorkingProjectChoice,
    session_id: str | None,
    default_project: str | None,
    directory: str,
) -> None:
    def script(shell: _Shell) -> None:
        shell.write("x" * (COMMAND_OUTPUT_MAX_BYTES - 2) + "äö")
        shell.write("dropped")
        shell.exit(0)

    launches = _Launches(script)

    output = await _runner(tmp_path, launches, default_project=default_project).run_command(
        "Get-Content status.txt",
        agent_id="agent-one",
        project_id=project_id,
        working_project_id=working_project_id,
        session_id=session_id,
    )

    assert output == "x" * (COMMAND_OUTPUT_MAX_BYTES - 2) + "ä"
    ((_argv, cwd, request_pipe),) = launches.calls
    assert (cwd, request_pipe) == (tmp_path / directory, True)
    (shell,) = launches.shells
    assert shell.requests == [f"{tmp_path / directory}|Get-Content status.txt".encode()]
    assert shell.killed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "session_id", "exit_code", "output", "problem"),
    [
        pytest.param(
            "agent-one", None, 3, "fetching\nboom  \n\n", "exited with code 3: boom", id="exit-code"
        ),
        pytest.param(
            "agent-one",
            None,
            _NOT_RUN + 1,
            "",
            "did not run: PowerShell could not enter",
            id="not-run",
        ),
        pytest.param("agent-one", None, None, "", "timed out after", id="timeout"),
        pytest.param(
            "gone", None, 0, "", "did not run: Agent gone is not available", id="no-agent"
        ),
        pytest.param(
            "agent-one", "missing-project", 0, "", "Working Project is unavailable", id="no-project"
        ),
        pytest.param(
            "agent-one", "missing-session", 0, "", "Session is unavailable", id="no-session"
        ),
    ],
)
async def test_a_check_command_fails_with_what_went_wrong(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_id: str,
    session_id: str | None,
    exit_code: int | None,
    output: str,
    problem: str,
) -> None:
    monkeypatch.setattr(watch_checks, "COMMAND_TIMEOUT_SECONDS", 0.01)

    def script(shell: _Shell) -> None:
        shell.write(output)
        if exit_code is not None:
            shell.exit(exit_code)

    launches = _Launches(script)

    with pytest.raises(WatchCheckError, match=problem):
        await _runner(tmp_path, launches).run_command(
            "Get-Content status.txt", agent_id=agent_id, project_id=None, session_id=session_id
        )

    assert all(shell.killed for shell in launches.shells)


def _page(status: int, content_type: str, body: bytes) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": content_type}, content=body)

    return httpx.MockTransport(answer)


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


@pytest.mark.asyncio
async def test_a_watched_url_answers_with_its_main_text(tmp_path: Path) -> None:
    html = (
        b"<html><head><title>Status</title></head><body><nav><a href='/'>Home</a></nav>"
        b"<main><h1>Service status</h1><p>All systems are up since this morning.</p></main>"
        b"<footer>Copyright</footer></body></html>"
    )
    page = _runner(tmp_path, transport=_page(200, "text/html; charset=utf-8", html))
    data = _runner(tmp_path, transport=_page(200, "application/json", b'{"ok": true}'))
    # The 1 MB cut splits the last character; the rest still decodes as UTF-8.
    log = "x" + "ä" * (URL_MAX_BYTES // 2)
    long = _runner(tmp_path, transport=_page(200, "application/json", log.encode()))

    assert await page.fetch_url("https://status.example/") == (
        "# Service status\n\nAll systems are up since this morning."
    )
    assert await data.fetch_url("https://status.example/api") == '{"ok": true}'
    assert await long.fetch_url("https://status.example/log") == log[:-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport", "problem"),
    [
        pytest.param(_page(503, "text/plain", b"down"), "answered HTTP 503", id="status"),
        pytest.param(
            _page(200, "image/png", b"\x89PNG\r\n\x1a\n\x00"),
            "answered with image/png, not text",
            id="binary",
        ),
        pytest.param(
            httpx.MockTransport(_refuse),
            "could not be fetched: refused",
            id="unreachable",
        ),
    ],
)
async def test_a_watched_url_fails_with_what_went_wrong(
    tmp_path: Path, transport: httpx.MockTransport, problem: str
) -> None:
    with pytest.raises(WatchCheckError, match=problem):
        await _runner(tmp_path, transport=transport).fetch_url("https://status.example/")


@pytest.mark.asyncio
async def test_a_context_command_returns_its_output_with_any_exit_code_within_the_session_rules(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    def script(shell: _Shell) -> None:
        shell.write("M core/app.py\n")
        shell.exit(1)

    launches = _Launches(script)
    analyzer = FakeAnalyzer(plan("Remove-Item", subtree_writes=[tmp_path / "elsewhere"]))
    runner = _runner(tmp_path, launches, command_checker=fake_checker(analyzer))
    open_boundary = PermissionBoundary.build(
        None, BoundaryContext(workdir=project, home=project, data_dir=tmp_path / "data")
    )

    # A non-zero exit is the command's answer, not a failure.
    assert await runner.run_context_command(
        "git status --short", workdir=project, boundary=open_boundary
    ) == (1, "M core/app.py\n")
    assert launches.calls[0][1] == project

    # A restricted Session's command is checked as its shell Tool checks it.
    with pytest.raises(WatchCheckError, match="The command was not run"):
        await runner.run_context_command(
            "Remove-Item -Recurse ../elsewhere", workdir=project, boundary=restricted(project)
        )
    with pytest.raises(WatchCheckError, match="does not exist"):
        await runner.run_context_command(
            "git status", workdir=tmp_path / "missing", boundary=open_boundary
        )
    assert len(launches.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address_project", ["project-a", None], ids=["project-agent", "identity-agent"]
)
async def test_a_job_with_a_permission_ceiling_runs_only_checks_its_rules_allow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _host_shell: list[CommandEnvironment],
    address_project: str | None,
) -> None:
    monkeypatch.setenv("VBOT_DATA_DIR", str(tmp_path / "data"))
    login = shell_environment._login_environment
    server = {"OPENAI_API_KEY": "sk-server", "FLAVOR": "release"}
    monkeypatch.setattr(shell_environment, "_login_environment", lambda: {**login(), **server})
    project = tmp_path / "project"
    analyzer = FakeAnalyzer(plan("Remove-Item", subtree_writes=[tmp_path / "elsewhere"]))
    launches = _Launches(lambda shell: shell.exit(0))
    ceiling = restricted(project, web="listed", allow=["Web(example.com)"]).ceiling(source="cron")
    assert ceiling is not None

    def boundary(
        stored: PermissionCeiling,
        agent_id: str | None,
        project_id: str | None,
        working_project_id: str | None,
    ) -> PermissionBoundary:
        # A command check names the job's Agent and Project; a URL check neither.
        assert (agent_id, project_id) in {("agent-one", address_project), (None, None)}
        assert working_project_id == ("project-a" if agent_id is not None else None)
        workdir = project if working_project_id is not None else tmp_path / "data"
        context = BoundaryContext(workdir=workdir, home=project, data_dir=tmp_path / "data")
        return PermissionBoundary.build(None, context, ceilings=(stored,))

    runner = _runner(
        tmp_path,
        launches,
        command_checker=fake_checker(analyzer),
        ceiling_boundary=boundary,
        credential_names=lambda: frozenset({"OPENAI_API_KEY"}),
    )

    # Worded for the user's alert as for the Agent: no "you", and how to fix the watch.
    refused = "The check did not run: the permission rules refuse .* Change the watch to a command"
    with pytest.raises(WatchCheckError, match=refused):
        await runner.run_command(
            "Remove-Item -Recurse ../elsewhere",
            agent_id="agent-one",
            project_id=address_project,
            working_project_id="project-a",
            ceiling=ceiling,
        )
    refused_url = await runner.permission_refusal(
        CronWatch.parse("url", "https://intranet.example/"),
        agent_id="agent-one",
        project_id=address_project,
        working_project_id="project-a",
        ceiling=ceiling,
    )
    with pytest.raises(WatchCheckError, match="refuse fetching intranet.example"):
        await runner.fetch_url("https://intranet.example/", ceiling=ceiling)

    assert launches.calls == []
    assert refused_url is not None and refused_url.target == "intranet.example"
    # Checked in the job's directory with the confined environment, then run in it:
    # without vBot's control variables and the credentials vBot knows.
    analyzer.result = plan("Get-Content", reads=[project / "status.txt"])
    output = await runner.run_command(
        "Get-Content status.txt",
        agent_id="agent-one",
        project_id=address_project,
        working_project_id="project-a",
        ceiling=ceiling,
    )
    assert output == ""
    line, _shell, cwd, environment = analyzer.lines[-1]
    assert (line, cwd) == ("Get-Content status.txt", project)
    started = _host_shell[-1].complete()
    for confined in (environment, started):
        assert not {"VBOT_DATA_DIR", "OPENAI_API_KEY"} & set(confined)
        assert confined["FLAVOR"] == "release"
    assert len(launches.calls) == 1

    # An allowed URL's longer body is cut at 1 MB, as without a ceiling, never refused,
    # whatever size it declares.
    async def public_address(_host: str, _port: int) -> list[object]:
        return [ipaddress.ip_address("93.184.216.34")]

    page = StreamingResponse(
        [b"a" * (URL_MAX_BYTES - 1), b"bc", b"d" * URL_MAX_BYTES],
        headers={
            "Content-Type": "text/plain",
            "Content-Length": str(public_http.MAX_RESPONSE_BYTES + 1),
        },
    )
    monkeypatch.setattr(public_http, "_resolve_host_addresses", public_address)
    monkeypatch.setattr(curl_cffi.requests, "AsyncSession", lambda **_: StreamingSession(page))
    text = await runner.fetch_url("https://example.com/status", ceiling=ceiling)
    assert text == "a" * (URL_MAX_BYTES - 1) + "b"
