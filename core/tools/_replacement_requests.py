"""Shared replacement fields and operation construction for the file edit Tools."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from core.tools._patch_syntax import _Hunk, _Operation, _Replacement
from core.tools.call_syntax import SpellingAliases, spelling
from core.tools.tools import JsonObject
from core.utils.line_endings import dominant_line_ending

REPLACEMENT_FIELD_NAMES = {
    "path": (
        "file_path",
        "file",
        "filename",
        "target_file",
        "absolute_path",
        "relative_path",
        "relative_workspace_path",
    ),
    "old_string": ("old_str", "old_text", "old", "search", "find", "original"),
    "new_string": ("new_str", "new_text", "new", "replace", "replacement", "replace_with"),
    "replace_all": ("all", "replace_all_occurrences", "global"),
}
REPLACEMENT_ALIASES = SpellingAliases(REPLACEMENT_FIELD_NAMES)


def replacement_field(key: str) -> str:
    """Return the canonical replacement field a spelling names, or the key itself."""
    if key in REPLACEMENT_FIELD_NAMES:
        return key
    return REPLACEMENT_ALIASES.get(
        key,
        next((name for name in REPLACEMENT_FIELD_NAMES if spelling(name) == spelling(key)), key),
    )


def replacement_fields(
    item: JsonObject,
    number: int,
    *,
    aliases: Mapping[str, str] = REPLACEMENT_ALIASES,
    empty_as_omitted: Collection[str] = ("replace_all",),
) -> JsonObject:
    """Canonicalize one item's fields, retaining unknown fields for its Tool's policy."""
    edit: JsonObject = {}
    for key, value in item.items():
        field = replacement_field(key)
        if field == key and key not in REPLACEMENT_FIELD_NAMES:
            field = aliases.get(key, key)
        if field in empty_as_omitted and value in (None, ""):
            continue
        if field in edit and edit[field] != value:
            raise ValueError(
                f"edits item {number} gives {field} twice with different values; send one."
            )
        edit[field] = value
    return edit


def content_operation(
    path: str, content: str, *, only_if_empty: bool = False, label: str = ""
) -> _Operation:
    """Create or replace a file, preserving its final break and the content's style."""
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    newline = dominant_line_ending(content) or "\n"
    hunks = []
    if text:
        lines = text.split("\n")
        no_newline = lines[-1] != ""
        if not no_newline:
            lines.pop()
        hunks = [_Hunk(lines=[("+", line) for line in lines], no_newline=no_newline)]
    return _Operation(
        "add",
        path,
        hunks=hunks,
        only_if_empty=only_if_empty,
        newline=newline,
        label=label,
        keeps_final_break=True,
    )


def replacement_operation(
    path: str, edit: Mapping[str, Any], *, label: str = "", precise_only: bool = False
) -> _Operation:
    """Build one replacement or empty-file creation; callers own batching and failures."""
    if edit["old_string"] == "":
        return content_operation(path, edit["new_string"], only_if_empty=True, label=label)
    return _Operation(
        "update",
        path,
        hunks=[
            _Hunk(
                replacement=_Replacement(
                    edit["old_string"], edit["new_string"], edit.get("replace_all", False)
                ),
                label=label,
                precise_only=precise_only,
            )
        ],
    )
