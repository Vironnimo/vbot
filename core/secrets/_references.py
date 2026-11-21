"""Secret references in text: finding, substituting and masking.

A reference is ``{{secret:NAME}}`` or ``{{secret:NAME.field}}``. The same text
is the placeholder an Agent writes and the mask vBot puts where a known value
appeared, so masked text written back through a sink restores the value.

An escaped reference, ``{{!secret:NAME}}``, stands for the reference text
itself: substitution removes one ``!`` (``{{!!secret:NAME}}`` gives
``{{!secret:NAME}}``), so every text can be written through a sink.
"""

from __future__ import annotations

import base64
import json
import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Iterator, Mapping
from copy import copy
from dataclasses import dataclass
from heapq import merge
from typing import TYPE_CHECKING, Any, Final, cast
from urllib.parse import quote, quote_plus

if TYPE_CHECKING:
    from core.secrets._agent_secret_model import AgentSecret

#: ``{{secret:NAME}}`` or ``{{secret:NAME.field}}``; names and fields are
#: matched case-insensitively and normalized by the resolver.
REFERENCE_PATTERN: Final = re.compile(
    r"\{\{\s*secret:\s*([A-Za-z][A-Za-z0-9_]{0,63})(?:\.([A-Za-z][A-Za-z0-9_]{0,31}))?\s*\}\}"
)

# A reference or an escaped one: group 1 holds the escape marks, empty for a
# reference. Names and fields as in ``REFERENCE_PATTERN``.
_REFERENCE_OR_ESCAPE: Final = re.compile(
    r"\{\{\s*(!*)\s*secret:\s*([A-Za-z][A-Za-z0-9_]{0,63})(?:\.([A-Za-z][A-Za-z0-9_]{0,31}))?\s*\}\}"
)

#: Values shorter than this are not masked: they would match unrelated text.
MIN_MASKED_LENGTH: Final = 8
REDACTED: Final = "[redacted]"
# Lines of a multi-line value at least this long are masked on their own too, so
# a key printed with other line breaks or cut in parts does not leak.
_MIN_MASKED_LINE = 20
# Characters JavaScript's encodeURIComponent and encodeURI leave unencoded
# beyond those ``quote`` always keeps.
_URI_COMPONENT_SAFE = "!~*'()"
_URI_SAFE = "!~*'();/?:@&=+$,#"
_PERCENT_ESCAPE = re.compile(r"%[0-9A-F]{2}")
# The line length of GNU ``base64`` output.
_BASE64_LINE = 76
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


def has_reference_or_escape(value: Any) -> bool:
    """Whether any string inside ``value`` (JSON-like) holds a reference or an escaped one."""
    return any(_REFERENCE_OR_ESCAPE.search(text) for text in iter_strings(value))


def unescape_references(text: str) -> str:
    """``text`` with one ``!`` removed from each escaped reference; references stay."""
    return substitute(text, lambda reference: text[reference.start : reference.end])


def iter_strings(value: Any, *, include_keys: bool = False) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if include_keys and isinstance(key, str):
                yield key
            yield from iter_strings(item, include_keys=include_keys)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_strings(item, include_keys=include_keys)


def map_strings(value: Any, change: Callable[[str], str], *, include_keys: bool = False) -> Any:
    """Apply ``change`` to strings; output masking may include property names."""
    if isinstance(value, str):
        return change(value)
    if isinstance(value, Mapping):
        keys = {key: change(key) if include_keys and isinstance(key, str) else key for key in value}
        unchanged = {key for key, mapped in keys.items() if key == mapped}
        result = {}
        for key, item in value.items():
            mapped = keys[key]
            if mapped != key:
                # A literal reference or another masked key may already use
                # this name. Keep every value and preserve unchanged names.
                base, suffix = mapped, 2
                while mapped in unchanged or mapped in result:
                    mapped = f"{base} [{suffix}]"
                    suffix += 1
            result[mapped] = map_strings(item, change, include_keys=include_keys)
        return result
    if isinstance(value, list):
        return [map_strings(item, change, include_keys=include_keys) for item in value]
    if isinstance(value, tuple):
        return tuple(map_strings(item, change, include_keys=include_keys) for item in value)
    return value


def substitute(text: str, resolve: Callable[[Reference], str]) -> str:
    """``text`` with every reference replaced by ``resolve``'s value.

    An escaped reference loses one ``!`` and is never resolved.
    """
    parts: list[str] = []
    position = 0
    for match in _REFERENCE_OR_ESCAPE.finditer(text):
        parts.append(text[position : match.start()])
        if match.group(1):
            # Escaped: drop the first ``!``.
            parts.append(text[match.start() : match.start(1)])
            parts.append(text[match.start(1) + 1 : match.end()])
        else:
            parts.append(
                resolve(
                    Reference(
                        name=match.group(2).upper(),
                        field=match.group(3).lower() if match.group(3) else None,
                        start=match.start(),
                        end=match.end(),
                    )
                )
            )
        position = match.end()
    if not parts:
        return text
    parts.append(text[position:])
    return "".join(parts)


class SecretMasker:
    """Replaces known values, and common encodings of them, with their references.

    Built once per change of the known values; :meth:`mask` is a single regular
    expression pass, longest match first.
    """

    def __init__(self, known: Mapping[str, str], *, filter_references: bool = False) -> None:
        """``known`` maps each value to the reference text that replaces it."""
        variants: dict[str, str] = {}
        for value, reference in sorted(known.items(), key=lambda item: len(item[0])):
            for variant in _variants(value):
                if len(variant) >= MIN_MASKED_LENGTH:
                    variants.setdefault(variant, reference)
        self._allowed: dict[str, frozenset[str | None]] | None = {} if filter_references else None
        self._environment: frozenset[str] = frozenset()
        self._local: dict[str, str] = {}
        self._exact: Mapping[str, str] = {}
        self._prepared_matches: Callable[[str, str | None, str], bool] | None = None
        self._catalog: Mapping[str, AgentSecret] = {}
        self._source_labels: Mapping[str, str] = {}
        self._compile(variants)

    def _compile(self, variants: dict[str, str]) -> None:
        self._variants = variants
        self._ordered = sorted(variants)
        self._extended_prefixes = [
            value for value in variants if len(value) > _GROWING_TAIL_LIMIT or "\n" in value
        ]
        self._longest = max(map(len, variants), default=0)
        ordered = sorted(variants, key=len, reverse=True)
        expressions = [re.escape(item) for item in ordered]
        self._overlap_pattern: re.Pattern[str] | None = None
        if self.filters_references:
            if any(item in REDACTED for item in variants):
                longer = [
                    re.escape(item)
                    for item in ordered
                    if item.startswith(REDACTED) and item != REDACTED
                ]
                expressions = [*longer, re.escape(REDACTED), *expressions]
            # A value containing braces may cross a reference boundary. Find
            # both full spans in that case, so neither can release the other's tail.
            if any("{" in item or "}" in item for item in variants):
                self._overlap_pattern = re.compile("|".join(expressions))
            expressions.insert(0, f"({REFERENCE_PATTERN.pattern})")
        self._pattern = re.compile("|".join(expressions)) if expressions else None

    def __bool__(self) -> bool:
        return self._pattern is not None

    @property
    def filters_references(self) -> bool:
        return self._allowed is not None

    def allows_name(self, name: str) -> bool:
        return self._allowed is None or name.upper() in self._allowed

    def catalog_record(self, name: str) -> AgentSecret | None:
        """Current display metadata for this name; concealed field values are absent."""
        return self._catalog.get(name.upper())

    def catalog_source_label(self, name: str) -> str | None:
        """The current record's source label, without contacting its source."""
        return self._source_labels.get(name.upper())

    def allows_environment_name(self, name: str) -> bool:
        return self._allowed is None or name.upper() in self._environment

    def allows_reference(self, name: str, field: str | None = None) -> bool:
        return self._allowed is None or (
            (field.lower() if field is not None else None) in self._allowed.get(name.upper(), ())
        )

    def for_session(
        self,
        values: Mapping[str, str],
        allowed: dict[str, frozenset[str | None]],
        environment: frozenset[str],
        prepared_matches: Callable[[str, str | None, str], bool] | None = None,
        catalog: Mapping[str, AgentSecret] | None = None,
        source_labels: Mapping[str, str] | None = None,
    ) -> SecretMasker:
        """Share the compiled global matcher; select only this Session's usable references."""
        masker = copy(self)
        masker._allowed = allowed
        masker._environment = environment
        masker._exact = values
        masker._prepared_matches = prepared_matches
        masker._catalog = catalog or {}
        masker._source_labels = source_labels or {}
        if not self.filters_references:
            masker._compile(self._variants)
        masker._local = {value: ref for value, ref in values.items() if value in self._variants}
        for value, reference in values.items():
            for variant in _variants(value):
                if variant in self._variants:
                    masker._local.setdefault(variant, reference)
        return masker

    def project_prepared(self, literal: str, reference: str) -> str:
        """The current recipient's reference for a value substituted in this call."""
        if not self.filters_references:
            return reference
        match = REFERENCE_PATTERN.fullmatch(reference)
        if (
            match
            and self._prepared_matches
            and self._prepared_matches(
                match[1].upper(), match[2].lower() if match[2] else None, literal
            )
        ):
            return reference
        return self._exact.get(literal, REDACTED)

    def for_diagnostics(self, literals: Iterable[str]) -> SecretMasker:
        """Mask known values anonymously, including short credentials actually sent."""
        variants = dict.fromkeys(self._variants, REDACTED)
        for value in literals:
            if value:
                for variant in _variants(value):
                    variants[variant] = REDACTED
        masker = SecretMasker({}, filter_references=True)
        masker._compile(variants)
        return masker

    def stream_boundary(self, text: str, end: int) -> int:
        """A raw chunk boundary with enough lookahead to keep any match whole."""
        end = max(0, min(end, len(text) - self._longest))
        if self.filters_references:
            end = _reference_boundary(text, end)
        for start, stop, _ in self._spans(text):
            if start >= end:
                break
            if end < stop:
                return start
        return end

    def mask(self, text: str) -> str:
        if self._pattern is None or not text:
            return text
        if self._overlap_pattern is None or "{{" not in text:
            return self._pattern.sub(self._replacement, text)
        return self._replace_spans(text, self._spans(text))

    @staticmethod
    def _replace_spans(text: str, spans: Iterable[tuple[int, int, str]]) -> str:
        parts: list[str] = []
        position = 0
        for start, end, replacement in spans:
            parts.extend((text[position:start], replacement))
            position = end
        if not parts:
            return text
        parts.append(text[position:])
        return "".join(parts)

    def _spans(self, text: str) -> Iterator[tuple[int, int, str]]:
        if self._pattern is None:
            return
        if self._overlap_pattern is None or "{{" not in text:
            for match in self._pattern.finditer(text):
                yield match.start(), match.end(), self._replacement(match)
            return
        references = (
            (m.start(), m.end(), m[0] if self.allows_reference(m[1], m[2]) else REDACTED)
            for m in REFERENCE_PATTERN.finditer(text)
        )
        literals = (
            (m.start(), m.end(), self._literal_replacement(m[0]))
            for m in self._overlap_pattern.finditer(text)
        )
        start, end, replacement = 0, 0, ""
        for left, right, value in merge(references, literals, key=lambda span: span[0]):
            if left >= end:
                if end > start:
                    yield start, end, replacement
                start, end, replacement = left, right, value
            elif right > end:
                # For containment the outer span determines the reference;
                # partially overlapping spans have no unique substitution.
                replacement = value if left == start else REDACTED
                end = right
        if end > start:
            yield start, end, replacement

    def _literal_replacement(self, value: str) -> str:
        if self.filters_references:
            return REDACTED if value == REDACTED else self._local.get(value, REDACTED)
        return self._variants[value]

    def _replacement(self, match: re.Match[str]) -> str:
        if self.filters_references and match[1] is not None:
            return match[0] if self.allows_reference(match[2], match[3]) else REDACTED
        return self._literal_replacement(match[0])

    def mask_documentation(self, text: str) -> str:
        """Mask known values in static documentation while keeping public syntax examples."""
        if self._pattern is None:
            return text
        if self._overlap_pattern is not None and "{{" in text:
            return self._overlap_pattern.sub(lambda m: self._literal_replacement(m[0]), text)
        return self._pattern.sub(
            lambda match: (
                self._literal_replacement(match[0]) if match[0] in self._variants else match[0]
            ),
            text,
        )

    def mask_slice(self, text: str, start: int, end: int) -> str:
        """Project a slice of the original text using complete matches across both edges."""
        return self.slice_projector(text)(start, end)

    def guard_slice(self, text: str, start: int, end: int) -> str:
        """Keep whole matches for a later recipient and redact matches crossing a cut."""
        start, end, _ = slice(start, end).indices(len(text))
        if start == 0 and end == len(text):
            return text
        return self.slice_projector(text, preserve_whole=True)(start, end)

    @property
    def window_context(self) -> int:
        """Context for a cut in rendered text, counted in non-whitespace characters.

        A reference has at most 32 field, 64 name and 12 syntax characters.
        Its whitespace can span any number of retained Terminal rows.
        """
        return max(108, self._longest)

    def guard_rows(
        self, source: str, spans: list[tuple[int, int]], display: str, *, growing: bool
    ) -> str:
        """Keep Terminal row layout and styles except where they break protected text."""
        rows = display.split("\n")
        safe_end = self.growing_boundary(source) if growing else len(source)
        project = self.slice_projector(source, preserve_whole=True)
        for index, (left, right) in enumerate(spans):
            raw = source[left:right]
            guarded = project(left, min(right, safe_end))
            if guarded != raw or (rows[index] != raw and next(self._spans(raw), None) is not None):
                rows[index] = guarded
        return "\n".join(rows)

    def guard_layout(self, text: str) -> str:
        """Preserve matches except those whitespace folding or quoting would split."""
        if self._pattern is None:
            return text
        return self._replace_spans(
            text,
            (
                (start, end, REDACTED)
                for start, end, _ in self._spans(text)
                if " ".join(text[start:end].split()) != text[start:end]
            ),
        )

    def slice_projector(
        self, text: str, *, preserve_whole: bool = False
    ) -> Callable[[int, int], str]:
        """Find protected spans once for any number of overlapping original-text windows."""
        changes: list[tuple[int, int, str]] = []
        ends: list[int] = []
        for start, end, replacement in self._spans(text):
            # Even an allowed reference stays whole, so a later reader
            # with a different grant can still recognize and project it.
            changes.append((start, end, text[start:end] if preserve_whole else replacement))
            ends.append(end)
        if not changes:
            return lambda start, end: text[start:end]

        def project(start: int, end: int) -> str:
            start, end, _ = slice(start, end).indices(len(text))
            if start >= end:
                return ""
            parts: list[str] = []
            position = start
            index = bisect_right(ends, start)
            while index < len(changes) and changes[index][0] < end:
                left, right, replacement = changes[index]
                if preserve_whole and (left < start or right > end):
                    # A marker must fit the original character budget.
                    replacement = (
                        REDACTED if min(right, end) - max(left, start) >= len(REDACTED) else ""
                    )
                parts.extend((text[position:left], replacement))
                position = right
                index += 1
            parts.append(text[position:end])
            return "".join(parts)

        return project

    def mask_growing(self, text: str) -> str:
        """Project a growing snapshot, withholding its incomplete Secret matches."""
        end = self.growing_boundary(text)
        if end == len(text):
            return self.mask(text)
        for left, right, replacement in self._spans(text):
            if left > end:
                break
            if left <= end < right:
                # A complete value can overlap a longer, unfinished one. Its
                # projected replacement is already safe for this recipient.
                return self.mask(text[:left]) + replacement
        return self.mask(text[:end])

    def growing_boundary(self, text: str) -> int:
        """End of retained original text before an incomplete reference or known value."""
        end = _reference_boundary(text, len(text)) if self.filters_references else len(text)
        end = min(end, self._value_prefix_boundary(text))
        for value in self._extended_prefixes:
            # The usual short, single-line check cannot see a prefix extending
            # over a newline or past its scan limit. Find only candidate starts.
            width = _MIN_HELD_PREFIX if "\n" in value else _GROWING_TAIL_LIMIT
            head = value[:width]
            pos = text.find(head, max(0, len(text) - len(value) + 1))
            while 0 <= pos < end:
                if value.startswith(text[pos:]):
                    end = pos
                    break
                pos = text.find(head, pos + 1)
        return end

    def _value_prefix_boundary(self, text: str) -> int:
        if not self._ordered:
            return len(text)
        start = max(text.rfind("\n") + 1, len(text) - _GROWING_TAIL_LIMIT)
        ordered = self._ordered
        for position in range(start, len(text) - _MIN_HELD_PREFIX + 1):
            tail = text[position:]
            index = bisect_left(ordered, tail)
            if index < len(ordered) and ordered[index] != tail and ordered[index].startswith(tail):
                return position
        return len(text)

    def mask_value[T](self, value: T, *, include_keys: bool = False) -> T:
        """``value`` (JSON-like) with every string masked; ``value`` itself when none matches."""
        if not self or not any(
            self.contains(text) for text in iter_strings(value, include_keys=include_keys)
        ):
            return value
        return cast(T, map_strings(value, self.mask, include_keys=include_keys))

    def contains(self, text: str) -> bool:
        if self._pattern is None:
            return False
        if not self.filters_references or ("{{" not in text and REDACTED not in text):
            return self._pattern.search(text) is not None
        return any(replacement != text[start:end] for start, end, replacement in self._spans(text))

    def mask_unbound[T](self, value: T) -> T:
        """Mask data leaving an operator action that has no Session to grant Secrets to."""
        from core.secrets.detection import detect_api_keys

        known_masked = self.mask_value(value, include_keys=True)
        found = {
            key.value: "[redacted API key]"
            for text in iter_strings(known_masked, include_keys=True)
            for key in detect_api_keys(text)
        }
        if not found:
            return known_masked
        return SecretMasker(found).mask_value(known_masked, include_keys=True)

    def matches_exactly(self, text: str) -> bool:
        """Whether the entire text is a known value or one of its masking variants."""
        return text in self._variants


EMPTY_MASKER: Final = SecretMasker({})


_UNFINISHED_REFERENCE = re.compile(
    r"\s*(?:[A-Za-z][A-Za-z0-9_]{0,63}(?:\.(?:[A-Za-z][A-Za-z0-9_]{0,31})?)?\s*\}?)?\Z"
)


def _reference_boundary(text: str, end: int) -> int:
    """Keep a possibly incomplete reference together across output chunks."""
    start = text.rfind("{{", 0, end)
    if start >= 0:
        tail = text[start + 2 : end].lstrip()
        if "secret:".startswith(tail) or (
            tail.startswith("secret:") and _UNFINISHED_REFERENCE.fullmatch(tail[7:])
        ):
            return start
    return end - 1 if end and text[end - 1] == "{" else end


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
    for encoded in (
        quote(value, safe=""),
        quote(value, safe=_URI_COMPONENT_SAFE),
        quote(value, safe=_URI_SAFE),
        quote_plus(value, safe=""),
    ):
        found.add(encoded)
        # Percent-encoding with lowercase hex digits (%2f).
        found.add(_PERCENT_ESCAPE.sub(lambda match: match.group(0).lower(), encoded))
    raw = value.encode("utf-8")
    for encode in (base64.b64encode, base64.urlsafe_b64encode):
        encoded = encode(raw).decode("ascii")
        found.update((encoded, encoded.rstrip("=")))
        for offset in range(3):
            fragment = _base64_fragment(encode, raw, offset)
            if len(fragment) >= MIN_MASKED_LENGTH:
                found.add(fragment)
    # GNU ``base64`` wraps its output at 76 characters: each line on its own.
    encoded_value = base64.b64encode(raw).decode("ascii")
    if len(encoded_value) > _BASE64_LINE:
        found.update(
            line
            for start in range(0, len(encoded_value), _BASE64_LINE)
            if len(line := encoded_value[start : start + _BASE64_LINE]) >= _MIN_MASKED_LINE
        )
    return found


def _base64_fragment(encode: Callable[[bytes], bytes], raw: bytes, offset: int) -> str:
    """The Base64 characters that depend only on ``raw`` when ``offset`` bytes precede it."""
    encoded = encode(b"\x00" * offset + raw).decode("ascii")
    start = -(-offset * 4 // 3)
    end = (offset + len(raw)) * 4 // 3
    return encoded[start:end]
