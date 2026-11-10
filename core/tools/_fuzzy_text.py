"""Text normalization, original-span mapping, and indentation preservation."""

from __future__ import annotations

import math
import re
import unicodedata
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from difflib import Match, SequenceMatcher
from functools import cache
from typing import Literal

from core.tools.arguments import TEXT_LINE_BREAK
from core.utils.line_endings import dominant_line_ending as dominant_line_ending

# Visually-equivalent characters models emit in place of their ASCII forms, keyed
# by code point so the source stays pure ASCII and the entries are unambiguous.
# Only 1:1 mappings live here so every normalized character maps back to exactly
# one original character; length-changing expansions (em-dash -> "--", ellipsis
# -> "...") are deliberately omitted to keep span mapping unambiguous.
_UNICODE_NORMALIZATION = {
    "“": '"',  # left double quotation mark
    "”": '"',  # right double quotation mark
    **dict.fromkeys("„«»", '"'),  # low double quotation mark and guillemets
    "‘": "'",  # left single quotation mark
    "’": "'",  # right single quotation mark
    **dict.fromkeys("‚‹›", "'"),  # low single quotation mark and guillemets
    "\u00a0": " ",  # non-breaking space
    **dict.fromkeys("‐‑–", "-"),  # hyphen, non-breaking hyphen, en dash
}

# Patch-only typography folds may expand a glyph. Default substring calls keep
# their established normalization and replacement semantics.
_TYPOGRAPHIC_NORMALIZATION = {
    **_UNICODE_NORMALIZATION,
    "\u2014": "--",
    "\u2026": "...",
    "\u2212": "-",
    **dict.fromkeys(
        "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000", " "
    ),
}

# The line endings file Tools number (``TEXT_LINE_BREAK``). Detection order
# matters: CRLF first, then LF before CR for mixed files. Other separators such
# as U+2028 or form feed are ordinary characters within a line.
_LINE_ENDINGS = ("\r\n", "\n", "\r")
_LINE_BREAK_RE = TEXT_LINE_BREAK
_HORIZONTAL_WHITESPACE_RE = re.compile(r"[ \t]+")
_UNICODE_TRANSLATION = str.maketrans(_UNICODE_NORMALIZATION)
_UNICODE_GLYPHS = "[" + "".join(re.escape(char) for char in _UNICODE_NORMALIZATION) + "]"
_TYPOGRAPHIC_GLYPHS = "[" + "".join(re.escape(char) for char in _TYPOGRAPHIC_NORMALIZATION) + "]"
_UNICODE_GLYPH_RE = re.compile(_UNICODE_GLYPHS)
_TYPOGRAPHIC_GLYPH_RE = re.compile(_TYPOGRAPHIC_GLYPHS)
# What ``_normalize_with_spans`` changes: a CRLF or CR line ending, or a mapped glyph.
_UNICODE_SPECIAL_RE = re.compile("\r\n?|" + _UNICODE_GLYPHS)
_TYPOGRAPHIC_SPECIAL_RE = re.compile("\r\n?|" + _TYPOGRAPHIC_GLYPHS)
# A run of characters beyond ASCII with the ASCII character before it, which can
# carry the run's marks: the only text Unicode normalization changes.
_MARKED_RUN_RE = re.compile(r"[\x00-\x7f]?[^\x00-\x7f]+")
_Form = Literal["NFC", "NFD"]


def _normalize_newlines(text: str) -> str:
    """Normalize every recognized line ending to LF for tolerant matching."""
    normalized = text
    for ending in _LINE_ENDINGS:
        if ending != "\n":
            normalized = normalized.replace(ending, "\n")
    return normalized


def _normalize_replacement_newlines(text: str) -> str:
    """Normalize CRLF and CR to LF; other separators stay literal line content.

    Models normally author multiline replacement text with LF regardless of the
    target file's newline style, which the replacement then adopts.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _to_line_ending(text_lf: str, file_ending: str | None) -> str:
    """Convert LF-normalized text to the target line ending."""
    if file_ending is None or file_ending == "\n":
        return text_lf
    return text_lf.replace("\n", file_ending)


def _inside_crlf(content: str, offset: int) -> bool:
    """Whether ``offset`` falls between the CR and the LF of one line break."""
    return 0 < offset < len(content) and content[offset - 1 : offset + 1] == "\r\n"


def _on_line_boundaries(content: str, start: int, end: int) -> bool:
    """Whether ``content[start:end]`` starts at a line start and ends at a line end."""
    return (start == 0 or content[start - 1] in "\n\r") and (
        end == len(content) or content[end] in "\n\r"
    )


def _line_number_at(content: str, offset: int) -> int:
    """1-based line number at ``offset``, counting every read-rendered break."""
    return len(_LINE_BREAK_RE.findall(content[:offset])) + 1


def _line_starts(content: str) -> list[int]:
    """The offset of each read-rendered line, for many lookups with ``bisect_right``."""
    return [0, *(found.end() for found in _LINE_BREAK_RE.finditer(content))]


def _combines(character: str) -> bool:
    """Whether ``character`` joins the character before it under normalization."""
    code = ord(character)
    # Marks, and the vowel and final consonant jamo of a decomposed Hangul syllable.
    return (
        unicodedata.category(character).startswith("M")
        or 0x1160 <= code <= 0x11FF
        or 0xD7B0 <= code <= 0xD7FF
    )


def _clusters(text: str) -> list[str]:
    """Split ``text`` into characters, each with the marks that combine with it."""
    clusters: list[str] = []
    for character in text:
        if clusters and _combines(character):
            clusters[-1] += character
        else:
            clusters.append(character)
    return clusters


def _swaps_character(cluster: str) -> bool:
    """Whether normalizing ``cluster`` replaces a character with another one.

    A singleton such as a CJK compatibility ideograph or the Angstrom sign has
    a canonical decomposition of one other character: both forms (NFC and NFD)
    replace it, so it is a character of its own, not a spelling of one.
    """
    for character in cluster:
        decomposition = unicodedata.decomposition(character)
        if decomposition and not decomposition.startswith("<") and " " not in decomposition:
            return True
    return False


def _composed_cluster(cluster: str) -> str:
    return cluster if _swaps_character(cluster) else unicodedata.normalize("NFC", cluster)


def _composed_with_spans(text: str) -> tuple[str, list[tuple[int, int]]] | None:
    """Spell characters with marks composed (NFC) and record each one's origin span.

    Only the differences between the two normalization forms fold: a decomposed
    ``e`` with a combining acute accent reads as ``é``. ``None`` when nothing
    changes, which a text already in NFC answers without a scan.
    """
    if unicodedata.is_normalized("NFC", text):
        return None
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for run in _MARKED_RUN_RE.finditer(text):
        begin = run.start()
        chars.append(text[cursor:begin])
        spans.extend(zip(range(cursor, begin), range(cursor + 1, begin + 1), strict=True))
        position = begin
        for cluster in _clusters(run.group()):
            end = position + len(cluster)
            composed = _composed_cluster(cluster)
            chars.append(composed)
            if composed == cluster:
                spans.extend(zip(range(position, end), range(position + 1, end + 1), strict=True))
            else:
                spans.extend([(position, end)] * len(composed))
            position = end
        cursor = run.end()
    chars.append(text[cursor:])
    spans.extend(zip(range(cursor, len(text)), range(cursor + 1, len(text) + 1), strict=True))
    return "".join(chars), spans


def _composed(text: str) -> str:
    """Return the text ``_composed_with_spans`` produces, without its span map."""
    if unicodedata.is_normalized("NFC", text):
        return text
    return _MARKED_RUN_RE.sub(
        lambda run: "".join(_composed_cluster(cluster) for cluster in _clusters(run.group())), text
    )


def _written_form(matched: str, old: str) -> _Form | None:
    """Return the normalization form an edit writes its new text in, or ``None``.

    When the file text an edit matched spells its characters in one form (NFC
    or NFD) and the caller's old text does not, the caller's text came in the
    other form, and so did its new text: that then takes the file's form.
    """
    if old.isascii():
        return None
    forms: tuple[tuple[_Form, _Form], ...] = (("NFC", "NFD"), ("NFD", "NFC"))
    for form, other in forms:
        if (
            unicodedata.is_normalized(form, matched)
            and not unicodedata.is_normalized(other, matched)
            and not unicodedata.is_normalized(form, old)
        ):
            return form
    return None


def _in_form(text: str, form: _Form | None) -> str:
    """Spell ``text`` in normalization ``form``, keeping characters of their own."""
    if form is None or unicodedata.is_normalized(form, text):
        return text
    return "".join(
        cluster if _swaps_character(cluster) else unicodedata.normalize(form, cluster)
        for cluster in _clusters(text)
    )


def _normalize_with_spans(
    text: str, *, typographic: bool = False
) -> tuple[str, list[tuple[int, int]]]:
    """Normalize newlines + Unicode and record each normalized char's origin span.

    ``spans[k]`` is the ``(start, end)`` range in ``text`` that produced the k-th
    normalized character, so a match found in the normalized string maps back to
    the exact original characters. Expanded glyphs share one origin span for
    every normalized character, so the character and span lists stay aligned.
    Characters with marks fold to their composed form (``_composed_with_spans``).
    """
    composed = _composed_with_spans(text)
    source = text if composed is None else composed[0]
    # Only line endings and mapped glyphs change; the runs between them are
    # copied with one-character spans built in C, not per character in Python.
    mapping = _TYPOGRAPHIC_NORMALIZATION if typographic else _UNICODE_NORMALIZATION
    special = _TYPOGRAPHIC_SPECIAL_RE if typographic else _UNICODE_SPECIAL_RE
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for found in special.finditer(source):
        start, end = found.span()
        if start > cursor:
            chars.append(source[cursor:start])
            spans.extend(zip(range(cursor, start), range(cursor + 1, start + 1), strict=True))
        glyph = found.group()
        folded = "\n" if glyph[0] == "\r" else mapping[glyph]
        chars.append(folded)
        spans.extend([(start, end)] * len(folded))
        cursor = end
    if cursor < len(source):
        chars.append(source[cursor:])
        spans.extend(
            zip(range(cursor, len(source)), range(cursor + 1, len(source) + 1), strict=True)
        )
    if composed is not None:
        origins = composed[1]
        spans = [(origins[start][0], origins[end - 1][1]) for start, end in spans]
    return "".join(chars), spans


def _normalize_text(text: str) -> str:
    return _normalize_newlines(_composed(text)).translate(_UNICODE_TRANSLATION)


def _folded(text: str, *, typographic: bool) -> str:
    """Return the text ``_normalize_with_spans`` produces, without its span map."""
    mapping = _TYPOGRAPHIC_NORMALIZATION if typographic else _UNICODE_NORMALIZATION
    glyphs = _TYPOGRAPHIC_GLYPH_RE if typographic else _UNICODE_GLYPH_RE
    return glyphs.sub(lambda found: mapping[found.group()], _normalize_newlines(_composed(text)))


def _found_with_crlf(content: str, pattern: str, *, typographic: bool) -> list[tuple[int, int]]:
    """Return the normalized matches of ``pattern`` as a literal search finds them.

    In a file with CRLF line breaks, the old text with CRLF line breaks finds
    its occurrences without mapping every character of the file. Each one is
    also a normalized match; when the normalized text holds no more matches
    than that, they are all of them. An empty result leaves the decision to
    the full normalized search.
    """
    if "\r\n" not in content:
        return []
    found = _find_all(content, _normalize_newlines(pattern).replace("\n", "\r\n"))
    if not found:
        return []
    folded = _find_all(
        _folded(content, typographic=typographic), _folded(pattern, typographic=typographic)
    )
    return found if len(folded) == len(found) else []


_WORD_RE = re.compile(r"\w+")


def _find_all(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Return every occurrence of ``needle``, including overlapping ones."""
    if not needle:
        return []
    matches: list[tuple[int, int]] = []
    position = haystack.find(needle)
    while position >= 0:
        matches.append((position, position + len(needle)))
        position = haystack.find(needle, position + 1)
    return matches


def _leftmost_non_overlapping(matches: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Select sorted spans left to right so ``replace_all`` cannot self-corrupt."""
    selected: list[tuple[int, int]] = []
    for start, end in matches:
        if not selected or start >= selected[-1][1]:
            selected.append((start, end))
    return selected


def _apply_replacements(
    content: str,
    matches: list[tuple[int, int]],
    replacement_text: str,
    *,
    reindent: bool,
    old_string_lf: str,
    file_ending: str | None,
    lines_correspond: bool = True,
    preserve_glyphs: bool = False,
) -> tuple[str, tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]:
    # The spans do not overlap: the result is built in one pass, so many
    # replacements cost linear time instead of one copy of the content each.
    parts: list[str] = []
    before_spans: list[tuple[int, int]] = []
    after_spans: list[tuple[int, int]] = []
    offset_shift = 0
    cursor = 0
    # Finding the file's indentation unit reads every line; only a replacement
    # whose indentation changes asks for it.
    file_unit = cache(lambda: _indent_unit([_meaningful_indents(content)]))
    typography_work = _TypographyWork()
    for start, end in sorted(matches):
        if reindent:
            line_start = content.rfind("\n", 0, start) + 1
            replacement_lf = _reindent_replacement(
                content[start:end],
                old_string_lf,
                replacement_text,
                file_unit,
                lines_correspond,
                starts_inside=bool(content[line_start:start].strip())
                and content[start : start + 1] not in ("\r", "\n"),
            )
        else:
            replacement_lf = replacement_text
        if preserve_glyphs:
            replacement_lf = _preserve_replacement_typography(
                content[start:end], old_string_lf, replacement_lf, typography_work
            )
        replacement = _in_form(
            _to_line_ending(replacement_lf, file_ending),
            _written_form(content[start:end], old_string_lf),
        )
        after_start = start + offset_shift
        parts += (content[cursor:start], replacement)
        cursor = end
        before_spans.append((start, end))
        after_spans.append((after_start, after_start + len(replacement)))
        offset_shift += len(replacement) - (end - start)
    parts.append(content[cursor:])
    return "".join(parts), tuple(before_spans), tuple(after_spans)


# Comparison units for typography projection, independent of file size. Common
# unchanged prefixes and suffixes are stripped in linear time before spending it.
_TYPOGRAPHY_WORK = 1_000_000


class TypographyMatchError(Exception):
    """Copied typography has no safe correspondence within the available work."""


class _TypographyWork:
    def __init__(self) -> None:
        self.remaining = _TYPOGRAPHY_WORK

    def spend(self, amount: int) -> None:
        self.remaining -= amount
        if self.remaining < 0:
            raise TypographyMatchError


def _matching_blocks[T](
    old: Sequence[T],
    new: Sequence[T],
    work: _TypographyWork | None = None,
    relevant: set[int] | None = None,
) -> list[Match]:
    """Align changed interiors after removing their shared, often very long edges."""
    prefix = 0
    size = min(len(old), len(new))
    while prefix < size and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < size - prefix and old[-suffix - 1] == new[-suffix - 1]:
        suffix += 1
    old_end, new_end = len(old) - suffix, len(new) - suffix
    needed = relevant is None or any(prefix <= index < old_end for index in relevant)
    blocks = [Match(0, 0, prefix)] if prefix else []
    if needed and old_end > prefix and new_end > prefix:
        if work is not None:
            work.spend((old_end - prefix) * (new_end - prefix))
        blocks.extend(
            Match(block.a + prefix, block.b + prefix, block.size)
            for block in SequenceMatcher(
                None, old[prefix:old_end], new[prefix:new_end], autojunk=False
            ).get_matching_blocks()
            if block.size
        )
    if suffix:
        blocks.append(Match(old_end, new_end, suffix))
    blocks.append(Match(len(old), len(new), 0))
    return blocks


def _preserve_replacement_typography(
    actual: str, locator: str, replacement: str, work: _TypographyWork
) -> str:
    """Pair unchanged and rewritten lines without donating glyphs to inserted lines."""
    if not replacement or not (
        _TYPOGRAPHIC_GLYPH_RE.search(actual) or _TYPOGRAPHIC_GLYPH_RE.search(locator)
    ):
        return replacement
    actual_lines = _normalize_newlines(actual).split("\n")
    old_lines = locator.split("\n")
    new_lines = replacement.split("\n")
    if len(actual_lines) != len(old_lines):
        return replacement
    old_bodies = [line.lstrip(" \t") for line in old_lines]
    new_bodies = [line.lstrip(" \t") for line in new_lines]
    old_start = new_start = 0
    relevant = {
        index
        for index, (actual_line, old_line) in enumerate(zip(actual_lines, old_lines, strict=True))
        if actual_line.lstrip(" \t") != old_line.lstrip(" \t")
    }
    for block in _matching_blocks(old_bodies, new_bodies, work, relevant):
        # A differently sized rewritten block has no proven line correspondence.
        # Refuse where guessing would donate or drop copied glyphs; equal
        # neighbors still retain theirs when insertions shift the line number.
        if (
            block.b > new_start
            and block.a - old_start != block.b - new_start
            and any(old_start <= index < block.a for index in relevant)
        ):
            raise TypographyMatchError
        if block.a - old_start == block.b - new_start:
            for offset in range(block.a - old_start):
                old_index, new_index = old_start + offset, new_start + offset
                new_lines[new_index] = preserve_typography(
                    actual_lines[old_index], old_lines[old_index], new_lines[new_index], work
                )
        for offset in range(block.size):
            old_index, new_index = block.a + offset, block.b + offset
            new_lines[new_index] = preserve_typography(
                actual_lines[old_index], old_lines[old_index], new_lines[new_index], work
            )
        old_start, new_start = block.a + block.size, block.b + block.size
    return "\n".join(new_lines)


def preserve_typography(
    actual: str, locator: str, replacement: str, work: _TypographyWork | None = None
) -> str:
    """Retain original glyphs where a precisely equivalent copied line stays unchanged.

    A glyph explicitly changed between locator and replacement stays as sent,
    even if another difference made the match use normalization.
    """
    actual_body, old_body, new_body = (
        text.lstrip(" \t") for text in (actual, locator, replacement)
    )
    if actual_body == old_body:
        return replacement
    actual_folded, actual_spans = _normalize_with_spans(actual_body, typographic=True)
    old_folded, old_spans = _normalize_with_spans(old_body, typographic=True)
    new_folded, new_spans = _normalize_with_spans(new_body, typographic=True)
    if actual_folded != old_folded:
        return replacement
    glyphs = []
    index = 0
    while index < len(actual_spans):
        limit = index + 1
        while limit < len(actual_spans) and (
            actual_spans[limit - 1] == actual_spans[limit]
            or old_spans[limit - 1] == old_spans[limit]
        ):
            limit += 1
        start, end = actual_spans[index][0], actual_spans[limit - 1][1]
        original = actual_body[start:end]
        old_start, old_end = old_spans[index][0], old_spans[limit - 1][1]
        copied = old_body[old_start:old_end]
        if original != copied:
            glyphs.append((index, limit, original, copied))
        index = limit
    if not glyphs:
        return replacement
    blocks = _matching_blocks(old_folded, new_folded, work, {glyph[0] for glyph in glyphs})
    replacements = []
    for index, limit, original, copied in glyphs:
        for block in blocks:
            if block.a <= index and limit <= block.a + block.size:
                new_start = block.b + index - block.a
                new_end = new_start + limit - index
                left, right = new_spans[new_start][0], new_spans[new_end - 1][1]
                if new_body[left:right] == copied:
                    replacements.append((left, right, original))
                break
    parts: list[str] = []
    cursor = 0
    for start, end, original in replacements:
        parts.extend((new_body[cursor:start], original))
        cursor = end
    parts.append(new_body[cursor:])
    new_body = "".join(parts)
    return replacement[: len(replacement) - len(replacement.lstrip(" \t"))] + new_body


def _leading_whitespace(line: str) -> str:
    index = 0
    while index < len(line) and line[index] in (" ", "\t"):
        index += 1
    return line[:index]


def _split_lines_preserving_endings(text: str) -> list[tuple[str, str]]:
    """Split read-visible lines without discarding their exact separators."""
    lines: list[tuple[str, str]] = []
    start = 0
    for match in _LINE_BREAK_RE.finditer(text):
        lines.append((text[start : match.start()], match.group(0)))
        start = match.end()
    lines.append((text[start:], ""))
    return lines


def _reindent_replacement(
    file_region: str,
    old_string_lf: str,
    replacement_text: str,
    file_unit: Callable[[], str | None],
    lines_correspond: bool = True,
    *,
    starts_inside: bool = False,
) -> str:
    """Rewrite the replacement's indentation in the file's own indentation style.

    A whitespace-tolerant match succeeds when the model's indentation differs
    from the file's: 2 spaces against 4, spaces against tabs, or a dropped outer
    level. Writing the replacement verbatim would corrupt indentation, so each
    replacement line gets the file indent that the matched lines show for the
    same model indent. An indent the matched lines do not show, such as a new
    deeper line, is converted level by level between the model's and the file's
    indentation units; when no consistent conversion exists, the model's base
    indent is anchored onto the file's, keeping the relative nesting the model
    intended. ``file_unit`` gives the file's indentation unit when a conversion
    needs it. Without ``lines_correspond``, as when the line breaks fall in other
    places, only the first lines pair up; when the match ``starts_inside`` a
    file line, the second lines do, and the replacement's first line continues
    that file line without indentation of its own (Swarm run 2026-10-20: a copy
    from inside a list item's first line wrote its continuation lines without
    the item's indentation).

    When one model indent pairs with several file indents, a pair whose lines
    read alike (whitespace aside) decides: a copied line that differs, such as
    ``{"quote": ...`` for the file's ``{`` and ``"quote": ...`` lines joined,
    carries the indent of other text than its file line.
    """
    if not replacement_text:
        return replacement_text

    old_lines = [line for line in _LINE_BREAK_RE.split(old_string_lf) if line.strip()]
    file_lines = [line for line in _LINE_BREAK_RE.split(file_region) if line.strip()]
    if not old_lines or not file_lines:
        return replacement_text
    old_indents = [_leading_whitespace(line) for line in old_lines]
    file_indents = [_leading_whitespace(line) for line in file_lines]
    continued = (
        starts_inside and not lines_correspond and len(old_lines) > 1 and len(file_lines) > 1
    )
    if continued:
        old_lines, file_lines = old_lines[1:], file_lines[1:]
        old_indents, file_indents = old_indents[1:], file_indents[1:]
        replacement_text = replacement_text.lstrip(" \t")
    pairs = (
        list(zip(old_indents, file_indents, strict=True))
        if lines_correspond and len(old_indents) == len(file_indents)
        else [(old_indents[0], file_indents[0])]
    )
    # Most indented lines as the file has them: the model wrote in the file's
    # indentation, and the other lines are typos, not a different style.
    indented = [(model, file) for model, file in pairs if model or file]
    if 2 * sum(model == file for model, file in indented) > len(indented):
        return replacement_text
    alike = [old.split() == held.split() for old, held in zip(old_lines, file_lines, strict=False)]
    seen: dict[str, str] = {}
    # Pairs of lines that read alike first; the order is stable otherwise.
    for index in sorted(range(len(pairs)), key=lambda index: not alike[index]):
        seen.setdefault(*pairs[index])
    # Pairs that the decided mappings contradict say nothing about the levels.
    pairs = [(model, file) for model, file in pairs if seen[model] == file]
    if all(model_indent == file_indent for model_indent, file_indent in seen.items()):
        return replacement_text

    replacement_lines = _split_lines_preserving_endings(replacement_text)
    replacement_indents = [
        _leading_whitespace(line) for line, _ending in replacement_lines if line.strip()
    ]
    convert = _level_converter(pairs, (old_indents, replacement_indents), file_unit())
    base_model, base_file = pairs[0]

    out_parts: list[str] = []
    first = True
    for line, ending in replacement_lines:
        if not line.strip():
            out_parts.append(line + ending)
            continue
        indent = _leading_whitespace(line)
        body = line[len(indent) :]
        if continued and first:
            first = False
            out_parts.append(body + ending)
            continue
        mapped = seen.get(indent)
        if mapped is None and convert is not None:
            mapped = convert(indent)
        if mapped is None:
            rest = indent[len(base_model) :] if indent.startswith(base_model) else ""
            mapped = base_file + rest
        out_parts.append(mapped + body + ending)
    return "".join(out_parts)


def _meaningful_indents(text: str) -> list[str]:
    return [_leading_whitespace(line) for line in _LINE_BREAK_RE.split(text) if line.strip()]


def _indent_unit(sequences: Iterable[list[str]]) -> str | None:
    """Return the indentation unit these indent sequences show: a tab or N spaces.

    Tabs win when most indented lines start with one. Otherwise the unit is the
    most common change between consecutive space indents, the smallest on ties.
    ``None`` when the text shows no indentation step.
    """
    tab_lines = space_lines = 0
    steps: Counter[int] = Counter()
    for indents in sequences:
        previous: int | None = None
        for indent in indents:
            if indent.startswith("\t"):
                tab_lines += 1
            elif indent:
                space_lines += 1
            if _only(indent, " "):
                if previous is not None and len(indent) != previous:
                    steps[abs(len(indent) - previous)] += 1
                previous = len(indent)
            else:
                previous = None
    if tab_lines > space_lines:
        return "\t"
    if not steps:
        return None
    return " " * max(sorted(steps), key=steps.__getitem__)


# Spaces per indentation level to try, most common first.
_LEVEL_WIDTHS = (4, 2, 8, 3)


def _level_converter(
    pairs: list[tuple[str, str]],
    model_sequences: tuple[list[str], ...],
    file_unit: str | None,
) -> Callable[[str], str | None] | None:
    """Convert model indents level by level into the file's indentation unit.

    An indent is whole levels of its unit plus trailing alignment spaces. The
    units are the ones each side shows; a side that shows no indentation step
    gets the candidate unit that explains the matched pairs, preferring the
    same level on both sides. The matched pairs that convert with unchanged
    alignment must agree on one constant level offset (a dropped outer level);
    pairs whose alignment cannot be told apart from levels are left out.
    ``None`` when no pair of units explains the matched lines.
    """
    model_unit = _indent_unit(model_sequences)
    model_units = _unit_candidates(model_unit, [model for model, _file in pairs])
    file_units = _unit_candidates(file_unit, [file for _model, file in pairs])
    best: tuple[tuple[int, bool, bool, int, int], str, str, int] | None = None
    for model_rank, model in enumerate(model_units):
        for file_rank, file in enumerate(file_units):
            fit = _level_offset(pairs, model, file)
            if fit is None:
                continue
            offset, explained = fit
            score = (
                -explained,
                model != model_unit,
                file != file_unit,
                abs(offset),
                model_rank + file_rank,
            )
            if best is None or score < best[0]:
                best = (score, model, file, offset)
    if best is None:
        return None
    _score, model, file, offset = best

    def convert(indent: str) -> str | None:
        split = _split_indent(indent, model)
        if split is None or split[0] + offset < 0:
            return None
        levels, alignment = split
        return file * (levels + offset) + " " * alignment

    return convert


def _unit_candidates(shown: str | None, indents: list[str]) -> list[str]:
    candidates = [shown] if shown else []
    if any("\t" in indent for indent in indents):
        candidates.append("\t")
    widths = [len(indent) for indent in indents if indent and _only(indent, " ")]
    if widths:
        candidates.append(" " * math.gcd(*widths))
    candidates.extend(" " * width for width in _LEVEL_WIDTHS)
    # One space is alignment, not a level: as a unit it multiplies every indent.
    return [unit for unit in dict.fromkeys(candidates) if unit != " "]


def _level_offset(
    pairs: list[tuple[str, str]], model_unit: str, file_unit: str
) -> tuple[int, int] | None:
    """Return the level offset the pairs agree on and how many pairs it explains."""
    offsets: set[int] = set()
    explained = 0
    for model_indent, file_indent in pairs:
        model_split = _split_indent(model_indent, model_unit)
        file_split = _split_indent(file_indent, file_unit)
        if model_split is None or file_split is None:
            return None
        if model_split[1] != file_split[1]:
            continue
        offsets.add(file_split[0] - model_split[0])
        explained += 1
    return (offsets.pop(), explained) if len(offsets) == 1 else None


def _split_indent(indent: str, unit: str) -> tuple[int, int] | None:
    """Split ``indent`` into whole ``unit`` levels and trailing alignment spaces."""
    if unit == "\t":
        levels = len(indent) - len(indent.lstrip("\t"))
        alignment = indent[levels:]
        return (levels, len(alignment)) if _only(alignment, " ") else None
    if not _only(indent, " "):
        return None
    return divmod(len(indent), len(unit))


def _only(indent: str, character: str) -> bool:
    """Whether ``indent`` is empty or consists of ``character`` alone."""
    return not indent.strip(character)


# Glyphs a reader takes for their plain form. Unlike the edit folds above, every
# dash, the minus sign included, reads as one hyphen: on a page they are typography
# for what a keyboard types as "-", and conflating them can only show one more
# passage. Primes and guillemets read as the quotes typed for them.
_SEARCH_GLYPHS = {
    **dict.fromkeys("‘’‚‛′‹›", "'"),
    **dict.fromkeys("“”„‟″«»", '"'),
    **dict.fromkeys("‐‑‒–—―−﹘﹣－", "-"),
    "…": "...",
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
}
# Characters that take no room: zero-width spaces and joiners, the byte order mark
# and the soft hyphen.
_SEARCH_INVISIBLE = "­​‌‍⁠﻿"
_SEARCH_SPECIAL_RE = re.compile(
    r"(?P<space>\s+)|(?P<glyph>["
    + "".join(_SEARCH_GLYPHS)
    + "])|(?P<invisible>["
    + _SEARCH_INVISIBLE
    + "]+)"
)


class SearchText:
    """Text as a reader sees it (``fold_for_search``), mapped back to the original.

    ``text`` is the folded text. Each of its pieces either copies an original run
    character by character or stands for a whole original range: a whitespace
    run, a glyph, or a replacement the caller named.
    """

    def __init__(self, original: str, replacements: Iterable[tuple[int, int, str]]) -> None:
        self._parts: list[str] = []
        self._starts: list[int] = []
        self._origins: list[tuple[int, int, bool]] = []
        self._length = 0
        self._original_length = len(original)
        cursor = 0
        for start, end, replacement in replacements:
            self._fold(original, cursor, start)
            if replacement.isspace():
                self._space(start, end)
            elif replacement:
                self._add(replacement, start, end, copied=False)
            cursor = end
        self._fold(original, cursor, len(original))
        self.text = "".join(self._parts)

    def _fold(self, original: str, start: int, end: int) -> None:
        cursor = start
        for found in _SEARCH_SPECIAL_RE.finditer(original, start, end):
            if found.start() > cursor:
                self._add(original[cursor : found.start()], cursor, found.start(), copied=True)
            if found["space"]:
                self._space(found.start(), found.end())
            elif found["glyph"]:
                self._add(_SEARCH_GLYPHS[found["glyph"]], found.start(), found.end(), copied=False)
            cursor = found.end()
        if end > cursor:
            self._add(original[cursor:end], cursor, end, copied=True)

    def _add(self, text: str, start: int, end: int, *, copied: bool) -> None:
        self._parts.append(text)
        self._starts.append(self._length)
        self._origins.append((start, end, copied))
        self._length += len(text)

    def _space(self, start: int, end: int) -> None:
        """Read whitespace as one space, also across dropped ranges between runs."""
        if self._parts and self._parts[-1] == " ":
            first, _, copied = self._origins[-1]
            self._origins[-1] = (first, end, copied)
        else:
            self._add(" ", start, end, copied=False)

    def _piece(self, index: int) -> tuple[int, int, int, bool]:
        piece = bisect_right(self._starts, index) - 1
        return (self._starts[piece], *self._origins[piece])

    def original(self, start: int, end: int) -> tuple[int, int]:
        """Return the original range that ``text[start:end]`` (not empty) was read from."""
        offset, first, last, copied = self._piece(start)
        begin = first + (start - offset) if copied else first
        offset, first, last, copied = self._piece(end - 1)
        return begin, first + (end - offset) if copied else last

    def resume(self, end: int) -> int:
        """Return the original offset where ``text[end:]`` starts, after dropped syntax."""
        if end >= self._length:
            return self._original_length
        offset, first, _, copied = self._piece(end)
        return first + (end - offset) if copied else first


def fold_for_search(text: str, replacements: Iterable[tuple[int, int, str]] = ()) -> SearchText:
    """Return ``text`` as a reader sees it, for literal case-insensitive search.

    ``replacements`` are sorted, non-overlapping ``(start, end, text)`` ranges of
    presentation the caller reads differently: ``""`` drops a range, ``" "`` reads
    it as a space. Every other character must match as written.
    """
    return SearchText(text, replacements)
