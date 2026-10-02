"""Process containment: launch flags, server-lifetime boundaries, and tree termination."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil  # type: ignore[import-untyped]
import pytest

from core.tools.process_manager import ProcessManager
from core.utils import processes as process_utils
from core.utils.processes import guarded_process_launch, subprocess_creation_flags
from tests.core.tools.process_manager_test_support import AGENT_A, spawn
from tests.core.tools.process_manager_test_support import (
    manager as manager,
)


def terminate_pid_forcibly(pid: int) -> None:
    """Really kill a test child so its wait task can settle after a fake kill."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(pid, signal.SIGKILL)


def wait_until_gone(pid: int) -> None:
    for _ in range(50):
        if not psutil.pid_exists(pid):
            return
        time.sleep(0.1)


# --- launch policy -------------------------------------------------------------


def test_windows_subprocess_creation_flags_hide_console_and_keep_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000, raising=False)
    monkeypatch.setattr(process_utils, "_windows_explicit_breakaway_allowed", lambda: True)

    assert subprocess_creation_flags(platform_name="nt") == 0x08000000
    assert (
        subprocess_creation_flags(new_process_group=True, breakaway=True, platform_name="nt")
        == 0x09000200
    )
    assert subprocess_creation_flags(new_process_group=True, platform_name="nt") == 0x08000200
    assert subprocess_creation_flags(new_process_group=True, platform_name="posix") == 0

    monkeypatch.setattr(process_utils, "_windows_explicit_breakaway_allowed", lambda: False)
    assert (
        subprocess_creation_flags(new_process_group=True, breakaway=True, platform_name="nt")
        == 0x08000200
    )


class _FakeWindowsFunction:
    def __init__(self, implementation) -> None:
        self.implementation = implementation
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.implementation(*args)


def _fake_job_kernel(
    monkeypatch: pytest.MonkeyPatch, *, in_job: bool, limit_flags: int, query_ok: bool = True
) -> list[tuple[Any, ...]]:
    queries: list[tuple[Any, ...]] = []

    def inspect_job(_process, _job, result) -> int:
        result._obj.value = int(in_job)
        return 1

    def query_job(job, info_class, limits, size, returned) -> int:
        queries.append((job, info_class, size, returned))
        limits._obj.LimitFlags = limit_flags
        return int(query_ok)

    kernel = SimpleNamespace(
        GetCurrentProcess=_FakeWindowsFunction(lambda: 73),
        IsProcessInJob=_FakeWindowsFunction(inspect_job),
        QueryInformationJobObject=_FakeWindowsFunction(query_job),
    )
    monkeypatch.setattr(
        process_utils.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False
    )
    return queries


@pytest.mark.parametrize(
    ("in_job", "limit_flags", "expected"),
    [
        (False, 0, False),
        (True, 0, False),
        (True, process_utils._JOB_OBJECT_LIMIT_BREAKAWAY_OK, True),
        (
            True,
            process_utils._JOB_OBJECT_LIMIT_BREAKAWAY_OK
            | process_utils._JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
            False,
        ),
    ],
    ids=["outside-a-job", "no-breakaway", "explicit-breakaway", "silent-breakaway"],
)
def test_windows_breakaway_queries_immediate_job_policy(
    monkeypatch: pytest.MonkeyPatch, in_job: bool, limit_flags: int, expected: bool
) -> None:
    queries = _fake_job_kernel(monkeypatch, in_job=in_job, limit_flags=limit_flags)

    assert process_utils._windows_explicit_breakaway_allowed() is expected
    if not in_job:
        assert queries == []
        return
    assert len(queries) == 1
    job, info_class, size, returned = queries[0]
    assert job is None
    assert info_class == process_utils._JOB_OBJECT_BASIC_LIMIT_INFORMATION_CLASS
    assert size > 0
    assert returned is None


def test_windows_breakaway_propagates_job_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_job_kernel(monkeypatch, in_job=True, limit_flags=0, query_ok=False)
    monkeypatch.setattr(process_utils.ctypes, "get_last_error", lambda: 5, raising=False)
    monkeypatch.setattr(
        process_utils.ctypes,
        "WinError",
        lambda code: PermissionError(code, "query denied"),
        raising=False,
    )

    with pytest.raises(PermissionError, match="query denied"):
        process_utils._windows_explicit_breakaway_allowed()


# --- server-lifetime boundary --------------------------------------------------


@pytest.mark.parametrize(
    ("lifetime_fd", "controlling_terminal", "environment", "options", "pass_fds"),
    [
        (41, False, {}, ["--lifetime-fd", "41"], (41,)),
        (
            41,
            True,
            {"LC_CTYPE": "C"},
            ["--controlling-terminal", "--lifetime-fd", "41", "--lc-ctype=C"],
            (41,),
        ),
        (None, True, {"LC_CTYPE": ""}, ["--controlling-terminal", "--lc-ctype="], ()),
        (None, False, {"LC_CTYPE": "C"}, None, ()),
    ],
    ids=["contained", "contained-terminal", "terminal", "uncontained"],
)
def test_guarded_posix_launch_wraps_exact_argv_and_lifetime_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    lifetime_fd: int | None,
    controlling_terminal: bool,
    environment: dict[str, str],
    options: list[str] | None,
    pass_fds: tuple[int, ...],
) -> None:
    monkeypatch.setattr(process_utils, "_POSIX_LIFETIME_READ_FD", lifetime_fd)
    argv = ["bash", "-c", "echo exact"]

    launch = guarded_process_launch(
        argv, env=environment, controlling_terminal=controlling_terminal, platform_name="posix"
    )

    if options is None:
        assert launch.argv == tuple(argv)
    else:
        # Isolated and without site-packages or bytecode writes, run by absolute path.
        assert launch.argv[:6] == (sys.executable, "-I", "-S", "-B", "-X", "utf8")
        script = Path(launch.argv[6])
        assert script.is_absolute()
        assert script.samefile(Path(process_utils.__file__).with_name("process_guardian.py"))
        assert launch.argv[7:] == (*options, "--", *argv)
    assert launch.pass_fds == pass_fds


@pytest.mark.skipif(os.name == "nt", reason="POSIX lifetime pipe contract")
def test_guardian_kills_child_group_when_server_pipe_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    read_fd, write_fd = os.pipe()
    pid_path = tmp_path / "child.pid"
    child_code = (
        "import os,pathlib,time; "
        "pathlib.Path(os.environ['CHILD_PID_PATH']).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    environment = dict(os.environ)
    environment["CHILD_PID_PATH"] = str(pid_path)
    monkeypatch.setattr(process_utils, "_POSIX_LIFETIME_READ_FD", read_fd)
    launch = guarded_process_launch([sys.executable, "-c", child_code], env=environment)
    guardian = subprocess.Popen(
        launch.argv, env=environment, pass_fds=launch.pass_fds, start_new_session=True
    )
    os.close(read_fd)
    try:
        for _ in range(50):
            if pid_path.exists():
                break
            time.sleep(0.1)
        child_pid = int(pid_path.read_text(encoding="utf-8"))

        os.close(write_fd)
        guardian.wait(timeout=10)
        wait_until_gone(child_pid)

        assert not psutil.pid_exists(child_pid)
    finally:
        if guardian.poll() is None:
            kill_process_group = os.killpg  # type: ignore[attr-defined]
            kill_process_group(guardian.pid, 9)
        with contextlib.suppress(OSError):
            os.close(write_fd)


# Prints the working directory and the environment the process was started with.
_REPORT_START = (
    "import os, sys; sys.stdout.write(repr((os.getcwd(), open('/proc/self/environ', 'rb').read())))"
)


@pytest.mark.parametrize("lc_ctype", [None, "C"], ids=["no-locale", "c-locale"])
def test_guardian_is_isolated_from_the_command_it_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, lc_ctype: str | None
) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux /proc contract")
    # A project with its own ``core`` package and modules named like the standard
    # library ones the guardian imports, also on the command's PYTHONPATH.
    workdir = tmp_path / "project"
    for module in ("core/__init__.py", "core/utils/__init__.py", "select.py", "subprocess.py"):
        (workdir / module).parent.mkdir(parents=True, exist_ok=True)
        (workdir / module).write_text("raise SystemExit('shadowed')\n", encoding="utf-8")
    # An installed-like copy of the guardian, so bytecode written next to it shows.
    application = tmp_path / "app"
    guardian = application / "core" / "utils" / "process_guardian.py"
    guardian.parent.mkdir(parents=True)
    shutil.copyfile(Path(process_utils.__file__).with_name("process_guardian.py"), guardian)
    monkeypatch.setattr(process_utils, "_GUARDIAN_SCRIPT", str(guardian))
    # Without a UTF-8 locale Python's startup sets LC_CTYPE (PEP 538).
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONPATH": str(workdir),
        "PYTHONHOME": str(tmp_path / "missing"),
        "VBOT_TEST_TEXT": "grüße",
    }
    if lc_ctype is not None:
        environment["LC_CTYPE"] = lc_ctype
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(process_utils, "_POSIX_LIFETIME_READ_FD", read_fd)
    launch = guarded_process_launch(
        [sys.executable, "-I", "-S", "-c", _REPORT_START], env=environment
    )
    try:
        completed = subprocess.run(
            launch.argv,
            cwd=workdir,
            env=environment,
            pass_fds=launch.pass_fds,
            start_new_session=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert completed.returncode == 0, completed.stderr
    cwd, started_environment = ast.literal_eval(completed.stdout.decode())
    assert Path(cwd) == workdir.resolve()
    assert dict(entry.split(b"=", 1) for entry in started_environment.split(b"\0") if entry) == {
        os.fsencode(name): os.fsencode(value) for name, value in environment.items()
    }
    assert list(application.rglob("__pycache__")) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object contract")
def test_windows_job_kills_descendant_when_containment_owner_crashes(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"
    owner_code = (
        "import os,pathlib,subprocess,sys,time; "
        "from core.utils.processes import activate_process_containment; "
        "activate_process_containment(); "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(.25); os._exit(7)"
    )
    owner = subprocess.Popen([sys.executable, "-c", owner_code, str(pid_path)])

    assert owner.wait(timeout=10) == 7
    child_pid = int(pid_path.read_text(encoding="utf-8"))
    wait_until_gone(child_pid)

    assert not psutil.pid_exists(child_pid)


# --- tree termination ------------------------------------------------------------


@pytest.mark.parametrize("denied", [False, True])
def test_posix_tree_kill_signals_the_group_and_never_falls_back_to_the_parent(
    monkeypatch: pytest.MonkeyPatch, denied: bool
) -> None:
    sent_signals: list[tuple[int, int]] = []

    def killpg(process_group_id: int, signal_number: int) -> None:
        if denied:
            raise PermissionError("test-owned denied process group")
        sent_signals.append((process_group_id, signal_number))

    def forbidden() -> None:
        pytest.fail("direct-child fallback loses tree ownership")

    monkeypatch.setattr(process_utils, "os", SimpleNamespace(name="posix", killpg=killpg))
    proc = SimpleNamespace(pid=12345, kill=forbidden)

    if denied:
        with pytest.raises(PermissionError):
            process_utils.kill_process_tree(proc)
        assert sent_signals == []
    else:
        process_utils.kill_process_tree(proc)
        assert sent_signals == [(12345, 9)]


def process_log_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "vbot.processes"
    ]


@pytest.mark.parametrize("survives", [False, True])
def test_windows_tree_fallback_kills_and_verifies_captured_descendants(
    monkeypatch, caplog, survives
):
    killed = []
    child = SimpleNamespace(pid=12346, kill=lambda: killed.append("child"))
    root = SimpleNamespace(
        is_running=lambda: True,
        children=lambda recursive: [child],
        kill=lambda: killed.append("root"),
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: root)
    monkeypatch.setattr(
        psutil, "wait_procs", lambda processes, timeout: ([], [child] if survives else [])
    )
    monkeypatch.setattr(process_utils, "os", SimpleNamespace(name="nt"))

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("taskkill", 5)

    monkeypatch.setattr(subprocess, "run", timeout)
    with caplog.at_level(logging.DEBUG, logger="vbot.processes"):
        assert process_utils.windows_taskkill_tree(12345) is (not survives)
    assert set(killed) == {"child", "root"}
    # A failed taskkill is DEBUG detail while the direct kill ends the tree; only
    # survivors warn, with their count and the cause.
    [(level, message)] = process_log_lines(caplog)
    assert level == (logging.WARNING if survives else logging.DEBUG)
    assert "taskkill timed out after 5 s" in message
    assert ("1 of 2 captured processes survive" in message) is survives


def test_windows_process_tree_kill_runs_taskkill_windowless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = SimpleNamespace(is_running=lambda: True, children=lambda recursive: [])
    monkeypatch.setattr(psutil, "Process", lambda pid: root)
    monkeypatch.setattr(psutil, "wait_procs", lambda processes, timeout: (processes, []))
    fallback_kills = 0
    run_kwargs: dict[str, Any] = {}

    class FakeProcess:
        pid = 12345

        def kill(self) -> None:
            nonlocal fallback_kills
            fallback_kills += 1

    def successful_taskkill(
        args: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[bytes]:
        run_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args, 0)

    expected_creation_flags = 0x08000000
    monkeypatch.setattr(process_utils, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        process_utils,
        "subprocess_creation_flags",
        lambda: expected_creation_flags,
    )
    monkeypatch.setattr("core.utils.processes.subprocess.run", successful_taskkill)

    process_utils.kill_process_tree(FakeProcess())

    assert run_kwargs["creationflags"] == expected_creation_flags
    assert fallback_kills == 0


@pytest.mark.parametrize(
    ("cause", "encoding"),
    [
        pytest.param("ERROR: The process with PID 2 could not be terminated.", "utf-8", id="utf-8"),
        # Windowless, taskkill writes localized messages in the OEM code page.
        pytest.param(
            "FEHLER: Der Prozess mit PID 2 konnte nicht beendet werden: ungültig.",
            "oem",
            id="oem-code-page",
            marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows code pages"),
        ),
    ],
)
def test_windows_failed_tree_kill_retains_orphan_for_retry(monkeypatch, caplog, cause, encoding):
    try:
        stderr = f"\r\n{cause}\r\nReason: denied\r\n".encode(encoding)
    except UnicodeEncodeError:
        pytest.skip("this OEM code page cannot encode the localized message")
    root_alive = True
    child_alive = True
    deny_child = True

    def kill_root():
        nonlocal root_alive
        root_alive = False

    def kill_child():
        nonlocal child_alive
        if deny_child:
            raise psutil.AccessDenied(2)
        child_alive = False

    child = SimpleNamespace(pid=2, kill=kill_child)
    root = SimpleNamespace(
        is_running=lambda: root_alive, children=lambda recursive: [child], kill=kill_root
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: root)
    monkeypatch.setattr(
        psutil, "wait_procs", lambda processes, timeout: ([], [child] if child_alive else [])
    )
    taskkill_runs: list[list[str]] = []

    def failing_taskkill(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        taskkill_runs.append(args)
        return subprocess.CompletedProcess(args, 128, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", failing_taskkill)
    targets: list[Any] = []
    with caplog.at_level(logging.DEBUG, logger="vbot.processes"):
        assert not process_utils.windows_taskkill_tree(1, targets=targets)
        assert not root_alive and child_alive
        [(level, message)] = process_log_lines(caplog)
        assert level == logging.WARNING
        assert "1 of 2 captured processes survive" in message
        # The exit code and the first stderr line name the taskkill failure.
        assert f"taskkill exit code 128: {cause}" in message
        assert "Reason" not in message
        assert "killing pid=2 failed" in message
        caplog.clear()

        deny_child = False
        assert process_utils.windows_taskkill_tree(1, targets=targets)
    assert not child_alive
    # With the root gone, the retry kills the retained identities without taskkill.
    assert len(taskkill_runs) == 1
    [(level, message)] = process_log_lines(caplog)
    assert level == logging.DEBUG
    assert "root already exited" in message and "taskkill exit" not in message


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="taskkill fallback path is Windows-only")
async def test_kill_terminates_the_tree_directly_without_a_warning_when_taskkill_fails(
    manager: ProcessManager, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    process_id = await spawn(manager)

    def failing_taskkill(*args: Any, **kwargs: Any) -> Any:
        raise OSError("taskkill missing")

    monkeypatch.setattr(subprocess, "run", failing_taskkill)

    with caplog.at_level(logging.DEBUG, logger="vbot.processes"):
        await manager.kill(process_id, AGENT_A)

    assert manager.get_process(process_id, AGENT_A).status == "killed"
    assert [level for level, _message in process_log_lines(caplog)] == [logging.DEBUG]


@pytest.mark.asyncio
async def test_kill_process_keeps_event_loop_responsive_during_windows_taskkill(
    manager: ProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows tree-kill must run off the loop, never freeze it.

    A stuck ``taskkill`` (up to its 5s timeout) previously blocked the whole
    event loop - every concurrent Run stalled behind one process kill.
    """
    process_id = await spawn(manager)

    # Simulate the Windows branch on any host: spawn has already used the real
    # platform semantics, so only the kill path is faked.
    monkeypatch.setattr(os, "name", "nt")
    entered_taskkill = threading.Event()
    release_taskkill = threading.Event()

    def slow_taskkill(pid: int, **kwargs) -> bool:
        entered_taskkill.set()
        release_taskkill.wait(timeout=5)
        terminate_pid_forcibly(pid)
        return True

    monkeypatch.setattr(process_utils, "windows_taskkill_tree", slow_taskkill)

    heartbeat_ticks = 0
    heartbeat_done = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal heartbeat_ticks
        while not heartbeat_done.is_set():
            await asyncio.sleep(0)
            heartbeat_ticks += 1

    heartbeat_task = asyncio.create_task(heartbeat())
    kill_task = asyncio.create_task(manager.kill(process_id, AGENT_A))
    try:
        # Wait until the kill primitive is actually blocked in its worker -
        # via a worker thread too, or this wait would freeze the loop itself.
        assert await asyncio.to_thread(entered_taskkill.wait, 5), "taskkill was never reached"

        ticks_while_blocked = heartbeat_ticks
        await asyncio.sleep(0.05)
        assert heartbeat_ticks > ticks_while_blocked, (
            "event loop froze while the process tree-kill was pending"
        )
    finally:
        release_taskkill.set()
        await kill_task
        heartbeat_done.set()
        await heartbeat_task

    assert manager.get_process(process_id, AGENT_A).status == "killed"
