"""Notes for search_files results that miss what their pattern seems to ask for.

The handler runs the passes behind these notes; this module decides when a
pattern looks like file names, like literal text or like a phrase that wrapped
text can break across lines, and words the notes.
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
# Plain file-name globs, sometimes joined with regex alternation by mistake.
_GLOB_NAMES = re.compile(r"[\w./*?\-]+(?:\|[\w./*?\-]+)*")
# Characters ripgrep's regex syntax reads as operators; escaped, each matches itself.
_REGEX_META = frozenset("\\.+*?()|[]{}^$#&-~")
# Whitespace between two words of a phrase that holds a line break.
_BROKEN_GAP = r"[^\S\n]*\n\s*"
# Bound on the regexes of one wrapped-phrase count, which go on a command line.
_WRAPPED_PHRASE_CHARACTERS = 16000


def repaired_glob_note(pattern: str, repaired: str, *, filtered: bool) -> str | None:
    """Offer name filters after a repaired glob-like regex matched neither contents nor names.

    Existing globs keep their scope: appending positive globs would widen it,
    while replacing them could discard exclusions or other explicit selection.
    """
    if not _GLOB_NAMES.fullmatch(pattern) or not any(
        part.startswith("*") for part in pattern.split("|")
    ):
        return None
    opening = (
        f'Searched "{repaired}" as a repaired regular expression; '
        "no file contents or names match it. "
    )
    if filtered:
        return opening + "To list files selected by the existing glob filters, omit pattern."
    globs = json.dumps(pattern.split("|"), ensure_ascii=False)
    return opening + f"If you meant file names, omit pattern and pass glob {globs}."


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


def _phrase_words(pattern: str, *, literal: bool) -> list[list[str]]:
    """Split fixed text into its alternatives, each a list of its words as regex text.

    A regex keeps its escapes and alternation; literal text is escaped and has
    no alternatives. Whitespace separates words.
    """
    alternatives: list[list[str]] = [[""]]
    index = 0
    while index < len(pattern):
        character = pattern[index]
        index += 1
        words = alternatives[-1]
        if character.isspace():
            if words[-1]:
                words.append("")
        elif literal:
            words[-1] += "\\" + character if character in _REGEX_META else character
        elif character == "\\":
            words[-1] += character + pattern[index : index + 1]
            index += 1
        elif character == "|":
            alternatives.append([""])
        else:
            words[-1] += character
    return [[word for word in words if word] for words in alternatives]


def wrapped_phrases(patterns: list[str], *, literal: bool) -> tuple[list[str], str] | None:
    """Return regexes matching the patterns' phrases only across a line break, and the
    regex that matches the phrases with any whitespace between their words.

    Wrapped prose (HTML, Markdown) breaks long phrases across lines, where a
    search for words separated by spaces misses them. Only fixed text
    qualifies: any pattern under -F, or a regex that ``finds_fixed_text``.
    Each returned regex requires a line break at one gap between two words, so
    their matches are the ones a search within lines misses. ``None`` when no
    pattern holds a phrase, or when the regexes would not fit a command line.
    """
    alternatives: list[list[str]] = []
    for pattern in patterns:
        if "\n" in pattern or not (literal or finds_fixed_text(pattern)):
            return None
        alternatives.extend(_phrase_words(pattern, literal=literal))
    spanning = [
        r"\s+".join(words[:gap]) + _BROKEN_GAP + r"\s+".join(words[gap:])
        for words in alternatives
        for gap in range(1, len(words))
    ]
    if not spanning or sum(map(len, spanning)) > _WRAPPED_PHRASE_CHARACTERS:
        return None
    return spanning, "|".join(r"\s+".join(words) for words in alternatives)


def wrapped_phrase_note(
    pattern: str, matches: int, files: int, *, matched: bool, literal: bool, multiline: bool
) -> str:
    """Say how many matches a line break between words of the phrase hides, and which
    call finds them; ``matched`` when the search found others within lines."""
    where = "1 file" if files == 1 else f"{files} files"
    them = "it" if matches == 1 else "them"
    if literal:
        change = " and remove -F from args" if multiline else " and replace -F with -U in args"
    else:
        change = "" if multiline else " and add -U to args"
    call = f'pass pattern "{pattern}"{change}'
    if matched:
        hidden = "1 match" if matches == 1 else f"{matches} matches"
        return (
            f"The results leave out {hidden} in {where} with a line break between words of "
            f"pattern. To include {them}, {call}."
        )
    times = "1 time" if matches == 1 else f"{matches} times"
    return (
        "Within one line, pattern matches nothing; with a line break between its words it "
        f"occurs {times} in {where}. To find {them}, {call}."
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
