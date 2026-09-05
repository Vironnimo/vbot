"""Tests for program-agnostic terminal rendering behavior."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.tools.terminal_backend as terminal_backend
from core.tools.terminal_backend import TERMINAL_TITLE_MAX_CHARS, TerminalRenderer


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


def test_posix_terminal_spawn_uses_shared_server_lifetime_guardian(monkeypatch) -> None:
    inherited: list[int] = []
    spawned: dict[str, object] = {}
    workdir = Path("/work")

    class FakePtyProcess:
        @staticmethod
        def spawn(argv, **kwargs):
            spawned["argv"] = argv
            spawned["kwargs"] = kwargs
            return SimpleNamespace(pid=123)

    monkeypatch.setattr(
        terminal_backend,
        "guarded_process_launch",
        lambda _argv: SimpleNamespace(argv=("guardian", "--", "tool"), pass_fds=(41,)),
    )
    monkeypatch.setattr(
        terminal_backend,
        "_make_file_descriptors_inheritable",
        lambda file_descriptors: inherited.extend(file_descriptors),
    )
    monkeypatch.setitem(sys.modules, "ptyprocess", SimpleNamespace(PtyProcess=FakePtyProcess))

    adapter = terminal_backend.spawn_terminal_adapter(
        ["tool"],
        workdir,
        {"PATH": "/bin"},
        32,
        120,
        platform_name="posix",
    )

    assert adapter.pid == 123
    spawn_kwargs = spawned["kwargs"]
    assert isinstance(spawn_kwargs, dict)
    preexec_fn = spawn_kwargs.pop("preexec_fn")
    assert callable(preexec_fn)
    preexec_fn()
    assert inherited == [41]
    assert spawned == {
        "argv": ["guardian", "--", "tool"],
        "kwargs": {
            "cwd": str(workdir),
            "env": {"PATH": "/bin"},
            "dimensions": (32, 120),
            "pass_fds": (41,),
        },
    }


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
    assert viewer.page(before=None, limit=20) == source.page(before=None, limit=20)
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

    assert late_viewer.page(before=None, limit=20) == source.page(before=None, limit=20)
    assert late_viewer.screen_text() == source.screen_text()


def test_page_from_addresses_whole_buffer_by_absolute_line() -> None:
    renderer = TerminalRenderer(12, 3, scrollback_lines=20)
    renderer.feed("".join(f"line-{index}\r\n" for index in range(8)))

    first = renderer.page_from(0, 3)
    assert first["text"] == "line-0\nline-1\nline-2"
    assert first["line_count"] == 3
    assert first["start_line"] == 0
    assert first["end_line"] == 3
    assert first["next_start_line"] == 3
    assert first["total_lines"] == 8
    assert first["viewport_rows"] == 3
    assert first["cursor_row"] == 8

    next_page = renderer.page_from(first["next_start_line"], 3)
    assert next_page["text"] == "line-3\nline-4\nline-5"
    assert next_page["start_line"] == 3
    assert next_page["end_line"] == 6

    tail = renderer.page_from(6, 100)
    assert tail["text"] == "line-6\nline-7"
    assert tail["line_count"] == 2
    assert tail["end_line"] == 8
    assert tail["next_start_line"] is None


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

    page = renderer.page(before=None, limit=2)

    assert page["text"] == "line-2\nline-3"
    assert page["total_lines"] == 6
    assert page["cursor_row"] == 6
    assert page["viewport_rows"] == 3
    assert page["next_start_line"] is not None
