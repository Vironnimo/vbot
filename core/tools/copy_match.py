"""Find the passage an edit's old text copies with errors, and apply the edit there.

Callers try the precise strategies of ``fuzzy_match`` first; they already absorb
indentation, spacing, line endings and typographic glyphs. When those find nothing,
this module looks for the one passage the caller's old text is a copy of. Two
questions decide it, separately.

Where. The evidence is the number of words copied correctly. Below 3, the copy must
be exact up to spacing. From 3, it may misspell one word per 3 correct words, and a
line the edit keeps may lack or add words the file's line has (a stale or
misremembered context line), one word or sign per 2 correct words. From 12, it may
also differ in other ways, with at least 4 correct words for each difference, but
never where one side has an identifier (a word with an underscore, a digit or an
inner capital) and the other another word: that names something else, such as a
sibling function in another file. A passage of 3 or more lines is placed as
well by exactly copied first and last lines of at least 2 words each around similar
lines. Each line keeps its place: a kept line must hold at least half of its file
line's words, and of its words and signs together (a blank file line holds none),
and a copy whose extra words continue the line above or below joined lines across a
line break (the copy left out or added a line), so the passage does not take the
edit. Words moved across a line break between a kept line and another line of the
passage are the exception, since the kept line stays as the file has it.
Passages copied up to misspellings and kept-line gaps are preferred, like the
precise strategies before them; candidates that overlap are one passage, placed by
the fewest differences; exactly one passage may qualify at the first level that has
any, else the result is ambiguous.

What. The caller's change, its old text against its new text, is applied to the
file's text like a merge. A line the edit writes comes out as the caller's new text
up to the file's spelling of words the caller misspelled: text the caller keeps in
it must match the file up to misspellings, since a difference there may be wording
the caller meant to write. Lines the edit keeps stay as the file has them. In the 3
words or signs on each side of each changed part the copy must match the file up to
misspellings: a change rests on its surroundings ("< limit" to "< limit + 1" where
the file says "> limit" does not qualify). Within the changed part, a copy that
differs otherwise must still be at least 80% right: a large rewrite discards the
old text anyway, but a small change ("3" to "4" where the file says 5) rests on text
the file does not hold, and the passage does not qualify. A passage that qualifies
but cannot take the change refuses the edit; it never goes to another passage.

A misspelling is a word of at least 4 characters that differs from the file's word
at its place only in letter case or one added, dropped, changed or swapped letter
(two from 8 characters), keeps its digits, and occurs nowhere in the file: a word
the file holds is a real other word there, not a copy error.

Some differences never matter: a hyphen (or two) for an em dash, a lone backslash
only one side holds (an escaped quote), and zero-width characters. They are neither
correct words nor differences, and text the caller keeps comes out as the file has
it, with the file's zero-width characters beside it; so zero-width characters the
caller writes at the edge of a change, other than its copy's there, do not fit. A
backslash only one side holds shows that the two escape differently, so the
text the edit writes may hold neither a backslash nor a character the file escapes
where the copy does not, even on another line: a copy that drops the file's escapes
comes with new text that drops them too. A backslash only the file holds must not
border a change: it would escape the new text.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache

from core.tools.arguments import TEXT_LINE_BREAK
from core.tools.fuzzy_match import (
    _TYPOGRAPHIC_NORMALIZATION,
    AmbiguousFuzzyMatch,
    FuzzyReplacement,
    _indent_unit,
    _line_and_character_at,
    _meaningful_indents,
    _normalize_with_spans,
    _reindent_replacement,
    _split_lines_preserving_endings,
)

# Where: below this many correct words a copy must be exact up to spacing.
_TOLERANT_MIN_WORDS = 3
# Where: misspellings need this many correct words each, and so do words or signs a
# kept line lacks or adds.
_WORDS_PER_MISSPELLING = 3
_WORDS_PER_GAP = 2
# Where: other differences need this many correct words in all, and this many per
# difference. The same rate bounds other differences within a changed part.
_DIFFERENCES_MIN_WORDS = 12
_WORDS_PER_DIFFERENCE = 4
# Where: exactly copied first and last lines place a passage of this many lines.
_ANCHOR_MIN_LINES = 3
_ANCHOR_MIN_WORDS = 2
_ANCHOR_UNIQUE_SIMILARITY = 0.50
_ANCHOR_SHARED_SIMILARITY = 0.70
# The least share of its words a copy placed without anchors has right, by the rates
# above; lines holding fewer of its words are not considered.
_MIN_CORRECT_SHARE = min(
    1 / (1 + 1 / _WORDS_PER_MISSPELLING + 1 / _WORDS_PER_GAP),
    1 / (1 + 1 / _WORDS_PER_DIFFERENCE),
)
# What: the words and signs on each side of a changed part that must match.
_CHANGE_NEIGHBORS = 3
# Misspelled words: shorter ones are too often another word one letter away.
_MISSPELLING_MIN_CHARACTERS = 4
_TWO_ERRORS_MIN_CHARACTERS = 8
# Longer tokens are data (hashes, encoded text): any difference is another value.
_MISSPELLING_MAX_CHARACTERS = 64
# Warnings: differing lines shown in full, and their length.
_WARNING_LINES = 3
_WARNING_LINE_CHARACTERS = 200

_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")
_WORD = re.compile(r"\w+")
_INNER_CAPITAL = re.compile(r"[^\W\d_A-Z][A-Z]")
_DIGITS = re.compile(r"\d+")
# Zero-width space, non-joiner, joiner, word joiner and byte order mark: invisible,
# so a copy cannot show where the file holds them.
_INVISIBLE = "\u200b\u200c\u200d\u2060\ufeff"
_FOLD = str.maketrans({**_TYPOGRAPHIC_NORMALIZATION, **dict.fromkeys(_INVISIBLE, "")})
_EM_DASH = _TYPOGRAPHIC_NORMALIZATION["\u2014"]
_HYPHENS = (["-"], ["-", "-"])
_BACKSLASH = "\\"
# ``moved``: spacing that differs in line breaks, which a change must not rest on.
# ``alike``: a hyphen for an em dash, or a lone backslash only the copy holds.
_SAME, _MISSPELLED, _MOVED, _ALIKE, _OTHER = "same", "misspelled", "moved", "alike", "other"


def replace_copied(
    content: str, old: str, new: str
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    """Apply an ``old``/``new`` text edit whose ``old`` copies the file with errors.

    ``old`` names whole lines, or part of one line when it has no line break.
    Returns ``None`` when no passage qualifies.
    """
    old_body, new_body = _without_final_break(old), _without_final_break(new)
    old_lines = TEXT_LINE_BREAK.split(old_body)
    whole = old_body != old
    # Replacing whole lines with nothing removes them, line breaks included.
    new_lines = [] if whole and new == "" else TEXT_LINE_BREAK.split(new_body)
    if not any(line.strip() for line in old_lines):
        return None
    lines: list[tuple[str, str]] = []
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            lines.extend((" ", line) for line in old_lines[i1:i2])
            continue
        lines.extend(("-", line) for line in old_lines[i1:i2])
        lines.extend(("+", line) for line in new_lines[j1:j2])
    file = _File(content)
    found = _match_lines(file, lines, at_eof=False, drop_breaks=True)
    if found is not None or len(old_lines) > 1 or whole:
        return found
    return _match_fragment(file, old, new)


def match_copied_edit(
    content: str, lines: Sequence[tuple[str, str]], *, at_eof: bool = False
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    """Apply a hunk whose kept (``" "``) and removed (``"-"``) lines copy whole lines.

    Added lines are ``"+"``. With ``at_eof`` the passage must end the file.
    Returns ``None`` when no passage qualifies.
    """
    if not any(text.strip() for prefix, text in lines if prefix in " -"):
        return None
    return _match_lines(_File(content), lines, at_eof=at_eof)


def copy_warnings(found: FuzzyReplacement, *, line_shift: int = 0) -> list[str]:
    """Tell the caller which lines differed from its copy and which words it misspelled."""
    warnings = []
    if found.differed:
        warnings.append(
            _lines_warning(
                found.differed,
                line_shift,
                "was edited anyway; it read:",
                "were edited anyway; they read:",
            )
        )
    if found.kept_differed:
        warnings.append(
            _lines_warning(
                found.kept_differed,
                line_shift,
                "was left as it reads:",
                "were left as they read:",
            )
        )
    if found.respelled:
        pairs = ", ".join(f'"{right}" for "{wrong}"' for wrong, right in found.respelled[:3])
        warnings.append(f"Your new text uses the file's spelling: {pairs}.")
    return warnings


def _lines_warning(lines: Sequence[tuple[int, str]], line_shift: int, one: str, many: str) -> str:
    numbered = [(number + line_shift, text) for number, text in lines]
    if len(numbered) == 1:
        number, text = numbered[0]
        return f"Line {number} did not match your old text exactly and {one} {text}"
    numbers = ", ".join(str(number) for number, _ in numbered)
    parts = [
        f"Lines {numbers} did not match your old text exactly and {many}",
        *(f"{number}: {text}" for number, text in numbered[:_WARNING_LINES]),
    ]
    if len(numbered) > _WARNING_LINES:
        parts.append(f"({len(numbered) - _WARNING_LINES} more)")
    return "\n".join(parts)


def _excerpt(text: str, copy: str) -> str:
    """The line, or the part of a long line from just before its first difference."""
    if len(text) <= _WARNING_LINE_CHARACTERS:
        return text
    held, copied = _read(text), _read(copy)
    matcher = SequenceMatcher(
        None,
        [" " if _is_space(key) else key for key in copied.keys],
        [" " if _is_space(key) else key for key in held.keys],
        autojunk=False,
    )
    at = next((j1 for tag, _, _, j1, _ in matcher.get_opcodes() if tag != "equal"), 0)
    offset = held.tokens[at].start if at < len(held.tokens) else len(text)
    begin = max(0, offset - _WARNING_LINE_CHARACTERS // 4)
    end = begin + _WARNING_LINE_CHARACTERS
    return ("..." if begin else "") + text[begin:end] + ("..." if end < len(text) else "")


@dataclass(frozen=True, slots=True)
class _Token:
    key: str  # folded text; whitespace reads " ", or one "\n" per line break
    start: int
    end: int


@dataclass(frozen=True)
class _Text:
    """Tokens of a text without its leading and trailing whitespace."""

    source: str
    tokens: list[_Token]
    lead: str
    trail: str

    @property
    def keys(self) -> list[str]:
        return [token.key for token in self.tokens]

    def span(self, first: int, last: int) -> str:
        if first >= last:
            return ""
        return self.source[self.tokens[first].start : self.tokens[last - 1].end]

    def gap(self, index: int) -> tuple[int, int]:
        """Offsets of the invisible characters between tokens ``index - 1`` and ``index``."""
        if not self.tokens:
            return 0, 0
        if index <= 0:
            return self.tokens[0].start, self.tokens[0].start
        if index >= len(self.tokens):
            return self.tokens[-1].end, self.tokens[-1].end
        return self.tokens[index - 1].end, self.tokens[index].start

    def gap_text(self, index: int) -> str:
        begin, end = self.gap(index)
        return self.source[begin:end]

    def window(self, first: int, last: int) -> _Text:
        return _Text(self.source, self.tokens[first:last], "", "")


def _read(source: str) -> _Text:
    folded, spans = _normalize_with_spans(source, typographic=True)
    if any(character in _INVISIBLE for character in folded):
        # Invisible characters get no key: a token's span holds those within it, and
        # those between tokens lie in the gap there.
        visible = [index for index, character in enumerate(folded) if character not in _INVISIBLE]
        folded = "".join(folded[index] for index in visible)
        spans = [spans[index] for index in visible]
    tokens: list[_Token] = []
    for match in _TOKEN.finditer(folded):
        key = match.group()
        if key.isspace():
            key = "\n" * key.count("\n") or " "
        start, end = spans[match.start()][0], spans[match.end() - 1][1]
        if tokens and start < tokens[-1].end:
            # The pieces of one expanded glyph ("--" for an em dash) stay one token.
            previous = tokens.pop()
            key, start, end = previous.key + key, previous.start, max(end, previous.end)
        tokens.append(_Token(key, start, end))
    first, last = 0, len(tokens)
    while first < last and _is_space(tokens[first].key):
        first += 1
    while last > first and _is_space(tokens[last - 1].key):
        last -= 1
    kept = tokens[first:last]
    if not kept:
        return _Text(source, [], source, "")
    return _Text(source, kept, source[: kept[0].start], source[kept[-1].end :])


def _is_word(key: str) -> bool:
    return bool(_WORD.match(key))


def _is_space(key: str) -> bool:
    return key[:1].isspace()


def _content(keys: Sequence[str]) -> int:
    return sum(not _is_space(key) for key in keys)


def _identifier(key: str) -> bool:
    """Whether a word reads as a code identifier rather than a plain word or number."""
    if not _is_word(key) or key.isdigit():
        return False
    return (
        "_" in key
        or any(character.isdigit() for character in key)
        or bool(_INNER_CAPITAL.search(key))
    )


def _line_key(text: str) -> str:
    return " ".join(text.translate(_FOLD).split())


def _misspelling(wrong: str, right: str) -> bool:
    """Whether word ``wrong`` reads as a copy error of word ``right`` (see the module)."""
    if not (_is_word(wrong) and _is_word(right)):
        return False
    shorter, longer = sorted((len(wrong), len(right)))
    if shorter < _MISSPELLING_MIN_CHARACTERS or longer > _MISSPELLING_MAX_CHARACTERS:
        return False
    if _DIGITS.findall(wrong) != _DIGITS.findall(right):
        return False
    bound = 2 if shorter >= _TWO_ERRORS_MIN_CHARACTERS else 1
    return _edit_distance(wrong.casefold(), right.casefold(), bound) <= bound


def _edit_distance(first: str, second: str, bound: int) -> int:
    """Optimal string alignment distance, or ``bound + 1`` once it exceeds ``bound``."""
    if abs(len(first) - len(second)) > bound:
        return bound + 1
    earlier: list[int] = []
    previous = list(range(len(second) + 1))
    for i, left in enumerate(first, 1):
        current = [i]
        for j, right in enumerate(second, 1):
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (left != right))
            if i > 1 and j > 1 and left == second[j - 2] and first[i - 2] == right:
                value = min(value, earlier[j - 2] + 1)
            current.append(value)
        if min(current) > bound:
            return bound + 1
        earlier, previous = previous, current
    return previous[-1]


class _Speller:
    """Tells misspellings apart, keeping one file spelling per misspelled word."""

    def __init__(self, words: frozenset[str]) -> None:
        self.words = words
        self.spelling: dict[str, str] = {}

    def misspelled(self, wrong: str, right: str) -> bool:
        if wrong in self.words or self.spelling.get(wrong, right) != right:
            return False
        if not _misspelling(wrong, right):
            return False
        self.spelling[wrong] = right
        return True

    def respell(self, text: str) -> str:
        if not self.spelling:
            return text
        return _WORD.sub(lambda match: self.spelling.get(match.group(), match.group()), text)


@dataclass
class _Alignment:
    """How the tokens of a copy line up with the file's tokens.

    ``before`` and ``after`` map a copy boundary to the file boundary before and
    after tokens only the file holds there (``extra``: their content count).
    ``gaps`` counts the ``other`` differences that are tokens only one side holds;
    ``foreign`` the other differences where either side is an identifier; ``alike``
    the differences that never matter. ``escapes`` holds the copy boundaries where
    only the file holds a lone backslash, and ``unwritable`` the characters text the
    edit writes must not hold, since the copy escapes differently from the file.
    """

    states: list[str]
    before: dict[int, int] = field(default_factory=dict)
    after: dict[int, int] = field(default_factory=dict)
    extra: dict[int, int] = field(default_factory=dict)
    escapes: set[int] = field(default_factory=set)
    unwritable: set[str] = field(default_factory=set)
    correct: int = 0
    misspelled: int = 0
    other: int = 0
    gaps: int = 0
    foreign: int = 0
    alike: int = 0


def _align(copy: _Text, actual: _Text, speller: _Speller) -> _Alignment:
    copied, held = copy.keys, actual.keys
    if copied == held:
        alignment = _Alignment([_SAME] * len(copied))
        alignment.before = {index: index for index in range(len(copied) + 1)}
        alignment.after = dict(alignment.before)
        alignment.correct = sum(_is_word(key) for key in copied)
        return alignment
    alignment = _Alignment([_OTHER] * len(copied))
    # Align by content: a moved line break must not pull words out of line.
    flat_copied = [" " if _is_space(key) else key for key in copied]
    flat_held = [" " if _is_space(key) else key for key in held]
    matcher = SequenceMatcher(None, flat_copied, flat_held, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        alignment.before.setdefault(i1, j1)
        if tag == "insert":
            alignment.after[i1] = j2
            if _lone_backslash(held[j1:j2]):
                alignment.escapes.add(i1)
                alignment.unwritable.update(_escaped(held, j2))
                alignment.alike += 1
                continue
            alignment.extra[i1] = _content(held[j1:j2])
            alignment.other += alignment.extra[i1]
            alignment.gaps += alignment.extra[i1]
            continue
        alignment.after[i1] = j1
        if tag == "delete":
            if _lone_backslash(copied[i1:i2]):
                _mark_alike(alignment, copied, i1, i2)
                alignment.unwritable.add(_BACKSLASH)
                continue
            alignment.other += _content(copied[i1:i2])
            alignment.gaps += _content(copied[i1:i2])
            continue
        if i2 - i1 != j2 - j1:
            if _dash_pair(copied[i1:i2], held[j1:j2]):
                _mark_alike(alignment, copied, i1, i2)
                continue
            alignment.other += max(_content(copied[i1:i2]), _content(held[j1:j2]))
            alignment.foreign += any(map(_identifier, [*copied[i1:i2], *held[j1:j2]]))
            continue
        for offset in range(i2 - i1):
            index, wrong, right = i1 + offset, copied[i1 + offset], held[j1 + offset]
            if offset:
                alignment.before[index] = alignment.after[index] = j1 + offset
            if wrong == right:
                alignment.states[index] = _SAME
                alignment.correct += _is_word(wrong)
            elif _is_space(wrong) and _is_space(right):
                alignment.states[index] = _MOVED
            elif _dash_pair([wrong], [right]):
                _mark_alike(alignment, copied, index, index + 1)
            elif speller.misspelled(wrong, right):
                alignment.states[index] = _MISSPELLED
                alignment.misspelled += 1
            else:
                alignment.other += 1
                alignment.foreign += _identifier(wrong) or _identifier(right)
    alignment.before.setdefault(len(copied), len(held))
    alignment.after.setdefault(len(copied), len(held))
    return alignment


def _lone_backslash(keys: Sequence[str]) -> bool:
    return [key for key in keys if not _is_space(key)] == [_BACKSLASH]


def _escaped(held: Sequence[str], index: int) -> set[str]:
    """A backslash, and the sign after a backslash only the file holds: what it escapes."""
    following = held[index][:1] if index < len(held) else ""
    if following and not (following.isalnum() or following.isspace()):
        return {_BACKSLASH, following}
    return {_BACKSLASH}


def _dash_pair(copied: Sequence[str], held: Sequence[str]) -> bool:
    """Whether one side holds a hyphen (or two) where the other holds an em dash."""
    first = [key for key in copied if not _is_space(key)]
    second = [key for key in held if not _is_space(key)]
    return (first == [_EM_DASH] and second in _HYPHENS) or (
        second == [_EM_DASH] and first in _HYPHENS
    )


def _mark_alike(alignment: _Alignment, copied: Sequence[str], first: int, last: int) -> None:
    for index in range(first, last):
        if not _is_space(copied[index]):
            alignment.states[index] = _ALIKE
    alignment.alike += 1


def _differs(state: str, key: str) -> bool:
    """Whether a copy token next to a change differs from the file beyond spacing."""
    return state == _MOVED or (state == _OTHER and not _is_space(key))


def _change_fits(alignment: _Alignment, keys: list[str], first: int, last: int) -> bool:
    """Whether the copy holds the file's text where the caller changes ``first:last``.

    In the words and signs next to the change the copy must match up to misspellings;
    within it, other differences need at least 4 correct words each. A backslash only
    the file holds must not border the change.
    """
    if first in alignment.escapes or last in alignment.escapes:
        return False
    low, seen = first, 0
    while low > 0 and seen < _CHANGE_NEIGHBORS:
        low -= 1
        seen += not _is_space(keys[low])
    high, seen = last, 0
    while high < len(keys) and seen < _CHANGE_NEIGHBORS:
        seen += not _is_space(keys[high])
        high += 1
    beside = [*range(low, first), *range(last, high)]
    if any(_differs(alignment.states[index], keys[index]) for index in beside):
        return False
    if any(
        count and (low <= boundary <= first or last <= boundary <= high)
        for boundary, count in alignment.extra.items()
    ):
        return False
    inside = range(first, last)
    other = sum(
        alignment.states[index] == _OTHER and not _is_space(keys[index]) for index in inside
    )
    other += sum(count for boundary, count in alignment.extra.items() if first < boundary < last)
    correct = sum(alignment.states[index] == _SAME and _is_word(keys[index]) for index in inside)
    return other * _WORDS_PER_DIFFERENCE <= correct


def _merge(
    copy: _Text,
    actual: _Text,
    new: _Text,
    alignment: _Alignment,
    speller: _Speller,
    unwritable: set[str],
) -> str | None:
    """Apply the change from ``copy`` to ``new`` onto ``actual``, or None if it does not fit.

    The result is the caller's new text up to the file's spelling: text the caller
    keeps must match the file up to misspellings, since a difference there may be
    wording the caller meant to write. The text written must not hold ``unwritable``
    characters. Kept text holds the file's invisible characters beside it, so ones the
    caller writes at the edge of a change, other than its copy's there, do not fit.
    """
    copied = copy.keys
    opcodes = _changes(tuple(copied), tuple(new.keys))
    pieces = []
    held = -1  # the file gap the kept text before already holds
    for tag, i1, i2, j1, j2 in opcodes:
        if tag != "equal":
            if not _change_fits(alignment, copied, i1, i2):
                return None
            if new.gap_text(j1) not in ("", copy.gap_text(i1)) or new.gap_text(j2) not in (
                "",
                copy.gap_text(i2),
            ):
                return None
            written = speller.respell(new.span(j1, j2))
            if unwritable and not unwritable.isdisjoint(written.translate(_FOLD)):
                return None
            pieces.append(written)
            continue
        if any(
            alignment.states[index] == _OTHER and not _is_space(copied[index])
            for index in range(i1, i2)
        ) or any(count and i1 <= boundary <= i2 for boundary, count in alignment.extra.items()):
            return None
        first, last = alignment.before.get(i1), alignment.after.get(i2)
        if first is None or last is None:
            return None
        breaks = sum(key.count("\n") for key in actual.keys[first:last])
        if breaks != sum(key.count("\n") for key in copied[i1:i2]):
            return None
        # The kept text takes the file's invisible characters on both sides.
        begin = actual.gap(first)[1] if first == held else actual.gap(first)[0]
        pieces.append(actual.source[begin : actual.gap(last)[1]])
        held = last
    kept_first = bool(opcodes) and opcodes[0][0] == "equal"
    kept_last = bool(opcodes) and opcodes[-1][0] == "equal"
    lead = actual.lead if kept_first else new.lead
    trail = actual.trail if kept_last else new.trail
    return lead + "".join(pieces) + trail


@lru_cache(maxsize=16)
def _changes(
    copied: tuple[str, ...], new: tuple[str, ...]
) -> tuple[tuple[str, int, int, int, int], ...]:
    """The copy's kept and changed parts; spacing between two changes joins them."""
    opcodes: list[tuple[str, int, int, int, int]] = []
    for opcode in SequenceMatcher(None, copied, new, autojunk=False).get_opcodes():
        tag, i1, i2, j1, j2 = opcode
        if (
            len(opcodes) >= 2
            and tag != "equal"
            and opcodes[-1][0] == "equal"
            and opcodes[-2][0] != "equal"
            and all(_is_space(key) for key in copied[opcodes[-1][1] : opcodes[-1][2]])
        ):
            opcodes.pop()
            _, i1, _, j1, _ = opcodes.pop()
            opcodes.append(("replace", i1, i2, j1, j2))
            continue
        opcodes.append(opcode)
    return tuple(opcodes)


def _tolerated(correct: int, misspelled: int, gaps: int) -> bool:
    """Whether ``correct`` words carry the misspellings and kept-line gaps."""
    if not misspelled and not gaps:
        return True
    return (
        correct >= _TOLERANT_MIN_WORDS
        and misspelled * _WORDS_PER_MISSPELLING + gaps * _WORDS_PER_GAP <= correct
    )


def _level(alignment: _Alignment, gaps: int = 0) -> int | None:
    """The level at which the correctly copied words place a passage, or None.

    0: copied up to misspellings and ``gaps``, the ``other`` differences kept lines
    lack or add. 1: copied with other differences.
    """
    correct, misspelled, other = alignment.correct, alignment.misspelled, alignment.other
    if other == gaps and _tolerated(correct, misspelled, gaps):
        return 0
    if (
        not alignment.foreign
        and correct >= _DIFFERENCES_MIN_WORDS
        and (misspelled + other) * _WORDS_PER_DIFFERENCE <= correct
    ):
        return 1
    return None


class _File:
    """The content by lines, with folded line keys and where each word occurs."""

    def __init__(self, content: str) -> None:
        self.content = content
        parts = _split_lines_preserving_endings(content)
        self.texts = [text for text, _ in parts]
        self.endings = [ending for _, ending in parts]
        self.starts: list[int] = []
        offset = 0
        for text, ending in parts:
            self.starts.append(offset)
            offset += len(text) + len(ending)
        self.keys: list[str] = []
        self.line_words: list[list[str]] = []
        self.postings: dict[str, list[int]] = {}
        for number, text in enumerate(self.texts):
            folded = text.translate(_FOLD)
            self.keys.append(" ".join(folded.split()))
            self.line_words.append(_WORD.findall(folded))
            for word in dict.fromkeys(self.line_words[-1]):
                self.postings.setdefault(word, []).append(number)
        self.words = frozenset(self.postings)
        self.ending = next((ending for ending in self.endings if ending), "\n")
        self._lines: dict[int, _Text] = {}
        self._groups: dict[tuple[int, int], _Text] = {}
        self._by_key: dict[str, list[int]] | None = None
        self._unit: str | None = None
        self._unit_known = False

    def line(self, number: int) -> _Text:
        if number not in self._lines:
            self._lines[number] = _read(self.texts[number])
        return self._lines[number]

    def group(self, first: int, count: int) -> _Text:
        """The ``count`` lines from ``first`` read as one text."""
        if count == 1:
            return self.line(first)
        if (first, count) not in self._groups:
            begin, end = self.region(first, first + count - 1)
            self._groups[first, count] = _read(self.content[begin:end])
        return self._groups[first, count]

    def by_key(self) -> dict[str, list[int]]:
        if self._by_key is None:
            self._by_key = {}
            for number, key in enumerate(self.keys):
                self._by_key.setdefault(key, []).append(number)
        return self._by_key

    def unit(self) -> str | None:
        if not self._unit_known:
            self._unit = _indent_unit([_meaningful_indents(self.content)])
            self._unit_known = True
        return self._unit

    def region(self, first: int, last: int) -> tuple[int, int]:
        """Offsets of lines ``first`` to ``last``, without the last line's ending."""
        return self.starts[first], self.starts[last] + len(self.texts[last])

    def ends_after(self, last: int) -> bool:
        return last == len(self.texts) - 1 or (last == len(self.texts) - 2 and self.texts[-1] == "")


def _misplaced(
    file: _File,
    copy: _Text,
    alignment: _Alignment,
    first: int,
    count: int,
    *,
    kept: bool,
    passage: tuple[int, int],
) -> bool:
    """Whether the copy of lines ``first`` to ``first + count - 1`` belongs elsewhere.

    A kept line must hold at least half of its file line's words, and of its words and
    signs together; a blank file line holds none. Words the copy has beyond these
    lines that continue the line above or below show a copy joined across a line
    break. For a kept line only a neighbor outside the ``passage`` lines counts: the
    words moved within the passage, and the kept line stays as the file has it. A
    written line might write them again.
    """
    if kept and _mostly_wrong(file.line(first), copy, alignment):
        return True
    keys = copy.keys
    content = [index for index, key in enumerate(keys) if not _is_space(key)]
    lead: list[str] = []
    for index in content:
        if alignment.states[index] != _OTHER:
            break
        lead.append(keys[index])
    trail: list[str] = []
    for index in reversed(content):
        if alignment.states[index] != _OTHER:
            break
        trail.insert(0, keys[index])
    lead_words = [key for key in lead if _is_word(key)]
    trail_words = [key for key in trail if _is_word(key)]
    begin, end = passage
    if lead_words and first > 0 and not (kept and first > begin):
        above = file.line_words[first - 1]
        if len(lead_words) <= len(above) and above[-len(lead_words) :] == lead_words:
            return True
    if trail_words and first + count < len(file.texts) and not (kept and first + count < end):
        below = file.line_words[first + count]
        if below[: len(trail_words)] == trail_words:
            return True
    return False


def _mostly_wrong(held: _Text, copy: _Text, alignment: _Alignment) -> bool:
    """Whether a kept line's copy holds less than half of the file line's words or signs.

    Signs count with words, so that lines without words (a closing tag, a table
    rule) are judged too; words count alone, so that shared markup ("##", "-") does
    not make another line look right.
    """
    signs = _content(held.keys) - len(alignment.escapes)
    if signs <= 0:
        return True
    words = sum(_is_word(key) for key in held.keys)
    matched = [
        key
        for key, state in zip(copy.keys, alignment.states, strict=True)
        if state in (_SAME, _MISSPELLED) or (state == _ALIKE and key != _BACKSLASH)
        if not _is_space(key)
    ]
    matched_words = sum(_is_word(key) for key in matched)
    return len(matched) * 2 < signs or matched_words * 2 < words


def _anchor_words(file: _File, lines: Sequence[str]) -> list[tuple[str, list[int]]]:
    """Rare words of ``lines`` that the file holds, enough that one is copied correctly.

    A misspelled word never occurs in the file, and a copy placed without anchors
    has at most one other difference per 2 correct words, so one of these words is
    correct.
    """
    positions: dict[str, list[int]] = {}
    total = 0
    for index, line in enumerate(lines):
        words = _WORD.findall(line.translate(_FOLD))
        total += len(words)
        for word in dict.fromkeys(words):
            if word in file.words:
                positions.setdefault(word, []).append(index)
    ranked = sorted(positions.items(), key=lambda item: len(file.postings[item[0]]))
    return ranked[: total // _WORDS_PER_GAP + 1]


def _line_starts(file: _File, old: Sequence[str]) -> list[int]:
    """First lines of passages that could hold ``old``: an exact line or a rare word."""
    starts: set[int] = set()
    by_key = file.by_key()
    for index, line in enumerate(old):
        key = _line_key(line)
        if _WORD.search(key):
            starts.update(number - index for number in by_key.get(key, ()))
    for word, positions in _anchor_words(file, old):
        for number in file.postings[word]:
            starts.update(number - index for index in positions)
    limit = len(file.texts) - len(old)
    return sorted(start for start in starts if 0 <= start <= limit)


def _ends_match(file: _File, old: Sequence[str], start: int) -> bool:
    last = start + len(old) - 1
    return file.keys[start] == _line_key(old[0]) and file.keys[last] == _line_key(old[-1])


def _anchored(file: _File, old: Sequence[str], start: int) -> bool:
    """Whether exactly copied first and last lines place the passage at ``start``."""
    count = len(old)
    if count < _ANCHOR_MIN_LINES:
        return False
    first, last = _line_key(old[0]), _line_key(old[-1])
    if file.keys[start] != first or file.keys[start + count - 1] != last:
        return False
    if min(len(_WORD.findall(first)), len(_WORD.findall(last))) < _ANCHOR_MIN_WORDS:
        return False
    shared = sum(
        number + count - 1 < len(file.keys) and file.keys[number + count - 1] == last
        for number in file.by_key().get(first, ())
    )
    threshold = _ANCHOR_UNIQUE_SIMILARITY if shared == 1 else _ANCHOR_SHARED_SIMILARITY
    middle = "\n".join(_line_key(line) for line in old[1:-1])
    held = "\n".join(file.keys[start + 1 : start + count - 1])
    return SequenceMatcher(None, middle, held).ratio() >= threshold


@dataclass(frozen=True)
class _Placed:
    """A qualifying passage; ``text`` is None when the change does not fit it.

    ``differed`` and ``kept_differed`` name replaced and kept lines that differed
    from the copy, as (1-based line, the caller's copy of it).
    """

    start: int
    end: int
    text: str | None
    differed: tuple[tuple[int, str], ...]
    respelled: tuple[tuple[str, str], ...]
    level: int
    cost: int  # misspellings, other differences and ones that never matter
    kept_differed: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class _Segment:
    """One kept line, or a group of removed and added lines, by index into each side."""

    kept: bool
    old: int
    removed: int
    new: int
    added: int


def _segments(lines: Sequence[tuple[str, str]]) -> Iterator[_Segment]:
    old = new = index = 0
    while index < len(lines):
        if lines[index][0] == " ":
            yield _Segment(True, old, 1, new, 1)
            old, new, index = old + 1, new + 1, index + 1
            continue
        end = index
        while end < len(lines) and lines[end][0] != " ":
            end += 1
        removed = sum(prefix == "-" for prefix, _ in lines[index:end])
        added = end - index - removed
        yield _Segment(False, old, removed, new, added)
        old, new, index = old + removed, new + added, end


@dataclass(frozen=True)
class _Hunk:
    """The caller's side of a hunk, read once for every candidate passage."""

    old: list[str]
    new: str
    segments: list[_Segment]
    copies: list[_Text]
    words: list[Counter[str]]

    @property
    def total(self) -> int:
        return sum(words.total() for words in self.words)


def _read_hunk(file: _File, lines: Sequence[tuple[str, str]]) -> _Hunk:
    old = [text for prefix, text in lines if prefix in " -"]
    segments = list(_segments(lines))
    copies = [
        _read(file.ending.join(old[segment.old : segment.old + segment.removed]))
        for segment in segments
    ]
    words = [Counter(key for key in copy.keys if _is_word(key)) for copy in copies]
    new = "\n".join(text for prefix, text in lines if prefix in " +")
    return _Hunk(old, new, segments, copies, words)


def _correct_bound(file: _File, hunk: _Hunk, start: int) -> int:
    """How many of the hunk's words could be copied correctly at ``start``, at most."""
    bound = 0
    for segment, words in zip(hunk.segments, hunk.words, strict=True):
        first = start + segment.old
        held = Counter(
            word
            for number in range(first, first + segment.removed)
            for word in file.line_words[number]
        )
        bound += (words & held).total()
    return bound


def _match_lines(
    file: _File, lines: Sequence[tuple[str, str]], *, at_eof: bool, drop_breaks: bool = False
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    hunk = _read_hunk(file, lines)
    candidates = []
    for start in _line_starts(file, hunk.old):
        if at_eof and not file.ends_after(start + len(hunk.old) - 1):
            continue
        bound = _correct_bound(file, hunk, start)
        if bound >= hunk.total * _MIN_CORRECT_SHARE or _ends_match(file, hunk.old, start):
            candidates.append((bound, start))
    # Likely passages first: once one qualifies, others must do as well to matter.
    candidates.sort(key=lambda item: (-item[0], item[1]))
    placed: list[_Placed] = []
    for _, start in candidates:
        best = min((item.level for item in placed), default=None)
        candidate = _place_lines(file, hunk, start, best=best, drop_breaks=drop_breaks)
        if candidate is not None:
            placed.append(candidate)
    return _result(file, placed)


def _reachable(total: _Alignment, gaps: int, remaining: int, best: int | None) -> bool:
    """Whether a passage aligned this far can still qualify at ``best`` or better."""
    most = total.correct + remaining
    if total.other == gaps and _tolerated(most, total.misspelled, gaps):
        return True
    return (
        best != 0
        and not total.foreign
        and most >= _DIFFERENCES_MIN_WORDS
        and (total.misspelled + total.other) * _WORDS_PER_DIFFERENCE <= most
    )


def _place_lines(
    file: _File, hunk: _Hunk, start: int, *, best: int | None, drop_breaks: bool
) -> _Placed | None:
    """Place the hunk at line ``start``; the passage covers its lines without the last ending.

    Only a passage qualifying at ``best`` or better counts. With ``drop_breaks``, a
    hunk that leaves no line covers that ending too, so the lines go entirely.
    """
    old = hunk.old
    anchored = best != 0 and _anchored(file, old, start)
    speller = _Speller(file.words)
    aligned = []
    total = _Alignment([])
    gaps = 0
    remaining = hunk.total
    for segment, copy, words in zip(hunk.segments, hunk.copies, hunk.words, strict=True):
        actual = file.group(start + segment.old, segment.removed) if segment.removed else copy
        alignment = _align(copy, actual, speller)
        total.correct += alignment.correct
        total.misspelled += alignment.misspelled
        total.other += alignment.other
        total.foreign += alignment.foreign
        total.alike += alignment.alike
        total.unwritable |= alignment.unwritable
        gaps += alignment.gaps if segment.kept else 0
        remaining -= words.total()
        if not anchored and not _reachable(total, gaps, remaining, best):
            return None
        aligned.append((segment, copy, actual, alignment))
    level = _level(total, gaps)
    if level is None and anchored:
        level = 1
    if level is None or (best is not None and level > best):
        return None
    begin, end = file.region(start, start + len(old) - 1)
    new = _reindent_replacement(
        file.content[begin:end], "\n".join(old), hunk.new, file.unit()
    ).split("\n")
    output = []
    differed = []
    kept_differed = []
    fits = True
    passage = (start, start + len(old))
    for segment, copy, actual, alignment in aligned:
        first = start + segment.old
        if (
            segment.removed
            and alignment.other
            and _misplaced(
                file,
                copy,
                alignment,
                first,
                segment.removed,
                kept=segment.kept,
                passage=passage,
            )
        ):
            fits = False
            break
        if segment.kept:
            output.append(file.texts[first])
            if file.keys[first] != _line_key(old[segment.old]):
                kept_differed.append((first + 1, old[segment.old]))
            continue
        wanted = _read(file.ending.join(new[segment.new : segment.new + segment.added]))
        # How the copy escapes anywhere in the passage bars what every written line holds.
        merged = _merge(copy, actual, wanted, alignment, speller, total.unwritable)
        if merged is None or (
            segment.added and len(TEXT_LINE_BREAK.findall(merged)) != segment.added - 1
        ):
            fits = False
            break
        if segment.added:
            output.append(merged)
        for offset in range(segment.removed):
            number, copied = first + offset, old[segment.old + offset]
            if file.keys[number] != _line_key(copied):
                differed.append((number + 1, copied))
    if fits and drop_breaks and not output:
        last = start + len(old) - 1
        if file.endings[last]:
            end += len(file.endings[last])
        elif start:
            begin -= len(file.endings[start - 1])
    return _Placed(
        begin,
        end,
        file.ending.join(output) if fits else None,
        tuple(differed),
        _respelled(speller, hunk.new),
        level,
        total.misspelled + total.other + total.alike,
        tuple(kept_differed),
    )


def _respelled(speller: _Speller, new: str) -> tuple[tuple[str, str], ...]:
    used = set(_WORD.findall(new))
    return tuple((wrong, right) for wrong, right in speller.spelling.items() if wrong in used)


def _match_fragment(
    file: _File, old: str, new: str
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    """Apply an edit whose one-line ``old`` copies part of a line."""
    lead = old[: len(old) - len(old.lstrip())]
    trail = old[len(old.rstrip()) :]
    if len(new) < len(lead) + len(trail) or not (new.startswith(lead) and new.endswith(trail)):
        return None
    copy = _read(old)
    if not copy.tokens:
        return None
    wanted = _read(TEXT_LINE_BREAK.sub(file.ending, new[len(lead) : len(new) - len(trail)]))
    lines = sorted(
        {number for word, _ in _anchor_words(file, [old]) for number in file.postings[word]}
    )
    copied = Counter(key for key in copy.keys if _is_word(key))
    placed: list[_Placed] = []
    for number in lines:
        held = Counter(file.line_words[number])
        if (copied & held).total() < copied.total() * _MIN_CORRECT_SHARE:
            continue
        line = file.line(number)
        for first, last in _windows(line, copy, file.words):
            speller = _Speller(file.words)
            window = line.window(first, last)
            alignment = _align(copy, window, speller)
            level = _level(alignment)
            if level is None:
                continue
            merged = _merge(copy, window, wanted, alignment, speller, alignment.unwritable)
            begin = file.starts[number] + line.tokens[first].start
            end = file.starts[number] + line.tokens[last - 1].end
            differed = () if window.keys == copy.keys else ((number + 1, old),)
            respelled = _respelled(speller, new)
            cost = alignment.misspelled + alignment.other + alignment.alike
            placed.append(_Placed(begin, end, merged, differed, respelled, level, cost))
    return _result(file, placed)


def _windows(line: _Text, copy: _Text, words: frozenset[str]) -> list[tuple[int, int]]:
    """Token ranges of ``line`` whose ends could be copied as the ends of ``copy``."""
    keys, wanted = line.keys, copy.keys
    slack = 2 * (_content(wanted) // _WORDS_PER_DIFFERENCE + 1)

    def fits(copied: str, held: str) -> bool:
        return copied == held or (copied not in words and _misspelling(copied, held))

    firsts = [index for index, key in enumerate(keys) if fits(wanted[0], key)]
    lasts = [index + 1 for index, key in enumerate(keys) if fits(wanted[-1], key)]
    return [
        (first, last)
        for first in firsts
        for last in lasts
        if first < last and abs(last - first - len(wanted)) <= slack
    ]


def _result(
    file: _File, placed: Sequence[_Placed]
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    if not placed:
        return None
    level = min(item.level for item in placed)
    # Candidates that overlap place one passage: the fewest differences place it,
    # and a tie between different spans stays ambiguous.
    unique: list[_Placed] = []
    for item in sorted(
        (item for item in placed if item.level == level),
        key=lambda item: (item.cost, item.start, item.end),
    ):
        if any(
            (item.start, item.end) == (other.start, other.end)
            or (item.start < other.end and other.start < item.end and item.cost > other.cost)
            for other in unique
        ):
            continue
        unique.append(item)
    content = file.content
    if len(unique) > 1:
        unique.sort(key=lambda item: item.start)
        lines = [bisect_right(file.starts, item.start) for item in unique]
        characters = [
            item.start - file.starts[line - 1] + 1 for item, line in zip(unique, lines, strict=True)
        ]
        return AmbiguousFuzzyMatch(len(unique), lines, characters)
    item = unique[0]
    if item.text is None:
        return None
    new_content = content[: item.start] + item.text + content[item.end :]
    after_end = item.start + len(item.text)
    return FuzzyReplacement(
        new_content,
        _line_and_character_at(content, item.start)[0],
        _line_and_character_at(new_content, max(item.start, after_end - 1))[0],
        1,
        "copied",
        ((item.start, item.end),),
        ((item.start, after_end),),
        differed=_excerpts(file, item.differed),
        respelled=item.respelled,
        kept_differed=_excerpts(file, item.kept_differed),
    )


def _excerpts(file: _File, lines: Sequence[tuple[int, str]]) -> tuple[tuple[int, str], ...]:
    return tuple((number, _excerpt(file.texts[number - 1], copy)) for number, copy in lines)


def _without_final_break(text: str) -> str:
    """Drop one final line break: whole-line text ends where its last line ends."""
    for ending in ("\r\n", "\n", "\r"):
        if text.endswith(ending):
            return text[: -len(ending)]
    return text


__all__ = ["copy_warnings", "match_copied_edit", "replace_copied"]
