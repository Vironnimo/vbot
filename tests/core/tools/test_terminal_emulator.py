"""Terminal emulation: rendered screen, stable history lines, replies and late-viewer snapshots."""

from __future__ import annotations

import asyncio

import pytest

from core.tools.terminal_emulator import (
    HIGHLIGHT_END,
    HIGHLIGHT_START,
    SUGGESTION_END,
    SUGGESTION_START,
    TERMINAL_TITLE_MAX_CHARS,
    TerminalEmulator,
    _EmulatorStream,
)
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
    original = emulator.observe().signature
    emulator.feed("\x1b[1;1H\x1b[?25l")
    assert emulator.observe().signature == original
    emulator.resize(120, 32)
    assert emulator.observe().signature == original
    emulator.feed("\x1b[7mFirst\x1b[0m")
    assert emulator.screen_text() == "First\nSecond"
    assert emulator.observe().signature != original


def test_observation_marks_highlighted_text_and_the_visible_cursor() -> None:
    emulator = TerminalEmulator(12, 4, scrollback_lines=20)
    emulator.feed("a\r\n\x1b[7m> b\x1b[0m c\r\n\x1b[7m bar      \x1b[0m\r\n")

    observed = emulator.observe()
    # Reverse-video text stands between the marks; blank cells ending a row are
    # left out, and so are blank rows below the last drawn one.
    assert observed.screen == (
        f"a\n{HIGHLIGHT_START}> b{HIGHLIGHT_END} c\n{HIGHLIGHT_START} bar{HIGHLIGHT_END}"
    )
    assert observed.cursor == (4, 1)
    # The screen-wide reverse mode changes how the whole screen looks, not what stands out.
    emulator.feed("\x1b[?5h")
    assert emulator.observe().screen == observed.screen
    emulator.feed("\x1b[?5l\x1b[?25l")
    hidden = emulator.observe()
    assert hidden.cursor is None
    # The program's next line goes to the cursor's row, also while the cursor is hidden.
    assert hidden.cursor_line == hidden.screen_start_line + 3


def test_observation_marks_faint_text_after_the_visible_cursor_as_a_suggestion() -> None:
    emulator = TerminalEmulator(30, 3, scrollback_lines=10)
    # PSReadLine draws its prediction faint after the typed text and moves the cursor back.
    emulator.feed("\x1b[2mPS\x1b[0m> Get-Chi\x1b[97;2;3mldItem -Name\x1b[0m\x1b[12G")

    assert emulator.observe().screen == f"PS> Get-Chi{SUGGESTION_START}ldItem -Name{SUGGESTION_END}"
    assert emulator.screen_text() == "PS> Get-ChildItem -Name"
    # A placeholder whose first cell a program draws as its cursor.
    emulator.feed("\r\n> \x1b[2;7mT\x1b[27mry this\x1b[0m\x1b[3G")
    assert emulator.observe().screen.endswith(
        f"> {SUGGESTION_START}{HIGHLIGHT_START}T{HIGHLIGHT_END}ry this{SUGGESTION_END}"
    )
    # Without a visible cursor, faint text is ordinary text.
    emulator.feed("\x1b[?25l")
    assert SUGGESTION_START not in emulator.observe().screen


# The screen as xterm.js 5 (headless, Unicode 11) shows the same output, except
# where a comment says otherwise.
XTERM_SCREENS = [
    # A zero-width character joins the character before it, and drawing goes on;
    # at the start of a row it joins only a row that wrapped into this one.
    pytest.param("ok \u2764\ufe0f done", 20, "ok \u2764\ufe0f done", id="variation-selector"),
    pytest.param("a\u200bb c", 20, "a\u200bb c", id="zero-width-space"),
    pytest.param(
        "\U0001f468\u200d\U0001f4bb dev", 20, "\U0001f468\u200d\U0001f4bb dev", id="joiner"
    ),
    # xterm.js gives the mark a cell of its own, which shows nothing.
    pytest.param("abc\r\n\u0301d", 20, "abc\nd", id="mark-at-row-start"),
    pytest.param("x" * 10 + "\u0301d", 10, "x" * 9 + "x\u0301\nd", id="mark-after-wrap"),
    # A cell keeps 16 marks at most; xterm.js keeps them all.
    pytest.param("a" + "\u0301" * 40 + "b", 20, "\u00e1" + "\u0301" * 16 + "b", id="many-marks"),
    # A narrow character over either half of a wide one blanks the other half.
    pytest.param("12345678\u23f3!\r\n\x1b[1A\x1b[9Gx", 20, "12345678x !", id="wide-right"),
    pytest.param("\u4f60\u597d\x1b[2Gx", 20, " x\u597d", id="wide-left"),
    # A wide character that does not fit wraps whole, leaving the last cell
    # blank in the current attributes; without auto-wrap it is dropped.
    pytest.param("x" * 9 + "\u4f60\u597d|", 10, "x" * 9 + "\n\u4f60\u597d|", id="wide-wraps"),
    pytest.param(
        "x" * 9 + "\x1b[7m\u4f60\x1b[0m|",
        10,
        "x" * 9 + "\u27e6 \u27e7\n\u27e6\u4f60\u27e7|",
        id="wide-wraps-in-attributes",
    ),
    pytest.param("\x1b[?7l" + "x" * 9 + "\u4f60\u597d|", 10, "x" * 9 + "|", id="wide-no-wrap"),
    # ED 3 erases the saved lines only.
    pytest.param("a\r\nb\x1b[3Jc", 20, "a\nbc", id="erase-saved-lines"),
    # DCS, SOS, PM and APC strings show nothing, also in their C1 forms; ESC
    # ends a string and starts the next sequence, CAN aborts it.
    pytest.param("a\x1bPq#0;2;0;0;0#1!3~\x1b\\b", 20, "ab", id="dcs"),
    pytest.param("a\x1bPabc\x07def\x1b\\b", 20, "ab", id="dcs-bell"),
    pytest.param("a\x1b_Gf=24;AAAA\x1b\\b", 20, "ab", id="apc"),
    pytest.param("a\x1bXsos\x1b\\b\x1b^pm\x1b\\c", 20, "abc", id="sos-pm"),
    pytest.param("a\x90abc\x9cb\x98x\x9cc\x9ex\x9cd\x9fx\x9ce", 20, "abcde", id="c1-strings"),
    pytest.param("a\x1bPabc\x1b[7mX\x1b[0mb", 20, "a\u27e6X\u27e7b", id="escape-ends-string"),
    pytest.param("a\x1bPabc\x18def", 20, "adef", id="cancel-ends-string"),
    pytest.param("\x1b]2;tt\x1b[7mX\x1b[0m", 20, "\u27e6X\u27e7", id="escape-ends-osc"),
    # Sequences with intermediates draw nothing; DECSTR resets the attributes.
    pytest.param('a\x1b[1"qb\x1b Fc\x1b[2 qd', 20, "abcd", id="intermediates"),
    pytest.param("\x1b[7ma\x1b[!pb", 20, "\u27e6a\u27e7b", id="soft-reset"),
    # Parameters the parser cannot read end the sequence; ESC starts the next one.
    pytest.param("a\x1b[\u00b2mb", 20, "amb", id="superscript-parameter"),
    pytest.param("a\x1b[" + "1" * 4400 + "mb", 20, "ab", id="long-parameter"),
    pytest.param("a\x1b[1\x1b[7mb\x1b[0m", 20, "a\u27e6b\u27e7", id="escape-in-csi"),
    pytest.param("a\x1b[38:2::255:0:0mb\x1b[4:3mc\x1b[1:2Hd", 20, "dbc", id="colon-sub-parameters"),
    pytest.param("a\x1b[>4;1mb\x1b[=1uc\x1b[<ud\x1b[?1u", 20, "abcd", id="keyboard-modes"),
    pytest.param("a\x85b\x84c", 20, "a\nb\n c", id="c1-next-line-index"),
    # One saved cursor (CSI s, ESC 7), tab stops forward and back, column position.
    pytest.param("ab\x1b[sXYZ\x1b[uQ", 20, "abQYZ", id="save-restore-cursor"),
    pytest.param("a\x1b7b\x1b7c\x1b8d\x1b8e", 20, "abe", id="one-saved-cursor"),
    pytest.param("a\x1b[Ib\x1b[2Ic", 40, "a       b               c", id="tab-forward"),
    pytest.param("\x1b[30Ga\x1b[Zb\x1b[2Zc", 40, " " * 16 + "c       b    a", id="tab-back"),
    pytest.param("abc\x1b[10`X", 20, "abc      X", id="column-position"),
    # DEC Special Graphics in G0 or, shifted out, G1.
    pytest.param("\x1b(0lqqk\x1b(B x", 20, "\u250c\u2500\u2500\u2510 x", id="graphics-g0"),
    pytest.param("\x1b)0a\x0elqqk\x0fb", 20, "a\u250c\u2500\u2500\u2510b", id="graphics-g1"),
    pytest.param(
        "\x1b(0xmjtuvwn~a`\x1b(B",
        20,
        "\u2502\u2514\u2518\u251c\u2524\u2534\u252c\u253c\u00b7\u2592\u25c6",
        id="graphics-set",
    ),
    # The screen-wide reverse mode leaves what stands out as it is.
    pytest.param("a\x1b[7mb\x1b[0mc\x1b[?5h", 20, "a\u27e6b\u27e7c", id="reverse-screen"),
    pytest.param("\x1b[?5ha\x1b[7mb\x1b[0mc", 20, "a\u27e6b\u27e7c", id="reverse-screen-first"),
    # Concealed text is blank, as on a screen; xterm.js keeps it in its buffer.
    pytest.param("a\x1b[8msecret\x1b[28mb\x1b[8mc\x1b[0md", 20, "a      b d", id="conceal"),
]


@pytest.mark.parametrize(("output", "columns", "screen"), XTERM_SCREENS)
def test_screen_shows_what_xterm_shows_whole_or_split(
    output: str, columns: int, screen: str
) -> None:
    whole = TerminalEmulator(columns, 3, scrollback_lines=10)
    split = TerminalEmulator(columns, 3, scrollback_lines=10)

    whole.feed(output)
    for character in output:
        split.feed(character)

    assert whole.observe().screen == split.observe().screen == screen


def test_output_the_screen_cannot_apply_is_skipped_and_drawing_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    emulator = TerminalEmulator(20, 3, scrollback_lines=10)
    # pyte fails on ED 5: the operation is skipped.
    update = emulator.feed("before\x1b[5Jafter")
    assert (emulator.screen_text(), update.skipped) == ("beforeafter", 1)

    # A sequence the parser fails on is skipped as well, and parsing starts over.
    def fail(_payload: str, _listener: object) -> None:
        raise ValueError("broken")

    monkeypatch.setattr(_EmulatorStream, "_dispatch_osc", staticmethod(fail))
    update = emulator.feed("\x1b]0;title\x07 ok")
    assert (emulator.screen_text(), update.skipped) == ("beforeafter ok", 1)
    assert "broken" in update.skipped_error
    assert emulator.feed("\r\nnext").skipped == 0
    assert emulator.screen_text() == "beforeafter ok\nnext"


def test_erasing_saved_lines_keeps_the_screen_and_a_clear_reports_its_first_line() -> None:
    emulator = TerminalEmulator(10, 2, scrollback_lines=10)
    emulator.feed("a\r\nb\r\nc")
    assert history(emulator) == "a"

    erased = emulator.feed("\x1b[3J")
    assert (emulator.screen_text(), history(emulator)) == ("b\nc", "")
    assert erased.cleared_line is None
    # Clearing the screen reports the line number its new content starts at.
    assert emulator.feed("\x1b[2J").cleared_line == emulator.observe().screen_start_line == 1
    assert emulator.feed("d").cleared_line is None


@pytest.mark.parametrize("chunk_size", [1, 7, 1000])
def test_terminal_queries_are_answered_in_the_update(chunk_size: int) -> None:
    emulator = TerminalEmulator(80, 24, scrollback_lines=100)
    output = (
        "\x1b[4;9HReady\x1b[?2004h"
        "\x1b[6n\x1b[?6n\x1b[5n\x1b[c\x1b[>c\x1b[18t"
        "\x1b[?2004$p\x1b[?2026$p\x1b[?7$p"
        # A program that asks for focus reports learns that the terminal has no focus.
        "\x1b[?1004h"
    )
    updates = [
        emulator.feed(output[index : index + chunk_size])
        for index in range(0, len(output), chunk_size)
    ]
    assert "".join(update.responses for update in updates) == (
        "\x1b[4;14R\x1b[?4;14R\x1b[0n\x1b[?6c\x1b[>0;0;0c\x1b[8;24;80t"
        "\x1b[?2004;1$y\x1b[?2026;0$y\x1b[?7;1$y\x1b[O"
    )
    assert updates[-1].focus_reporting is True
    assert emulator.screen_text() == "\n\n\n        Ready"
    emulator.resize(100, 30)
    assert emulator.feed("\x1b[18t").responses == "\x1b[8;30;100t"
    assert emulator.feed("\x1b[?1004l").focus_reporting is False


@pytest.mark.parametrize("chunk_size", [1, 5, 1000])
def test_update_reports_what_the_program_declares_about_its_work(chunk_size: int) -> None:
    """Bells, notifications, progress, shell marks and a line editor's
    bracketed-paste changes are signals, also when split across chunks."""
    output = [
        ("\x07", ["attention"]),
        ("\x1b]9;Build finished\x07", ["attention"]),
        ("\x1b]777;notify;Codex;done\x1b\\", ["attention"]),
        ("\x1b]99;i=1;Task done\x1b\\", ["attention"]),
        ("\x1b]9;4;1;40\x07\x1b]9;4;0;0\x07", ["progress", "progress_end"]),
        ("\x1b]133;A\x07$ make\x1b]133;C\x07", ["prompt", "command"]),
        ("\x1b]633;D;0\x07\x1b]9;12\x07", ["prompt", "prompt"]),
        # A line editor reads a line, reads the next one, and runs it.
        ("\x1b[?2004h>>> \x1b[?2004h", ["prompt"]),
        ("\x1b[?1;2004l", ["command"]),
        # A bell that ends a title or sits in a control string declares nothing,
        # and neither do ConEmu's numbered OSC 9 commands.
        ("\x1b]0;title\x07\x1bP+q\x07\x1b\\\x1b]9;9;C:\\work\x07", []),
    ]
    emulator = TerminalEmulator(80, 24, scrollback_lines=100)
    text = "".join(part for part, _signals in output)

    signals = [
        signal
        for index in range(0, len(text), chunk_size)
        for signal in emulator.feed(text[index : index + chunk_size]).signals
    ]

    assert signals == [signal for _part, expected in output for signal in expected]


def test_terminal_title_uses_vt_metadata_and_is_safe_for_single_line_ui() -> None:
    emulator = TerminalEmulator(20, 2, scrollback_lines=20)

    assert emulator.feed("\x1b]1;  fallback   icon  \x07").title == "fallback icon"

    emulator.feed(f"\x1b]2;  Codex\trefactor  {'x' * 200}\x07")
    assert emulator.title.startswith("Codex refactor ")
    assert len(emulator.title) == TERMINAL_TITLE_MAX_CHARS
    # Only OSC 0, 1 and 2 set it: shell marks and other OSC codes do not.
    title = emulator.title
    emulator.feed("\x1b]133;A\x07\x1b]3;user=me\x07\x1b]1337;CurrentDir=/x\x1b\\\x1b]11;?\x07")
    assert emulator.title == title


def test_update_reports_bracketed_paste_and_cursor_key_modes() -> None:
    emulator = TerminalEmulator(12, 3, scrollback_lines=20)

    assert emulator.feed("\x1b[?2004h").bracketed_paste is True
    assert emulator.feed("text").bracketed_paste is True
    assert emulator.feed("\x1b[?2004l").bracketed_paste is False
    # Cursor keys send their application form while the program sets DECCKM.
    assert emulator.feed("\x1b[?1h").application_cursor is True
    assert emulator.feed("\x1b[?1l").application_cursor is False
    # A soft reset (DECSTR) and a full reset (RIS) turn both off.
    for reset in ("\x1b[!p", "\x1bc"):
        emulator.feed("\x1b[?1;2004h")
        update = emulator.feed(reset)
        assert (update.bracketed_paste, update.application_cursor) == (False, False)


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

    # A full reset (RIS) leaves the alternate screen, blank.
    emulator.feed("\x1b[?1049hfull")
    reset = emulator.feed("\x1bc")
    assert (reset.alternate_screen, reset.alternate_exited) == (False, True)
    assert emulator.screen_text() == ""


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


def test_pattern_text_holds_the_logical_lines_printed_after_the_last_input() -> None:
    emulator = TerminalEmulator(10, 4, scrollback_lines=20)
    # Without input every retained line counts; an auto-wrapped line is one line again,
    # also when its first row scrolled into history.
    emulator.feed("booting\r\n0123456789abcdefghij0123456789xy\r\n> ")
    assert emulator.pattern_text(start_line=None) == {
        "text": "booting\n0123456789abcdefghij0123456789xy\n>",
        "next_line": 1,
    }

    # After input, neither earlier output nor the echo of the input line counts,
    # even when the echo wraps.
    emulator.mark_input()
    assert emulator.pattern_text(start_line=None)["text"] == ""
    emulator.feed("make all the things\r\nbuilt\r\n")
    assert emulator.pattern_text(start_line=None)["text"] == "built"

    # Everything on a cleared screen is new.
    emulator.feed("\x1b[2J\x1b[Hfresh\r\n")
    assert emulator.pattern_text(start_line=None)["text"] == "fresh"

    # next_line starts the logical line above the screen, the first that can still change.
    paged = TerminalEmulator(10, 4, scrollback_lines=20)
    paged.feed("".join(f"line{index}\r\n" for index in range(6)) + "end")
    assert paged.pattern_text(start_line=None)["next_line"] == 2
    assert paged.pattern_text(start_line=2)["text"] == "line2\nline3\nline4\nline5\nend"


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


def test_dim_and_conceal_follow_their_sgr_codes_but_not_color_operands() -> None:
    plain = drawn("")
    assert drawn("\x1b[2m") != plain
    assert drawn("\x1b[1;2m") != drawn("\x1b[1m")
    assert drawn("\x1b[2;22m") == drawn("\x1b[2m\x1b[0m") == drawn("\x1b[2m\x1b[m") == plain
    assert drawn("\x1b[8m") != plain
    assert drawn("\x1b[8;28m") == drawn("\x1b[8m\x1b[0m") == plain
    # A 2 that is a 256-color index or an RGB component sets no dim (22 would clear it).
    assert drawn("\x1b[38;5;2m") == drawn("\x1b[38;5;2;22m") != drawn("\x1b[38;5;2;2m")
    assert drawn("\x1b[48;2;2;2;2m") == drawn("\x1b[48;2;2;2;2;22m")
    # A late viewer draws the cells dim and concealed as well.
    source = TerminalEmulator(20, 2, scrollback_lines=10)
    source.feed("\x1b[2mX\x1b[8mY")
    viewer = TerminalEmulator(20, 2, scrollback_lines=10)
    viewer.feed(source.ansi_snapshot())
    assert viewer.observe().signature == source.observe().signature


@pytest.mark.parametrize(
    ("sequence", "equivalent"),
    [
        ("\x1b[31m", "\x1b[31m"),
        # xterm keyboard modes (modifyOtherKeys, kitty keyboard) change nothing on
        # screen; read as SGR they would underline and embolden every later cell.
        ("\x1b[>4;1m", ""),
        ("\x1b[=1u", ""),
        ("\x1b[<1u", ""),
        # Colon sub-parameters keep their SGR meaning; other sequences ignore them.
        ("\x1b[38:2::1:2:3m", "\x1b[38;2;1;2;3m"),
        ("\x1b[38:2:1:2:3m", "\x1b[38;2;1;2;3m"),
        ("\x1b[1;48:5:196m", "\x1b[1;48;5;196m"),
        ("\x1b[4:3m", "\x1b[4m"),
        ("\x1b[4m\x1b[4:0m", ""),
        ("\x1b[58:2::9:9:9m", ""),
        ("\x1b[1:2u", "\x1b[u"),
        # Control strings, shell marks and a soft reset show nothing.
        ("\x1bP+q544e\x1b\\", ""),
        ("\x9fGa=q\x9c", ""),
        ("\x1b]133;A\x07", ""),
        ("\x1b[1;7m\x1b[!p", ""),
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
    assert viewer.observe().signature == source.observe().signature
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

    # The character set in use carries over to the output a viewer draws next.
    source = TerminalEmulator(12, 3, scrollback_lines=20)
    source.feed("\x1b(0lqk")
    viewer = TerminalEmulator(12, 3, scrollback_lines=20)
    viewer.feed(source.ansi_snapshot() + "x")
    source.feed("x")
    assert viewer.screen_text() == source.screen_text() == "\u250c\u2500\u2510\u2502"


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
