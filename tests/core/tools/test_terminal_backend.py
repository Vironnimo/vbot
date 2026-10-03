"""Tests for program-agnostic terminal rendering and the PTY/ConPTY transport."""

from __future__ import annotations

import ast
import contextlib
import os
import re
import shlex
import signal
import socket
import sys
import threading
import time
import warnings
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.tools.terminal_backend as terminal_backend
from core.tools.terminal_backend import TERMINAL_TITLE_MAX_CHARS, TerminalRenderer
from core.utils import processes as process_utils

# The adapter's child process, already ended, for tests of its descriptor I/O.
EXITED_PROCESS = SimpleNamespace(pid=0, poll=lambda: 0, wait=lambda timeout=None: 0)


def test_screen_signature_tracks_styles_but_ignores_cursor_and_empty_extent() -> None:
    renderer = TerminalRenderer(80, 24, scrollback_lines=100)
    renderer.feed("First\r\nSecond")
    original = renderer.screen_signature()
    renderer.feed("\x1b[1;1H\x1b[?25l")
    assert renderer.screen_signature() == original
    renderer.resize(120, 32)
    assert renderer.screen_signature() == original
    renderer.feed("\x1b[7mFirst\x1b[0m")
    assert renderer.screen_text() == "First\nSecond"
    assert renderer.screen_signature() != original


@pytest.mark.parametrize("chunk_size", [1, 7, 1000])
def test_terminal_queries_use_canonical_cursor_modes_and_size(chunk_size: int) -> None:
    renderer = TerminalRenderer(80, 24, scrollback_lines=100)
    output = (
        "\x1b[4;9HReady\x1b[?2004h"
        "\x1b[6n\x1b[?6n\x1b[5n\x1b[c\x1b[>c\x1b[18t"
        "\x1b[?2004$p\x1b[?2026$p\x1b[?7$p"
    )
    responses = []
    for index in range(0, len(output), chunk_size):
        renderer.feed(output[index : index + chunk_size])
        responses.append(renderer.take_responses())
    assert "".join(responses) == (
        "\x1b[4;14R\x1b[?4;14R\x1b[0n\x1b[?6c\x1b[>0;0;0c\x1b[8;24;80t"
        "\x1b[?2004;1$y\x1b[?2026;0$y\x1b[?7;1$y"
    )
    assert renderer.take_responses() == ""
    assert renderer.screen_text() == "\n\n\n        Ready"
    renderer.resize(100, 30)
    renderer.feed("\x1b[18t")
    assert renderer.take_responses() == "\x1b[8;30;100t"


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


def test_terminal_title_uses_vt_metadata_and_is_safe_for_single_line_ui() -> None:
    renderer = TerminalRenderer(20, 2, scrollback_lines=20)

    renderer.feed("\x1b]1;  fallback   icon  \x07")
    assert renderer.title == "fallback icon"

    renderer.feed(f"\x1b]2;  Codex\trefactor  {'x' * 200}\x07")
    assert renderer.title.startswith("Codex refactor ")
    assert len(renderer.title) == TERMINAL_TITLE_MAX_CHARS


def test_alternate_screen_is_rendered_and_primary_screen_restores_after_resize() -> None:
    renderer = TerminalRenderer(12, 3, scrollback_lines=20)
    renderer.feed("primary")

    assert renderer.feed("\x1b[?1049h\x1b[2J\x1b[Hgame\x1b[2;1Hscore: 7") is False

    assert renderer.screen_text() == "game\nscore: 7"
    assert "game" in renderer.ansi_snapshot()
    assert "primary" not in renderer.ansi_snapshot()

    renderer.resize(16, 4)
    renderer.feed("\x1b[3;1Hresized")
    assert renderer.screen_text() == "game\nscore: 7\nresized"

    assert renderer.feed("\x1b[?1049l") is True

    assert renderer.columns == 16
    assert renderer.rows == 4
    assert renderer.screen_text() == "primary"
    assert "primary" in renderer.ansi_snapshot()


def test_renderer_tracks_bracketed_paste_mode() -> None:
    renderer = TerminalRenderer(12, 3, scrollback_lines=20)

    renderer.feed("\x1b[?2004h")
    assert renderer.bracketed_paste_enabled is True

    renderer.feed("\x1b[?2004l")
    assert renderer.bracketed_paste_enabled is False


def test_keyboard_mode_sequences_do_not_corrupt_cell_attributes() -> None:
    renderer = TerminalRenderer(20, 3, scrollback_lines=20)

    # opencode2 enables xterm modifyOtherKeys with CSI > 4 ; 1 m. pyte
    # ignores the ">" prefix and would misread it as SGR underscore+bold,
    # painting white underlines under every blank cell in snapshots.
    renderer.feed("\x1b[>4;1m\x1b[2J\x1b[H")

    assert all(not renderer._screen.buffer[0][col].underscore for col in range(20))
    assert all(not renderer._screen.buffer[0][col].bold for col in range(20))
    assert ";4m" not in renderer.ansi_snapshot()

    renderer.feed("\x1b[>4;2m\x1b[1;1Htext")
    assert renderer.screen_text() == "text"


def test_keyboard_mode_sequence_split_across_feeds_is_dropped() -> None:
    renderer = TerminalRenderer(20, 3, scrollback_lines=20)

    renderer.feed("a\x1b[>4")
    renderer.feed(";1mb")

    assert renderer.screen_text().startswith("ab")
    assert all(not renderer._screen.buffer[0][col].underscore for col in range(20))
    assert all(not renderer._screen.buffer[0][col].bold for col in range(20))


@pytest.mark.parametrize("sequence", ["\x1b[>4;1m", "\x1b[=1u", "\x1b[<1u"])
def test_keyboard_modes_are_filtered_at_every_stream_boundary(sequence: str) -> None:
    for boundary in range(1, len(sequence)):
        renderer = TerminalRenderer(40, 10, scrollback_lines=20)
        renderer.feed("before" + sequence[:boundary])
        renderer.feed(sequence[boundary:] + "after")
        assert renderer.screen_text() == "beforeafter"
        assert not renderer._screen.cursor.attrs.underscore
        assert not renderer._screen.cursor.attrs.bold


@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize(
    "text", ["\u4e2d\u6587AB", "\U0001f600AB", "e\u0301\u4e2d", "x" * 38 + "\u4e2d"]
)
def test_unicode_snapshot_preserves_screen_and_cursor(text: str, alternate: bool) -> None:
    source = TerminalRenderer(40, 10, scrollback_lines=20)
    if alternate:
        source.feed("\x1b[?1049h")
    source.feed(text + "\r\n\x1b[31mTAIL\x1b[0m")
    viewer = TerminalRenderer(40, 10, scrollback_lines=20)
    viewer.feed(source.ansi_snapshot())
    assert viewer.screen_text() == source.screen_text()
    assert viewer.page(limit=20) == source.page(limit=20)
    assert (viewer._screen.cursor.x, viewer._screen.cursor.y) == (
        source._screen.cursor.x,
        source._screen.cursor.y,
    )
    assert viewer._screen.buffer[1][0].fg == "red"


def test_ansi_snapshot_reemits_alternate_screen_and_terminal_modes() -> None:
    renderer = TerminalRenderer(12, 3, scrollback_lines=20)
    renderer.feed("primary")

    renderer.feed("\x1b[?1049h\x1b[?1h\x1b[?1000h\x1b[?2004h\x1b[2J\x1b[Hgame")
    assert renderer.screen_text() == "game"

    snapshot = renderer.ansi_snapshot()
    assert "\x1b[?1049h" in snapshot
    assert "\x1b[?1h" in snapshot
    assert "\x1b[?1000h" in snapshot
    assert "\x1b[?2004h" in snapshot
    assert "game" in snapshot

    renderer.feed("\x1b[?1049l")
    assert renderer.screen_text() == "primary"
    assert "\x1b[?1049h" not in renderer.ansi_snapshot()
    assert "\x1b[?1h" not in renderer.ansi_snapshot()
    assert "\x1b[?1000h" not in renderer.ansi_snapshot()
    assert "\x1b[?2004h" not in renderer.ansi_snapshot()


def test_ansi_snapshot_preserves_styles_cursor_and_visibility() -> None:
    renderer = TerminalRenderer(10, 2, scrollback_lines=20)
    renderer.feed("\x1b[31;1mred\x1b[0m\x1b[2;4Htail\x1b[?25l")

    snapshot = renderer.ansi_snapshot()

    assert "\x1b[31;1m" in snapshot or "\x1b[0;31;49;1m" in snapshot
    assert "red" in snapshot
    assert "tail" in snapshot
    assert snapshot.endswith("\x1b[?25l")


def test_ansi_snapshot_rebuilds_bounded_scrollback_for_a_late_viewer() -> None:
    source = TerminalRenderer(12, 3, scrollback_lines=4)
    source.feed("".join(f"line-{index}\r\n" for index in range(8)))
    source.feed("\x1b[31mFINAL\x1b[0m")

    late_viewer = TerminalRenderer(12, 3, scrollback_lines=20)
    late_viewer.feed(source.ansi_snapshot())

    assert late_viewer.page(limit=20) == source.page(limit=20)
    assert late_viewer.screen_text() == source.screen_text()


def test_page_from_handles_empty_and_overflow_addresses() -> None:
    renderer = TerminalRenderer(12, 3, scrollback_lines=20)
    renderer.feed("")

    empty = renderer.page_from(0, 3)
    assert empty["text"] == ""
    assert empty["total_lines"] == 0
    assert empty["start_line"] == 0
    assert empty["end_line"] == 0
    assert empty["next_start_line"] is None
    assert empty["cursor_row"] == 0

    renderer.feed("one\ntwo")
    beyond = renderer.page_from(99, 3)
    assert beyond["total_lines"] == 2
    assert beyond["start_line"] == 2
    assert beyond["line_count"] == 0
    assert beyond["end_line"] == 2
    assert beyond["next_start_line"] is None


def test_screen_tail_returns_newest_non_blank_rows() -> None:
    renderer = TerminalRenderer(12, 3, scrollback_lines=20)
    renderer.feed("a\r\nb\r\nc")

    assert renderer.screen_tail(2) == "b\nc"
    assert renderer.screen_tail(10) == "a\nb\nc"
    assert renderer.screen_tail(0) == ""


def test_cursor_page_carries_absolute_buffer_metrics() -> None:
    renderer = TerminalRenderer(12, 3, scrollback_lines=20)
    renderer.feed("".join(f"line-{index}\r\n" for index in range(6)))

    page = renderer.page(limit=2)

    assert page["text"] == "line-2\nline-3"
    assert page["total_lines"] == 6
    assert page["cursor_row"] == 6
    assert page["viewport_rows"] == 3
    assert page["next_start_line"] is not None


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
