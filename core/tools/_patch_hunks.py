"""Match one parsed hunk against current text and apply it.

Line hunks (V4A, unified diffs, SEARCH/REPLACE blocks) delegate to
``fuzzy_match.replace_fuzzy`` with patch-only options (whole lines, EOF
anchoring) and add patch recoveries: read-output gutters, escaped text, surplus
blank context, and already-applied post-states; old text copied with other
errors goes to ``copy_match``. Context lines keep their actual bytes; only
changed lines come from the patch. Text replacements (``old_string``/
``new_string``) match within lines precisely, else through ``copy_match``, and
line insertions go after a line number.
"""

from __future__ import annotations

import re
from dataclasses import replace
from os.path import commonprefix
from typing import Literal

from core.tools._patch_syntax import _Hunk, _PatchError
from core.tools.arguments import (
    TEXT_LINE_BREAK,
    line_number_gutter_candidates,
    split_text_lines,
    strip_line_number_gutters,
)
from core.tools.copy_match import copy_warnings, match_copied_edit, replace_copied
from core.tools.fuzzy_match import (
    AmbiguousFuzzyMatch,
    FuzzyReplacement,
    find_closest_candidates,
    preserve_typography,
    replace_fuzzy,
)
from core.tools.tools import JsonObject

_GUTTER_WARNING = "Removed read-output line-number prefixes before applying the hunk."
_ESCAPE_WARNING = "Normalized escaped patch text after the literal text did not match."
_EOF_WARNING = (
    "The lines before *** End of File are not at the end of the file; the hunk was applied "
    "where they are."
)
_BREAK = TEXT_LINE_BREAK
_GUTTER = re.compile(r"^\s*[1-9][0-9]*(?::[1-9][0-9]*)?\|")
_ESCAPE = re.compile(r"\\(n|r|t|\\|\"|')")


def _line_parts(content: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    start = 0
    for match in _BREAK.finditer(content):
        parts.append((content[start : match.start()], match.group()))
        start = match.end()
    if start < len(content):
        parts.append((content[start:], ""))
    return parts


def _ending(content: str) -> str:
    for ending in ("\r\n", "\n", "\r"):
        if ending in content:
            return ending
    return "\n"


def _hunk_text(hunk: _Hunk, prefixes: str) -> str:
    return "\n".join(text for prefix, text in hunk.lines if prefix in prefixes)


def _candidates(content: str, pattern: str) -> JsonObject:
    file_lines = split_text_lines(content)
    candidates = []
    for candidate in find_closest_candidates(content, pattern):
        details: JsonObject = {
            "line": candidate.line_number,
            "text": candidate.text,
            "truncated": candidate.truncated,
        }
        if candidate.truncated:
            shown = candidate.text.split("\n")
            last = candidate.line_number + len(shown) - 1
            character = len(shown[-1]) + 1
            # A cut between whole lines resumes at the next line, not beyond
            # the end of the one just shown. Coordinates count characters,
            # like read, regardless of UTF-8 bytes or the file's line endings.
            if len(shown[-1]) == len(file_lines[last - 1]):
                last, character = last + 1, 1
            remaining = max(1, len(split_text_lines(pattern)) - (last - candidate.line_number))
            details["continuations"] = [
                {"offset": f"{last}:{character}", "limit": min(8, remaining)}
            ]
        candidates.append(details)
    return {"candidates": candidates}


def _loose(text: str) -> str:
    return " ".join(text.split())


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


def _difference_window(text: str, position: int) -> tuple[int, str]:
    """Keep the actual mismatch in a bounded window, including a missing suffix."""
    if len(text) <= 240:
        return 1, text
    start = max(0, position - 120)
    return start + 1, text[start : start + 240]


def _not_found(content: str, old: str, *, source: Literal["patch", "old_string"]) -> JsonObject:
    """Return the closest current text and the first line where it differs.

    ``source`` names the argument that holds ``old``, so the report can name it.
    """
    details = _candidates(content, old)
    if details["candidates"]:
        start = details["candidates"][0]["line"]
        file_lines = split_text_lines(content)
        wanted_lines = split_text_lines(old)
        first = next((index for index, line in enumerate(wanted_lines) if line.strip()), 0)
        for position, wanted in enumerate(wanted_lines[first:], first):
            number = start + position - first
            if number > len(file_lines):
                break
            actual = file_lines[number - 1]
            if _loose(actual) != _loose(wanted):
                file_position, copy_position = _difference_positions(actual, wanted)
                file_start, file_text = _difference_window(actual, file_position)
                copy_start, copy_text = _difference_window(wanted, copy_position)
                details["difference"] = {
                    "line": number,
                    "character": file_position + 1,
                    "copy_line": position + 1,
                    "copy_character": copy_position + 1,
                    "file_start": file_start,
                    "copy_start": copy_start,
                    "file": file_text,
                    "copy": copy_text,
                    "truncated": len(file_text) < len(actual) or len(copy_text) < len(wanted),
                    "source": source,
                }
                break
    return details


def _line_list(numbers: list[int]) -> str:
    shown = [str(number) for number in numbers[:6]]
    if len(numbers) > len(shown):
        shown.append("...")
    return ("line " if len(numbers) == 1 else "lines ") + ", ".join(shown)


def _ambiguity(
    content: str, match: AmbiguousFuzzyMatch, offset: int = 0
) -> tuple[JsonObject, JsonObject]:
    """Return excerpts around each occurrence, and the values an error message names."""
    shift = len(_BREAK.findall(content[:offset]))
    numbers = [number + shift for number in dict.fromkeys(match.line_numbers)]
    lines = split_text_lines(content)
    candidates = []
    for number in numbers[:3]:
        start, end = max(0, number - 2), min(len(lines), number + 1)
        candidates.append(
            {
                "line": start + 1,
                "text": "\n".join(line[:240] for line in lines[start:end]),
                "truncated": any(len(line) > 240 for line in lines[start:end]),
                "continuations": [
                    {"offset": f"{index}:241", "limit": 1}
                    for index, line in enumerate(lines[start:end], start + 1)
                    if len(line) > 240
                ],
            }
        )
    details: JsonObject = {"occurrences": match.occurrences, "candidates": candidates}
    return details, {"occurrences": match.occurrences, "lines": _line_list(numbers)}


def _match(
    content: str,
    old: str,
    new: str,
    *,
    eof: bool = False,
) -> FuzzyReplacement | AmbiguousFuzzyMatch | None:
    if old == "":
        positions = []
        offset = 0
        for line, (text, ending) in enumerate(_line_parts(content), 1):
            if text == "" and (not eof or offset + len(ending) == len(content)):
                positions.append((offset, line))
            offset += len(text) + len(ending)
        if not positions:
            return None
        if len(positions) > 1:
            return AmbiguousFuzzyMatch(
                len(positions), [line for _, line in positions], [1] * len(positions)
            )
        start, line = positions[0]
        return FuzzyReplacement(
            content[:start] + new + content[start:],
            line,
            line,
            1,
            "exact",
            ((start, start),),
            ((start, start + len(new)),),
        )
    return replace_fuzzy(
        content,
        old,
        new,
        replace_all=False,
        whole_lines=True,
        at_eof=eof,
        typographic=True,
    )


def _normalize_gutters(hunk: _Hunk) -> _Hunk | None:
    old_lines = [text for prefix, text in hunk.lines if prefix in " -"]
    old = "\n".join(old_lines)
    if strip_line_number_gutters(old) is None:
        return None
    # Locators may mix raw lines with copied gutters. Only a unique whole-line
    # match against the current file authorizes this recovery.
    lines = []
    for prefix, text in hunk.lines:
        stripped = strip_line_number_gutters(text)
        if stripped is not None and re.match(r"^\s*\d+:\d+\|", text):
            return None
        lines.append((prefix, text if stripped is None else stripped))
    return replace(hunk, lines=lines)


def _unescape(text: str, *, replacement_for: str | None = None) -> str:
    values = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}

    def decode(match: re.Match[str]) -> str:
        kind = match[1]
        if replacement_for is not None:
            if kind == "n" or match[0] not in replacement_for:
                return match[0]
            if kind in "tr" and not re.fullmatch(r"(?:[ \t]|\\[tr])*", text[: match.start()]):
                return match[0]
        return values[kind]

    return _ESCAPE.sub(decode, text)


def _clean_additions(hunk: _Hunk, path: object) -> tuple[_Hunk, list[str]]:
    # Existing literal gutter-shaped context is authoritative. New standalone
    # additions require complete-block gutter recovery.
    if any(_GUTTER.match(text) for prefix, text in hunk.lines if prefix in " -"):
        return hunk, []
    lines = list(hunk.lines)
    warnings = []
    start = 0
    while start < len(lines):
        if lines[start][0] != "+":
            start += 1
            continue
        end = start + 1
        while end < len(lines) and lines[end][0] == "+":
            end += 1
        texts = [text for _, text in lines[start:end]]
        if sum(bool(_GUTTER.match(text)) for text in texts) >= 2:
            candidates = line_number_gutter_candidates("\n".join(texts), allow_continuations=False)
            if not candidates:
                raise _PatchError("line_numbered_content", path=path, label=hunk.label)
            lines[start:end] = [("+", text) for text in candidates[0].split("\n")]
            warnings.append(_GUTTER_WARNING)
        start = end
    return replace(hunk, lines=lines), warnings


def _hint_text(hint: str) -> str:
    """Show a context hint in one line; a multi-line context block by its first line."""
    first, _, rest = hint.partition("\n")
    return first[:120] + (" ..." if rest or len(first) > 120 else "")


def _inline_context_identifies(content: str, old: str, new: str) -> bool:
    """Identify a single-line replacement by two retained, unique text anchors."""
    if "\n" in old or "\n" in new:
        return False
    prefix = commonprefix((old, new))
    suffix = commonprefix((old[len(prefix) :][::-1], new[len(prefix) :][::-1]))[::-1]
    # Syntax/indentation alone cannot identify a target. Both sides must retain
    # substantive text, and together they must select exactly one current line.
    if any(
        len(anchor.strip()) < 4 or not re.search(r"\w{3}", anchor) for anchor in (prefix, suffix)
    ):
        return False
    candidates = [
        line
        for line in split_text_lines(content)
        if line.startswith(prefix) and line.endswith(suffix)
    ]
    return candidates == [new]


def _apply_replacement(content: str, hunk: _Hunk, path: object) -> tuple[str, list[str]]:
    """Replace ``old_string`` text, located precisely, else as a copy with errors."""
    replacement = hunk.replacement
    assert replacement is not None
    replace_all = replacement.replace_all or (replacement.expected or 1) > 1
    attempts: list[tuple[str, str, bool, str | None]] = [
        (replacement.old, replacement.new, False, None)
    ]
    stripped = strip_line_number_gutters(replacement.old)
    if stripped is not None:
        new = strip_line_number_gutters(replacement.new)
        attempts.append((stripped, replacement.new if new is None else new, True, _GUTTER_WARNING))
    if _unescape(replacement.old) != replacement.old:
        attempts.append(
            (
                _unescape(replacement.old),
                _unescape(replacement.new, replacement_for=replacement.old),
                False,
                _ESCAPE_WARNING,
            )
        )
    for old, new, whole_lines, note in attempts:
        found = replace_fuzzy(
            content,
            old,
            new,
            replace_all=replace_all,
            whole_lines=whole_lines,
            typographic=True,
        )
        if found is None:
            continue
        if isinstance(found, AmbiguousFuzzyMatch):
            details, values = _ambiguity(content, found)
            raise _PatchError(
                "ambiguous_match",
                template="ambiguous_replacement",
                path=path,
                label=hunk.label,
                details=details,
                **values,
            )
        if replacement.expected is not None and found.replacements != replacement.expected:
            numbers = [len(_BREAK.findall(content[:start])) + 1 for start, _ in found.before_spans]
            raise _PatchError(
                "occurrence_mismatch",
                template="replacement_count",
                path=path,
                label=hunk.label,
                occurrences=found.replacements,
                lines=_line_list(numbers),
                expected=replacement.expected,
            )
        return found.new_content, [note] if note else []
    details = _not_found(content, replacement.old, source="old_string")
    present = None
    if replacement.new.strip() and replacement.new != replacement.old:
        present = replace_fuzzy(
            content,
            replacement.new,
            replacement.new,
            replace_all=True,
            typographic=True,
        )
        # Text the old text already holds proves nothing about an earlier edit.
        if isinstance(present, FuzzyReplacement) and replacement.new.strip() not in (
            replacement.old
        ):
            details["already_present"] = present.first_changed_line
    if not replace_all and present is None and not hunk.precise_only:
        copied = replace_copied(content, replacement.old, replacement.new)
        if isinstance(copied, AmbiguousFuzzyMatch):
            details, values = _ambiguity(content, copied)
            raise _PatchError(
                "ambiguous_match",
                template="ambiguous_copy",
                path=path,
                label=hunk.label,
                details=details,
                **values,
            )
        if copied is not None:
            return copied.new_content, copy_warnings(copied)
    raise _PatchError(
        "text_not_found",
        template="old_text_not_found",
        path=path,
        label=hunk.label,
        details=details,
    )


def _insert_at_line(content: str, hunk: _Hunk, path: object) -> str:
    """Insert the hunk's lines after 1-based line ``insert_line`` (0 = file start)."""
    assert hunk.insert_line is not None
    parts = _line_parts(content)
    if hunk.insert_line > len(parts):
        raise _PatchError(
            "text_not_found",
            template="insert_past_end",
            path=path,
            label=hunk.label,
            line=hunk.insert_line,
            count=len(parts),
        )
    ending = _ending(content)
    texts = [text for _, text in hunk.lines]
    if hunk.insert_line == len(parts) and parts and parts[-1][1] == "":
        # After a last line without a line break: the file keeps ending without one.
        return content + ending + ending.join(texts)
    position = sum(len(text) + len(end) for text, end in parts[: hunk.insert_line])
    return content[:position] + "".join(text + ending for text in texts) + content[position:]


def _apply_hunk(content: str, hunk: _Hunk, path: object) -> tuple[str, list[str]]:
    if hunk.replacement is not None:
        return _apply_replacement(content, hunk, path)
    if hunk.insert_line is not None:
        return _insert_at_line(content, hunk, path), []
    if not any(prefix in "+-" for prefix, _ in hunk.lines):
        return content, []
    hunk, warnings = _clean_additions(hunk, path)
    offset = 0
    hint_start = 0
    for hint in hunk.hints:
        found = _match(content[offset:], hint, hint)
        if isinstance(found, AmbiguousFuzzyMatch):
            details, values = _ambiguity(content, found, offset)
            raise _PatchError(
                "ambiguous_context",
                path=path,
                label=hunk.label,
                hint=_hint_text(hint),
                details=details,
                **values,
            )
        if found is None:
            raise _PatchError(
                "context_not_found",
                path=path,
                label=hunk.label,
                hint=_hint_text(hint),
                details=_candidates(content, hint),
            )
        hint_start = offset + found.before_spans[0][0]
        offset += found.before_spans[0][1]
        ending = _BREAK.match(content, offset)
        if ending:
            offset = ending.end()
    window = content[offset:]
    old, new = _hunk_text(hunk, " -"), _hunk_text(hunk, " +")
    if not any(prefix in " -" for prefix, _ in hunk.lines):
        position = offset if hunk.hints else len(content)
        if hunk.no_newline and position != len(content):
            raise _PatchError(
                "invalid_patch", template="no_newline_position", path=path, label=hunk.label
            )
        if hunk.eof and position != len(content):
            raise _PatchError(
                "text_not_found",
                template="eof_not_found",
                path=path,
                label=hunk.label,
                details=_candidates(content, new),
            )
        inserted = new.replace("\n", _ending(content))
        if not hunk.no_newline:
            inserted += _ending(content)
        # A hint ties the post-state to an insertion point. A suffix alone
        # cannot distinguish a retry from an intentional repeated append.
        if hunk.hints and inserted.strip() and content[position:].startswith(inserted):
            return content, warnings
        separator = (
            _ending(content)
            if position and not _BREAK.search(content[position - 1 : position])
            else ""
        )
        return content[:position] + separator + inserted + content[position:], warnings
    if hunk.hints:
        # Models sometimes repeat the final hint as the first context/removal
        # line. Include that line in the search without weakening earlier hints.
        offset = hint_start
        window = content[offset:]
    found = _match(window, old, new, eof=hunk.eof)
    normalized = _normalize_gutters(hunk)
    if found is None and normalized is not None:
        candidate_old, candidate_new = _hunk_text(normalized, " -"), _hunk_text(normalized, " +")
        candidate_match = _match(window, candidate_old, candidate_new, eof=hunk.eof)
        hunk, old, new, found = normalized, candidate_old, candidate_new, candidate_match
        warnings.append(_GUTTER_WARNING)
    if found is None and any(_GUTTER.match(t) for _, t in hunk.lines):
        raise _PatchError(
            "line_numbered_content",
            path=path,
            label=hunk.label,
            details=_candidates(content, old),
        )
    if found is None and _unescape(old) != old:
        escaped = replace(
            hunk,
            lines=[
                (p, _unescape(t, replacement_for=old if p == "+" else None)) for p, t in hunk.lines
            ],
        )
        # Unescaping line separators changes the hunk's physical line structure.
        escaped.lines = [(p, line) for p, text in escaped.lines for line in text.split("\n")]
        candidate_old, candidate_new = _hunk_text(escaped, " -"), _hunk_text(escaped, " +")
        candidate_match = _match(window, candidate_old, candidate_new, eof=hunk.eof)
        if candidate_match is not None:
            hunk, old, new, found = escaped, candidate_old, candidate_new, candidate_match
            warnings.append(_ESCAPE_WARNING)
    if found is None:
        # Ignore surplus blank context at a hunk boundary only after the full
        # locator misses. Removed blank lines remain part of the operation.
        lines = list(hunk.lines)
        while lines and lines[0][0] == " " and not lines[0][1].strip():
            lines.pop(0)
        while lines and lines[-1][0] == " " and not lines[-1][1].strip():
            lines.pop()
        if lines != hunk.lines and any(p in " -" for p, _ in lines):
            trimmed = replace(hunk, lines=lines)
            candidate_old, candidate_new = _hunk_text(trimmed, " -"), _hunk_text(trimmed, " +")
            candidate_match = _match(window, candidate_old, candidate_new, eof=hunk.eof)
            if candidate_match is not None:
                hunk, old, new, found = trimmed, candidate_old, candidate_new, candidate_match
    if found is None:
        context = "".join(text.strip() for prefix, text in hunk.lines if prefix == " ")
        poststate = _match(window, new, new, eof=hunk.eof or hunk.no_newline) if new else None
        if (
            (len(context) >= 4 or _inline_context_identifies(window, old, new))
            and isinstance(poststate, FuzzyReplacement)
            and (not hunk.no_newline or poststate.before_spans[0][1] == len(window))
        ):
            return content, warnings
        # An exact post-state elsewhere does not prove this target is satisfied.
        # Do not let approximate matching choose it (or a similar other target)
        # after the independent locator above failed to establish that fact.
        # Old text copied with errors is placed and merged by ``copy_match``.
        if not hunk.precise_only and (not new or _match(window, new, new) is None):
            found = match_copied_edit(window, hunk.lines, at_eof=hunk.eof)
    if isinstance(found, AmbiguousFuzzyMatch):
        details, values = _ambiguity(content, found, offset)
        raise _PatchError("ambiguous_match", path=path, label=hunk.label, details=details, **values)
    if found is None and hunk.eof:
        # The marker only claims where the lines are; the lines alone may still place
        # them. Text found elsewhere never proves the change was made earlier.
        try:
            edited, placed = _apply_hunk(content, replace(hunk, eof=False), path)
        except _PatchError:
            pass
        else:
            if edited != content:
                return edited, [*warnings, _EOF_WARNING, *placed]
    if found is None:
        if any(_GUTTER.match(t) for _, t in hunk.lines):
            raise _PatchError(
                "line_numbered_content",
                path=path,
                label=hunk.label,
                details=_candidates(content, old),
            )
        raise _PatchError(
            "text_not_found",
            path=path,
            label=hunk.label,
            details=_not_found(content, old, source="patch"),
        )
    if [t for p, t in hunk.lines if p in " -"] == [t for p, t in hunk.lines if p in " +"]:
        return content, warnings
    start, end = found.before_spans[0]
    after_start, after_end = found.after_spans[0]
    actual = _line_parts(window[start:end])
    if found.strategy != "exact" and any(
        token in old and token in new and token not in window[start:end]
        for token in ('\\"', "\\'", "\\\\")
    ):
        raise _PatchError(
            "text_not_found",
            path=path,
            label=hunk.label,
            details=_not_found(content, old, source="patch"),
        )
    prepared = _line_parts(found.new_content[after_start:after_end])
    # Line splitting omits the final empty line; the hunk still gives it a position.
    old_count = sum(p in " -" for p, _ in hunk.lines)
    new_count = sum(p in " +" for p, _ in hunk.lines)
    if len(actual) < old_count:
        actual.append(("", ""))
    if len(prepared) < new_count:
        prepared.append(("", ""))
    if len(actual) != old_count or len(prepared) != new_count:
        raise _PatchError(
            "text_not_found",
            path=path,
            label=hunk.label,
            details=_not_found(content, old, source="patch"),
        )
    trailing = _BREAK.match(window, end)
    final_ending = trailing.group() if trailing else ""
    if trailing:
        end = trailing.end()
    if hunk.no_newline and end != len(window):
        raise _PatchError(
            "invalid_patch", template="no_newline_position", path=path, label=hunk.label
        )
    if actual:
        actual[-1] = (actual[-1][0], final_ending)
    output: list[tuple[str, str]] = []
    last_output_prefix = ""
    old_index = new_index = 0
    removed_lines: list[tuple[str, str]] = []
    for prefix, locator_line in hunk.lines:
        if prefix == " ":
            removed_lines.clear()
            output.append(actual[old_index])  # Preserve every context byte.
            last_output_prefix = prefix
        elif prefix == "+":
            replacement_line = prepared[new_index][0]
            if removed_lines:
                actual_line, old_line = removed_lines.pop(0)
                if found.strategy != "exact":
                    replacement_line = preserve_typography(actual_line, old_line, replacement_line)
            output.append((replacement_line, _ending(content)))
            last_output_prefix = prefix
        elif prefix == "-":
            removed_lines.append((actual[old_index][0], locator_line))
        if prefix in " -":
            old_index += 1
        if prefix in " +":
            new_index += 1
    if output:
        output = [(text, ending or _ending(content)) for text, ending in output[:-1]] + output[-1:]
        if last_output_prefix == "+" or hunk.no_newline:
            output[-1] = (output[-1][0], "" if hunk.no_newline else final_ending)
    replacement_text = "".join(text + ending for text, ending in output)
    warnings.extend(copy_warnings(found, line_shift=len(_BREAK.findall(content, 0, offset))))
    return content[: offset + start] + replacement_text + window[end:], warnings
