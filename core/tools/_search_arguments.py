"""Normalize file-search call dialects into one SearchQuery."""

from __future__ import annotations

import contextlib
import json
import re
import shlex
from functools import cache
from typing import Any

from core.tools._argument_repair import load_json_value
from core.tools._search_definition import SEARCH_FILES_TOOL_NAME, SEARCH_FILES_TOOL_PARAMETERS
from core.tools._search_query import (
    MAX_LIMIT,
    MAX_OFFSET,
    SearchArgumentError,
    SearchQuery,
    flag_list,
    interpret,
)
from core.tools.arguments import optional_int
from core.tools.call_syntax import SpellingAliases as _SpellingAliases
from core.tools.call_syntax import normalize_call_arguments
from core.tools.call_syntax import spelling as _spelling
from core.tools.contracts import ToolContractError, compile_tool_contract
from core.tools.tools import (
    JsonObject,
)


def _repair_argument_array(value: Any) -> Any:
    """Decode list syntax without turning a malformed list into a regex class.

    A string that starts with an option is a command line such as
    "-F computeDamage( src"; it splits like one, with quotes grouping words.
    Backslashes would be shell escapes there but regex escapes here, so such a
    string stays one argument. Flags that broken list syntax wraps, such as
    '--files"]' or "['--files']", are those flags (``flag_list``); a valid JSON
    list is decoded first, so its items keep every character.
    """
    if not isinstance(value, str):
        return value
    encoded = re.match(r'^\s*\[\s*"', value) is not None
    if encoded:
        with contextlib.suppress(ValueError):
            return load_json_value(value)
    flags = flag_list(value)
    if flags is not None:
        return flags
    if value.lstrip().startswith("-") and "\\" not in value:
        with contextlib.suppress(ValueError):
            words = shlex.split(value)
            if len(words) > 1:
                return words
    if not encoded:
        return value

    # In encoded regex lists, \( and \w often omit the JSON escape for the
    # backslash. Preserve that backslash; never change members of real arrays.
    # Mixed JSON control/unicode escapes could mean text or regex instructions,
    # so a malformed list containing them needs clarification.
    ambiguous = False

    def escape(match: re.Match[str]) -> str:
        nonlocal ambiguous
        character = match[1]
        if character in "bfnrtu":
            ambiguous = True
        if character in '"\\/bfnrtu':
            return match[0]
        return "\\" + match[0]

    repaired = re.sub(r"\\(.)", escape, value, flags=re.DOTALL)
    if not ambiguous:
        try:
            return load_json_value(repaired)
        except ValueError:
            pass
    raise ValueError(
        "args contains a malformed encoded list. Send each rg argument as a string, "
        'for example ["-F", "TODO"].'
    )


def _repair_name_list(value: Any) -> Any:
    """Read a path or glob list sent as JSON text, such as '["src", "tests"]'."""
    if not isinstance(value, str) or not re.match(r'^\s*\[\s*"', value):
        return value
    try:
        decoded = load_json_value(value)
    except ValueError, ToolContractError:
        return value
    if isinstance(decoded, list) and decoded and all(isinstance(item, str) for item in decoded):
        return decoded
    return value


# Names other search Tools and command-line habits use for the same fields.
_FIELD_ALIASES = _SpellingAliases(
    {
        "args": ("argv", "options", "flags", "rg_args", "extra_args"),
        "pattern": (
            "query",
            "regexp",
            "search",
            "search_pattern",
            "search_term",
            "term",
            "patterns",
            "expression",
        ),
        "path": (
            "paths",
            "directory",
            "directories",
            "dir",
            "folder",
            "root",
            "roots",
            "file",
            "file_path",
            "search_path",
            "target_path",
            "target_directory",
            "cwd",
        ),
        "glob": (
            "include",
            "includes",
            "globs",
            "iglob",
            "file_glob",
            "glob_pattern",
            "file_pattern",
            "filename_pattern",
            "name_pattern",
        ),
        "output": ("output_mode", "mode"),
        "context": ("context_lines", "surrounding_lines"),
        "limit": ("head_limit", "max_results", "num_results", "max"),
        "offset": ("skip", "start"),
    }
)
_OUTPUT_VALUES = {
    **dict.fromkeys(("content", "contents", "lines", "matches", "text", "default"), "content"),
    **dict.fromkeys(
        (
            "files",
            "file",
            "fileswithmatches",
            "filesonly",
            "filenames",
            "filename",
            "paths",
            "names",
            "list",
            "l",
        ),
        "files",
    ),
    **dict.fromkeys(("count", "counts", "countmatches", "c"), "count"),
}
# Single-letter keys keep ripgrep's case: -c counts, -C adds context.
_SWITCH_LETTERS = {
    "i": ["-i"],
    "s": ["-s"],
    "S": ["-S"],
    "F": ["-F"],
    "w": ["-w"],
    "x": ["-x"],
    "v": ["-v"],
    "o": ["-o"],
    "P": ["-P"],
    "U": ["-U", "--multiline-dotall"],
    "u": ["-u"],
    "L": ["-L"],
    "n": [],
    "r": [],
    "R": [],
    "H": [],
}
# Word keys map to the args a true and a false value request.
_SWITCH_WORDS: dict[str, tuple[list[str], list[str]]] = {
    **dict.fromkeys(("ignorecase", "caseinsensitive", "insensitive", "nocase"), (["-i"], [])),
    "casesensitive": (["-s"], ["-i"]),
    **dict.fromkeys(("literal", "fixedstrings", "fixedstring", "fixed"), (["-F"], [])),
    **dict.fromkeys(("regex", "isregex", "useregex"), ([], ["-F"])),
    **dict.fromkeys(("word", "words", "wordregexp", "wholeword", "wholewords"), (["-w"], [])),
    **dict.fromkeys(("multiline", "dotall"), (["-U", "--multiline-dotall"], [])),
    **dict.fromkeys(("invert", "invertmatch"), (["-v"], [])),
    **dict.fromkeys(("unrestricted", "noignore", "includeignored"), (["-u"], [])),
    **dict.fromkeys(("linenumbers", "linenumber", "showlinenumbers", "withfilename"), ([], [])),
    "recursive": ([], ["-d", "1"]),
    "hidden": ([], ["--no-hidden"]),
}
_VALUED_LETTERS = {"A": "-A", "B": "-B", "t": "-t", "T": "-T", "m": "-m", "d": "-d"}
_VALUED_WORDS = {
    **dict.fromkeys(("after", "aftercontext", "contextafter", "linesafter"), "-A"),
    **dict.fromkeys(("before", "beforecontext", "contextbefore", "linesbefore"), "-B"),
    **dict.fromkeys(("type", "types", "filetype", "filetypes", "language"), "-t"),
    **dict.fromkeys(("maxdepth", "depth"), "-d"),
    **dict.fromkeys(("maxcount", "maxcountperfile"), "-m"),
}
_EXCLUDE_WORDS = {
    "exclude",
    "excludes",
    "excludeglob",
    "excludepattern",
    "excludedir",
    "excludedirs",
}
_FIELD_LETTERS = {"C": "context", "g": "glob", "e": "pattern"}
_TARGET_VALUES = {
    **dict.fromkeys(("files", "file", "filenames", "names", "paths"), "files"),
    **dict.fromkeys(("content", "contents", "text", "grep"), "content"),
}


def _normalize_output(value: Any) -> Any:
    return _OUTPUT_VALUES.get(_spelling(value), value) if isinstance(value, str) else value


def _switch(value: Any) -> bool | None:
    """Return the evident truth value of a flag field, or None if unclear."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        return {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}.get(
            value.strip().casefold()
        )
    return None


def _values(value: Any) -> list[str] | None:
    items = value if isinstance(value, list) else [value]
    if any(isinstance(item, bool) or not isinstance(item, str | int) for item in items):
        return None
    return [str(item) for item in items]


def _translate_flag_fields(arguments: dict[str, Any]) -> dict[str, Any]:
    """Turn flag-shaped fields from other search interfaces into args.

    A field is translated only when its meaning is exact. A flag field whose
    value is unclear (``ignore_case: "maybe"``) refuses the call; any other
    field stays for validation.
    """
    result = dict(arguments)
    sources = {key: (key, value) for key, value in arguments.items()}
    tokens: list[str] = []

    def set_field(field: str, value: Any, source: tuple[str, Any]) -> None:
        if field in result and result[field] != value:
            raise ToolContractError.conflicting_values(
                field, sources[field], source, tool_name=SEARCH_FILES_TOOL_NAME
            )
        result[field] = value
        sources.setdefault(field, source)

    def unclear(key: str, value: Any, expected: str) -> ToolContractError:
        received = json.dumps(value, ensure_ascii=False)
        return ToolContractError(
            f'{SEARCH_FILES_TOOL_NAME} was not run: "{key}" must be {expected}; '
            f"received {received}."
        )

    for key, value in arguments.items():
        if key in SEARCH_FILES_TOOL_PARAMETERS["properties"]:
            continue
        letter = key.lstrip("-") if len(key.lstrip("-")) == 1 else ""
        word = _spelling(key)
        switch = _switch(value)
        if word == "regex" and isinstance(value, str) and switch is None:
            set_field("pattern", value, (key, value))
        elif letter in _SWITCH_LETTERS:
            if switch is None:
                raise unclear(key, value, "true or false")
            if switch:
                tokens.extend(_SWITCH_LETTERS[letter])
        elif word in _SWITCH_WORDS:
            if switch is None:
                raise unclear(key, value, "true or false")
            tokens.extend(_SWITCH_WORDS[word][0 if switch else 1])
        elif letter in {"l", "c"} or word in {"count", "fileswithmatches", "filesonly"}:
            if switch is None:
                raise unclear(key, value, "true or false")
            if switch:
                set_field(
                    "output", "count" if letter == "c" or word == "count" else "files", (key, value)
                )
        elif letter in _VALUED_LETTERS or word in _VALUED_WORDS:
            values = _values(value)
            if values is None:
                raise unclear(key, value, "a value or a list of values")
            flag = _VALUED_LETTERS.get(letter) or _VALUED_WORDS[word]
            tokens.extend(token for item in values for token in (flag, item))
        elif word in _EXCLUDE_WORDS:
            values = _values(value)
            if values is None:
                raise unclear(key, value, "a value or a list of values")
            tokens.extend(token for item in values for token in ("-g", "!" + item.lstrip("!")))
        elif letter in _FIELD_LETTERS:
            set_field(_FIELD_LETTERS[letter], value, (key, value))
        elif word == "target" and isinstance(value, str) and _spelling(value) in _TARGET_VALUES:
            # Some search Tools select name matching with target="files".
            if _TARGET_VALUES[_spelling(value)] == "files" and "pattern" in result:
                set_field("glob", result.pop("pattern"), sources["pattern"])
        else:
            continue
        del result[key]
    pattern = result.get("pattern")
    if isinstance(pattern, list) and all(isinstance(item, str) for item in pattern):
        # Several patterns match any of them, like repeated -e.
        tokens.extend(token for item in pattern[1:] for token in ("-e", item))
        if pattern:
            result["pattern"] = pattern[0]
        else:
            del result["pattern"]
    if tokens:
        existing = result.get("args", [])
        result["args"] = [*tokens, *(existing if isinstance(existing, list) else [existing])]
    return result


@cache
def _repair_contract():
    return compile_tool_contract(
        name=SEARCH_FILES_TOOL_NAME,
        input_schema=SEARCH_FILES_TOOL_PARAMETERS,
        require_closed_input=False,
    )


def normalize_search_arguments(arguments: Any) -> Any:
    normalized = normalize_call_arguments(
        _repair_contract(),
        arguments,
        field_aliases=_FIELD_ALIASES,
        field_normalizers={
            "args": _repair_argument_array,
            "path": _repair_name_list,
            "glob": _repair_name_list,
            "output": _normalize_output,
        },
        empty_as_omitted=("pattern", "path", "glob", "output", "context"),
    )
    if not isinstance(normalized, dict):
        return normalized
    return _translate_flag_fields(normalized)


def _strings(arguments: JsonObject, name: str) -> list[str]:
    value = arguments.get(name, [])
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(
        isinstance(item, str) and "\x00" not in item for item in value
    ):
        raise SearchArgumentError(
            f"{name} must be a string or a list of strings without NUL characters."
        )
    return value


def interpret_search_call(arguments: Any, *, edit_tool: str | None = None) -> SearchQuery:
    """Interpret one call's named fields and args as a single ripgrep query.

    *edit_tool* is the file edit Tool the Agent is offered, which notes may name.
    """
    arguments = normalize_search_arguments(arguments)
    if not isinstance(arguments, dict):
        raise SearchArgumentError("Provide one search argument object.")
    pattern = arguments.get("pattern")
    if pattern is not None and not isinstance(pattern, str):
        raise SearchArgumentError("pattern must be a string.")
    output = arguments.get("output")
    return interpret(
        pattern=pattern,
        roots=_strings(arguments, "path"),
        globs=_strings(arguments, "glob"),
        output=output if isinstance(output, str) else None,
        context=optional_int(arguments.get("context"), field_name="context", minimum=0),
        args=_strings(arguments, "args"),
        limit=optional_int(
            arguments.get("limit"), field_name="limit", minimum=1, maximum=MAX_LIMIT
        ),
        offset=optional_int(
            arguments.get("offset"), field_name="offset", minimum=0, maximum=MAX_OFFSET
        ),
        edit_tool=edit_tool,
    )
