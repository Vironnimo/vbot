"""Terminal emulation: rendered screen, stable history lines, replies and late-viewer snapshots."""

from __future__ import annotations

import asyncio

import pytest

from core.tools.terminal_emulator import TERMINAL_TITLE_MAX_CHARS, TerminalEmulator
from core.tools.terminal_manager import TerminalRenderHost


def history(emulator: TerminalEmulator) -> str:
    """The retained lines above the screen."""
    return str(emulator.page(start_line=None, limit=100)["text"])


def drawn(sequence: str) -> str:
    """The late-viewer snapshot of a fresh terminal that drew ``X`` after *sequence*."""
    emulator = TerminalEmulator(20, 2, scrollback_lines=10)
    emulator.feed(sequence + "X")
    return emulator.ansi_snapshot()


def test_screen_signature_tracks_styles_but_ignores_cursor_and_empty_extent() -> None:
    emulator = TerminalEmulator(80, 24, scrollback_lines=100)
    emulator.feed("First\r\nSecond")
    original = emulator.observe(0).signature
    emulator.feed("\x1b[1;1H\x1b[?25l")
    assert emulator.observe(0).signature == original
    emulator.resize(120, 32)
    assert emulator.observe(0).signature == original
    emulator.feed("\x1b[7mFirst\x1b[0m")
    assert emulator.screen_text() == "First\nSecond"
    assert emulator.observe(0).signature != original


def test_observation_tail_holds_the_newest_non_blank_rows() -> None:
    emulator = TerminalEmulator(12, 4, scrollback_lines=20)
    emulator.feed("a\r\nb\r\nc")

    assert [emulator.observe(rows).tail for rows in (2, 10, 0)] == ["b\nc", "a\nb\nc", ""]


@pytest.mark.parametrize("chunk_size", [1, 7, 1000])
def test_terminal_queries_are_answered_in_the_update(chunk_size: int) -> None:
    emulator = TerminalEmulator(80, 24, scrollback_lines=100)
    output = (
        "\x1b[4;9HReady\x1b[?2004h"
        "\x1b[6n\x1b[?6n\x1b[5n\x1b[c\x1b[>c\x1b[18t"
        "\x1b[?2004$p\x1b[?2026$p\x1b[?7$p"
    )
    responses = "".join(
        emulator.feed(output[index : index + chunk_size]).responses
        for index in range(0, len(output), chunk_size)
    )
    assert responses == (
        "\x1b[4;14R\x1b[?4;14R\x1b[0n\x1b[?6c\x1b[>0;0;0c\x1b[8;24;80t"
        "\x1b[?2004;1$y\x1b[?2026;0$y\x1b[?7;1$y"
    )
    assert emulator.screen_text() == "\n\n\n        Ready"
    emulator.resize(100, 30)
    assert emulator.feed("\x1b[18t").responses == "\x1b[8;30;100t"


def test_terminal_title_uses_vt_metadata_and_is_safe_for_single_line_ui() -> None:
    emulator = TerminalEmulator(20, 2, scrollback_lines=20)

    assert emulator.feed("\x1b]1;  fallback   icon  \x07").title == "fallback icon"

    emulator.feed(f"\x1b]2;  Codex\trefactor  {'x' * 200}\x07")
    assert emulator.title.startswith("Codex refactor ")
    assert len(emulator.title) == TERMINAL_TITLE_MAX_CHARS


def test_update_reports_bracketed_paste_mode() -> None:
    emulator = TerminalEmulator(12, 3, scrollback_lines=20)

    assert emulator.feed("\x1b[?2004h").bracketed_paste is True
    assert emulator.feed("text").bracketed_paste is True
    assert emulator.feed("\x1b[?2004l").bracketed_paste is False


def test_alternate_screen_is_rendered_and_primary_screen_restores_after_resize() -> None:
    emulator = TerminalEmulator(12, 3, scrollback_lines=20)
    assert emulator.feed("primary").alternate_screen is False

    entered = emulator.feed("\x1b[?1049h\x1b[2J\x1b[Hgame\x1b[2;1Hscore: 7")
    assert (entered.alternate_screen, entered.alternate_exited) == (True, False)
    assert emulator.screen_text() == "game\nscore: 7"
    assert "game" in emulator.ansi_snapshot()
    assert "primary" not in emulator.ansi_snapshot()

    emulator.resize(16, 4)
    emulator.feed("\x1b[3;1Hresized")
    assert emulator.screen_text() == "game\nscore: 7\nresized"

    left = emulator.feed("\x1b[?1049l")
    assert (left.alternate_screen, left.alternate_exited) == (False, True)
    assert (emulator.columns, emulator.rows) == (16, 4)
    assert emulator.screen_text() == "primary"
    assert "primary" in emulator.ansi_snapshot()


def test_page_addresses_retained_lines_by_stable_number() -> None:
    emulator = TerminalEmulator(12, 3, scrollback_lines=4)
    assert emulator.page(start_line=None, limit=3) == {
        "text": "",
        "first_line": 0,
        "start_line": 0,
        "end_line": 0,
        "total_lines": 0,
        "screen_start_line": 0,
        "previous_start_line": None,
        "next_start_line": None,
    }
    # Lines 0-7 scrolled off; the newest four of them are retained.
    emulator.feed("".join(f"line-{index}\r\n" for index in range(10)) + "prompt")

    assert emulator.page(start_line=None, limit=3) == {
        "text": "line-5\nline-6\nline-7",
        "first_line": 4,
        "start_line": 5,
        "end_line": 8,
        "total_lines": 11,
        "screen_start_line": 8,
        "previous_start_line": 4,
        "next_start_line": None,
    }
    # A number before the oldest retained line reads from that line.
    oldest = emulator.page(start_line=0, limit=2)
    assert (oldest["text"], oldest["start_line"], oldest["previous_start_line"]) == (
        "line-4\nline-5",
        4,
        None,
    )
    assert oldest["next_start_line"] == 6
    # A page runs on into the screen.
    into_screen = emulator.page(start_line=7, limit=3)
    assert (into_screen["text"], into_screen["next_start_line"]) == ("line-7\nline-8\nline-9", 10)
    beyond = emulator.page(start_line=99, limit=3)
    assert (beyond["text"], beyond["start_line"], beyond["end_line"]) == ("", 11, 11)
    assert (beyond["previous_start_line"], beyond["next_start_line"]) == (8, None)

    # A number keeps naming its line while new output scrolls the screen.
    emulator.feed("\r\nmore\r\nmore")
    assert emulator.page(start_line=7, limit=3)["text"] == "line-7\nline-8\nline-9"
    assert emulator.page(start_line=None, limit=1)["screen_start_line"] == 10


def test_only_scrolling_from_the_top_row_of_the_primary_screen_keeps_history() -> None:
    emulator = TerminalEmulator(20, 5, scrollback_lines=10)
    emulator.feed("one\r\ntwo\r\nthree\r\nfour\r\nstatus")

    # Like xterm, a region that starts at the top row scrolls its top line into history.
    emulator.feed("\x1b[1;4r\x1b[4;1H\r\nfive")
    assert emulator.screen_text() == "two\nthree\nfour\nfive\nstatus"
    assert history(emulator) == "one"

    # A region lower on the screen, such as an inline UI below earlier output,
    # discards the lines it scrolls instead of repeating them in history.
    emulator.feed("\x1b[3;4r\x1b[4;1H\r\nsix")
    assert emulator.screen_text() == "two\nthree\nfive\nsix\nstatus"
    assert history(emulator) == "one"

    # The alternate screen keeps no history either.
    emulator.feed("\x1b[?1049h" + "full\r\n" * 10 + "\x1b[?1049l")
    assert history(emulator) == "one"


def test_shrinking_moves_rows_above_the_cursor_into_history() -> None:
    emulator = TerminalEmulator(20, 5, scrollback_lines=10)
    emulator.feed("1\r\n2\r\n3\r\n4\r\n5")

    emulator.resize(20, 3)

    assert emulator.screen_text() == "3\n4\n5"
    assert history(emulator) == "1\n2"
    assert emulator.page(start_line=None, limit=10)["screen_start_line"] == 2

    # Blank rows below the cursor go first.
    short = TerminalEmulator(20, 5, scrollback_lines=10)
    short.feed("1\r\n2")
    short.resize(20, 3)
    assert (short.screen_text(), history(short)) == ("1\n2", "")


def test_scroll_up_and_down_move_the_region_and_keep_the_cursor() -> None:
    emulator = TerminalEmulator(20, 3, scrollback_lines=10)
    emulator.feed("a\r\nb\r\nc")

    emulator.feed("\x1b[S")
    assert (emulator.screen_text(), history(emulator)) == ("b\nc", "a")

    emulator.feed("\x1b[2T")
    assert emulator.screen_text() == "\n\nb"
    # CSI T with several values starts mouse highlight tracking; it scrolls nothing.
    emulator.feed("\x1b[1;2;3;4;5T")
    assert emulator.screen_text() == "\n\nb"

    emulator.feed("!")
    assert emulator.screen_text() == "\n\nb!"


def test_repeat_draws_the_last_character_again_at_most_one_screen_long() -> None:
    emulator = TerminalEmulator(20, 3, scrollback_lines=10)
    emulator.feed("\x1b[5b")
    assert emulator.screen_text() == ""

    emulator.feed("ab\x1b[3b")
    assert emulator.screen_text() == "abbbb"

    emulator.feed("\x1b[1000b")
    everything = emulator.page(start_line=0, limit=100)["text"]
    assert everything.count("b") == 4 + 20 * 3


def test_dim_follows_sgr_2_22_and_0_but_not_color_operands() -> None:
    plain = drawn("")
    assert drawn("\x1b[2m") != plain
    assert drawn("\x1b[1;2m") != drawn("\x1b[1m")
    assert drawn("\x1b[2;22m") == drawn("\x1b[2m\x1b[0m") == drawn("\x1b[2m\x1b[m") == plain
    # A 2 that is a 256-color index or an RGB component sets no dim (22 would clear it).
    assert drawn("\x1b[38;5;2m") == drawn("\x1b[38;5;2;22m") != drawn("\x1b[38;5;2;2m")
    assert drawn("\x1b[48;2;2;2;2m") == drawn("\x1b[48;2;2;2;2;22m")
    # A late viewer draws the cell dim as well.
    source = TerminalEmulator(20, 2, scrollback_lines=10)
    source.feed("\x1b[2mX")
    viewer = TerminalEmulator(20, 2, scrollback_lines=10)
    viewer.feed(source.ansi_snapshot())
    assert viewer.observe(0).signature == source.observe(0).signature


@pytest.mark.parametrize(
    ("sequence", "equivalent"),
    [
        ("\x1b[31m", "\x1b[31m"),
        # xterm keyboard modes (modifyOtherKeys, kitty keyboard) change nothing on
        # screen; read as SGR they would underline and embolden every later cell.
        ("\x1b[>4;1m", ""),
        ("\x1b[=1u", ""),
        ("\x1b[<1u", ""),
        # Colon sub-parameters keep their SGR meaning; other colon sequences are dropped.
        ("\x1b[38:2::1:2:3m", "\x1b[38;2;1;2;3m"),
        ("\x1b[38:2:1:2:3m", "\x1b[38;2;1;2;3m"),
        ("\x1b[1;48:5:196m", "\x1b[1;48;5;196m"),
        ("\x1b[4:3m", "\x1b[4m"),
        ("\x1b[4m\x1b[4:0m", ""),
        ("\x1b[58:2::9:9:9m", ""),
        ("\x1b[1:2u", ""),
    ],
)
def test_sequences_render_as_their_equivalent_whole_or_split(
    sequence: str, equivalent: str
) -> None:
    reference = TerminalEmulator(40, 3, scrollback_lines=10)
    # Erasing the line draws blank cells with the active attributes.
    reference.feed("before" + equivalent + "after\x1b[K")
    for boundary in range(len(sequence) + 1):
        emulator = TerminalEmulator(40, 3, scrollback_lines=10)
        emulator.feed("before" + sequence[:boundary])
        emulator.feed(sequence[boundary:] + "after\x1b[K")
        assert emulator.ansi_snapshot() == reference.ansi_snapshot(), boundary


@pytest.mark.parametrize("alternate", [False, True])
@pytest.mark.parametrize(
    "text", ["\u4e2d\u6587AB", "\U0001f600AB", "e\u0301\u4e2d", "x" * 38 + "\u4e2d"]
)
def test_unicode_snapshot_preserves_screen_and_cursor(text: str, alternate: bool) -> None:
    source = TerminalEmulator(40, 10, scrollback_lines=20)
    if alternate:
        source.feed("\x1b[?1049h")
    source.feed(text + "\r\n\x1b[31mTAIL\x1b[0m")
    viewer = TerminalEmulator(40, 10, scrollback_lines=20)
    viewer.feed(source.ansi_snapshot())

    assert viewer.screen_text() == source.screen_text()
    # The same cells with the same styles, and the cursor in the same place.
    assert viewer.observe(0).signature == source.observe(0).signature
    assert viewer.feed("\x1b[6n").responses == source.feed("\x1b[6n").responses


def test_ansi_snapshot_reemits_alternate_screen_and_terminal_modes() -> None:
    emulator = TerminalEmulator(12, 3, scrollback_lines=20)
    emulator.feed("primary")

    emulator.feed("\x1b[?1049h\x1b[?1h\x1b[?1000h\x1b[?2004h\x1b[2J\x1b[Hgame")
    assert emulator.screen_text() == "game"

    snapshot = emulator.ansi_snapshot()
    for mode in ("\x1b[?1049h", "\x1b[?1h", "\x1b[?1000h", "\x1b[?2004h"):
        assert mode in snapshot
    assert "game" in snapshot

    emulator.feed("\x1b[?1049l")
    assert emulator.screen_text() == "primary"
    snapshot = emulator.ansi_snapshot()
    for mode in ("\x1b[?1049h", "\x1b[?1h", "\x1b[?1000h", "\x1b[?2004h"):
        assert mode not in snapshot


def test_ansi_snapshot_preserves_styles_cursor_and_visibility() -> None:
    emulator = TerminalEmulator(10, 2, scrollback_lines=20)
    emulator.feed("\x1b[31;1mred\x1b[0m\x1b[2;4Htail\x1b[?25l")

    snapshot = emulator.ansi_snapshot()

    assert "\x1b[0;31;49;1mred" in snapshot
    assert "tail" in snapshot
    assert snapshot.endswith("\x1b[2;8H\x1b[?25l")


def test_ansi_snapshot_rebuilds_bounded_scrollback_for_a_late_viewer() -> None:
    source = TerminalEmulator(12, 3, scrollback_lines=4)
    source.feed("".join(f"line-{index}\r\n" for index in range(8)))
    source.feed("\x1b[31mFINAL\x1b[0m")

    late_viewer = TerminalEmulator(12, 3, scrollback_lines=20)
    late_viewer.feed(source.ansi_snapshot())

    # The viewer numbers the lines it received from 0; the text is the same.
    assert history(late_viewer) == history(source) == "line-2\nline-3\nline-4\nline-5"
    assert late_viewer.screen_text() == source.screen_text()


@pytest.mark.asyncio
async def test_render_host_renders_on_a_worker_until_it_is_closed() -> None:
    host = TerminalRenderHost(workers=1)
    try:
        screen = host.open_screen(40, 10, scrollback_lines=10)
        update = await screen.feed("\x1b]0;hosted\x07one\r\ntwo\x1b[6n")
        assert (update.title, update.responses) == ("hosted", "\x1b[2;4R")
        assert await screen.screen_text() == "one\ntwo"
        assert (await screen.page(start_line=0, limit=5))["text"] == "one\ntwo"
    finally:
        host.close()

    # An inline screen would keep working; a stopped worker refuses at once.
    async with asyncio.timeout(5):
        with pytest.raises(RuntimeError):
            await screen.screen_text()
    with pytest.raises(RuntimeError):
        host.open_screen(40, 10, scrollback_lines=10)
