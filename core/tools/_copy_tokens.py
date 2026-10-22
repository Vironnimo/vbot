"""Token alignment, spelling tolerance, and copied-text merging."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache

from core.tools._fuzzy_text import _TYPOGRAPHIC_NORMALIZATION, _composed, _normalize_with_spans
from core.tools.arguments import TEXT_LINE_BREAK
from core.utils.edit_distance import edit_distance

# Where: below this many correct words a copy must be exact up to spacing.
# ``moved``: spacing that differs in line breaks, which a change must not rest on.
# ``alike``: a hyphen for an em dash, or a lone backslash only the copy holds.
_SAME, _MISSPELLED, _MOVED, _ALIKE, _OTHER = "same", "misspelled", "moved", "alike", "other"


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
_INLINE_SPACE = " \t"


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


def _code_names(text: _Text) -> set[int]:
    """Recognize locator names by their spelling or code syntax.

    This is evidence in a copied locator, not a language parser. Calls,
    declarations and member/namespace access identify names that cannot simply
    substitute for one another. Plain prose keeps its existing tolerance.
    """
    tokens = text.tokens
    indices = [index for index, token in enumerate(tokens) if not _is_space(token.key)]
    names = {index for index in indices if _identifier(tokens[index].key)}
    declarations = {"def", "fn", "func", "function", "class", "struct", "enum", "trait"}
    modifiers = {"pub", "public", "private", "protected", "static", "async", "export", "abstract"}
    for position, index in enumerate(indices):
        token = tokens[index]
        if not _is_word(token.key) or token.key.isdigit():
            continue
        if position + 1 < len(indices):
            following = tokens[indices[position + 1]]
            if following.key == "(" and token.end == following.start:
                names.add(index)
            if (
                position
                and tokens[indices[position - 1]].key in declarations
                and all(tokens[item].key in modifiers for item in indices[: position - 1])
                and following.key in {"(", ":", "{", "<"}
            ):
                names.add(index)
        for width, separators in ((1, ["."]), (2, [":", ":"]), (2, ["-", ">"])):
            end = position + width + 1
            if end >= len(indices):
                continue
            between = [tokens[item].key for item in indices[position + 1 : end]]
            other = tokens[indices[end]]
            if between != separators or not _is_word(other.key):
                continue
            # A full stop in prose is not member access. Namespace and arrow
            # separators are already distinctive; spaces around those are fine.
            if separators == ["."] and (
                token.end != tokens[indices[position + 1]].start
                or tokens[indices[position + 1]].end != other.start
            ):
                continue
            names.update((index, indices[end]))
    return names


def _line_key(text: str) -> str:
    return " ".join(_composed(text).translate(_FOLD).split())


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
    return edit_distance(wrong.casefold(), right.casefold(), bound) <= bound


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
    ``foreign`` the other differences where either side names a target;
    ``named_conflict`` a copied name present elsewhere in the file, which even
    exact outer anchors cannot excuse. ``alike`` counts differences that never
    matter. ``escapes`` holds the copy boundaries where
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
    named_conflict: bool = False
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
    copy_names, actual_names = _code_names(copy), _code_names(actual)

    def different_names(i1: int, i2: int, j1: int, j2: int) -> None:
        copied_names = {copied[index] for index in copy_names if i1 <= index < i2}
        held_names = {held[index] for index in actual_names if j1 <= index < j2}
        alignment.foreign += bool(copied_names or held_names)
        # Even exact surrounding lines cannot make a known name refer to a
        # different target. A misspelling (handled separately below) occurs
        # nowhere in the file; a known name is an independent locator.
        alignment.named_conflict |= bool((copied_names - held_names) & speller.words)

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
            if actual_names.intersection(range(j1, j2)):
                different_names(i1, i2, j1, j2)
            continue
        alignment.after[i1] = j1
        if tag == "delete":
            if _lone_backslash(copied[i1:i2]):
                _mark_alike(alignment, copied, i1, i2)
                alignment.unwritable.add(_BACKSLASH)
                continue
            alignment.other += _content(copied[i1:i2])
            alignment.gaps += _content(copied[i1:i2])
            if copy_names.intersection(range(i1, i2)):
                different_names(i1, i2, j1, j2)
            continue
        if i2 - i1 != j2 - j1:
            if _dash_pair(copied[i1:i2], held[j1:j2]):
                _mark_alike(alignment, copied, i1, i2)
                continue
            alignment.other += max(_content(copied[i1:i2]), _content(held[j1:j2]))
            different_names(i1, i2, j1, j2)
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
                different_names(index, index + 1, j1 + offset, j1 + offset + 1)
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
    # Locating a copy ignores spacing; deciding what the caller changed does not.
    # In particular, a kept word moved into a new block needs its new indentation.
    opcodes = _changes(_change_keys(copy), _change_keys(new))
    pieces: list[str] = []
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
            if pieces and written.startswith(tuple(_INLINE_SPACE)):
                # Spacing the caller writes replaces the file's spacing at the seam.
                pieces[-1] = pieces[-1].rstrip(_INLINE_SPACE)
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
        kept = actual.source[begin : actual.gap(last)[1]]
        if pieces and pieces[-1].endswith(tuple(_INLINE_SPACE)):
            kept = kept.lstrip(_INLINE_SPACE)
        pieces.append(kept)
        held = last
    kept_first = bool(opcodes) and opcodes[0][0] == "equal"
    kept_last = bool(opcodes) and opcodes[-1][0] == "equal"
    lead = actual.lead if kept_first and _spacing(copy.lead) == _spacing(new.lead) else new.lead
    trail = actual.trail if kept_last and _spacing(copy.trail) == _spacing(new.trail) else new.trail
    return lead + "".join(pieces) + trail


def _spacing(text: str) -> str:
    """Compare written whitespace without treating line endings or invisibles as edits."""
    return TEXT_LINE_BREAK.sub("\n", text.translate(_FOLD))


def _change_keys(text: _Text) -> tuple[str, ...]:
    """Keep whitespace spelling in the old/new diff, after new indentation was mapped."""
    return tuple(
        _spacing(text.source[token.start : token.end]) if _is_space(token.key) else token.key
        for token in text.tokens
    )


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
    if alignment.named_conflict or alignment.foreign:
        return None
    correct, misspelled, other = alignment.correct, alignment.misspelled, alignment.other
    if other == gaps and _tolerated(correct, misspelled, gaps):
        return 0
    if (
        correct >= _DIFFERENCES_MIN_WORDS
        and (misspelled + other) * _WORDS_PER_DIFFERENCE <= correct
    ):
        return 1
    return None
