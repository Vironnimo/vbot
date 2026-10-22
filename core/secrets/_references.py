"""Secret references in text: finding, substituting and masking.

A reference is ``{{secret:NAME}}`` or ``{{secret:NAME.field}}``. The same text
is the placeholder an Agent writes and the mask vBot puts where a known value
appeared, so masked text written back through a sink restores the value.
"""

from __future__ import annotations

import base64
import json
import re
from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, cast
from urllib.parse import quote

#: ``{{secret:NAME}}`` or ``{{secret:NAME.field}}``; names and fields are
#: matched case-insensitively and normalized by the resolver.
REFERENCE_PATTERN: Final = re.compile(
    r"\{\{\s*secret:\s*([A-Za-z][A-Za-z0-9_]{0,63})(?:\.([A-Za-z][A-Za-z0-9_]{0,31}))?\s*\}\}"
)

#: Values shorter than this are not masked: they would match unrelated text.
MIN_MASKED_LENGTH: Final = 8
# Lines of a multi-line value at least this long are masked on their own too, so
# a key printed with other line breaks or cut in parts does not leak.
_MIN_MASKED_LINE = 20
# How much of the open end of growing text :meth:`SecretMasker.mask_growing`
# checks, and the shortest beginning of a value it holds back there.
_GROWING_TAIL_LIMIT = 1024
_MIN_HELD_PREFIX = 3


@dataclass(frozen=True)
class Reference:
    """One reference found in text: the Secret name as written and the field."""

    name: str
    field: str | None
    start: int
    end: int

    @property
    def text_name(self) -> str:
        return self.name if self.field is None else f"{self.name}.{self.field}"


def find_references(text: str) -> list[Reference]:
    return [
        Reference(
            name=match.group(1).upper(),
            field=match.group(2).lower() if match.group(2) else None,
            start=match.start(),
            end=match.end(),
        )
        for match in REFERENCE_PATTERN.finditer(text)
    ]


def has_reference(value: Any) -> bool:
    """Whether any string inside ``value`` (JSON-like) holds a reference."""
    return any(REFERENCE_PATTERN.search(text) for text in iter_strings(value))


def iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_strings(item)


def map_strings(value: Any, change: Callable[[str], str]) -> Any:
    """``value`` with ``change`` applied to every string (keys stay unchanged)."""
    if isinstance(value, str):
        return change(value)
    if isinstance(value, Mapping):
        return {key: map_strings(item, change) for key, item in value.items()}
    if isinstance(value, list):
        return [map_strings(item, change) for item in value]
    if isinstance(value, tuple):
        return tuple(map_strings(item, change) for item in value)
    return value


def substitute(text: str, resolve: Callable[[Reference], str]) -> str:
    """``text`` with every reference replaced by ``resolve``'s value."""
    references = find_references(text)
    if not references:
        return text
    parts: list[str] = []
    position = 0
    for reference in references:
        parts.append(text[position : reference.start])
        parts.append(resolve(reference))
        position = reference.end
    parts.append(text[position:])
    return "".join(parts)


class SecretMasker:
    """Replaces known values, and common encodings of them, with their references.

    Built once per change of the known values; :meth:`mask` is a single regular
    expression pass, longest match first.
    """

    def __init__(self, known: Mapping[str, str]) -> None:
        """``known`` maps each value to the reference text that replaces it."""
        variants: dict[str, str] = {}
        for value, reference in sorted(known.items(), key=lambda item: len(item[0])):
            for variant in _variants(value):
                if len(variant) >= MIN_MASKED_LENGTH:
                    variants.setdefault(variant, reference)
        self._variants = variants
        self._ordered = sorted(variants)
        self._pattern = (
            re.compile(
                "|".join(re.escape(item) for item in sorted(variants, key=len, reverse=True))
            )
            if variants
            else None
        )

    def __bool__(self) -> bool:
        return self._pattern is not None

    def mask(self, text: str) -> str:
        if self._pattern is None or not text:
            return text
        return self._pattern.sub(lambda match: self._variants[match.group(0)], text)

    def mask_growing(self, text: str) -> str:
        """``text`` masked, without an end that could be the beginning of a value.

        For snapshots of output still being written: a value cut off at the end
        does not match yet, so that end is held back until a later snapshot
        holds the whole value. Only the last line's final
        ``_GROWING_TAIL_LIMIT`` characters are checked.
        """
        masked = self.mask(text)
        if self._pattern is None:
            return masked
        start = max(masked.rfind("\n") + 1, len(masked) - _GROWING_TAIL_LIMIT)
        ordered = self._ordered
        for position in range(start, len(masked) - _MIN_HELD_PREFIX + 1):
            tail = masked[position:]
            index = bisect_left(ordered, tail)
            if index < len(ordered) and ordered[index].startswith(tail):
                return masked[:position]
        return masked

    def mask_value[T](self, value: T) -> T:
        """``value`` (JSON-like) with every string masked; ``value`` itself when none matches."""
        if self._pattern is None or not any(self.contains(text) for text in iter_strings(value)):
            return value
        return cast(T, map_strings(value, self.mask))

    def contains(self, text: str) -> bool:
        return self._pattern is not None and self._pattern.search(text) is not None


EMPTY_MASKER: Final = SecretMasker({})


def _variants(value: str) -> set[str]:
    found = {value}
    if "\n" in value:
        found.add(value.replace("\r\n", "\n").replace("\n", "\r\n"))
        found.update(
            line.strip() for line in value.splitlines() if len(line.strip()) >= _MIN_MASKED_LINE
        )
    escaped = json.dumps(value, ensure_ascii=False)[1:-1]
    found.add(escaped)
    found.add(json.dumps(value)[1:-1])
    found.add(quote(value, safe=""))
    raw = value.encode("utf-8")
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        for offset in range(3):
            fragment = _base64_fragment(encode, raw, offset)
            if fragment:
                found.add(fragment)
    return found


def _base64_fragment(encode: Callable[[bytes], bytes], raw: bytes, offset: int) -> str:
    """The Base64 characters that depend only on ``raw`` when ``offset`` bytes precede it."""
    encoded = encode(b"\x00" * offset + raw).decode("ascii")
    start = -(-offset * 4 // 3)
    end = (offset + len(raw)) * 4 // 3
    return encoded[start:end]
