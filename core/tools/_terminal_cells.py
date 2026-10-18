"""Terminal cell rendering and ANSI attribute serialization."""

from __future__ import annotations

from typing import Any

from pyte.screens import wcwidth

from ._terminal_vt_types import (
    _ANSI_COLOR_CODES,
    HIGHLIGHT_END,
    HIGHLIGHT_START,
    SUGGESTION_END,
    SUGGESTION_START,
    TERMINAL_TITLE_MAX_CHARS,
)


def _sgr_attributes(groups: list[list[int]]) -> list[int]:
    """SGR attributes with colon sub-parameters in the semicolon form pyte reads."""
    attributes: list[int] = []
    for values in groups:
        head = values[0]
        if len(values) == 1:
            attributes.append(head)
        elif head in (38, 48):
            if values[1] == 5 and len(values) >= 3:
                attributes.extend((head, 5, values[2]))
            elif values[1] == 2 and len(values) >= 5:
                # 38:2:<colorspace>:R:G:B or 38:2:R:G:B
                attributes.extend((head, 2, *values[-3:]))
        elif head == 4:
            attributes.append(24 if values[1] == 0 else 4)
        elif head != 58:  # 58: underline color, not rendered
            attributes.append(head)
    return attributes


def _is_wide(data: str) -> bool:
    """Whether a cell's character takes two cells."""
    return bool(data) and not data.isascii() and wcwidth(data[0]) == 2


def _visible_cells(line: Any, columns: int, *, conceal: bool) -> list[tuple[int, Any, str]]:
    """Each cell of a row a viewer sees, with its column and the text it shows.

    pyte keeps a wide character's second cell as an empty one, skipped here.
    Like a real terminal, a wide character a narrow one half overwrote is
    gone: both of its cells show a space instead of pyte's broken pair.
    With *conceal*, concealed cells (SGR 8) show blanks, as on a screen.
    """
    cells: list[tuple[int, Any, str]] = []
    wide = False
    for column in range(columns):
        cell = line[column]
        data = cell.data
        if wide:
            wide = False
            if not data:
                continue
            previous_column, previous, _text = cells[-1]
            cells[-1] = (previous_column, previous, " ")
        if not data:
            cells.append((column, cell, " "))
            continue
        wide = not data.isascii() and wcwidth(data[0]) == 2
        if conceal and getattr(cell, "conceal", False):
            data = "  " if wide else " "
        cells.append((column, cell, data))
    return cells


def _render_buffer_line(line: Any, columns: int, *, strip: bool = True) -> str:
    if strip:
        # Cells never written are blank and would be stripped anyway.
        columns = min(columns, max(line.keys(), default=-1) + 1)
    text = "".join(text for _column, _cell, text in _visible_cells(line, columns, conceal=True))
    return text.rstrip() if strip else text


def _marked_row(line: Any, columns: int, *, suggestion_from: int | None) -> str:
    """A row's text as the Agent reads it, with marks around what it cannot see as text.

    Reverse-video runs stand between the highlight marks. From column
    *suggestion_from* on (the visible cursor's, then zero on its wrapped rows), faint text
    stands between the suggestion marks: a prediction or placeholder that
    Enter does not submit. Blank cells at the end of the row are left out,
    also inside a highlighted run, which keeps at least one.
    """
    # Runs of (suggestion, highlighted, text).
    runs: list[tuple[bool, bool, str]] = []
    for column, cell, text in _visible_cells(line, columns, conceal=True):
        highlighted = bool(cell.reverse)
        suggestion = suggestion_from is not None and column >= suggestion_from and cell.dim
        if runs and runs[-1][:2] == (suggestion, highlighted):
            runs[-1] = (suggestion, highlighted, runs[-1][2] + text)
        else:
            runs.append((suggestion, highlighted, text))
    # Blank faint text shows nothing to mark.
    runs = [
        (suggestion and bool(text.strip()), highlighted, text)
        for suggestion, highlighted, text in runs
    ]
    while runs and not runs[-1][1] and not runs[-1][2].strip():
        runs.pop()
    if not runs:
        return ""
    suggestion, highlighted, text = runs[-1]
    runs[-1] = (suggestion, highlighted, text.rstrip() or " ")
    parts: list[str] = []
    in_suggestion = False
    for suggestion, highlighted, text in runs:
        if suggestion != in_suggestion:
            parts.append(SUGGESTION_START if suggestion else SUGGESTION_END)
            in_suggestion = suggestion
        parts.append(f"{HIGHLIGHT_START}{text}{HIGHLIGHT_END}" if highlighted else text)
    if in_suggestion:
        parts.append(SUGGESTION_END)
    return "".join(parts)


def _normalize_terminal_title(value: object) -> str:
    printable = "".join(
        character if character.isprintable() else " " for character in str(value or "")
    )
    return " ".join(printable.split())[:TERMINAL_TITLE_MAX_CHARS]


def _cell_style(cell: Any) -> tuple[Any, ...]:
    return (
        cell.fg,
        cell.bg,
        cell.bold,
        cell.italics,
        cell.underscore,
        cell.strikethrough,
        cell.reverse,
        cell.blink,
        getattr(cell, "dim", False),
        getattr(cell, "conceal", False),
    )


def _style_sequence(cell: Any) -> str:
    codes = [0]
    codes.extend(_ansi_color(cell.fg, background=False))
    codes.extend(_ansi_color(cell.bg, background=True))
    if cell.bold:
        codes.append(1)
    if getattr(cell, "dim", False):
        codes.append(2)
    if cell.italics:
        codes.append(3)
    if cell.underscore:
        codes.append(4)
    if cell.blink:
        codes.append(5)
    if cell.reverse:
        codes.append(7)
    if getattr(cell, "conceal", False):
        codes.append(8)
    if cell.strikethrough:
        codes.append(9)
    return f"\x1b[{';'.join(str(code) for code in codes)}m"


def _ansi_color(value: Any, *, background: bool) -> list[int]:
    if not isinstance(value, str) or value == "default":
        return [49 if background else 39]
    named = _ANSI_COLOR_CODES.get(value)
    if named is not None:
        return [named + 10 if background else named]
    if len(value) == 6:
        try:
            red = int(value[0:2], 16)
            green = int(value[2:4], 16)
            blue = int(value[4:6], 16)
        except ValueError:
            pass
        else:
            return [48 if background else 38, 2, red, green, blue]
    return [49 if background else 39]
