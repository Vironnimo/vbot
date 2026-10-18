"""Shared immutable results of precise and copied-text matching."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FuzzyReplacement:
    """A successful fuzzy replacement applied to the original content."""

    new_content: str
    first_changed_line: int
    last_changed_line: int
    replacements: int
    strategy: str
    before_spans: tuple[tuple[int, int], ...]
    after_spans: tuple[tuple[int, int], ...]
    # Only ``copy_match`` sets these: replaced and kept lines that differed from the
    # caller's copy as (1-based line, original text or its part around the first
    # difference), and the caller's misspelled words its new text took the file's
    # spelling of, as (caller's word, file's word).
    differed: tuple[tuple[int, str], ...] = ()
    respelled: tuple[tuple[str, str], ...] = ()
    kept_differed: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class AmbiguousFuzzyMatch:
    """The winning strategy matched more than once without ``replace_all``.

    ``line_numbers`` holds the 1-based line each match starts on and ``spans``
    its (start, end) offsets in the searched text, both in text order.
    """

    occurrences: int
    line_numbers: list[int]
    spans: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class ClosestFuzzyCandidate:
    """A bounded raw excerpt similar to an unmatched edit locator."""

    line_number: int
    text: str
    truncated: bool


@dataclass(frozen=True)
class LineDifferences:
    """Every line where a copy differs from the passage it was compared with.

    The passage spans file lines ``first_line`` to ``last_line`` (1-based).
    ``differences`` holds the first few differing lines in passage order and
    ``count`` all of them. Each names the file ``line`` and the ``copy_line``
    with at most 240-character windows of both (``file``, ``copy``); a line
    only the file holds has no copy side, a line only the copy holds no file
    side (``None``). ``spacing`` is ``"start"`` or ``"end"`` for a line that
    differs only in the whitespace at that side, else ``None``.
    """

    first_line: int
    last_line: int
    differences: tuple[dict[str, int | str | bool | None], ...]
    count: int
