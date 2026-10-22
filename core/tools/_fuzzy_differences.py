"""Line alignment and bounded difference diagnostics for copied text."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from os.path import commonprefix

from core.tools._fuzzy_text import _HORIZONTAL_WHITESPACE_RE
from core.tools._fuzzy_types import LineDifferences
from core.tools.arguments import split_text_lines

# A copied line this similar to a file line is a reworded copy of it.
_SIMILAR_LINE = 0.5
# File lines past a copy's length that ``line_differences`` still compares, so the
# passage of a copy that dropped a few lines ends where the copy ends.
_DIFFERENCE_SLACK_LINES = 3
# A differing block this large pairs its lines in order instead of by similarity.
_ALIGN_MAX_CELLS = 2_500
_ALIGN_LINE_CHARS = 500
# Pairing two unlike lines still beats leaving both unpaired, so a rewritten line
# pairs with the line it replaced.
_UNLIKE_PAIR = 0.01


def first_difference(
    content: str,
    copy: str,
    start: int,
    *,
    placed: bool = False,
    whole_lines: bool = False,
    character: int = 1,
) -> dict[str, int | str | bool | list[list[str]]] | None:
    """Locate where ``copy`` first differs from ``content`` near line ``start``.

    ``start`` is the 1-based line of the closest candidate for ``copy``;
    ``placed`` says it holds the copy's first word, from its 1-based
    ``character`` on when that is known. Lines compare with runs of
    whitespace collapsed. The result names the file line and the copied line
    with 1-based character positions, and both lines whole when they hold at
    most 80 characters, else 80-character windows of them from 30 characters
    before the difference on. ``None`` means every copied line matches
    the file from the aligned position on. ``whole_lines`` says the copy's
    first line is a whole line; otherwise only a copy that starts with a line
    break claims to start one.

    Without ``whole_lines``, a copy of several lines can start inside its first
    file line and end inside its last; those lines differ only when the file
    line does not end, or start, with them. Such a copy matches only with the
    whitespace at its inner line breaks, so a line that differs there alone,
    such as a continuation line without its indentation, is the difference, and
    ``spacing`` names its side (``line_differences`` compares the same way).
    ``forms`` lists, for a line that differs only in Unicode normalization, the
    differing spellings as [file, copy] pairs (see ``_normalization_forms``).
    """
    file_lines = split_text_lines(content)
    wanted_lines = split_text_lines(copy)
    first = next((index for index, line in enumerate(wanted_lines) if line.strip()), 0)
    aligned = start if placed else _aligned_start(file_lines, start, wanted_lines, first)
    last = len(wanted_lines) - 1
    if not (placed or whole_lines) and first == last == 0 and 0 < aligned <= len(file_lines):
        character = _copy_start(file_lines[aligned - 1], wanted_lines[0])
    # Whether the copy can start inside its first file line, or end inside its last.
    starts_inside = not whole_lines and first == 0 and last > first
    ends_inside = not whole_lines and not copy.endswith(("\n", "\r")) and last > first
    spaced = _copied_inside(
        file_lines,
        wanted_lines,
        [
            (aligned, first, starts_inside, False),
            (aligned + last - first, last, False, ends_inside),
        ],
    )
    for position, wanted in enumerate(wanted_lines[first:], first):
        number = aligned + position - first
        if number > len(file_lines):
            return None
        actual = file_lines[number - 1]
        # A first line placed inside its file line compares from there, with the
        # whitespace before it, unless the copy claims to start a line.
        skip = 0
        if position == first and character > 1 and not (whole_lines or first > 0):
            skip = len(actual[: character - 1].rstrip())
        differs, spacing = _paired_difference(
            actual[skip:],
            wanted,
            start=position == first and (starts_inside or skip > 0),
            end=position == last and ends_inside,
            spaced=spaced,
        )
        if differs and spacing is not None:
            file_position, copy_position = (
                (skip, 0) if spacing == "start" else (len(actual), len(wanted))
            )
            file_start, file_text = _first_difference_window(actual, file_position)
            copy_start, copy_text = _first_difference_window(wanted, copy_position)
            return {
                "line": number,
                "character": file_position + 1,
                "copy_line": position + 1,
                "copy_character": copy_position + 1,
                "file_start": file_start,
                "copy_start": copy_start,
                "file": file_text,
                "copy": copy_text,
                "truncated": len(file_text) < len(actual) or len(copy_text) < len(wanted),
                "spacing": spacing,
            }
        if differs:
            file_position, copy_position = _difference_positions(actual[skip:], wanted)
            file_position += skip
            file_start, file_text = _first_difference_window(actual, file_position)
            copy_start, copy_text = _first_difference_window(wanted, copy_position)
            following = (
                file_lines[number] if number < len(file_lines) else None,
                wanted_lines[position + 1] if position + 1 < len(wanted_lines) else None,
            )
            forms = _normalization_forms(
                actual[skip:],
                wanted,
                start=position == first and (starts_inside or skip > 0),
                end=position == last and ends_inside,
            )
            return {
                **({"forms": forms} if forms else {}),
                "line": number,
                "character": file_position + 1,
                "copy_line": position + 1,
                "copy_character": copy_position + 1,
                "file_start": file_start,
                "copy_start": copy_start,
                "file": file_text,
                "copy": copy_text,
                "truncated": len(file_text) < len(actual) or len(copy_text) < len(wanted),
                **_break_difference(
                    actual[skip:],
                    wanted,
                    following,
                    starts=position == first and not skip and (whole_lines or first > 0),
                    part=position == first and not skip and not (whole_lines or first > 0),
                ),
            }
    return None


def line_differences(content: str, copy: str, start: int, *, limit: int) -> LineDifferences | None:
    """Compare ``copy`` with the passage of ``content`` it was taken from, line by line.

    ``start`` is the 1-based line of the closest candidate for ``copy``; the
    passage starts where ``first_difference`` aligns the copy. Lines compare
    with runs of whitespace collapsed, and lines the copy added or dropped are
    told apart from changed ones, so one missing line does not make every later
    line differ. File lines before the copy's first line and after its last are
    not part of the passage. A copy of several lines can start inside its first
    file line and end inside its last one; such a copy matches only with the
    whitespace at its inner line breaks, so a line that differs there alone,
    such as a continuation line without its indentation, differs too and names
    the ``spacing`` side. ``None`` when the copy holds no text or the file no
    line at the aligned place.
    """
    file_lines = split_text_lines(content)
    copy_lines = split_text_lines(copy)
    marked = [index for index, line in enumerate(copy_lines) if line.strip()]
    if not marked:
        return None
    first, end = marked[0], marked[-1] + 1
    begin = _aligned_start(file_lines, start, copy_lines, first) - 1
    wanted = copy_lines[first:end]
    # A few more file lines than copied, so a dropped line does not cut the passage short.
    window = file_lines[begin : begin + len(wanted) + _DIFFERENCE_SLACK_LINES]
    if not window:
        return None
    matcher = SequenceMatcher(
        None, [_loose(line) for line in window], [_loose(line) for line in wanted], autojunk=False
    )
    pairs: list[tuple[int | None, int | None]] = []
    passage: list[int] = []
    for tag, file_start, file_end, copy_start, copy_end in matcher.get_opcodes():
        if copy_start == len(wanted):
            break  # Only file lines after the copy's last line remain.
        if tag == "equal":
            passage.extend(range(file_start, file_end))
            pairs.extend(zip(range(file_start, file_end), range(copy_start, copy_end), strict=True))
            continue
        leading = not passage and copy_start == 0
        # The copy's first lines lead into the lines that follow, so they align from the end.
        block = _aligned_block(
            window[file_start:file_end], wanted[copy_start:copy_end], from_end=leading
        )
        if leading:
            # File lines before the copy's first line precede the passage.
            while block and block[0][1] is None:
                block.pop(0)
        if copy_end == len(wanted):
            # File lines after the copy's last line follow the passage.
            while block and block[-1][1] is None:
                block.pop()
        block = [
            (
                None if file_index is None else file_start + file_index,
                None if copy_index is None else copy_start + copy_index,
            )
            for file_index, copy_index in block
        ]
        pairs.extend(block)
        passage.extend(index for index, _ in block if index is not None)
    if not passage:
        return None
    passage.sort()
    last = len(wanted) - 1
    # A copy that starts or ends inside a file line cannot match line by line with
    # its lines trimmed; it matches with the whitespace at each inner line break.
    inside = last > 0 and any(
        _loose(window[file_index]) != _loose(wanted[copy_index])
        for file_index, copy_index in pairs
        if file_index is not None and copy_index in (0, last)
    )
    differing: list[tuple[int | None, int | None, str | None]] = []
    for file_index, copy_index in pairs:
        if file_index is None or copy_index is None:
            differing.append((file_index, copy_index, None))
            continue
        differs, spacing = _paired_difference(
            window[file_index],
            wanted[copy_index],
            start=copy_index == 0,
            end=copy_index == last,
            spaced=inside,
        )
        if differs:
            differing.append((file_index, copy_index, spacing))
    shown = [
        _line_difference(
            None if file_index is None else (begin + file_index + 1, window[file_index]),
            None if copy_index is None else (first + copy_index + 1, wanted[copy_index]),
            spacing=spacing,
        )
        for file_index, copy_index, spacing in differing[:limit]
    ]
    return LineDifferences(
        begin + passage[0] + 1, begin + passage[-1] + 1, tuple(shown), len(differing)
    )


_COPY_START_MIN = 12
_COPY_START_MAX = 60


def _copy_start(line: str, copy: str) -> int:
    """Return the 1-based character of ``line`` where a one-line copy of part of it starts.

    A copy taken from inside a long line, such as a sentence of a paragraph,
    compares from there: compared from the line's start, its first difference
    would be its first character. The copy's start must occur once in the line,
    at least 12 characters of it; otherwise the copy compares from the start.
    """
    text = copy.lstrip()
    if line.lstrip().startswith(text[:_COPY_START_MIN]):
        return 1
    for length in range(min(len(text), _COPY_START_MAX), _COPY_START_MIN - 1, -1):
        found = line.find(text[:length])
        if found < 0:
            continue
        return found + 1 if line.find(text[:length], found + 1) < 0 else 1
    return 1


def _paired_difference(
    file_line: str, copy_line: str, *, start: bool, end: bool, spaced: bool
) -> tuple[bool, str | None]:
    """Tell whether a copied line differs from the file line it is paired with.

    ``start`` and ``end`` mark the first and last line of a copy of several
    lines: the first can be the end of its file line, the last its beginning.
    ``spaced`` says the whitespace at the line's breaks must match as well.
    The second value names the side, ``"start"`` or ``"end"``, of a line that
    differs only in its whitespace there.
    """
    if not _fits(_loose(file_line), _loose(copy_line), start=start, end=end):
        return True, None
    if not spaced:
        return False, None
    held, copied = (_HORIZONTAL_WHITESPACE_RE.sub(" ", line) for line in (file_line, copy_line))
    if _fits(held, copied, start=start, end=end):
        return False, None
    leading = held.startswith(" ") != copied.startswith(" ")
    trailing = held.endswith(" ") != copied.endswith(" ")
    # The first line's leading whitespace lies inside its file line.
    return True, "start" if leading and not (start and trailing) else "end"


def _copied_inside(
    file_lines: list[str], copy_lines: list[str], edges: list[tuple[int, int, bool, bool]]
) -> bool:
    """Whether a copy starts inside its first file line or ends inside its last.

    ``edges`` holds the 1-based file line, the copy line and whether it may
    ``start`` or ``end`` inside that line, for the copy's first and last line.
    A copy of whole lines matches with other indentation; one that starts or
    ends inside a line matches only with the whitespace at its line breaks.
    """
    for number, index, start, end in edges:
        if not (start or end) or not 0 < number <= len(file_lines):
            continue
        held, copied = _loose(file_lines[number - 1]), _loose(copy_lines[index])
        if held != copied and _fits(held, copied, start=start, end=end):
            return True
    return False


def _fits(held: str, copied: str, *, start: bool, end: bool) -> bool:
    """Whether ``copied`` is the file line ``held``, its end (``start``) or its beginning."""
    if start and not end:
        return held.endswith(copied)
    if end and not start:
        return held.startswith(copied)
    return held == copied


def _aligned_block(
    file_lines: list[str], copy_lines: list[str], *, from_end: bool
) -> list[tuple[int | None, int | None]]:
    """Pair the lines of one differing block in order, similar lines with each other.

    Each entry pairs a file line with a copied line, by their index in the block,
    or holds a line the other side lacks. A block too large to compare line by
    line pairs its lines in order, and its surplus lines stay unpaired.
    """
    if from_end:
        reversed_pairs = _aligned_block(file_lines[::-1], copy_lines[::-1], from_end=False)
        rows, cols = len(file_lines), len(copy_lines)
        return [
            (None if row is None else rows - 1 - row, None if col is None else cols - 1 - col)
            for row, col in reversed(reversed_pairs)
        ]
    rows, cols = len(file_lines), len(copy_lines)
    if rows * cols > _ALIGN_MAX_CELLS:
        paired = min(rows, cols)
        return [
            *((index, index) for index in range(paired)),
            *((index, None) for index in range(paired, rows)),
            *((None, index) for index in range(paired, cols)),
        ]
    similarity = [
        [
            ratio
            if (
                ratio := SequenceMatcher(
                    None,
                    _loose(file_line)[:_ALIGN_LINE_CHARS],
                    _loose(copy_line)[:_ALIGN_LINE_CHARS],
                    autojunk=False,
                ).ratio()
            )
            >= _SIMILAR_LINE
            else _UNLIKE_PAIR
            for copy_line in copy_lines
        ]
        for file_line in file_lines
    ]
    # best[row][col]: the highest total similarity of the lines from row and col on.
    best = [[0.0] * (cols + 1) for _ in range(rows + 1)]
    for row in range(rows - 1, -1, -1):
        for col in range(cols - 1, -1, -1):
            best[row][col] = max(
                similarity[row][col] + best[row + 1][col + 1],
                best[row + 1][col],
                best[row][col + 1],
            )
    pairs: list[tuple[int | None, int | None]] = []
    row = col = 0
    while row < rows and col < cols:
        if best[row][col] == similarity[row][col] + best[row + 1][col + 1]:
            pairs.append((row, col))
            row, col = row + 1, col + 1
        elif best[row][col] == best[row + 1][col]:
            pairs.append((row, None))
            row += 1
        else:
            pairs.append((None, col))
            col += 1
    pairs.extend((index, None) for index in range(row, rows))
    pairs.extend((None, index) for index in range(col, cols))
    return pairs


def _line_difference(
    file_line: tuple[int, str] | None,
    copy_line: tuple[int, str] | None,
    *,
    spacing: str | None = None,
) -> dict[str, int | str | bool | None]:
    """Describe one differing line: numbered windows of both sides that hold it.

    ``spacing`` names the side of a line that differs only in its whitespace.
    """
    if spacing is not None and file_line is not None and copy_line is not None:
        file_position, copy_position = (
            (0, 0) if spacing == "start" else (len(file_line[1]), len(copy_line[1]))
        )
    elif file_line is not None and copy_line is not None:
        file_position, copy_position = _difference_positions(file_line[1], copy_line[1])
    else:
        file_position = copy_position = 0
    difference: dict[str, int | str | bool | None] = {
        "line": None,
        "copy_line": None,
        "file": None,
        "copy": None,
        "spacing": spacing,
    }
    truncated = False
    for key, side, position in (
        ("line", file_line, file_position),
        ("copy_line", copy_line, copy_position),
    ):
        if side is None:
            continue
        number, text = side
        _start, window = _difference_window(text, position)
        difference[key] = number
        difference["file" if key == "line" else "copy"] = window
        truncated = truncated or len(window) < len(text)
    difference["truncated"] = truncated
    return difference


# A line break difference quotes at most this many characters on each side.
_BREAK_SIDE_CHARACTERS = 60
# A copy's first line is tried at most at this many places inside the file line.
_BREAK_PLACES = 20


def _break_difference(
    actual: str,
    wanted: str,
    following: tuple[str | None, str | None],
    *,
    starts: bool,
    part: bool = False,
) -> dict[str, str]:
    """Describe a first difference that is a line break in another place, if it is one.

    ``following`` holds the file's and the copy's next lines. ``break`` is
    ``file`` when the file line ends where the copy line goes on with what the
    next file line starts with, ``copy`` for the reverse, and ``start`` when the
    copy's first line, which ``starts`` a line, is the end of the file line.
    ``before`` is the text before the break, ``after`` the text the other side
    continues with. A copy's first line that is ``part`` of a line can start
    inside the file line.
    """
    if part:
        start = wanted.split()[:1]
        places = re.finditer(re.escape(start[0]), actual) if start else iter(())
        for _, found in zip(range(_BREAK_PLACES), places, strict=False):
            difference = _break_difference(actual[found.start() :], wanted, following, starts=False)
            if difference:
                return difference
        return {}
    held = list(re.finditer(r"\S+", actual))
    copied = list(re.finditer(r"\S+", wanted))
    words = [found[0] for found in held]
    copy_words = [found[0] for found in copied]

    def side(text: str, *, end: bool) -> str:
        text = text.strip()
        if len(text) <= _BREAK_SIDE_CHARACTERS:
            return text
        if end:
            return "..." + text[-_BREAK_SIDE_CHARACTERS:]
        return text[:_BREAK_SIDE_CHARACTERS] + "..."

    def first_word(line: str | None) -> str | None:
        found = (line or "").split()
        return found[0] if found else None

    def holds(held_word: str | None, copied_word: str | None) -> bool:
        # The copy can end inside a word.
        return (
            held_word is not None and copied_word is not None and held_word.startswith(copied_word)
        )

    count, copy_count = len(words), len(copy_words)
    if (
        0 < count < copy_count
        and copy_words[:count] == words
        and holds(first_word(following[0]), copy_words[count])
    ):
        return {
            "break": "file",
            "before": side(actual, end=True),
            "after": side(wanted[copied[count].start() :], end=False),
        }
    if (
        0 < copy_count < count
        and words[:copy_count] == copy_words
        and holds(words[copy_count], first_word(following[1]))
    ):
        return {
            "break": "copy",
            "before": side(wanted, end=True),
            "after": side(actual[held[copy_count].start() :], end=False),
        }
    if starts and 0 < copy_count < count and words[-copy_count:] == copy_words:
        return {
            "break": "start",
            "before": side(actual[: held[count - copy_count].start()], end=True),
            "after": side(wanted, end=False),
        }
    return {}


def _loose(text: str) -> str:
    return " ".join(text.split())


# A normalization difference names at most this many different spellings of a line.
_FORMS_SHOWN = 3


def _normalization_forms(held: str, copied: str, *, start: bool, end: bool) -> list[list[str]]:
    """List how a copied line spells characters differently from its file line.

    Each entry pairs the file's spelling of a character with its marks, such as
    ``e`` with a combining acute accent, with the copy's, such as ``é``: two
    forms of the same text under Unicode normalization (NFC). Empty unless that
    is the only difference between the lines; ``start`` and ``end`` mean what
    they mean for ``_paired_difference``.
    """
    held_clusters, copied_clusters = _clusters(_loose(held)), _clusters(_loose(copied))
    count = len(copied_clusters)
    if start and not end:
        held_clusters = held_clusters[-count:] if count else []
    elif end and not start:
        held_clusters = held_clusters[:count]
    if len(held_clusters) != count:
        return []
    forms: list[list[str]] = []
    for file_form, copy_form in zip(held_clusters, copied_clusters, strict=True):
        if file_form == copy_form:
            continue
        if unicodedata.normalize("NFC", file_form) != unicodedata.normalize("NFC", copy_form):
            return []
        if [file_form, copy_form] not in forms:
            forms.append([file_form, copy_form])
    return forms[:_FORMS_SHOWN]


def _clusters(text: str) -> list[str]:
    """Split ``text`` into characters, each with the marks that combine with it."""
    clusters: list[str] = []
    for character in text:
        code = ord(character)
        # Marks, and the vowel and final consonant jamo of a decomposed Hangul syllable.
        combines = (
            unicodedata.category(character).startswith("M")
            or 0x1160 <= code <= 0x11FF
            or 0xD7B0 <= code <= 0xD7FF
        )
        if clusters and combines:
            clusters[-1] += character
        else:
            clusters.append(character)
    return clusters


def _difference_positions(actual: str, wanted: str) -> tuple[int, int]:
    """Locate the first different token, ignoring earlier spacing-only differences."""
    actual_words = list(re.finditer(r"\S+", actual))
    wanted_words = list(re.finditer(r"\S+", wanted))
    for file_word, copy_word in zip(actual_words, wanted_words, strict=False):
        if file_word[0] != copy_word[0]:
            shared = len(commonprefix((file_word[0], copy_word[0])))
            return file_word.start() + shared, copy_word.start() + shared
    shared_words = min(len(actual_words), len(wanted_words))
    return (
        actual_words[shared_words].start() if shared_words < len(actual_words) else len(actual),
        wanted_words[shared_words].start() if shared_words < len(wanted_words) else len(wanted),
    )


def _difference_window(
    text: str, position: int, *, width: int = 240, before: int = 120
) -> tuple[int, str]:
    """Keep the actual mismatch in a bounded window, including a missing suffix.

    A line of at most ``width`` characters shows whole; a longer one shows
    ``width`` characters from ``before`` characters ahead of ``position``.
    """
    if len(text) <= width:
        return 1, text
    start = max(0, position - before)
    return start + 1, text[start : start + width]


# A first difference quotes a line of at most this many characters whole; a longer
# line shows this many of its characters, from a few before the difference on.
# Swarm run 2026-10-20: a whole 120-character line hid a ' that differed from ".
_FIRST_DIFFERENCE_WIDTH = 80
_FIRST_DIFFERENCE_BEFORE = 30


def _first_difference_window(text: str, position: int) -> tuple[int, str]:
    return _difference_window(
        text, position, width=_FIRST_DIFFERENCE_WIDTH, before=_FIRST_DIFFERENCE_BEFORE
    )


def _aligned_start(file_lines: list[str], start: int, wanted_lines: list[str], first: int) -> int:
    """Return the file line where patch line ``first`` belongs.

    A candidate window starts where its best-matching lines put it, so every line
    the copy added or dropped before them shifts it. The first patch line the
    file holds near the window fixes the alignment instead; the lines before it
    belong directly above it. Lines without a word, such as a closing quote or
    bracket, occur too often to fix it. When the file holds none of the patch
    lines, patch line ``first`` belongs at the nearby file line most like its
    start: a reworded copy resembles its line, and a copy that joins lines
    starts like the first of them. Words decide before characters: lines that
    differ in their names can share most of their signs (``_EQ_OPS = {...}`` and
    ``_RANGE_OPS = {...}``).
    """
    span = len(wanted_lines) - first
    if span == 1:
        # One copied line drops or adds no line above it; the candidate places it.
        return start
    for position in range(first, len(wanted_lines)):
        text = _loose(wanted_lines[position])
        if not re.search(r"\w", text):
            continue
        expected = start - 1 + position - first
        near = [
            index
            for index in range(max(0, expected - span), min(len(file_lines), expected + span + 1))
            if _loose(file_lines[index]) == text
        ]
        if near:
            index = min(near, key=lambda index: (abs(index - expected), index))
            return max(1, index + 1 - (position - first))
    text = _loose(wanted_lines[first])
    words = re.findall(r"\w+", text)
    expected = start - 1
    scores = []
    for index in range(max(0, expected - span), min(len(file_lines), expected + span + 1)):
        line = _loose(file_lines[index])
        held = re.findall(r"\w+", line)
        if len(line) < 4 or not held:
            continue
        # The copy's start against the whole line, by words and then by characters.
        shared = SequenceMatcher(None, words[: len(held)], held, autojunk=False).ratio()
        similarity = SequenceMatcher(None, text[: len(line)], line).ratio()
        if max(shared, similarity) >= _SIMILAR_LINE:
            scores.append((shared, similarity, -abs(index - expected), index))
    return max(scores)[3] + 1 if scores else start
