"""Cron watch checks: a command in the host shell or a GET of a URL, without a Model.

Cron runs one before each Run of a watching job and compares its output with
the previous one (``core.automation.cron.WatchChecks``). Every failure raises
``WatchCheckError`` with a message that says what failed, without a final period.

Command. The shell Tool's host shell: PowerShell 7 with the shell Tool's
bootstrap on Windows (UTF-8 console, English messages, plain text), bash
elsewhere (``host_shell_start``), started on pipes and contained in its process
tree (``launch_pipe_process``). It runs in the job's working directory: the
Project's directory for a Project Agent, the Agent's Workspace otherwise, with
a command's start environment (``unattended_command_environment``). Its output
is stdout and stderr together, as the shell Tool shows them, decoded as UTF-8,
up to ``COMMAND_OUTPUT_MAX_BYTES``; the rest is read and dropped. A non-zero
exit, a shell that cannot run it and a timeout after
``COMMAND_TIMEOUT_SECONDS`` fail the check. Processes the command leaves
running end with the check.

URL. A GET through the shared TLS context that follows redirects, within
``URL_TIMEOUT_SECONDS``; at most ``URL_MAX_BYTES`` of the body are read. Any
address is allowed, local services included: the check runs what the user's
Agent set up, as its shell commands do. An HTML page is reduced to its main
text as web_fetch extracts it; other text is decoded as a browser would. A
status outside 2xx and content that is not text fail the check.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx

from core.agents import default_workspace_dir
from core.automation.cron import WatchCheckError
from core.tools._command_launch import PipeCommandProcess, launch_pipe_process
from core.tools._public_http import _decoded
from core.tools.process_tree import ProcessTreeTracker, track_process_tree
from core.tools.shell import HostShellMissingError, HostShellStart, host_shell_start
from core.tools.shell_environment import unattended_command_environment
from core.tools.web_fetch import is_html_page, is_textual_content_type, looks_binary
from core.utils.logging import get_logger
from core.utils.tls import shared_ssl_context
from core.utils.workers import BoundedWorkerPool, settle_before_cancelling

COMMAND_TIMEOUT_SECONDS = 60
COMMAND_OUTPUT_MAX_BYTES = 64 * 1024
URL_TIMEOUT_SECONDS = 30
URL_MAX_BYTES = 1024 * 1024

# How long output may still arrive after the shell exited before the check ends
# what the command left running.
_DRAIN_SECONDS = 1.0
# The end of a failed command's output its error shows.
_ERROR_TAIL_CHARS = 300
_HEADERS = {
    "User-Agent": "vBot-Watch",
    "Accept": "text/html, application/json, text/plain;q=0.9, */*;q=0.8",
}
# Shell starts, process kills, Project reads and page extraction.
_WORKERS = BoundedWorkerPool(name="watch-checks", max_workers=2)

_LOGGER = get_logger("tools.watch_checks")

Launch = Callable[..., Awaitable[PipeCommandProcess]]


class _Agents(Protocol):
    async def get_async(self, agent_id: str) -> Any: ...


class _Projects(Protocol):
    def get(self, project_id: str) -> Any: ...


class WatchCheckRunner:
    """Run Cron watch checks for the Agents and Projects of this vBot instance."""

    def __init__(
        self,
        *,
        agents: _Agents,
        projects: _Projects,
        data_root: Path,
        launch: Launch = launch_pipe_process,
        tracker: ProcessTreeTracker = track_process_tree,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._agents = agents
        self._projects = projects
        self._data_root = data_root
        self._launch = launch
        self._tracker = tracker
        self._transport = transport

    async def run_command(self, command: str, *, agent_id: str, project_id: str | None) -> str:
        """Run ``command`` in the job's working directory; its output."""
        workdir = await self._workdir(agent_id, project_id)
        try:
            start = await _WORKERS.run(_shell_start, command, workdir)
        except HostShellMissingError as error:
            raise WatchCheckError(f"The check command did not run: {error}") from error
        try:
            process = await self._launch(
                start.argv,
                workdir,
                start.env,
                self._tracker,
                request_pipe=start.request is not None,
            )
        except OSError as error:
            raise WatchCheckError(f"The check command could not start: {error}") from error
        output = _CappedOutput(COMMAND_OUTPUT_MAX_BYTES)
        reader = asyncio.create_task(_read_into(process, output))
        try:
            if start.request is not None:
                process.send(start.request.data)
            try:
                async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
                    code = await process.wait()
            except TimeoutError:
                raise WatchCheckError(
                    f"The check command timed out after {COMMAND_TIMEOUT_SECONDS} seconds"
                ) from None
            await asyncio.wait({reader}, timeout=_DRAIN_SECONDS)
        finally:
            # Ends what the command left running, and with it the output.
            await settle_before_cancelling(_WORKERS.run(_release, process))
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        text = output.text()
        if code != 0:
            raise WatchCheckError(_exit_problem(code, start, workdir, text))
        return text

    async def fetch_url(self, url: str) -> str:
        """GET ``url``; its main text for an HTML page, else the decoded body."""
        try:
            async with (
                asyncio.timeout(URL_TIMEOUT_SECONDS),
                httpx.AsyncClient(
                    verify=shared_ssl_context(),
                    follow_redirects=True,
                    timeout=URL_TIMEOUT_SECONDS,
                    headers=_HEADERS,
                    transport=self._transport,
                ) as client,
                client.stream("GET", url) as response,
            ):
                if not 200 <= response.status_code < 300:
                    raise WatchCheckError(f"The watched URL answered HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk[: URL_MAX_BYTES - len(body)])
                    if len(body) >= URL_MAX_BYTES:
                        break
                final_url = str(response.url)
        except TimeoutError, httpx.TimeoutException:
            raise WatchCheckError(
                f"The watched URL did not answer within {URL_TIMEOUT_SECONDS} seconds"
            ) from None
        except httpx.HTTPError as error:
            raise WatchCheckError(
                f"The watched URL could not be fetched: {str(error) or type(error).__name__}"
            ) from error
        return await _WORKERS.run(_page_text, bytes(body), content_type, final_url)

    async def _workdir(self, agent_id: str, project_id: str | None) -> Path:
        """The Project's directory for a Project Agent, else the Agent's Workspace."""
        try:
            if project_id is not None:
                project = await _WORKERS.run(self._projects.get, project_id)
                path = Path(project.cwd)
            else:
                agent = await self._agents.get_async(agent_id)
                workspace = getattr(agent, "workspace", None)
                path = (
                    Path(workspace)
                    if workspace is not None
                    else default_workspace_dir(self._data_root, agent_id)
                )
        except Exception as error:
            owner = f"Project {project_id}" if project_id is not None else f"Agent {agent_id}"
            raise WatchCheckError(
                f"The check command did not run: {owner} is not available: {error}"
            ) from error
        if not await _WORKERS.run(path.is_dir):
            raise WatchCheckError(
                f"The check command did not run: its working directory {path} does not exist"
            )
        return path


def _shell_start(command: str, workdir: Path) -> HostShellStart:
    """The host shell for a check; blocking: the environment reads the registry on Windows."""
    return host_shell_start(command, workdir, unattended_command_environment())


class _CappedOutput:
    """The first bytes of a command's output; later bytes are counted and dropped."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()

    def add(self, chunk: bytes) -> None:
        room = self._limit - len(self._data)
        if room > 0:
            self._data.extend(chunk[:room])

    def text(self) -> str:
        return self._data.decode("utf-8", errors="replace")


async def _read_into(process: PipeCommandProcess, output: _CappedOutput) -> None:
    while chunk := await process.read():
        output.add(chunk)


def _release(process: PipeCommandProcess) -> None:
    """End the command's process tree and release its pipes and tracking; blocking."""
    try:
        process.kill()
    except OSError as error:
        _LOGGER.warning("Watch check processes may still run (pid=%s): %s", process.pid, error)
    with contextlib.suppress(Exception):
        process.tree.close()
    with contextlib.suppress(Exception):
        process.close()


def _exit_problem(code: int, start: HostShellStart, workdir: Path, output: str) -> str:
    reason = start.request.not_run_codes.get(code) if start.request is not None else None
    if reason == "workdir_unavailable":
        return f"The check command did not run: PowerShell could not enter {workdir}"
    if reason == "language_mode_unavailable":
        return "The check command did not run: PowerShell runs in a restricted language mode"
    if reason is not None:
        return "The check command did not run: PowerShell could not read it"
    tail = _last_line(output)
    return f"The check command exited with code {code}" + (f": {tail}" if tail else "")


def _last_line(output: str) -> str:
    lines: Sequence[str] = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""
    last = lines[-1]
    return last if len(last) <= _ERROR_TAIL_CHARS else f"{last[: _ERROR_TAIL_CHARS - 1]}…"


def _page_text(content: bytes, content_type: str, url: str) -> str:
    """A fetched body as text: an HTML page's main text, other text decoded; blocking."""
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not is_textual_content_type(media_type) and (
        media_type not in {"", "application/octet-stream"} or looks_binary(content)
    ):
        raise WatchCheckError(
            f"The watched URL answered with {media_type or 'binary data'}, not text"
        )
    text = _decoded(content, content_type)
    if not is_html_page(media_type, text):
        return text
    # HTML codecs load only for pages.
    from core.tools._web_fetch_html import extract_views

    main, _page, _metadata, _warnings = extract_views(text, url, include_links=False)
    return main
