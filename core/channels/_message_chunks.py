"""Split outbound Channel text into platform-size chunks without breaking structure."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

# CommonMark fences: up to three spaces of indentation, then three or more backticks
# or tildes. A backtick fence's info string cannot itself contain a backtick.
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}(?=[^`]*$)|~{3,})(.*)$")
_ZWJ = "\u200d"


@dataclass(frozen=True)
class _Fence:
    opener: str
    marker: str

    def closes(self, line: str) -> bool:
        stripped = line.lstrip(" ")
        if len(line) - len(stripped) > 3:
            return False
        run = len(stripped) - len(stripped.lstrip(self.marker[0]))
        return run >= len(self.marker) and not stripped[run:].strip()


def split_message(
    message: str,
    limit: int,
    *,
    measure: Callable[[str], int] = len,
    code_fences: bool = True,
) -> list[str]:
    """Split ``message`` into chunks whose ``measure`` never exceeds ``limit``.

    Cuts prefer a paragraph break, then a line break, then whitespace; only a single
    token longer than a whole chunk is cut hard, and then between graphemes where
    possible. With ``code_fences`` (for platforms rendering Markdown), a chunk that
    ends inside a fenced code block closes the fence and the next chunk reopens it
    with the same opener line; the added markers count toward ``limit``. ``measure``
    must be additive over concatenation (code points by default).
    """
    if limit <= 0:
        raise ValueError("max_chars must be positive")
    chunks: list[str] = []
    rest = message
    fence: _Fence | None = None
    while rest:
        prefix = f"{fence.opener}\n" if fence is not None else ""
        if measure(prefix) + measure(rest) <= limit:
            _append(chunks, prefix + rest)
            break
        chunk, rest, fence = _next_chunk(rest, prefix, limit, measure, code_fences)
        _append(chunks, chunk)
    return chunks


def _next_chunk(
    rest: str,
    prefix: str,
    limit: int,
    measure: Callable[[str], int],
    code_fences: bool,
) -> tuple[str, str, _Fence | None]:
    reserve = 0  # Room kept for a closing fence marker.
    while True:
        budget = limit - measure(prefix) - reserve
        if (prefix or reserve) and _fitting_length(rest, budget, measure) == 0:
            # The fence markers alone exhaust the chunk: continue as plain text.
            cut, resume = _choose_cut(rest, limit, measure)
            return rest[:cut], rest[resume:], None
        cut, resume = _choose_cut(rest, budget, measure)
        body = prefix + rest[:cut]
        fence = _open_fence(body) if code_fences else None
        closing = "\n" + fence.marker if fence is not None else ""
        if measure(closing) <= reserve:
            return body + closing, rest[resume:], fence
        reserve = measure(closing)


def _choose_cut(text: str, budget: int, measure: Callable[[str], int]) -> tuple[int, int]:
    """Return (chunk end, next start) within ``budget`` using the preferred boundary."""
    end = _fitting_length(text, budget, measure)
    if end == 0:
        # Not even one character fits; send one grapheme to guarantee progress.
        return (step := _grapheme_end(text, 1)), step
    half = end // 2
    # A separator starting right after the window is as good as one inside it.
    for separator in ("\n\n", "\n"):
        index = text.rfind(separator, 0, end + len(separator))
        if index >= max(half, 1):
            return index, index + len(separator)
    spaces = [match.start() for match in re.finditer(r"\s", text[: end + 1])]
    if spaces and spaces[-1] > 0:
        return spaces[-1], spaces[-1] + 1
    cut = _grapheme_start(text, end)
    if cut == 0:
        cut = end
    return cut, cut


def _fitting_length(text: str, budget: int, measure: Callable[[str], int]) -> int:
    used = 0
    for index, character in enumerate(text):
        used += measure(character)
        if used > budget:
            return index
    return len(text)


def _grapheme_start(text: str, index: int) -> int:
    """Move ``index`` back until it does not split a grapheme cluster."""
    while 0 < index < len(text) and _joins_previous(text, index):
        index -= 1
    return index


def _grapheme_end(text: str, index: int) -> int:
    while index < len(text) and _joins_previous(text, index):
        index += 1
    return index


def _joins_previous(text: str, index: int) -> bool:
    character, previous = text[index], text[index - 1]
    if character == _ZWJ or previous == _ZWJ:
        return True
    code = ord(character)
    if (
        0xFE00 <= code <= 0xFE0F  # variation selectors
        or 0xE0100 <= code <= 0xE01EF
        or 0x1F3FB <= code <= 0x1F3FF  # emoji skin tones
        or 0xE0020 <= code <= 0xE007F  # emoji tag sequences
        or unicodedata.category(character) in {"Mn", "Mc", "Me"}
    ):
        return True
    if _is_regional_indicator(character) and _is_regional_indicator(previous):
        # Flags pair regional indicators; an odd run length before means mid-flag.
        run = 0
        while index - run - 1 >= 0 and _is_regional_indicator(text[index - run - 1]):
            run += 1
        return run % 2 == 1
    return False


def _is_regional_indicator(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def _open_fence(text: str) -> _Fence | None:
    fence: _Fence | None = None
    for line in text.split("\n"):
        if fence is not None:
            if fence.closes(line):
                fence = None
            continue
        match = _FENCE_OPEN.match(line)
        if match is not None:
            fence = _Fence(opener=line, marker=match.group(1))
    return fence


def _append(chunks: list[str], chunk: str) -> None:
    if chunk.strip():
        chunks.append(chunk)
