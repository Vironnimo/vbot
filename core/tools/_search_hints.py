"""Notes for search_files results that miss what their pattern seems to ask for.

The handler runs the passes behind these notes; this module decides when a
pattern looks like file names or like literal text, and words the notes.
"""

from __future__ import annotations

import json
import re

# A pattern built only from file name characters and plain regex structure:
# words, "-", "/", dots, anchors, \b, alternation and groups.
_NAME_SHAPED = re.compile(r"(?:\(\?:|\\\.|\\b|[\w\-/.^$|()])+")
# An extension ending a name: ".py" or "\.py" before "|", ")" or the end.
_EXTENSION = re.compile(r"[\w\-)]\\?\.([^\W\d_][\w\-]*)(?=\$?(?:\\b)?(?:[)|]|$))")
# The fixed start of a name pattern, up to its last separator: "harvest_", "tools/".
_NAME_START = re.compile(r"\^?(?:\\b)?([\w\-/]*[_\-/])")
# Regex operators besides ".", which Agents also write for a literal dot.
_OPERATORS = frozenset("?*+()[]{}^$|\\")
# Operators that make a regex match by shape rather than fixed text.
_SHAPE_OPERATORS = _OPERATORS - {"|"}
# Operators with which a regex can miss text that holds the pattern literally;
# "." matches itself, and "*" or "|" leave a part that still matches.
_LITERAL_MISSES = _SHAPE_OPERATORS - {"*"}
# Escaped punctuation, such as "\.", stands for itself; "\b" only bounds a word.
_ESCAPED_PUNCTUATION = re.compile(r"\\(?:[^\w\s]|b)")
_WORD = re.compile(r"\w")
# Regex parts written to match a line break, which a search without -U never
# reaches: \s (also in [\s\S]) and the s flag.
_LINE_BREAKING = re.compile(r"(?<!\\)(?:\\\\)*\\s|\(\?[a-zA-Z]*s[a-zA-Z]*[:)]")


def file_name_extensions(pattern: str) -> list[str] | None:
    """Return the extensions of a pattern shaped like file names, or ``None`` for others.

    ``verify.py``, ``tools/corpus\\.py`` and ``harvest_(3|22)\\.txt`` name
    their extensions; ``(verify|corpus)`` names none but lists names; a single
    word without an extension, such as an identifier, is no file name.
    """
    if not _NAME_SHAPED.fullmatch(pattern):
        return None
    extensions = [match.group(1).casefold() for match in _EXTENSION.finditer(pattern)]
    if not extensions and "|" not in pattern:
        return None
    return extensions


def unnamed_files_note(
    pattern: str, *, file_list: bool, files: list[tuple[str, str]]
) -> str | None:
    """Say that no searched file has a name matching a pattern shaped like file names.

    ``files`` holds each searched file's shown path and name. The note applies
    when an extension of the pattern is one a searched file has, or, for a
    file-list search listing alternatives, when some searched file's name
    starts as the pattern does (``pilot_element_`` in
    ``pilot_element_003|pilot_element_022``); other patterns can be text to
    find. Its example glob lists such files.
    """
    extensions = file_name_extensions(pattern)
    if extensions is None:
        return None
    shown = [(label.casefold(), name.casefold()) for label, name in files]
    suffixes = {name.rpartition(".")[2] for _, name in shown if "." in name}
    extension = next((extension for extension in extensions if extension in suffixes), None)
    start = _NAME_START.match(pattern)
    prefix = start.group(1).casefold() if start else ""
    ending = f".{extension}" if extension else ""
    similar = bool(prefix) and any(
        (label if "/" in prefix else name).startswith(prefix) and name.endswith(ending)
        for label, name in shown
    )
    if extension is None and not (file_list and "|" in pattern and similar):
        return None
    glob = f"{prefix}*{ending}" if similar else f"*{ending}"
    return (
        "No file name matches pattern either. To list similar file names, pass glob "
        f"{json.dumps(glob, ensure_ascii=False)} without pattern."
    )


def can_miss_literal_text(pattern: str) -> bool:
    """Whether the pattern, read as a regex, can miss text that holds it literally."""
    return any(character in _LITERAL_MISSES for character in pattern)


def literal_text_note(pattern: str, files: int) -> str:
    """Say that a regex without matches occurs as literal text in ``files`` files."""
    operators = [f'"{operator}"' for operator in dict.fromkeys(pattern) if operator in _OPERATORS]
    if len(operators) == 1:
        named = f"{operators[0]} is an operator"
    else:
        named = f"{', '.join(operators[:-1])} and {operators[-1]} are operators"
    where = "1 file" if files == 1 else f"{files} files"
    return (
        f"As a regular expression, pattern matches nothing ({named}); as literal text it "
        f"occurs in {where}. Add -F to args to search for it literally."
    )


def can_span_lines(pattern: str) -> bool:
    """Whether the pattern has a part that matches a line break once matches span lines."""
    return _LINE_BREAKING.search(pattern) is not None


def spanning_note(files: int) -> str:
    """Say that a regex without matches within lines matches across lines in ``files`` files."""
    where = "1 file" if files == 1 else f"{files} files"
    return (
        f"Within one line, pattern matches nothing; across lines it occurs in {where}. "
        "Add -U to args to let a match span lines."
    )


def finds_fixed_text(pattern: str) -> bool:
    """Whether a regex finds fixed text or alternatives of it, whose occurrences count.

    ``radioactive-elements-htm``, ``TODO|FIXME``, ``os.path`` and ``\\.kind\\b`` do; ``.``,
    ``\\w+`` and ``^def`` match by shape, so counting their matches counts
    characters or words nobody asked for.
    """
    plain = _ESCAPED_PUNCTUATION.sub("x", pattern)
    if any(character in _SHAPE_OPERATORS for character in plain):
        return False
    return all(_WORD.search(alternative) for alternative in plain.split("|"))


def match_count_note(matches: int) -> str:
    """Say how many matches the counted lines hold, when that is more than the lines."""
    return (
        f"The counts are matching lines, which hold {matches} matches in all. Add "
        "--count-matches to args to count matches per file."
    )
