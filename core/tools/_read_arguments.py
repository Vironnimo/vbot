"""Read calls in other harnesses' shapes, turned into path, offset and limit.

Models trained on other agent harnesses send ``file_path``, ``start_line`` and
``end_line``, ``view_range: [10, 20]``, ``lines: "10-20"`` or ``tail: 50``. Each
shape names one exact line window, so it runs as that window. A one-file list
(``paths: ["a.py"]``, ``files: [{"path": ..., "line_ranges": [...]}]``) runs as
that file; notes such as ``explanation`` and a ``command: "view"`` request no
effect of their own and are dropped. Shapes that could mean different windows,
several files, or that contradict each other, fail with the calls to send
instead.
"""

from __future__ import annotations

import json
import re
from functools import cache
from typing import Any, TypeGuard

from core.tools._argument_repair import normalize_call_arguments
from core.tools._call_vocabulary import SpellingAliases, spelling
from core.tools.contracts import ToolContract, ToolContractError, compile_tool_contract
from core.tools.model_names import model_tool_name

READ_TOOL_NAME = "read"

# Accepted but not advertised: a line filter for calls that send search fields
# to read.
READ_HIDDEN_PARAMETERS: dict[str, Any] = {
    "pattern": {"type": "string", "minLength": 1},
    "ignore_case": {"type": "boolean"},
    "context": {"type": "integer", "minimum": 0},
}

# Fields that exist only while a call is translated; none reaches the handler.
_TRANSLATED_FIELDS: dict[str, Any] = {
    "end_line": {"type": "integer"},
    "tail": {"type": "integer"},
    "lines": {},
    "entire": {"type": "boolean"},
}

_FIELD_ALIASES = SpellingAliases(
    {
        "path": (
            "file_path",
            "file",
            "filename",
            "target_file",
            "absolute_path",
            "relative_path",
            "relative_workspace_path",
            "directory",
            "dir",
            "dir_path",
            "folder",
        ),
        "offset": (
            "start_line",
            "line_start",
            "from_line",
            "first_line",
            "start",
            "from",
            "begin",
            "line",
            "line_number",
            "lineno",
            "start_line_one_indexed",
        ),
        "limit": (
            "max_lines",
            "num_lines",
            "number_of_lines",
            "line_count",
            "lines_to_read",
            "count",
            "n",
            "length",
            "head",
        ),
        "end_line": (
            "line_end",
            "to_line",
            "last_line",
            "end",
            "to",
            "until",
            "end_line_inclusive",
            "end_line_one_indexed_inclusive",
        ),
        "lines": ("range", "line_range", "line_ranges", "view_range", "line_numbers"),
        "tail": ("last", "last_lines", "tail_lines"),
        "entire": ("should_read_entire_file", "read_entire_file", "entire_file", "whole_file"),
        "pattern": (
            "query",
            "regex",
            "regexp",
            "search",
            "grep",
            "search_pattern",
            "search_term",
            "filter",
        ),
        "ignore_case": ("i", "case_insensitive", "ignorecase", "insensitive", "nocase"),
        "context": ("context_lines", "surrounding_lines"),
    }
)

# Fields that only a search over many files uses; read names the search instead.
_SEARCH_FIELDS = SpellingAliases(
    {
        field: (field,)
        for field in ("glob", "include", "output_mode", "files_with_matches", "file_pattern")
    }
)

# Notes that come with a call and request no effect of their own.
_REMARKS = frozenset({"explanation", "description"})
# Lists of files to read; read shows one file per call.
_PATH_LISTS = frozenset({"paths", "filepaths", "files", "targetfiles", "absolutepaths"})

_POSITION = re.compile(r"\s*(\d+):(\d+)\s*")
_RANGE = re.compile(
    r"\s*L?(\d+)\s*(?:-|\.\.\.?|:|,|to)\s*(?:L?(\d+)|end|eof|\$)?\s*",
    re.IGNORECASE,
)


@cache
def _repair_contract() -> ToolContract:
    from core.tools.read import READ_TOOL_PARAMETERS

    schema = dict(READ_TOOL_PARAMETERS)
    schema["properties"] = {
        **READ_TOOL_PARAMETERS["properties"],
        **READ_HIDDEN_PARAMETERS,
        **_TRANSLATED_FIELDS,
    }
    return compile_tool_contract(
        name=READ_TOOL_NAME, input_schema=schema, require_closed_input=False
    )


def normalize_read_arguments(arguments: Any) -> Any:
    """Return read arguments with other harnesses' spellings translated."""
    if isinstance(arguments, dict):
        arguments = _one_file(_without_remarks(arguments))
    normalized = normalize_call_arguments(
        _repair_contract(),
        arguments,
        field_aliases=_FIELD_ALIASES,
        empty_as_omitted=("offset", "limit", "pattern", "context", "ignore_case"),
    )
    if not isinstance(normalized, dict):
        return normalized
    _reject_search_fields(normalized)
    return _translate_line_window(normalized)


def _without_remarks(arguments: dict[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in arguments.items() if spelling(key) not in _REMARKS}
    for key, value in arguments.items():
        name = spelling(key)
        if name == "includesummaryofotherlines" and value is False:
            del result[key]
        elif name == "command":
            if not isinstance(value, str) or spelling(value) != "view":
                raise ValueError(
                    f"read has no command {_literal(value)}: it shows the file or lists the "
                    "directory given as path. To change a file, call "
                    f"{model_tool_name('apply_patch')}."
                )
            del result[key]
    return result


def _one_file(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a call whose list of files names one file as that file's call."""
    lists = [
        key
        for key, value in arguments.items()
        if spelling(key) in _PATH_LISTS
        or (isinstance(value, list) and _FIELD_ALIASES.get(key, spelling(key)) == "path")
    ]
    result = dict(arguments)
    for key in lists:
        items = result.pop(key)
        items = items if isinstance(items, list) else [items]
        if len(items) > 1:
            calls = ", ".join(_read_call(_item_fields(item)) for item in items)
            raise ValueError(f"read shows one file per call. Send one call per file: {calls}.")
        for field, value in (_item_fields(items[0]) if items else {}).items():
            _set(result, field, value, field)
    return result


def _item_fields(item: Any) -> dict[str, Any]:
    return dict(item) if isinstance(item, dict) else {"path": item}


def _read_call(fields: dict[str, Any]) -> str:
    call = normalize_read_arguments(fields)
    rendered = ", ".join(
        f"{key}={_literal(call[key])}" for key in ("path", "offset", "limit") if key in call
    )
    return f"{model_tool_name(READ_TOOL_NAME)}({rendered})"


def _reject_search_fields(arguments: dict[str, Any]) -> None:
    fields = {key: value for key, value in arguments.items() if key in _SEARCH_FIELDS}
    if not fields:
        return
    call = {
        key: arguments[key] for key in ("pattern", "path") if arguments.get(key) not in (None, "")
    } | fields
    rendered = ", ".join(f"{key}={_literal(value)}" for key, value in call.items())
    raise ValueError(
        f"read shows one file or lists one directory and has no {', '.join(fields)} field. "
        f"To search files, call search_files({rendered})."
    )


def _translate_line_window(arguments: dict[str, Any]) -> dict[str, Any]:
    result = dict(arguments)
    entire = result.pop("entire", None)
    if entire is not None and not isinstance(entire, bool):
        raise ValueError("should_read_entire_file must be true or false.")
    if entire:
        # A whole-file read ignores line bounds, as in the harness that sends it.
        for field in ("offset", "limit", "lines", "end_line", "tail"):
            result.pop(field, None)

    offset = result.get("offset")
    if isinstance(offset, str):
        position = _POSITION.fullmatch(offset)
        if position is not None:
            if int(position[1]) < 1 or int(position[2]) < 1:
                raise ValueError(
                    f"offset {offset.strip()} is not a line:character position; lines and "
                    "characters count from 1."
                )
            result["offset"] = f"{int(position[1])}:{int(position[2])}"
        elif _RANGE.fullmatch(offset):
            _set(result, "lines", offset, "offset")
            del result["offset"]

    window = _line_range(result.pop("lines")) if "lines" in result else None
    end_line = _line_number(result.pop("end_line", None), "end_line")
    tail = _line_number(result.pop("tail", None), "tail")
    offset = result.get("offset")
    limit = result.get("limit")
    given_offset = offset if _is_int(offset) and offset != 0 else None

    if window is not None:
        window_start, window_end = window
        if given_offset is not None and given_offset != window_start:
            raise _conflict(f"offset={offset}", _window_call(window_start, window_end, limit))
        if end_line is not None and window_end is not None and end_line != window_end:
            raise _conflict(f"end_line={end_line}", _window_call(window_start, window_end, limit))
        given_offset = window_start
        if window_end is not None:
            end_line = window_end
        result["offset"] = window_start

    if end_line is not None:
        if given_offset is not None and given_offset < 0:
            raise ValueError(
                f"offset={given_offset} counts from the end, so end_line={end_line} cannot "
                f"close it. Send offset={given_offset} alone to read the last "
                f"{-given_offset} lines, or offset=<first line>, limit=<count>."
            )
        first = given_offset or 1
        if end_line < first:
            raise ValueError(
                f"The line range {first}-{end_line} ends before it starts. To read lines "
                f"{end_line}-{first}, send {_window_call(end_line, first)}."
            )
        count = end_line - first + 1
        if _is_int(limit) and limit > 0 and limit != count:
            raise _conflict(
                f"limit={limit} (lines {first}-{first + limit - 1})",
                f"{_window_call(first, end_line)} (lines {first}-{end_line})",
            )
        result["offset"] = first
        result["limit"] = count

    if tail is not None:
        if given_offset is not None and given_offset != -tail:
            raise _conflict(f"offset={given_offset}", f"offset={-tail} (the last {tail} lines)")
        result["offset"] = -tail

    if result.get("offset") in (0, 1) and "offset" in result:
        # Line 1 is where every read starts; 0 is the same start counted from 0.
        del result["offset"]
    if _is_int(limit) and limit <= 0 and result.get("limit") == limit:
        # 0 or a negative count asks for no bound; the default bound still applies.
        del result["limit"]
    return result


def _line_range(value: Any) -> tuple[int, int | None]:
    """Return the first and last line of a range such as "10-20" or [10, 20]."""
    if isinstance(value, list) and value and all(map(_is_range, value)):
        if len(value) == 1:
            return _line_range(value[0])
        calls = "; ".join(_window_call(*_line_range(item)) for item in value)
        raise ValueError(
            f"lines={_literal(value)} names {len(value)} line ranges, and read shows one "
            f"range per call. Send one call per range: {calls}."
        )
    bounds: list[Any]
    if isinstance(value, dict):
        bounds = [
            _first_value(value, ("start", "from", "first")),
            _first_value(value, ("end", "to", "last")),
        ]
    elif isinstance(value, list):
        bounds = list(value)
    elif isinstance(value, str) and (match := _RANGE.fullmatch(value)):
        bounds = [int(match[1]), int(match[2]) if match[2] else None]
    else:
        bounds = [value]
    if len(bounds) == 1 and _line_number(bounds[0], "lines", required=False) is not None:
        number = _line_number(bounds[0], "lines")
        raise ValueError(
            f"lines={_literal(value)} could mean the first {number} lines or line {number} "
            f"alone. Send limit={number} for the first {number} lines, or offset={number}, "
            "limit=1 for that line."
        )
    if len(bounds) != 2:
        raise ValueError(
            f"lines={_literal(value)} is not a line range. Send offset=<first line> and "
            "limit=<count>, for example offset=10, limit=11 for lines 10-20."
        )
    first = _line_number(bounds[0], "lines")
    last_value = bounds[1]
    if last_value is None or (_is_int(last_value) and last_value == -1):
        return first or 1, None
    last = _line_number(last_value, "lines")
    return first or 1, last


def _is_range(value: Any) -> bool:
    """Return whether a list item is itself a whole range, not one bound of a range."""
    return isinstance(value, (list, dict)) or (
        isinstance(value, str) and _RANGE.fullmatch(value) is not None
    )


def _first_value(value: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in value:
            return value[key]
    return None


def _line_number(value: Any, field: str, *, required: bool = True) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if _is_int(value) and value >= 0:
        return value if value > 0 else 1
    if not required:
        return None
    raise ValueError(f"{field} must be a line number counting from 1, not {_literal(value)}.")


def _is_int(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _set(result: dict[str, Any], field: str, value: Any, source: str) -> None:
    if field in result and result[field] != value:
        raise ToolContractError(f"Conflicting values for {source}; provide one intended value.")
    result[field] = value


def _window_call(first: int, last: int | None, limit: Any = None) -> str:
    if last is None:
        return f"offset={first}" + (f", limit={limit}" if _is_int(limit) and limit > 0 else "")
    return f"offset={first}, limit={last - first + 1}"


def _conflict(first: str, second: str) -> ValueError:
    return ValueError(
        f"The requested line windows disagree: {first} or {second}. Send one of them."
    )


def _literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


__all__ = ["READ_HIDDEN_PARAMETERS", "normalize_read_arguments"]
