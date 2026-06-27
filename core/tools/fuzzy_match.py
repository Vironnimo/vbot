"""Fuzzy find-and-replace for the edit tool.

The exact text an edit targets is frequently *almost* right: the model sends
straight quotes where the file has curly ones, a bare ``\\n`` where the file uses
``\\r\\n``, or a different indentation than the file actually has. A literal match
then fails and the edit is rejected even though the intended target is
unambiguous. This module tries a short chain of increasingly tolerant — but never
*guessing* — strategies and always replaces the real original bytes at the
matched span.

Strategies, in order; the first that finds any match wins (its own ambiguity is
terminal — it does not fall through to a looser strategy):

1. ``exact`` — literal substring match.
2. ``normalized`` — match after collapsing CR/CRLF to LF and mapping a few
   visually-equivalent Unicode characters (curly quotes, non-breaking space,
   en-dash) to ASCII, on both sides. Character-level, so it also matches a
   fragment within a line.
3. ``line_trimmed`` — match whole lines after stripping each line's leading and
   trailing whitespace (plus the same Unicode mapping). The replacement is
   re-indented to the file's actual indentation, so a whitespace-only match never
   corrupts indentation.

Both non-exact strategies search a normalized copy of the content and map the
match back to the original bytes through a per-character span map, so CRLF line
endings and the exact original characters are always preserved. Deliberately
excluded: similarity / anchor matching (replacing text that is merely *similar*).
For a destructive operation, failing so the model retries with a better target is
safer than silently editing the wrong block.
"""

from __future__ import annotations

from dataclasses import dataclass

# Visually-equivalent characters models emit in place of their ASCII forms, keyed
# by code point so the source stays pure ASCII and the entries are unambiguous.
# Only 1:1 mappings live here so every normalized character maps back to exactly
# one original character; length-changing expansions (em-dash -> "--", ellipsis
# -> "...") are deliberately omitted to keep span mapping unambiguous.
_UNICODE_NORMALIZATION = {
    "“": '"',  # left double quotation mark
    "”": '"',  # right double quotation mark
    "‘": "'",  # left single quotation mark
    "’": "'",  # right single quotation mark
    " ": " ",  # non-breaking space
    "–": "-",  # en dash
}


@dataclass(frozen=True)
class FuzzyReplacement:
    """A successful fuzzy replacement applied to the original content."""

    new_content: str
    first_changed_line: int
    replacements: int
    strategy: str


@dataclass(frozen=True)
class AmbiguousFuzzyMatch:
    """The winning strategy matched more than once without ``replace_all``."""

    occurrences: int
    line_numbers: list[int]


def replace_fuzzy(
    content: str,
    old_string: str,
    new_string: str,
    *,
    replace_all: bool,
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    """Find ``old_string`` in ``content`` via the strategy chain and replace it.

    Returns a :class:`FuzzyReplacement` on success, an :class:`AmbiguousFuzzyMatch`
    when the winning strategy matched more than once without ``replace_all``, or
    ``None`` when no strategy matched.
    """
    new_lf = _normalize_newlines(new_string)
    old_lf = _normalize_newlines(old_string)
    file_ending = _detect_line_ending(content)

    for name, matcher, reindent in _STRATEGIES:
        matches = matcher(content, old_string)
        if not matches:
            continue
        if len(matches) > 1 and not replace_all:
            line_numbers = [_line_number_at(content, start) for start, _ in matches]
            return AmbiguousFuzzyMatch(len(matches), line_numbers)

        selected = matches if replace_all else matches[:1]
        new_content = _apply_replacements(
            content,
            selected,
            new_lf,
            reindent=reindent,
            old_string_lf=old_lf,
            file_ending=file_ending,
        )
        first_line = _line_number_at(content, min(start for start, _ in selected))
        return FuzzyReplacement(new_content, first_line, len(selected), name)

    return None


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _detect_line_ending(content: str) -> str | None:
    if "\r\n" in content:
        return "\r\n"
    if "\n" in content or "\r" in content:
        return "\n"
    return None


def _to_line_ending(text_lf: str, file_ending: str | None) -> str:
    if file_ending == "\r\n":
        return text_lf.replace("\n", "\r\n")
    return text_lf


def _line_number_at(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _normalize_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalize newlines + Unicode and record each normalized char's origin span.

    ``spans[k]`` is the ``(start, end)`` range in ``text`` that produced the k-th
    normalized character, so a match found in the normalized string maps back to
    the exact original bytes. Every mapping is K-original-chars -> 1-normalized
    (CRLF -> LF is 2->1; everything else is 1->1), so the lists stay aligned.
    """
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(text)

    while index < length:
        char = text[index]
        if char == "\r" and index + 1 < length and text[index + 1] == "\n":
            chars.append("\n")
            spans.append((index, index + 2))
            index += 2
            continue
        if char == "\r":
            chars.append("\n")
            spans.append((index, index + 1))
            index += 1
            continue
        chars.append(_UNICODE_NORMALIZATION.get(char, char))
        spans.append((index, index + 1))
        index += 1

    return "".join(chars), spans


def _normalize_text(text: str) -> str:
    return _normalize_with_spans(text)[0]


def _match_exact(content: str, pattern: str) -> list[tuple[int, int]]:
    return _find_non_overlapping(content, pattern)


def _match_normalized(content: str, pattern: str) -> list[tuple[int, int]]:
    normalized_pattern = _normalize_text(pattern)
    if not normalized_pattern:
        return []
    normalized_content, spans = _normalize_with_spans(content)

    matches: list[tuple[int, int]] = []
    pattern_length = len(normalized_pattern)
    start = 0
    while True:
        position = normalized_content.find(normalized_pattern, start)
        if position < 0:
            break
        matches.append((spans[position][0], spans[position + pattern_length - 1][1]))
        start = position + pattern_length
    return matches


def _match_line_trimmed(content: str, pattern: str) -> list[tuple[int, int]]:
    # Work on the normalized content (LF, Unicode-folded) with its span map, so a
    # match maps back to the exact original bytes and CRLF endings are preserved.
    normalized_content, spans = _normalize_with_spans(content)
    content_lines = normalized_content.split("\n")
    pattern_lines = _normalize_text(pattern).split("\n")

    trimmed_content = [line.strip() for line in content_lines]
    trimmed_pattern = [line.strip() for line in pattern_lines]
    window = len(trimmed_pattern)
    if window == 0:
        return []

    # Character offset (in the normalized string) where each line begins.
    line_offsets: list[int] = []
    cursor = 0
    for line in content_lines:
        line_offsets.append(cursor)
        cursor += len(line) + 1  # +1 for the splitting "\n"

    matches: list[tuple[int, int]] = []
    index = 0
    last_start = len(trimmed_content) - window
    while index <= last_start:
        if trimmed_content[index : index + window] == trimmed_pattern:
            norm_start = line_offsets[index]
            last_line = index + window - 1
            norm_end = line_offsets[last_line] + len(content_lines[last_line])
            if norm_start < len(spans) and norm_end > norm_start:
                matches.append((spans[norm_start][0], spans[norm_end - 1][1]))
                index += window  # non-overlapping, so replace_all cannot self-corrupt
                continue
        index += 1
    return matches


def _find_non_overlapping(haystack: str, needle: str) -> list[tuple[int, int]]:
    if not needle:
        return []
    matches: list[tuple[int, int]] = []
    start = 0
    while True:
        position = haystack.find(needle, start)
        if position < 0:
            break
        matches.append((position, position + len(needle)))
        start = position + len(needle)
    return matches


def _apply_replacements(
    content: str,
    matches: list[tuple[int, int]],
    new_string_lf: str,
    *,
    reindent: bool,
    old_string_lf: str,
    file_ending: str | None,
) -> str:
    result = content
    # Splice from the end so earlier spans keep their offsets.
    for start, end in sorted(matches, reverse=True):
        if reindent:
            replacement_lf = _reindent_replacement(content[start:end], old_string_lf, new_string_lf)
        else:
            replacement_lf = new_string_lf
        result = result[:start] + _to_line_ending(replacement_lf, file_ending) + result[end:]
    return result


def _leading_whitespace(line: str) -> str:
    index = 0
    while index < len(line) and line[index] in (" ", "\t"):
        index += 1
    return line[:index]


def _first_meaningful_line(text: str) -> str | None:
    for line in text.split("\n"):
        if line.strip():
            return line
    return None


def _reindent_replacement(file_region: str, old_string_lf: str, new_string_lf: str) -> str:
    """Shift ``new_string`` so its base indent matches the file's actual indent.

    A line-trimmed match can succeed when the model's indentation differs from the
    file's (e.g. 2-space args vs a 4-space file). Writing the replacement verbatim
    would then corrupt indentation, so anchor the model's base indent onto the
    file's while preserving the relative nesting the model intended.
    """
    if not new_string_lf:
        return new_string_lf

    old_first = _first_meaningful_line(old_string_lf)
    file_first = _first_meaningful_line(file_region)
    if old_first is None or file_first is None:
        return new_string_lf

    old_indent = _leading_whitespace(old_first)
    file_indent = _leading_whitespace(file_first)
    if old_indent == file_indent:
        return new_string_lf

    out_lines: list[str] = []
    for line in new_string_lf.split("\n"):
        if not line.strip():
            out_lines.append(line)
            continue
        if _leading_whitespace(line).startswith(old_indent):
            out_lines.append(file_indent + line[len(old_indent) :])
        else:
            out_lines.append(file_indent + line.lstrip(" \t"))
    return "\n".join(out_lines)


# (name, matcher, reindent-replacement) in increasing tolerance. Only the
# line-level strategy needs re-indentation; the character-level ones match the
# file's real whitespace already.
_STRATEGIES = (
    ("exact", _match_exact, False),
    ("normalized", _match_normalized, False),
    ("line_trimmed", _match_line_trimmed, True),
)


__all__ = ["AmbiguousFuzzyMatch", "FuzzyReplacement", "replace_fuzzy"]
