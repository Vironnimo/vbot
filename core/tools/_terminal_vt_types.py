"""Shared VT values and constants; standard library and pyte only."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal, NamedTuple

_ALTERNATE_SCREEN_MODES = frozenset({47, 1047, 1049})
_BRACKETED_PASTE_MODE = 2004
_FOCUS_REPORTING_MODE = 1004
_REVERSE_SCREEN_MODE = 5
# Focus reports: a terminal has the focus only while Agent input is under way,
# until the Agent has its reply.
FOCUS_IN_REPORT = "\x1b[I"
FOCUS_OUT_REPORT = "\x1b[O"
# DECCKM: cursor keys send ESC O instead of CSI while it is set.
_APPLICATION_CURSOR_MODE = 1
# Private modes a soft reset (DECSTR) turns off, as xterm does: cursor keys,
# origin, keypad, reverse wrap, focus reports, bracketed paste.
_SOFT_RESET_MODES = frozenset({1, 6, 45, 66, _FOCUS_REPORTING_MODE, _BRACKETED_PASTE_MODE})
TERMINAL_TITLE_MAX_CHARS = 160
# A repeat request (REP) draws at most one screen of characters.
_MAX_REPEAT_SCREENS = 1
# Zero-width characters (combining marks, joiners) one cell keeps at most.
_MAX_CELL_MARKS = 16
# Output the parser reads character by character: C0 controls, DEL and C1
# controls. Everything else is drawn in runs.
_PLAIN_TEXT_RUN = re.compile(r"[^\x00-\x1f\x7f-\x9f]+")
# What ends an OSC payload, and what ends a DCS, SOS, PM or APC string.
_OSC_END = re.compile(r"[\x07\x18\x1a\x1b\x80-\x9f]")
_STRING_END = re.compile(r"[\x18\x1a\x1b\x9c]")
# The C1 controls that open a CSI, an OSC, a DCS and an ignored string.
_CSI_C1 = "\x9b"
_OSC_C1 = "\x9d"
_STRING_C1 = frozenset("\x90\x98\x9e\x9f")
_STRING_ESCAPES = {"P": "\x90", "X": "\x98", "^": "\x9e", "_": "\x9f"}
# CSI parameters beyond this many are ignored; values are capped like pyte's.
_MAX_CSI_PARAMETERS = 32
_MAX_CSI_VALUE = 9999
# An OSC payload is kept up to this many characters (a title needs far fewer).
_OSC_PAYLOAD_MAX_CHARS = 4096
# What the signal scanner looks for between control strings: a whole CSI
# (read in one step), a bell, or the start of another escape sequence.
_SCAN_GROUND = re.compile(r"\x1b\[([0-?]*)[ -/]*([@-~])|\x07|\x1b")
_SCAN_CSI_END = re.compile(r"[@-~\x1b\x18\x1a]")
_SCAN_CSI_PARAMETERS = re.compile(r"[0-?]*")
# A CSI split across chunks is kept up to this many characters; longer ones
# set no mode the scanner reads.
_CSI_PREFIX_CHARS = 32
_SCAN_OSC_END = re.compile(r"[\x07\x1b\x18\x1a]")
_SCAN_STRING_END = re.compile(r"[\x1b\x18\x1a]")
# ConEmu's numbered OSC 9 commands; any other OSC 9 text is a notification.
_OSC9_COMMAND = re.compile(r"\d+(?:;|$)")
# What the Agent's screen text puts around text shown in reverse video, such
# as a selection or a drawn cursor: characters programs practically never print.
HIGHLIGHT_START = "⟦"
HIGHLIGHT_END = "⟧"
# ... and around faint text right of the visible cursor on its row: a
# suggestion such as PSReadLine's prediction or a placeholder, not input.
SUGGESTION_START = "⟪"
SUGGESTION_END = "⟫"
# Erase in Display mode 3: forget the saved lines (scrollback), keep the screen.
_ERASE_SAVED_LINES = 3
# An OSC payload is classified by its start; longer payloads (images,
# clipboard data) are not kept.
_OSC_PREFIX_CHARS = 64
_SCREEN_STATE_FIELDS = (
    "savepoints",
    "columns",
    "lines",
    "buffer",
    "dirty",
    "margins",
    "mode",
    "title",
    "icon_name",
    "charset",
    "g0_charset",
    "g1_charset",
    "tabstops",
    "cursor",
    "saved_columns",
    "_private_modes",
    "bracketed_paste_enabled",
    "_last_graphic",
)
_ANSI_COLOR_CODES = {
    "black": 30,
    "red": 31,
    "green": 32,
    "brown": 33,
    "blue": 34,
    "magenta": 35,
    "cyan": 36,
    "white": 37,
    "brightblack": 90,
    "brightred": 91,
    "brightgreen": 92,
    "brightbrown": 93,
    "brightblue": 94,
    "brightmagenta": 95,
    "brightcyan": 96,
    "brightwhite": 97,
}


class _Cell(NamedTuple):
    """pyte's character cell plus the dim (faint) and concealed attributes pyte lacks."""

    data: str
    fg: str = "default"
    bg: str = "default"
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False
    blink: bool = False
    dim: bool = False
    conceal: bool = False


ViewChange = Literal["same", "returned", "new"]


class EmulatorUpdate(NamedTuple):
    """What one fed chunk changed that the Terminal Session acts on."""

    # Replies to terminal queries, to write back to the program.
    responses: str
    title: str
    bracketed_paste: bool
    alternate_screen: bool
    # The chunk left the alternate screen (the primary screen came back).
    alternate_exited: bool
    # What the program declared about its work in this chunk, in order (see
    # ``SignalScanner``).
    signals: tuple[str, ...] = ()
    # The program asked for focus reports (CSI ? 1004 h) and still wants them.
    focus_reporting: bool = False
    # The chunk erased the whole primary screen: the history line number its
    # first row had then, which the next lines drawn there get again.
    cleared_line: int | None = None
    # Screen operations or sequences of this chunk that failed and were
    # skipped, with the first failure's description (see ``_GuardedScreen``).
    skipped: int = 0
    skipped_error: str = ""
    # Cursor keys send their application form (DECCKM, CSI ? 1 h).
    application_cursor: bool = False
    # How the chunk changed what the terminal shows (its cells, cursor,
    # scrolled lines and title): not at all, back to the view shown right
    # before the current one (as a blinking cursor does), or to a new view.
    view_change: ViewChange = "new"


class ScreenObservation(NamedTuple):
    """The visible screen at one moment, as the Agent is shown it.

    ``screen`` is every row through the last non-blank one: reverse-video
    text between ``HIGHLIGHT_START`` and ``HIGHLIGHT_END``, faint text right
    of the visible cursor on its row between ``SUGGESTION_START`` and
    ``SUGGESTION_END``, concealed text (SGR 8) blank. ``cursor`` is the
    visible cursor's row and column, counted from 1 (None while the program
    hides it). ``screen_start_line`` numbers the first screen row as a history
    line; ``first_line`` is the oldest retained history line. ``cursor_line``
    is the history line of the primary screen's cursor row, hidden or not:
    where the program's next line of output goes.
    """

    signature: str
    screen: str
    cursor: tuple[int, int] | None
    screen_start_line: int
    first_line: int
    cursor_line: int


QueryResponder = Callable[[str, tuple[int, ...], str, str], None]
