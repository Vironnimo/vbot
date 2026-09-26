"""Recognize a long Wiki line copied with different Markdown emphasis markers.

This is deliberately narrower than a Markdown renderer: code, links, escapes and
block syntax are never normalized. The complete visible line must match, and
only paired inline emphasis delimiters can differ. A copied locator is not
permission to change emphasis in text the requested edit leaves alone.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_EMPHASIS = re.compile(
    r"(?<![\w\\*_])(?P<mark>\*\*|__|\*|_)"
    r"(?P<body>[^\s*_](?:[^*_\r\n]*?[^\s*_])?)(?P=mark)(?![\w*_])"
)
_CODE = re.compile(r"(`+)(.*?)(?<!`)\1(?!`)")
# Recognize possible container fences conservatively. Exact edits remain
# available inside them; emphasis-copy recovery must never change code literals.
# Prefix tokens cannot start a backtick/tilde fence. Commit each token so failed
# fence matches cannot repartition its whitespace across nested repetitions.
_FENCE = re.compile(r"^(?>[ \t]|>[ \t]?|(?:[-+*]|\d+[.)])[ \t]+)*(`{3,}|~{3,})(.*)$")
_MIN_WORDS = 12


def _fold(line: str) -> str | None:
    """Remove paired emphasis only outside exact inline code spans."""

    parts: list[str] = []
    offset = 0
    for match in _CODE.finditer(line):
        prose = _fold_prose(line[offset : match.start()])
        if prose is None:
            return None
        parts.extend((prose, match.group()))
        offset = match.end()
    prose = _fold_prose(line[offset:])
    return None if prose is None else "".join((*parts, prose))


def _fold_prose(text: str) -> str | None:
    # Those characters may delimit code, links, escaped literals or HTML. Their
    # meaning is outside this repair, even if a renderer would hide some of them.
    if any(char in text for char in "`\\[]<>"):
        return None
    return _EMPHASIS.sub(lambda match: match["body"], text)


def matching_lines(content: str, old: str) -> list[tuple[int, int, str, bool]]:
    """Return matching lines and whether their marker differences are prose emphasis.

    Literal marker differences remain terminal candidates: a later generic copy
    matcher must not reinterpret code or link syntax as harmless emphasis.
    """

    if "\n" in old or "\r" in old:
        return []
    folded = _fold(old)
    if len(re.findall(r"\w+", old)) < _MIN_WORDS:
        return []
    without_markers = old.translate(str.maketrans("", "", "*_"))
    candidates = []
    offset = 0
    fence = ""
    for number, raw in enumerate(content.splitlines(keepends=True), 1):
        line = raw.rstrip("\r\n")
        marker = _FENCE.match(line)
        if marker:
            found = marker[1]
            if not fence:
                fence = found
            elif found[0] == fence[0] and len(found) >= len(fence) and not marker[2].strip():
                fence = ""
        elif line != old and line.translate(str.maketrans("", "", "*_")) == without_markers:
            safe = (
                not fence
                and not line.startswith(("    ", "\t"))
                and folded is not None
                and _fold(line) == folded
            )
            candidates.append((number, offset, line, safe))
        offset += len(raw)
    return candidates


def preserve_kept_emphasis(old: str, current: str, new: str) -> str | None:
    """Transfer the page's emphasis only through unchanged portions of the edit.

    Changed prose and newly supplied formatting remain as requested. If the new
    text turns a kept passage into code or link syntax, this repair cannot judge
    which markers are literal, so the caller must provide an exact locator.
    """

    kept = [
        (i1, i2, j1)
        for tag, i1, i2, j1, _ in SequenceMatcher(None, old, new, autojunk=False).get_opcodes()
        if tag == "equal"
    ]
    edits = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old, current, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if any(char not in "*_" for char in old[i1:i2] + current[j1:j2]):
            return None
        for begin, end, target in kept:
            if (begin <= i1 < i2 <= end) or (i1 == i2 and begin < i1 < end):
                edits.append((target + i1 - begin, target + i2 - begin, current[j1:j2]))
                break
    if not edits:
        return new
    result = new
    for start, end, replacement in sorted(edits, reverse=True):
        result = result[:start] + replacement + result[end:]
    # Exact code spans are retained by _fold, so marker changes inside new code
    # cannot pass this check. Unsupported new link/escape syntax also stays exact.
    folded = _fold(new)
    return result if folded is not None and folded == _fold(result) else None
