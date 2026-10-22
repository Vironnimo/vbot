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
from core.tools import _public_http as public_http
from core.tools import watch_checks
from core.tools._powershell import PowerShellRequest
from core.tools.shell import HostShellStart
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


def _runner(tmp_path: Path, launches: _Launches | None = None, **kwargs: Any) -> WatchCheckRunner:
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)

    async def agent(agent_id: str) -> Any:
        if agent_id == "gone":
            raise LookupError("Agent not found: gone")
        return SimpleNamespace(workspace=str(workspace))

    return WatchCheckRunner(
        agents=SimpleNamespace(get_async=agent),
        projects=SimpleNamespace(get=lambda _project_id: SimpleNamespace(cwd=str(project_dir))),
        data_root=tmp_path / "data",
        launch=cast(Any, launches if launches is not None else _Launches(lambda _shell: None)),
        tracker=cast(Any, None),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _host_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PowerShell-like host shell that takes its command as a request."""

    def start(command: str, workdir: Path, _environment: Any) -> HostShellStart:
        request = PowerShellRequest(
            f"{workdir}|{command}".encode(),
            {_NOT_RUN: "request_unreadable", _NOT_RUN + 1: "workdir_unavailable"},
        )
        return HostShellStart(["pwsh", "-Command", "bootstrap"], {}, request)

    monkeypatch.setattr(watch_checks, "host_shell_start", start)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project_id", "directory"),
    [
        pytest.param("project-a", "project", id="project"),
        pytest.param(None, "workspace", id="agent"),
    ],
)
async def test_a_check_command_runs_in_the_jobs_directory_and_keeps_the_start_of_its_output(
    tmp_path: Path, project_id: str | None, directory: str
) -> None:
    def script(shell: _Shell) -> None:
        shell.write("x" * (COMMAND_OUTPUT_MAX_BYTES - 2) + "äö")
        shell.write("dropped")
        shell.exit(0)

    launches = _Launches(script)

    output = await _runner(tmp_path, launches).run_command(
        "Get-Content status.txt", agent_id="agent-one", project_id=project_id
    )

    assert output == "x" * (COMMAND_OUTPUT_MAX_BYTES - 2) + "ä"
    ((_argv, cwd, request_pipe),) = launches.calls
    assert (cwd, request_pipe) == (tmp_path / directory, True)
    (shell,) = launches.shells
    assert shell.requests == [f"{tmp_path / directory}|Get-Content status.txt".encode()]
    assert shell.killed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "exit_code", "output", "problem"),
    [
        pytest.param(
            "agent-one", 3, "fetching\nboom  \n\n", "exited with code 3: boom", id="exit-code"
        ),
        pytest.param(
            "agent-one", _NOT_RUN + 1, "", "did not run: PowerShell could not enter", id="not-run"
        ),
        pytest.param("agent-one", None, "", "timed out after", id="timeout"),
        pytest.param("gone", 0, "", "did not run: Agent gone is not available", id="no-agent"),
    ],
)
async def test_a_check_command_fails_with_what_went_wrong(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_id: str,
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
            "Get-Content status.txt", agent_id=agent_id, project_id=None
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

    assert await page.fetch_url("https://status.example/") == (
        "# Service status\n\nAll systems are up since this morning."
    )
    assert await data.fetch_url("https://status.example/api") == '{"ok": true}'


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
async def test_a_job_with_a_permission_ceiling_runs_only_checks_its_rules_allow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VBOT_DATA_DIR", str(tmp_path / "data"))
    project = tmp_path / "project"
    analyzer = FakeAnalyzer(plan("Remove-Item", subtree_writes=[tmp_path / "elsewhere"]))
    launches = _Launches(lambda shell: shell.exit(0))
    ceiling = restricted(project, web="listed", allow=["Web(example.com)"]).ceiling(source="cron")
    assert ceiling is not None

    def boundary(
        stored: PermissionCeiling, agent_id: str | None, project_id: str | None
    ) -> PermissionBoundary:
        # A command check names the job's Agent and Project; a URL check neither.
        assert (agent_id, project_id) in {("agent-one", "project-a"), (None, None)}
        workdir = project if project_id is not None else tmp_path / "data"
        context = BoundaryContext(workdir=workdir, home=project, data_dir=tmp_path / "data")
        return PermissionBoundary.build(None, context, ceilings=(stored,))

    runner = _runner(
        tmp_path, launches, command_checker=fake_checker(analyzer), ceiling_boundary=boundary
    )

    # Worded for the user's alert as for the Agent: no "you", and how to fix the watch.
    refused = "The check did not run: the permission rules refuse .* Change the watch to a command"
    with pytest.raises(WatchCheckError, match=refused):
        await runner.run_command(
            "Remove-Item -Recurse ../elsewhere",
            agent_id="agent-one",
            project_id="project-a",
            ceiling=ceiling,
        )
    refused_url = await runner.permission_refusal(
        CronWatch.parse("url", "https://intranet.example/"),
        agent_id="agent-one",
        project_id="project-a",
        ceiling=ceiling,
    )
    with pytest.raises(WatchCheckError, match="refuse fetching intranet.example"):
        await runner.fetch_url("https://intranet.example/", ceiling=ceiling)

    assert launches.calls == []
    assert refused_url is not None and refused_url.target == "intranet.example"
    # Checked in the job's directory with the confined environment, then run.
    analyzer.result = plan("Get-Content", reads=[project / "status.txt"])
    output = await runner.run_command(
        "Get-Content status.txt", agent_id="agent-one", project_id="project-a", ceiling=ceiling
    )
    assert output == ""
    line, _shell, cwd, environment = analyzer.lines[-1]
    assert (line, cwd) == ("Get-Content status.txt", project)
    assert "VBOT_DATA_DIR" not in environment
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
