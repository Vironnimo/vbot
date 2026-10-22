"""What a timeline Tool row carries of its call, and what it leaves for a raw read.

A Tool row is presentation: its display, status and times, plus bounded previews
of the call's arguments and result that hold the small fields rows are built from
(a command's id and status, a Sub-Agent's Session, a speech artifact). The
complete arguments and result stay with the projection and reach a client only
when it asks for the raw call. Detail blocks that point into the arguments or
result are resolved here, so a client never needs the full values to show them.
"""

from __future__ import annotations

import copy
import json
from typing import Any

JsonObject = dict[str, Any]

# A preview keeps every field, but each string to this many characters, each
# list to this many entries and nesting to this depth.
PREVIEW_STRING_LENGTH = 1_000
PREVIEW_LIST_LENGTH = 20
PREVIEW_DEPTH = 6
# A resolved detail text, like the Tool display's own text blocks
# (`core.tools._tool_display.MAX_TOOL_DISPLAY_TEXT_LENGTH`).
DETAIL_TEXT_LENGTH = 16_384
# Texts whose newest lines matter most keep their end when shortened.
_TAIL_TEXT_LABELS = frozenset({"output", "screen", "scrollback"})
_TRUNCATED = "…"


def bounded_preview(value: Any) -> tuple[Any, bool]:
    """Return a bounded copy of *value* and whether anything was left out.

    A JSON string is previewed as the value it holds, so a persisted result
    reads like a live one.
    """
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = None
        if isinstance(decoded, (dict, list)):
            value = decoded
    return _bounded(value, PREVIEW_DEPTH)


def _bounded(value: Any, depth: int) -> tuple[Any, bool]:
    if isinstance(value, str):
        if len(value) <= PREVIEW_STRING_LENGTH:
            return value, False
        return value[: PREVIEW_STRING_LENGTH - 1] + _TRUNCATED, True
    if isinstance(value, dict):
        if depth <= 0:
            return {}, bool(value)
        result: JsonObject = {}
        cut = False
        for key, entry in value.items():
            result[key], entry_cut = _bounded(entry, depth - 1)
            cut = cut or entry_cut
        return result, cut
    if isinstance(value, list):
        if depth <= 0:
            return [], bool(value)
        entries = []
        cut = len(value) > PREVIEW_LIST_LENGTH
        for entry in value[:PREVIEW_LIST_LENGTH]:
            bounded, entry_cut = _bounded(entry, depth - 1)
            entries.append(bounded)
            cut = cut or entry_cut
        return entries, cut
    return copy.deepcopy(value), False


def presented_display(display: Any, arguments: Any, result: Any) -> Any:
    """Return *display* with each detail text that names a source resolved.

    A source that holds no text drops its block, as the WebUI did before.
    """
    if not isinstance(display, dict) or not isinstance(display.get("details"), list):
        return copy.deepcopy(display)
    presented = copy.deepcopy(display)
    blocks = []
    for block in presented["details"]:
        if isinstance(block, dict) and block.get("type") == "text" and "source" in block:
            text = _source_text(block["source"], arguments, result)
            if not text:
                continue
            block = {key: value for key, value in block.items() if key != "source"}
            block["text"], cut = _detail_text(text, tail=block.get("label") in _TAIL_TEXT_LABELS)
            if cut:
                block["truncated"] = True
        blocks.append(block)
    presented["details"] = blocks
    return presented


def merged_secrets_used(previous: Any, display: Any) -> Any:
    """Keep the Secrets an earlier display of the call named in its newer one."""
    if not isinstance(display, dict) or not isinstance(previous, dict):
        return display
    earlier = previous.get("secrets_used")
    if not isinstance(earlier, list) or not earlier:
        return display
    later = display.get("secrets_used")
    if not isinstance(later, list):
        later = []
    merged = list(dict.fromkeys([*earlier, *later]))
    return {**display, "secrets_used": merged} if merged != later else display


def _source_text(source: Any, arguments: Any, result: Any) -> str:
    if not isinstance(source, dict) or not isinstance(source.get("path"), list):
        return ""
    origin = source.get("from")
    value = _decoded(arguments if origin == "arguments" else result if origin == "result" else None)
    for key in source["path"]:
        if not isinstance(key, str) or not isinstance(value, dict):
            return ""
        value = value.get(key)
    return value if isinstance(value, str) and value.strip() else ""


def _decoded(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _detail_text(text: str, *, tail: bool) -> tuple[str, bool]:
    if len(text) <= DETAIL_TEXT_LENGTH:
        return text, False
    if tail:
        return _TRUNCATED + text[-(DETAIL_TEXT_LENGTH - 1) :], True
    return text[: DETAIL_TEXT_LENGTH - 1] + _TRUNCATED, True
