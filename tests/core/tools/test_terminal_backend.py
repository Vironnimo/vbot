"""PTY/ConPTY transport, default shells and Windows command lines."""

from __future__ import annotations

import ast
import contextlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.tools.terminal_backend as terminal_backend
from core.utils import processes as process_utils

# The adapter's child process, already ended, for tests of its descriptor I/O.
EXITED_PROCESS = SimpleNamespace(pid=0, poll=lambda: 0, wait=lambda timeout=None: 0)


def select_default_terminal(
    platform_name: str,
    environment: dict[str, str],
    available: set[str],
    *,
    login_shell: str | None = None,
) -> list[str]:
    def lookup(command: str, *, path: str) -> str | None:
        assert path == environment.get("PATH", "")
        return command if command in available else None

    return terminal_backend._select_default_terminal_argv(
        platform_name,
        environment,
        executable_lookup=lookup,
        posix_login_shell=login_shell,
    )


def test_windows_default_terminal_prefers_powershell_7() -> None:
    assert select_default_terminal(
        "nt",
        {"PATH": "windows-path", "COMSPEC": "custom-cmd.exe"},
        {"pwsh.exe", "powershell.exe"},
    ) == ["pwsh.exe"]


def test_windows_default_terminal_falls_back_through_windows_powershell_to_comspec() -> None:
    environment = {"PATH": "windows-path", "COMSPEC": "custom-cmd.exe"}

    assert select_default_terminal("nt", environment, {"powershell.exe"}) == ["powershell.exe"]
    assert select_default_terminal("nt", environment, set()) == ["custom-cmd.exe"]
    assert select_default_terminal("nt", {}, set()) == ["cmd.exe"]


def test_posix_default_terminal_uses_environment_then_login_shell_then_sh() -> None:
    assert select_default_terminal(
        "posix", {"SHELL": "/bin/fish"}, set(), login_shell="/bin/zsh"
    ) == ["/bin/fish"]
    assert select_default_terminal("posix", {}, set(), login_shell="/bin/zsh") == ["/bin/zsh"]
    assert select_default_terminal("posix", {}, set()) == ["/bin/sh"]


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe command-line contract")
def test_batch_programs_receive_their_arguments_unchanged_through_the_command_processor(
    tmp_path: Path,
) -> None:
    # npm installs programs such as codex as .cmd shims that hand %* to node.
    shim = tmp_path / "program dir" / "echo-args.cmd"
    shim.parent.mkdir()
    report = "import json, sys; print(json.dumps(sys.argv[1:]))"
    shim.write_text(f'@"{sys.executable}" -c "{report}" %*\r\n', encoding="utf-8")
    arguments = [
        "fix the bug",
        'say "hi"',
        "50% & more",
        "%PATH%",
        "a^b (c) <d> |e| !f!",
        "",
        # Metacharacters without whitespace: the shim's %* parses them again.
        "a&b",
        "x|y",
        "(c)",
        "^d",
        "<e>",
        "dir&\\",
    ]
    environment = {"PATH": os.environ["PATH"], "COMSPEC": os.environ["COMSPEC"]}

    executable, line = terminal_backend._windows_command([str(shim), *arguments], environment, None)

    assert Path(executable).name.lower() == "cmd.exe"
    completed = subprocess.run(
        f'"{executable}" {line}', capture_output=True, text=True, timeout=10, check=True
    )
    assert json.loads(completed.stdout) == arguments
    # Other programs get the C runtime quoting of their arguments.
    assert terminal_backend._windows_command([sys.executable, "a b", 'c"d'], environment, None) == (
        sys.executable,
        subprocess.list2cmdline(["a b", 'c"d']),
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows console Ctrl+C inheritance")
def test_ctrl_c_stops_a_terminal_program_of_a_server_that_ignores_ctrl_c(tmp_path: Path) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
    # A Server started in a new process group, as the managed one is,
    # ignores Ctrl+C, and its children inherit that.
    kernel32.SetConsoleCtrlHandler(None, True)
    adapter = None
    try:
        adapter = terminal_backend.spawn_terminal_adapter(
            [sys.executable, "-c", "import time; print('READY', flush=True); time.sleep(30)"],
            tmp_path,
            dict(os.environ),
            24,
            80,
        )
        output = ""
        deadline = time.monotonic() + 10
        while "READY" not in output and time.monotonic() < deadline:
            with contextlib.suppress(TimeoutError):
                chunk = adapter.read(4096)
                output += chunk
                if "\x1b[c" in chunk:
                    # ConPTY holds output until its device query is answered.
                    adapter.write("\x1b[?6c")
        assert "READY" in output
        adapter.write("\x03")
        deadline = time.monotonic() + 5
        while adapter.is_alive() and time.monotonic() < deadline:
            with contextlib.suppress(TimeoutError, EOFError):
                adapter.read(4096)
        assert not adapter.is_alive()
    finally:
        kernel32.SetConsoleCtrlHandler(None, False)
        if adapter is not None:
            adapter.terminate()
            adapter.close()


@pytest.fixture(params=[False, True], ids=["direct", "guardian"])
def server_lifetime(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Spawn once as in tests and once under the server-lifetime guardian, as in the server."""
    if not request.param:
        yield
        return
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(process_utils, "_POSIX_LIFETIME_READ_FD", read_fd)
    try:
        yield
    finally:
        os.close(read_fd)
        os.close(write_fd)


def spawn_posix_terminal(argv: list[str], workdir: Path) -> terminal_backend.TerminalAdapter:
    environment = {"PATH": os.environ.get("PATH", os.defpath)}
    return terminal_backend.spawn_terminal_adapter(argv, workdir, environment, 24, 80)


def read_until(adapter: terminal_backend.TerminalAdapter, marker: str) -> str:
    output = ""
    deadline = time.monotonic() + 5
    while marker not in output:
        assert time.monotonic() < deadline, f"{marker!r} did not appear in {output!r}"
        with contextlib.suppress(TimeoutError):
            output += adapter.read(4096)
    return output


def read_to_end(adapter: terminal_backend.TerminalAdapter) -> str:
    output = ""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            output += adapter.read(4096)
        except TimeoutError:
            continue
        except EOFError, OSError:
            return output
    raise AssertionError(f"The terminal did not end: {output!r}")


@pytest.mark.usefixtures("server_lifetime")
def test_posix_terminal_gives_a_shell_its_controlling_terminal(tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux PTY contract")
    # Python code run in a child forked from the multi-threaded server can
    # deadlock it. CPython reports such a fork with a DeprecationWarning it
    # never raises, even under an "error" filter, so the test records it.
    released = threading.Event()
    other_thread = threading.Thread(target=released.wait)
    other_thread.start()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            adapter = spawn_posix_terminal(["sh"], tmp_path)
    finally:
        released.set()
        other_thread.join()
    assert [str(item.message) for item in caught if item.category is DeprecationWarning] == []
    try:
        # The echoed input differs from the output: '<'1'>' prints <1>.
        adapter.write("tty; stty size; echo '<'1'>'\n")
        assert re.search(r"/dev/pts/\d+\r\n24 80\r\n<1>", read_until(adapter, "<1>"))
        adapter.resize(30, 100)
        adapter.write("stty size; echo '<'2'>'\n")
        assert "30 100\r\n<2>" in read_until(adapter, "<2>")
        # Ctrl-C signals the foreground job of the shell's controlling terminal,
        # and the shell reports the job's interrupt. The job prints <3> once it
        # holds the foreground and handles SIGINT (a shell script would race).
        job = "print('<' + '3>', flush=True); import time; time.sleep(10)"
        adapter.write(shlex.join([sys.executable, "-S", "-c", job]) + "\n")
        read_until(adapter, "<3>")
        adapter.write("\x03")
        adapter.write("echo status=$?; echo '<'4'>'\n")
        assert "status=130" in read_until(adapter, "<4>")
        # Programs start with default signal actions, not those of Python.
        adapter.write("grep SigIgn /proc/self/status; echo '<'5'>'\n")
        ignored = re.search(r"SigIgn:\s*([0-9a-f]+)", read_until(adapter, "<5>"))
        assert ignored is not None
        assert not int(ignored.group(1), 16) & (1 << (signal.SIGPIPE - 1))
        adapter.write("exit 3\n")
        read_to_end(adapter)
        assert adapter.exit_code() == 3
    finally:
        adapter.close()


@pytest.mark.usefixtures("server_lifetime")
def test_posix_terminal_program_without_job_control_receives_ctrl_c(tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux PTY contract")
    program = (
        "import sys, time\n"
        # Ctrl+C can arrive while print is still returning under parallel load.
        "try:\n    print('<ready>', flush=True)\n    time.sleep(10)\n"
        "except KeyboardInterrupt:\n    print('interrupted')\n    sys.exit(7)\n"
    )
    adapter = spawn_posix_terminal([sys.executable, "-c", program], tmp_path)
    try:
        read_until(adapter, "<ready>")
        adapter.write("\x03")
        output = read_to_end(adapter)
        assert "interrupted" in output
        assert "Traceback" not in output
        assert adapter.exit_code() == 7
    finally:
        adapter.close()


@pytest.mark.usefixtures("server_lifetime")
def test_posix_terminal_starts_with_exactly_its_working_directory_and_environment(
    tmp_path: Path,
) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux PTY contract")
    # A project whose own ``core`` package, also on its PYTHONPATH, would hide vBot's.
    workdir = tmp_path / "project"
    (workdir / "core").mkdir(parents=True)
    (workdir / "core" / "__init__.py").write_text("raise SystemExit('shadowed')\n", "utf-8")
    # Without a UTF-8 locale Python's startup sets LC_CTYPE (PEP 538).
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONPATH": str(workdir),
        "PYTHONHOME": str(tmp_path / "missing"),
    }
    report = "import os; print(repr((os.getcwd(), open('/proc/self/environ', 'rb').read())))"
    adapter = terminal_backend.spawn_terminal_adapter(
        [sys.executable, "-I", "-S", "-c", report], workdir, environment, 24, 80
    )
    try:
        output = read_to_end(adapter)
        assert adapter.exit_code() == 0, output
    finally:
        adapter.close()

    cwd, started_environment = ast.literal_eval(output.strip())
    assert Path(cwd) == workdir.resolve()
    assert dict(entry.split(b"=", 1) for entry in started_environment.split(b"\0") if entry) == {
        os.fsencode(name): os.fsencode(value) for name, value in environment.items()
    }


@pytest.mark.usefixtures("server_lifetime")
def test_posix_terminal_reports_a_missing_program_before_launch(tmp_path: Path) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux PTY contract")
    with pytest.raises(FileNotFoundError) as raised:
        spawn_posix_terminal(["vbot-no-such-program"], tmp_path)
    assert raised.value.filename == "vbot-no-such-program"


@pytest.mark.parametrize("platform_name", ["nt", "posix"])
def test_adapter_read_is_bounded_and_preserves_split_unicode(platform_name, monkeypatch):
    if platform_name == "posix" and os.name == "nt":
        pytest.skip("POSIX descriptor readiness requires POSIX")
    # A read waits at most this long for output; zero proves it never blocks past it.
    monkeypatch.setattr(terminal_backend, "TERMINAL_READ_TIMEOUT_SECONDS", 0)
    if platform_name == "nt":
        receiver, sender = socket.socketpair()
        process = SimpleNamespace(fileobj=receiver)
        adapter = terminal_backend._WindowsTerminalAdapter(process)
        send = sender.sendall

        def close():
            receiver.close()
            sender.close()
    else:
        read_fd, write_fd = os.pipe()
        adapter = terminal_backend._PosixTerminalAdapter(EXITED_PROCESS, read_fd)

        def send(value):
            os.write(write_fd, value)

        def close():
            adapter.close()
            os.close(write_fd)

    try:
        with pytest.raises(TimeoutError):
            adapter.read(4096)
        encoded = "😀".encode()
        send(encoded[:2])
        assert adapter.read(4096) == ""
        with pytest.raises(TimeoutError):
            adapter.read(4096)
        send(encoded[2:])
        assert adapter.read(4096) == "😀"
    finally:
        close()
    # A kill closes the terminal while its reader thread still reads: that read
    # ends the output like any end of output.
    with pytest.raises(EOFError):
        adapter.read(4096)


def test_posix_write_preserves_partial_unicode_input_until_closed(monkeypatch):
    read_fd, write_fd = os.pipe()
    adapter = terminal_backend._PosixTerminalAdapter(EXITED_PROCESS, write_fd)
    monkeypatch.setattr(terminal_backend.select, "select", lambda *args: ([], [write_fd], []))
    written = bytearray()
    calls = 0

    def partial_write(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise BlockingIOError
        written.extend(data[:2])
        return len(data[:2])

    monkeypatch.setattr(terminal_backend.os, "write", partial_write)
    try:
        adapter.write("😀xyz")
    finally:
        adapter.close()
        os.close(read_fd)
    assert written == "😀xyz".encode()
    # A closed transport ends without touching its former descriptor number.
    with pytest.raises(EOFError):
        adapter.write("more")
    with pytest.raises(EOFError):
        adapter.read(4096)
