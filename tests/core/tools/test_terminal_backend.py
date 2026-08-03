"""Tests for program-agnostic terminal rendering behavior."""

from __future__ import annotations

from core.tools.terminal_backend import TerminalRenderer


def test_alternate_screen_is_rendered_and_primary_screen_restores_after_resize() -> None:
    renderer = TerminalRenderer(12, 3, scrollback_lines=20)
    renderer.feed("primary")

    renderer.feed("\x1b[?1049h\x1b[2J\x1b[Hgame\x1b[2;1Hscore: 7")

    assert renderer.screen_text() == "game\nscore: 7"
    assert "game" in renderer.ansi_snapshot()
    assert "primary" not in renderer.ansi_snapshot()

    renderer.resize(16, 4)
    renderer.feed("\x1b[3;1Hresized")
    assert renderer.screen_text() == "game\nscore: 7\nresized"

    renderer.feed("\x1b[?1049l")

    assert renderer.columns == 16
    assert renderer.rows == 4
    assert renderer.screen_text() == "primary"
    assert "primary" in renderer.ansi_snapshot()


def test_ansi_snapshot_preserves_styles_cursor_and_visibility() -> None:
    renderer = TerminalRenderer(10, 2, scrollback_lines=20)
    renderer.feed("\x1b[31;1mred\x1b[0m\x1b[2;4Htail\x1b[?25l")

    snapshot = renderer.ansi_snapshot()

    assert "\x1b[31;1m" in snapshot or "\x1b[0;31;49;1m" in snapshot
    assert "red" in snapshot
    assert "tail" in snapshot
    assert snapshot.endswith("\x1b[?25l")
