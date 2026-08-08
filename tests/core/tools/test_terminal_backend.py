"""Tests for program-agnostic terminal rendering behavior."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

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
