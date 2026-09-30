"""Tool row presentation metadata and normalization."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from core.tools.contracts import JsonObject
from core.utils.paths import model_path

if TYPE_CHECKING:
    from core.tools._tool_context import ToolContext

ToolSummaryBuilder = Callable[[JsonObject], str | None]
ToolDisplayFactBuilder = Callable[[JsonObject, JsonObject | None], Sequence[JsonObject]]
# (arguments, result or None while the call runs) -> detail blocks
ToolDisplayDetailBuilder = Callable[[JsonObject, JsonObject | None], Sequence[JsonObject]]
MAX_TOOL_DISPLAY_SUMMARY_LENGTH = 120
MAX_TOOL_DISPLAY_VALUE_LENGTH = 8192
DEFAULT_TOOL_DISPLAY_MAX_CHARACTERS = 64
TOOL_DISPLAY_VALUE_KINDS = frozenset(
    {"command", "description", "identifier", "path", "query", "text", "url"}
)
TOOL_DISPLAY_TRUNCATION_MODES = frozenset({"start", "end", "middle", "never"})
TOOL_DISPLAY_TOOLTIP_MODES = frozenset({"always", "none", "truncated"})
TOOL_DISPLAY_FACT_UNITS = frozenset({"edits", "failures", "files", "matches", "results"})
TOOL_DISPLAY_LINE_CHANGES = frozenset({"added", "removed"})
TOOL_DISPLAY_NOTICE_LEVELS = frozenset({"info", "warning", "error"})
TOOL_DISPLAY_TEXT_LABELS = frozenset({"command", "output", "page", "query", "results"})
TOOL_DISPLAY_TEXT_SOURCES = frozenset({"arguments", "result"})
MAX_TOOL_DISPLAY_TEXT_LENGTH = 16_384
MAX_TOOL_DISPLAY_RESULTS = 20
MAX_TOOL_DISPLAY_RESULT_TEXT_LENGTH = 600
TOOL_DISPLAY_RESULT_FIELDS = ("meta", "time", "text")
MAX_TOOL_DISPLAY_RESULT_URL_LENGTH = 2048
TOOL_DISPLAY_MEMORY_SCOPES = frozenset({"agent", "user"})
TOOL_DISPLAY_MEMORY_CHANGE_OPS = frozenset({"added", "removed", "replaced"})


@dataclass(frozen=True)
class ToolDisplayField:
    """One ordered argument candidate for a Tool row's flexible primary value."""

    argument_key: str
    kind: str = "text"
    truncate: str = "end"
    tooltip: str = "truncated"
    quote: bool = False
    copyable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.argument_key, str) or not self.argument_key:
            raise ValueError("Tool display argument_key must be a non-empty string")
        if self.kind not in TOOL_DISPLAY_VALUE_KINDS:
            raise ValueError(f"Unsupported Tool display value kind: {self.kind}")
        if self.truncate not in TOOL_DISPLAY_TRUNCATION_MODES:
            raise ValueError(f"Unsupported Tool display truncation mode: {self.truncate}")
        if self.tooltip not in TOOL_DISPLAY_TOOLTIP_MODES:
            raise ValueError(f"Unsupported Tool display tooltip mode: {self.tooltip}")
        if not isinstance(self.quote, bool) or not isinstance(self.copyable, bool):
            raise ValueError("Tool display quote and copyable flags must be booleans")


@dataclass(frozen=True)
class ToolDisplayPart:
    """One computed semantic value returned by a Tool-specific row builder.

    `detail` is the complete value a shorter part stands for, such as the
    command behind a Bash description: the row shows the part, while the value
    card shows the part as its heading and offers `detail` (of `detail_kind`)
    in full for copying.
    """

    value: str
    kind: str = "text"
    truncate: str = "end"
    tooltip: str = "truncated"
    full_value: str | None = None
    quote: bool = False
    copyable: bool = False
    detail: str | None = None
    detail_kind: str = "text"

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("Tool display part value must be a non-empty string")
        if self.full_value is not None and not isinstance(self.full_value, str):
            raise ValueError("Tool display part full_value must be a string or None")
        if self.detail is not None and not isinstance(self.detail, str):
            raise ValueError("Tool display part detail must be a string or None")
        if self.kind not in TOOL_DISPLAY_VALUE_KINDS:
            raise ValueError(f"Unsupported Tool display value kind: {self.kind}")
        if self.detail_kind not in TOOL_DISPLAY_VALUE_KINDS:
            raise ValueError(f"Unsupported Tool display detail kind: {self.detail_kind}")
        if self.truncate not in TOOL_DISPLAY_TRUNCATION_MODES:
            raise ValueError(f"Unsupported Tool display truncation mode: {self.truncate}")
        if self.tooltip not in TOOL_DISPLAY_TOOLTIP_MODES:
            raise ValueError(f"Unsupported Tool display tooltip mode: {self.tooltip}")
        if not isinstance(self.quote, bool) or not isinstance(self.copyable, bool):
            raise ValueError("Tool display quote and copyable flags must be booleans")


ToolDisplayPartBuilder = Callable[[JsonObject], Sequence[ToolDisplayPart]]


def result_count_fact_builder(
    data_field: str,
    *,
    when_arguments: Mapping[str, Any] | None = None,
    at_least_field: str | None = None,
    unit: str = "results",
) -> ToolDisplayFactBuilder:
    """Build a UI-only count fact from one successful Tool result field."""
    if not isinstance(data_field, str) or not data_field:
        raise ValueError("Tool display result count data_field must be a non-empty string")
    if at_least_field is not None and (not isinstance(at_least_field, str) or not at_least_field):
        raise ValueError("Tool display result count at_least_field must be a non-empty string")
    if unit not in TOOL_DISPLAY_FACT_UNITS:
        raise ValueError(f"Unsupported Tool display fact unit: {unit}")
    conditions = dict(when_arguments or {})
    if not all(isinstance(key, str) and key for key in conditions):
        raise ValueError("Tool display result count conditions must use non-empty string keys")

    def build(arguments: JsonObject, result: JsonObject | None) -> Sequence[JsonObject]:
        if any(arguments.get(key) != expected for key, expected in conditions.items()):
            return ()
        if not isinstance(result, dict) or result.get("ok") is not True:
            return ()
        data = result.get("data")
        if not isinstance(data, dict):
            return ()
        raw_count = data.get(data_field)
        if isinstance(raw_count, list):
            count = len(raw_count)
        elif isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 0:
            count = raw_count
        else:
            return ()
        return (
            {
                "kind": "count",
                "value": count,
                "unit": unit,
                "at_least": (
                    count > 0 and data.get(at_least_field) is True if at_least_field else False
                ),
            },
        )

    return build


@dataclass(frozen=True)
class ToolDisplay:
    """Presentation metadata for one tool invocation."""

    summary_fields: Sequence[str] = ()
    hidden_argument_keys: Sequence[str] = field(default_factory=tuple)
    summary_builder: ToolSummaryBuilder | None = None
    summary_separator: str = " · "
    primary_candidates: Sequence[ToolDisplayField] = ()
    secondary_fields: Sequence[ToolDisplayField] = ()
    parts_builder: ToolDisplayPartBuilder | None = None
    fact_builder: ToolDisplayFactBuilder | None = None
    max_characters: int = DEFAULT_TOOL_DISPLAY_MAX_CHARACTERS
    # The Tool shows user-facing detail blocks instead of the raw arguments and
    # result: built from the call by ``detail_builder``, then recorded during
    # execution (``ToolContext.add_display_file_change``/``add_display_notice``/
    # ``add_display_text``/``add_display_memory_changes``).
    details: bool = False
    detail_builder: ToolDisplayDetailBuilder | None = None

    def __post_init__(self) -> None:
        _validate_display_strings(self.summary_fields, "summary_fields")
        _validate_display_strings(self.hidden_argument_keys, "hidden_argument_keys")
        if self.summary_builder is not None and not callable(self.summary_builder):
            raise ValueError("Tool display summary_builder must be callable")
        if self.parts_builder is not None and not callable(self.parts_builder):
            raise ValueError("Tool display parts_builder must be callable")
        if self.fact_builder is not None and not callable(self.fact_builder):
            raise ValueError("Tool display fact_builder must be callable")
        if not isinstance(self.details, bool):
            raise ValueError("Tool display details must be a boolean")
        if self.detail_builder is not None and not callable(self.detail_builder):
            raise ValueError("Tool display detail_builder must be callable")
        if isinstance(self.max_characters, bool) or not isinstance(self.max_characters, int):
            raise ValueError("Tool display max_characters must be an integer")
        if self.max_characters <= 0:
            raise ValueError("Tool display max_characters must be positive")
        for field_name, values in (
            ("primary_candidates", self.primary_candidates),
            ("secondary_fields", self.secondary_fields),
        ):
            if not all(isinstance(value, ToolDisplayField) for value in values):
                raise ValueError(f"Tool display {field_name} must contain ToolDisplayField values")
        object.__setattr__(self, "summary_fields", tuple(self.summary_fields))
        object.__setattr__(self, "hidden_argument_keys", tuple(self.hidden_argument_keys))
        object.__setattr__(self, "primary_candidates", tuple(self.primary_candidates))
        object.__setattr__(self, "secondary_fields", tuple(self.secondary_fields))

    def to_payload(
        self,
        arguments: Any,
        *,
        context: ToolContext | None = None,
        result: JsonObject | None = None,
        facts: Sequence[JsonObject] = (),
    ) -> JsonObject:
        """Return the UI-safe display payload for one concrete invocation."""
        primary = self._primary_payload(arguments, context=context)
        payload: JsonObject = {
            "version": 1,
            "summary": self._payload_summary(arguments, primary),
            "hidden_argument_keys": sorted(self.hidden_argument_keys),
            "primary": primary,
            "facts": self._fact_payload(arguments, result=result, facts=facts),
        }
        if self.details or self.detail_builder is not None:
            payload["details"] = self._detail_payload(arguments, result=result)
        return payload

    def _detail_payload(self, arguments: Any, *, result: JsonObject | None) -> list[JsonObject]:
        if self.detail_builder is None or not isinstance(arguments, dict):
            return []
        blocks = list(self.detail_builder(arguments, result))
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") not in {
                "notice",
                "results",
                "text",
            }:
                raise ValueError(
                    "Tool display detail_builder must return notice, results or text blocks"
                )
        return blocks

    def _primary_payload(
        self,
        arguments: Any,
        *,
        context: ToolContext | None,
    ) -> list[JsonObject]:
        if not isinstance(arguments, dict):
            return []

        if self.parts_builder is not None:
            computed_parts = self.parts_builder(arguments)
            if isinstance(computed_parts, (str, bytes)) or not isinstance(computed_parts, Sequence):
                raise ValueError("Tool display parts_builder must return a sequence")
            if not all(isinstance(part, ToolDisplayPart) for part in computed_parts):
                raise ValueError("Tool display parts_builder must return ToolDisplayPart values")
            return [self._part_payload(part, context=context) for part in computed_parts[:2]]

        parts: list[JsonObject] = []
        for candidate in self.primary_candidates:
            value = _display_argument_value(arguments.get(candidate.argument_key))
            if not value:
                continue
            parts.append(self._field_payload(candidate, value, context=context))
            break
        for configured_field in self.secondary_fields:
            value = _display_argument_value(arguments.get(configured_field.argument_key))
            if value:
                parts.append(self._field_payload(configured_field, value, context=context))
        if parts:
            return parts[:2]

        summary = self.summary(arguments)
        if not summary:
            return []
        return [
            {
                "kind": "text",
                "value": summary,
                "full_value": summary,
                "truncate": "end",
                "tooltip": "truncated",
                "max_characters": self.max_characters,
                "quote": False,
                "copyable": False,
            }
        ]

    def _field_payload(
        self,
        configured_field: ToolDisplayField,
        value: str,
        *,
        context: ToolContext | None,
    ) -> JsonObject:
        return self._part_payload(
            ToolDisplayPart(
                value=value,
                kind=configured_field.kind,
                truncate=configured_field.truncate,
                tooltip=configured_field.tooltip,
                quote=configured_field.quote,
                copyable=configured_field.copyable,
            ),
            context=context,
        )

    def _part_payload(
        self,
        configured_part: ToolDisplayPart,
        *,
        context: ToolContext | None,
    ) -> JsonObject:
        visible_value = (
            model_path(configured_part.value)
            if configured_part.kind == "path"
            else configured_part.value
        )
        full_value = configured_part.full_value or visible_value
        if configured_part.kind == "path" and context is not None:
            try:
                full_value = model_path(context.resolve_path(full_value))
            except (OSError, RuntimeError, ValueError):
                full_value = visible_value
        payload: JsonObject = {
            "kind": configured_part.kind,
            "value": _normalize_display_value(visible_value),
            "full_value": _normalize_display_value(full_value),
            "truncate": configured_part.truncate,
            "tooltip": configured_part.tooltip,
            "max_characters": self.max_characters,
            "quote": configured_part.quote,
            "copyable": configured_part.copyable,
        }
        detail = _normalize_display_value(configured_part.detail)
        if detail:
            payload["detail"] = detail
            payload["detail_kind"] = configured_part.detail_kind
        return payload

    def _payload_summary(self, arguments: Any, primary: Sequence[JsonObject]) -> str:
        if primary:
            return _normalize_display_summary(
                self.summary_separator.join(
                    str(part.get("value", "")) for part in primary if part.get("value")
                )
            )
        return self.summary(arguments)

    def _fact_payload(
        self,
        arguments: Any,
        *,
        result: JsonObject | None,
        facts: Sequence[JsonObject],
    ) -> list[JsonObject]:
        raw_facts = list(facts)
        if self.fact_builder is not None and isinstance(arguments, dict):
            raw_facts.extend(self.fact_builder(arguments, result))
        normalized: list[JsonObject] = []
        for fact in raw_facts:
            prepared = _normalize_display_fact(fact)
            if prepared is not None:
                normalized.append(prepared)
        return normalized

    def summary(self, arguments: Any) -> str:
        """Return a compact display summary, or an empty string when none applies."""
        if not isinstance(arguments, dict):
            return ""

        if self.summary_builder is not None:
            built_summary = _normalize_display_summary(self.summary_builder(arguments))
            if built_summary:
                return built_summary

        if self.parts_builder is not None:
            computed_parts = self.parts_builder(arguments)
            if isinstance(computed_parts, Sequence) and not isinstance(
                computed_parts, (str, bytes)
            ):
                built_summary = _normalize_display_summary(
                    self.summary_separator.join(
                        part.value
                        for part in computed_parts[:2]
                        if isinstance(part, ToolDisplayPart)
                    )
                )
                if built_summary:
                    return built_summary

        parts = [
            value.strip()
            for field_name in self.summary_fields
            if isinstance((value := arguments.get(field_name)), str) and value.strip()
        ]
        return _normalize_display_summary(self.summary_separator.join(parts))


def _validate_display_strings(values: Sequence[str], field_name: str) -> None:
    if isinstance(values, str) or not all(isinstance(value, str) and value for value in values):
        raise ValueError(f"Tool display {field_name} must contain non-empty strings")


def _normalize_display_summary(value: str | None) -> str:
    if not isinstance(value, str):
        return ""

    text = value.strip()
    if len(text) <= MAX_TOOL_DISPLAY_SUMMARY_LENGTH:
        return text

    return f"{text[: MAX_TOOL_DISPLAY_SUMMARY_LENGTH - 3]}..."


def _display_argument_value(value: Any) -> str:
    if isinstance(value, str):
        return _normalize_display_value(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def _normalize_display_value(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if len(text) <= MAX_TOOL_DISPLAY_VALUE_LENGTH:
        return text
    return f"{text[: MAX_TOOL_DISPLAY_VALUE_LENGTH - 1]}…"


def display_notice(level: str, text: str, *, subject: str | None = None) -> JsonObject:
    """Return one detail notice block: a short message for the user about the call.

    ``subject`` names what the notice is about, such as a file or one change of it.
    """
    if level not in TOOL_DISPLAY_NOTICE_LEVELS:
        raise ValueError(f"Unsupported Tool display notice level: {level}")
    shown = _normalize_display_value(text)
    if not shown:
        raise ValueError("Tool display notice text must be a non-empty string")
    block: JsonObject = {"type": "notice", "level": level, "text": shown}
    subject_text = _normalize_display_value(subject)
    if subject_text:
        block["subject"] = subject_text
    return block


def display_text(
    label: str,
    *,
    text: str | None = None,
    source: str | None = None,
    path: Sequence[str] = (),
) -> JsonObject:
    """Return one text detail block, shown as a labelled code box.

    The text is either ``text`` itself, or the string at ``path`` inside the
    call's ``arguments`` or its ``result`` envelope (``source``), which the
    display then need not copy. A missing or empty value shows no block.
    """
    if label not in TOOL_DISPLAY_TEXT_LABELS:
        raise ValueError(f"Unsupported Tool display text label: {label}")
    block: JsonObject = {"type": "text", "label": label}
    if (text is None) == (source is None):
        raise ValueError("Tool display text needs either text or a source")
    if source is not None:
        keys = list(path)
        if source not in TOOL_DISPLAY_TEXT_SOURCES or not keys:
            raise ValueError("Tool display text source needs arguments or result and a path")
        if not all(isinstance(key, str) and key for key in keys):
            raise ValueError("Tool display text source path must hold non-empty strings")
        block["source"] = {"from": source, "path": keys}
        return block
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Tool display text must be a non-empty string")
    if len(text) > MAX_TOOL_DISPLAY_TEXT_LENGTH:
        text = f"{text[: MAX_TOOL_DISPLAY_TEXT_LENGTH - 1]}…"
    block["text"] = text
    return block


def _display_url(value: str | None) -> str:
    """Return an absolute http(s) URL unchanged, or ``""`` for anything else."""
    if not isinstance(value, str) or len(value) > MAX_TOOL_DISPLAY_RESULT_URL_LENGTH:
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc or value != value.strip():
        return ""
    return value


def display_results(items: Sequence[Mapping[str, str | None]]) -> JsonObject:
    """Return one results detail block: the things a call found, as a list.

    Each item has a ``title`` and optionally ``url`` (a web address the title
    links to), ``meta`` (a short description such as its kind), ``time`` (an
    ISO-8601 moment the WebUI shows in local time) and ``text`` (an excerpt, cut
    at 600 characters). Items without a title, empty fields and addresses that
    are not absolute http(s) URLs of at most 2048 characters are left out; at
    most 20 items are kept.
    """
    shown: list[JsonObject] = []
    for item in items:
        title = _normalize_display_value(item.get("title"))
        if not title:
            continue
        entry: JsonObject = {"title": title}
        url = _display_url(item.get("url"))
        if url:
            entry["url"] = url
        for key in TOOL_DISPLAY_RESULT_FIELDS:
            value = _normalize_display_value(item.get(key))
            if key == "text" and len(value) > MAX_TOOL_DISPLAY_RESULT_TEXT_LENGTH:
                value = f"{value[: MAX_TOOL_DISPLAY_RESULT_TEXT_LENGTH - 1]}…"
            if value:
                entry[key] = value
        shown.append(entry)
        if len(shown) == MAX_TOOL_DISPLAY_RESULTS:
            break
    return {"type": "results", "items": shown}


def display_memory_changes(
    scope: str, changes: Sequence[Mapping[str, str | None]], *, revision: int | None = None
) -> JsonObject:
    """Return one memory_changes detail block: the entries a call changed in one Memory scope.

    Each change is ``added`` or ``removed`` with its ``text``, or ``replaced``
    with the ``previous`` text and the new ``text``. ``revision`` is the Memory
    history revision that recorded the change, which the user can refer to.
    """
    if scope not in TOOL_DISPLAY_MEMORY_SCOPES:
        raise ValueError(f"Unsupported Tool display Memory scope: {scope}")
    shown: list[JsonObject] = []
    for change in changes:
        op = change.get("op")
        text = _normalize_display_value(change.get("text"))
        previous = _normalize_display_value(change.get("previous"))
        if (
            op not in TOOL_DISPLAY_MEMORY_CHANGE_OPS
            or not text
            or (op == "replaced") != bool(previous)
        ):
            raise ValueError("Tool display Memory change needs op and text, and previous text")
        entry: JsonObject = {"op": op, "text": text}
        if previous:
            entry["previous"] = previous
        shown.append(entry)
    if not shown:
        raise ValueError("Tool display Memory changes must not be empty")
    block: JsonObject = {"type": "memory_changes", "scope": scope, "changes": shown}
    if revision is not None:
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("Tool display Memory revision must be a positive integer")
        block["revision"] = revision
    return block


def _normalize_display_fact(value: Any) -> JsonObject | None:
    if not isinstance(value, dict):
        return None
    kind = value.get("kind")
    if kind == "line_range":
        start = value.get("start")
        end = value.get("end")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or start < 1
            or isinstance(end, bool)
            or not isinstance(end, int)
            or end < start
        ):
            return None
        return {"kind": "line_range", "start": start, "end": end}
    if kind == "line_change":
        count = value.get("value")
        change = value.get("change")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or change not in TOOL_DISPLAY_LINE_CHANGES
        ):
            return None
        return {"kind": "line_change", "change": change, "value": count}
    if kind != "count":
        return None
    count = value.get("value")
    unit = value.get("unit")
    at_least = value.get("at_least", False)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    if unit not in TOOL_DISPLAY_FACT_UNITS or not isinstance(at_least, bool):
        return None
    return {
        "kind": "count",
        "value": count,
        "unit": unit,
        "at_least": at_least,
    }
