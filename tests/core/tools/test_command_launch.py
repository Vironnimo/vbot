"""PipeLauncher's warm shell: a shell started ahead of time that serves one command.

The lifecycle cases drive fake shells on a fake clock. One real case on Windows
shows what only PowerShell can: a claimed warm shell runs its command in the
command's workdir and environment, and nothing the command starts gets input.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from core.storage.temp_files import TemporaryFileManager
from core.tools._command_launch import (
    WARM_SHELL_IDLE_SECONDS,
    PipeCommandProcess,
    PipeLauncher,
    launch_pipe_process,
)
from core.tools._interpreter_host import InterpreterHost
from core.tools._powershell import POWERSHELL_BOOTSTRAP, powershell_request
from core.tools.command_manager import CommandManager
from core.tools.process_tree import ProcessTree, ProcessTreeTracker
from core.tools.scratch_areas import ScratchFolder
from core.tools.shell_environment import RunIdentity, command_environment
from tests.core.tools.command_manager_helpers import (
    OWNER,
    FakeClock,
    FakeProcess,
    FakeSink,
    format_report,
    run_inline,
    turns,
)

ARGV = ("shell", "--bootstrap")
BASE = {"PATH": "/bin"}


def _tracker(pid: int) -> ProcessTree:
    raise AssertionError("the fake start tracks nothing")


class FakeShell(FakeProcess):
    """A started shell waiting for its request; a kill ends it unclaimed."""

    def __init__(self, pid: int) -> None:
        super().__init__(pid)
        self.requests: list[bytes] = []
        self.killed = False

    def running(self) -> bool:
        return not self.killed and not self._exit.done()

    def send(self, request: bytes) -> None:
        self.requests.append(request)

    def kill(self) -> None:
        self.killed = True


class Starts:
    """The launcher's start: records each start's directory and makes a fake shell.

    Start number N makes the shell with pid 20_000 + N.
    """

    def __init__(self) -> None:
        self.shells: list[FakeShell] = []
        self.cwds: list[Path] = []
        # Start number -> the error that start raises instead of creating a shell.
        self.errors: dict[int, OSError] = {}
        # While set and not open, starts wait for it.
        self.gate: asyncio.Event | None = None

    async def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        tracker: ProcessTreeTracker,
        *,
        request_pipe: bool = False,
    ) -> FakeShell:
        assert request_pipe
        number = len(self.cwds)
        self.cwds.append(cwd)
        if self.gate is not None:
            await self.gate.wait()
        if number in self.errors:
            raise self.errors[number]
        shell = FakeShell(20_000 + number)
        self.shells.append(shell)
        return shell

    def cwd_of(self, shell: FakeShell) -> Path:
        return self.cwds[shell.pid - 20_000]


def _launcher(starts: Starts, clock: FakeClock) -> PipeLauncher:
    return PipeLauncher(warm=True, start=starts, sleep=clock.sleep)


async def _launch(
    launcher: PipeLauncher,
    cwd: Path,
    request: bytes,
    *,
    argv: Sequence[str] = ARGV,
    env: Mapping[str, str] = BASE,
    warm: bool = True,
) -> FakeShell:
    """Launch with *request*, then let the warm shell's start run."""
    shell = await launcher.launch(argv, cwd, env, _tracker, request, warm=warm)
    assert isinstance(shell, FakeShell)
    await turns()
    return shell


@pytest.mark.asyncio
async def test_a_launch_claims_the_warm_shell_of_its_start_and_the_next_one_starts(
    tmp_path: Path,
) -> None:
    starts = Starts()
    launcher = _launcher(starts, FakeClock())

    # The first launch starts its own shell; a warm shell starts after it, elsewhere.
    first = await _launch(launcher, tmp_path, b"first")
    assert (first.requests, starts.cwd_of(first)) == ([b"first"], tmp_path)
    assert len(starts.shells) == 2 and starts.cwds[1] != tmp_path

    # A launch whose environment holds values of its own call starts its own shell
    # and leaves the warm shell waiting; no other warm shell starts for it.
    own = await _launch(launcher, tmp_path, b"own", env={**BASE, "EMPTY": ""}, warm=False)
    assert own is starts.shells[2] and starts.cwd_of(own) == tmp_path
    assert len(starts.shells) == 3 and not starts.shells[1].killed

    # The next launch with the same program and environment gets the warm shell,
    # whatever its workdir; it serves only this request, and the next one starts.
    second = await _launch(launcher, tmp_path / "other", b"second")
    assert second is starts.shells[1]
    assert second.requests == [b"second"] and not second.killed
    assert len(starts.shells) == 4 and starts.shells[3].requests == []
    await launcher.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("difference", ["environment", "arguments"])
async def test_a_launch_with_another_start_ends_the_warm_shell_and_starts_its_own(
    tmp_path: Path, difference: str
) -> None:
    starts = Starts()
    launcher = _launcher(starts, FakeClock())
    await _launch(launcher, tmp_path, b"first")
    warm = starts.shells[1]
    argv = (*ARGV, "-x") if difference == "arguments" else ARGV
    env = {**BASE, "LOGIN": "changed"} if difference == "environment" else BASE

    shell = await _launch(launcher, tmp_path, b"second", argv=argv, env=env)

    assert shell is starts.shells[2] and starts.cwd_of(shell) == tmp_path
    assert warm.killed and warm.requests == []
    # The next warm shell is started for the new start.
    assert len(starts.shells) == 4
    await launcher.aclose()


@pytest.mark.asyncio
async def test_a_burst_claims_only_a_created_warm_shell_and_starts_no_more_shells_than_calls(
    tmp_path: Path,
) -> None:
    starts = Starts()
    launcher = _launcher(starts, FakeClock())
    await _launch(launcher, tmp_path, b"first")
    starts.gate = asyncio.Event()

    requests = (b"a", b"b", b"c")
    burst = [asyncio.ensure_future(_launch(launcher, tmp_path, request)) for request in requests]
    await turns()
    starts.gate.set()
    shells = await asyncio.gather(*burst)

    # The first call claims the warm shell. The others start their own instead
    # of claiming the next warm shell while it is being created.
    assert shells[0] is starts.shells[1]
    assert [shell.requests for shell in shells] == [[b"a"], [b"b"], [b"c"]]
    assert [starts.cwd_of(shell) for shell in shells[1:]] == [tmp_path, tmp_path]
    assert len(starts.shells) == 5
    # The next warm shell stays for a later launch.
    [waiting] = [shell for shell in starts.shells if not shell.requests]
    assert await _launch(launcher, tmp_path, b"d") is waiting
    await launcher.aclose()


@pytest.mark.asyncio
async def test_a_warm_shell_that_exited_or_failed_to_start_is_passed_over_with_one_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    starts = Starts()
    launcher = _launcher(starts, FakeClock())
    caplog.set_level(logging.WARNING)
    await _launch(launcher, tmp_path, b"1")

    starts.shells[1].exit(1)
    # Start 3 is the warm shell after the second launch.
    starts.errors[3] = OSError("access denied")
    second = await _launch(launcher, tmp_path, b"2")
    third = await _launch(launcher, tmp_path, b"3")
    starts.shells[-1].exit(1)
    fourth = await _launch(launcher, tmp_path, b"4")

    # Each launch ran its request in a shell of its own, in its workdir.
    for shell, request in ((second, b"2"), (third, b"3"), (fourth, b"4")):
        assert shell.requests == [request] and starts.cwd_of(shell) == tmp_path
    assert starts.shells[1].killed and starts.shells[4].killed
    warnings = [record.getMessage() for record in caplog.records if "warm shell" in record.msg]
    assert warnings == [
        "The warm shell 20001 exited before a command claimed it; commands start their own shell",
        "A warm shell could not start: access denied; commands start their own shell",
    ]
    await launcher.aclose()


@pytest.mark.asyncio
async def test_a_warm_shell_nobody_claims_ends_after_the_idle_time(tmp_path: Path) -> None:
    starts = Starts()
    clock = FakeClock()
    launcher = _launcher(starts, clock)
    await _launch(launcher, tmp_path, b"first")
    warm = starts.shells[1]

    await clock.advance(WARM_SHELL_IDLE_SECONDS - 1)
    await turns()
    assert not warm.killed
    await clock.advance(1)
    await turns()
    assert warm.killed

    # The next launch starts its own shell again.
    shell = await _launch(launcher, tmp_path, b"second")
    assert shell is starts.shells[2] and warm.requests == []
    await launcher.aclose()


@pytest.mark.parametrize("closing", ["aclose", "close", "close-after-the-event-loop"])
@pytest.mark.parametrize("state", ["ready", "starting"])
def test_closing_ends_the_warm_shell_at_once_and_starts_no_other(
    tmp_path: Path, closing: str, state: str
) -> None:
    starts = Starts()
    launcher = _launcher(starts, FakeClock())

    async def open_warm_shell() -> None:
        # The fake start of the first shell returns without yielding to the Event Loop.
        await launcher.launch(ARGV, tmp_path, BASE, _tracker, b"first")
        if state == "starting":
            # The warm shell's start waits until after the close.
            starts.gate = asyncio.Event()
        await turns()

    async def close() -> None:
        if closing == "aclose":
            await launcher.aclose()
        else:
            launcher.close()
        await turns()
        if starts.gate is not None:
            starts.gate.set()
            await turns()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(open_warm_shell())
        if closing == "close-after-the-event-loop":
            # The tasks of a closed Event Loop never run again; a start in progress
            # creates no shell.
            loop.close()
            launcher.close()
            starts.gate = None
        else:
            loop.run_until_complete(close())
    finally:
        loop.close()

    # A ready shell is killed; a start in progress is cancelled before it creates one.
    assert [shell.killed for shell in starts.shells[1:]] == ([True] if state == "ready" else [])
    shell = asyncio.run(_launch(launcher, tmp_path, b"after"))
    assert shell.requests == [b"after"] and len(starts.cwds) == 3
    if closing == "close-after-the-event-loop":
        # The closed Event Loop's pending tasks, which the launcher's clock still
        # holds, are destroyed within the test; asyncio logs that they were pending.
        del launcher
        gc.collect()


@pytest.mark.asyncio
async def test_the_warm_shell_is_no_command(tmp_path: Path) -> None:
    starts = Starts()
    clock = FakeClock()
    manager = CommandManager(
        FakeSink(),
        temporary_files=TemporaryFileManager(tmp_path / "data"),
        launcher=_launcher(starts, clock),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
        run_blocking=run_inline,
        max_live=1,
        output_host=InterpreterHost.in_process(),
    )
    manager.start()

    async def launch(command: str) -> str:
        handle = await manager.launch(
            OWNER,
            list(ARGV),
            command=command,
            description=None,
            cwd=tmp_path,
            env=BASE,
            origin_run_id="run-a",
            formatter=format_report,
            request=command.encode(),
        )
        await turns()
        manager.to_background(handle.command_id, deliver=False)
        return handle.command_id

    try:
        first = await launch("first")
        warm = starts.shells[1]
        # The warm shell runs beside the one command and is not listed.
        assert [report.command_id for report in manager.commands([OWNER])] == [first]
        starts.shells[0].exit(0)
        for _step in range(100):
            if manager.report(first).finished:
                break
            await clock.advance(0.25)
            await turns()
        assert manager.report(first).finished

        # It takes no room under the running command limit: the next command claims it.
        second = await launch("second")
        assert manager.report(second).pid == warm.pid and warm.requests == [b"second"]
        assert len(manager.commands([OWNER])) == 2
    finally:
        await manager.aclose()
    # The manager's close ends the next warm shell with it.
    assert starts.shells[2].killed


# --- Real PowerShell -------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("pwsh") is None, reason="Windows PowerShell 7"
)
@pytest.mark.asyncio
async def test_a_claimed_warm_shell_runs_the_command_in_its_workdir_and_environment(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh")
    assert pwsh is not None
    started: list[PipeCommandProcess] = []

    async def start(*arguments: Any, **options: Any) -> PipeCommandProcess:
        shell = await launch_pipe_process(*arguments, **options)
        started.append(shell)
        return shell

    first_own, own = tmp_path / "first", tmp_path / "second"
    for folder in (first_own, own):
        (folder / "tmp").mkdir(parents=True)
    scratch = ScratchFolder(
        own=own, area=tmp_path, tmp=own / "tmp", recreated=True, incarnation="second"
    )
    layers = command_environment(RunIdentity("agent-a", "session-a", None), scratch=scratch)
    base = {**layers.base, "VBOT_PROBE_REMOVED": "base"}
    changes = {**layers.changes, "VBOT_PROBE": "ä b", "VBOT_PROBE_REMOVED": None}
    argv = [pwsh, "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_BOOTSTRAP]
    workdir = tmp_path / "wörk dir"
    workdir.mkdir()
    (workdir / "relative.txt").write_bytes(b"relative-sentinel")
    # A native program and PowerShell see the workdir, the call's variables, and
    # an input that ends at once.
    probe = (
        "import os, sys, tempfile; print(os.getcwd()); print(os.environ.get('VBOT_PROBE')); "
        "print('VBOT_PROBE_REMOVED' in os.environ); print(repr(sys.stdin.read())); "
        "print(os.environ['VBOT_SCRATCH_DIR']); print(tempfile.gettempdir())"
    )
    command = "\n".join(
        [
            "cmd /c cd",
            f"& '{sys.executable}' -c \"{probe}\"",
            "[Environment]::CurrentDirectory",
            "(Get-Location).Path",
            "Get-Content relative.txt",
        ]
    )
    manager = CommandManager(
        FakeSink(),
        temporary_files=TemporaryFileManager(tmp_path / "data"),
        launcher=PipeLauncher(warm=True, start=start),
    )

    async def run(
        text: str, cwd: Path, request_changes: dict[str, str | None]
    ) -> tuple[int, int | None, str]:
        handle = await manager.launch(
            OWNER,
            argv,
            command=text,
            description=None,
            cwd=cwd,
            env=base,
            origin_run_id="run-a",
            formatter=format_report,
            request=powershell_request(text, str(cwd), request_changes),
        )
        assert await manager.wait(handle.command_id, seconds=60) == "ended"
        report = manager.report(handle.command_id)
        transcript = report.transcript
        output = "".join(f"{line}\n" for line in (*transcript.head, *transcript.tail))
        return handle.pid, report.exit_code, output

    try:
        await run(
            "'first'",
            tmp_path,
            {
                **changes,
                "VBOT_SCRATCH_DIR": str(first_own),
                "TEMP": str(first_own / "tmp"),
                "TMP": str(first_own / "tmp"),
                "TMPDIR": str(first_own / "tmp"),
            },
        )
        for _attempt in range(1000):
            if len(started) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(started) == 2, "the warm shell did not start"

        pid, exit_code, output = await run(command, workdir, changes)
    finally:
        await manager.aclose()

    assert pid == started[1].pid, "the command did not claim the warm shell"
    assert exit_code == 0, output
    assert output.splitlines() == [
        str(workdir),
        str(workdir),
        "ä b",
        "False",
        "''",
        str(own),
        str(own / "tmp"),
        str(workdir),
        str(workdir),
        "relative-sentinel",
    ], output
