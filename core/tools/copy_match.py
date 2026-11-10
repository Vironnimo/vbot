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
never by substituting or dropping a name: identifiers (an underscore, a digit or
an inner capital) and names evidenced by call, declaration or member syntax name
something else, such as a sibling function. A passage of 3 or more lines is placed as
well by exactly copied first and last lines of at least 2 words each around similar
lines, unless it contradicts a copied name the file actually holds. Each line keeps
its place: a kept line must hold at least half of its file
line's words, and of its words and signs together (a blank file line holds none),
and a copy whose extra words continue the line above or below joined lines across a
line break (the copy left out or added a line), so the passage does not take the
edit. Words moved across a line break between a kept line and another line of the
passage are the exception, since the kept line stays as the file has it. Lines the
edit replaces hold no more blank lines than their copy: a copy that breaks a line
in two must not take the blank line after it as its second line.
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

Characters with marks compare composed, so a copy in the other Unicode normalization
form (NFC or NFD) copies them correctly; the text the edit writes then takes the
passage's form.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from functools import lru_cache

from core.tools._copy_tokens import (
    _ALIKE,
    _ANCHOR_MIN_LINES,
    _ANCHOR_MIN_WORDS,
    _ANCHOR_SHARED_SIMILARITY,
    _ANCHOR_UNIQUE_SIMILARITY,
    _BACKSLASH,
    _DIFFERENCES_MIN_WORDS,
    _FOLD,
    _MIN_CORRECT_SHARE,
    _MISSPELLED,
    _OTHER,
    _SAME,
    _WARNING_LINE_CHARACTERS,
    _WARNING_LINES,
    _WORD,
    _WORDS_PER_DIFFERENCE,
    _WORDS_PER_GAP,
    _align,
    _Alignment,
    _content,
    _is_space,
    _is_word,
    _level,
    _line_key,
    _merge,
    _misspelling,
    _read,
    _Speller,
    _Text,
    _tolerated,
    _WorkBudget,
    _WorkLimitError,
)
from core.tools._fuzzy_text import (
    _in_form,
    _indent_unit,
    _line_number_at,
    _meaningful_indents,
    _reindent_replacement,
    _split_lines_preserving_endings,
    _written_form,
)
from core.tools.arguments import TEXT_LINE_BREAK
from core.tools.fuzzy_match import AmbiguousFuzzyMatch, FuzzyReplacement, dominant_line_ending


class CopyMatchLimitError(RuntimeError):
    """The copy search was incomplete, so absence of a match is not established."""


def replace_copied(
    content: str, old: str, new: str, *, require_complete: bool = False
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    """Apply an ``old``/``new`` text edit whose ``old`` copies the file with errors.

    ``old`` names whole lines, or part of one line when it has no line break.
    Returns ``None`` when no passage qualifies or the work budget is exhausted.
    With ``require_complete``, exhaustion raises ``CopyMatchLimitError`` instead:
    callers using absence as evidence must distinguish an incomplete search.
    """
    try:
        return _replace_copied(content, old, new, _WorkBudget())
    except _WorkLimitError:
        if require_complete:
            raise CopyMatchLimitError from None
        return None


def _replace_copied(
    content: str, old: str, new: str, budget: _WorkBudget
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    old_body, new_body = _without_final_break(old), _without_final_break(new)
    old_lines = TEXT_LINE_BREAK.split(old_body)
    whole = old_body != old
    # Replacing whole lines with nothing removes them, line breaks included.
    new_lines = [] if whole and new == "" else TEXT_LINE_BREAK.split(new_body)
    if not any(line.strip() for line in old_lines):
        return None
    lines: list[tuple[str, str]] = []
    budget.compare(old_lines, new_lines)
    matcher = SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            lines.extend((" ", line) for line in old_lines[i1:i2])
            continue
        lines.extend(("-", line) for line in old_lines[i1:i2])
        lines.extend(("+", line) for line in new_lines[j1:j2])
    file = _File(content, budget)
    found = _match_lines(file, lines, at_eof=False, drop_breaks=True)
    if found is None and len(old_lines) == 1 and not whole:
        found = _match_fragment(file, old, new)
    return _in_file_form(content, old, found)


def match_copied_edit(
    content: str, lines: Sequence[tuple[str, str]], *, at_eof: bool = False
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    """Apply a hunk whose kept (``" "``) and removed (``"-"``) lines copy whole lines.

    Added lines are ``"+"``. With ``at_eof`` the passage must end the file.
    Returns ``None`` when no passage qualifies or the work budget is exhausted.
    """
    if not any(text.strip() for prefix, text in lines if prefix in " -"):
        return None
    try:
        found = _match_lines(_File(content, _WorkBudget()), lines, at_eof=at_eof)
        old = "\n".join(text for prefix, text in lines if prefix in " -")
        return _in_file_form(content, old, found)
    except _WorkLimitError:
        return None


def _in_file_form(
    content: str, old: str, found: FuzzyReplacement | AmbiguousFuzzyMatch | None
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    """Write the edit in the Unicode normalization form of the passage it replaces.

    Only when the copy came in the other form (``_written_form``); text the edit
    keeps already has the passage's form.
    """
    if not isinstance(found, FuzzyReplacement):
        return found
    (start, end), (after_start, after_end) = found.before_spans[0], found.after_spans[0]
    written = found.new_content[after_start:after_end]
    text = _in_form(written, _written_form(content[start:end], old))
    if text == written:
        return found
    new_content = content[:start] + text + content[end:]
    return replace(
        found,
        new_content=new_content,
        last_changed_line=_line_number_at(new_content, max(start, start + len(text) - 1)),
        after_spans=((start, start + len(text)),),
    )


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


def _excerpt(text: str, copy: str, budget: _WorkBudget) -> str:
    """The line, or the part of a long line from just before its first difference."""
    if len(text) <= _WARNING_LINE_CHARACTERS:
        return text
    held, copied = _read(text), _read(copy)
    budget.compare(copied.keys, held.keys)
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


class _File:
    """The content by lines, with folded line keys and where each word occurs."""

    def __init__(self, content: str, budget: _WorkBudget) -> None:
        self.content = content
        self.budget = budget
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
        self.ending = dominant_line_ending(content) or "\n"
        self._lines: dict[int, _Text] = {}
        self._groups: dict[tuple[int, int], _Text] = {}
        self._by_key: dict[str, list[int]] | None = None
        self._unit: str | None = None
        self._unit_known = False

    def line(self, number: int) -> _Text:
        if number not in self._lines:
            self.budget.spend(len(self.texts[number]))
            self._lines[number] = _read(self.texts[number])
        return self._lines[number]

    def group(self, first: int, count: int) -> _Text:
        """The ``count`` lines from ``first`` read as one text."""
        if count == 1:
            return self.line(first)
        if (first, count) not in self._groups:
            begin, end = self.region(first, first + count - 1)
            self.budget.spend(end - begin)
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
    limit = len(file.texts) - len(old)

    def add(number: int, index: int) -> None:
        file.budget.spend(1)
        start = number - index
        if 0 <= start <= limit:
            starts.add(start)
            if len(starts) > file.budget.candidates:
                raise _WorkLimitError

    by_key = file.by_key()
    for index, line in enumerate(old):
        key = _line_key(line)
        if _WORD.search(key):
            for number in by_key.get(key, ()):
                add(number, index)
    for word, positions in _anchor_words(file, old):
        for number in file.postings[word]:
            for index in positions:
                add(number, index)
    return sorted(starts)


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
    anchors = file.by_key().get(first, ())
    file.budget.spend(len(anchors))
    shared = sum(
        number + count - 1 < len(file.keys) and file.keys[number + count - 1] == last
        for number in anchors
    )
    threshold = _ANCHOR_UNIQUE_SIMILARITY if shared == 1 else _ANCHOR_SHARED_SIMILARITY
    middle = "\n".join(_line_key(line) for line in old[1:-1])
    held = "\n".join(file.keys[start + 1 : start + count - 1])
    file.budget.compare(middle, held)
    return SequenceMatcher(None, middle, held).ratio() >= threshold


@dataclass(frozen=True)
class _Placed:
    """A qualifying passage; ``text`` is None when the change does not fit it.

    ``differed`` names replaced lines that differed from the copy, as (1-based line,
    the caller's copy of it); ``kept_differed`` names kept lines that differed, as
    (1-based line once the edit is applied, the file's line as excerpt).
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
        file.budget.spend(
            len(words)
            + sum(len(file.line_words[number]) for number in range(first, first + segment.removed))
        )
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
        file.budget.candidate()
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
    if any(_takes_blank_line(file, old, segment, start) for segment in hunk.segments):
        return None
    anchored = best != 0 and _anchored(file, old, start)
    speller = _Speller(file.words, file.budget)
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
        if alignment.named_conflict:
            return None
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
    region = file.content[begin:end]
    new = _reindent_replacement(region, "\n".join(old), hunk.new, file.unit).split("\n")
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
                # The kept line is named where the edit leaves it.
                excerpt = _excerpt(file.texts[first], old[segment.old], file.budget)
                kept_differed.append((start + segment.new + 1, excerpt))
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


def _takes_blank_line(file: _File, old: Sequence[str], segment: _Segment, start: int) -> bool:
    """Whether the lines a segment replaces hold more blank lines than its copy.

    A blank line holds no words or signs the copy could miss, so only the count
    shows that the copy, its line breaks moved, leaves one out ("return" and "1"
    copying a line "return 1", then the blank line after it). Replacing it would
    remove a line the copy does not hold. A kept line stays as the file has it.
    """
    if segment.kept:
        return False
    first = start + segment.old
    held = sum(not key for key in file.keys[first : first + segment.removed])
    copied = sum(not _line_key(line) for line in old[segment.old : segment.old + segment.removed])
    return held > copied


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
    lines: set[int] = set()
    for word, _ in _anchor_words(file, [old]):
        for number in file.postings[word]:
            file.budget.spend(1)
            lines.add(number)
            if len(lines) > file.budget.candidates:
                raise _WorkLimitError
    copied = Counter(key for key in copy.keys if _is_word(key))
    placed: list[_Placed] = []
    for number in sorted(lines):
        file.budget.spend(len(file.line_words[number]))
        held = Counter(file.line_words[number])
        if (copied & held).total() < copied.total() * _MIN_CORRECT_SHARE:
            continue
        line = file.line(number)
        for first, last in _windows(line, copy, file.words, file.budget):
            file.budget.candidate()
            speller = _Speller(file.words, file.budget)
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


def _windows(
    line: _Text, copy: _Text, words: frozenset[str], budget: _WorkBudget
) -> Iterator[tuple[int, int]]:
    """Token ranges of ``line`` whose ends could be copied as the ends of ``copy``."""
    keys, wanted = line.keys, copy.keys
    budget.spend(len(keys))
    slack = 2 * (_content(wanted) // _WORDS_PER_DIFFERENCE + 1)

    @lru_cache(maxsize=512)
    def fits(copied: str, held: str) -> bool:
        return copied == held or (copied not in words and _misspelling(copied, held, budget))

    firsts = [index for index, key in enumerate(keys) if fits(wanted[0], key)]
    lasts = [index + 1 for index, key in enumerate(keys) if fits(wanted[-1], key)]
    for first in firsts:
        low = bisect_left(lasts, max(first + 1, first + len(wanted) - slack))
        high = bisect_right(lasts, first + len(wanted) + slack)
        for index in range(low, high):
            yield first, lasts[index]


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
        file.budget.spend(len(unique))
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
        return AmbiguousFuzzyMatch(
            len(unique),
            [bisect_right(file.starts, item.start) for item in unique],
            tuple((item.start, item.end) for item in unique),
        )
    item = unique[0]
    if item.text is None:
        return None
    new_content = content[: item.start] + item.text + content[item.end :]
    after_end = item.start + len(item.text)
    return FuzzyReplacement(
        new_content,
        _line_number_at(content, item.start),
        _line_number_at(new_content, max(item.start, after_end - 1)),
        1,
        "copied",
        ((item.start, item.end),),
        ((item.start, after_end),),
        differed=_excerpts(file, item.differed),
        respelled=item.respelled,
        kept_differed=item.kept_differed,
    )


def _excerpts(file: _File, lines: Sequence[tuple[int, str]]) -> tuple[tuple[int, str], ...]:
    return tuple(
        (number, _excerpt(file.texts[number - 1], copy, file.budget)) for number, copy in lines
    )


def _without_final_break(text: str) -> str:
    """Drop one final line break: whole-line text ends where its last line ends."""
    for ending in ("\r\n", "\n", "\r"):
        if text.endswith(ending):
            return text[: -len(ending)]
    return text


__all__ = ["CopyMatchLimitError", "copy_warnings", "match_copied_edit", "replace_copied"]
