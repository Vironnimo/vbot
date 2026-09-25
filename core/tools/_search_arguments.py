"""Parse one ripgrep argument vector without invoking or emulating a shell."""

from __future__ import annotations

import codecs
import json
from collections.abc import Sequence
from typing import Any

from core.tools._search_options import BY_NAME

_ENTRY_SELECTORS = {"--files": "files", "--dirs": "directories", "--entries": "all"}
# grep spellings whose meaning is exact in ripgrep's file selection.
_GREP_GLOBS = {"--include": "", "--exclude": "!", "--exclude-dir": "!"}


def parse_search_args(
    tokens: list[str], *, patterns: Sequence[str] = (), roots: Sequence[str] = ()
) -> dict[str, Any]:
    """Separate option values, content patterns, and literal roots by rg grammar.

    `patterns` and `roots` come from named fields and act like -e patterns and
    trailing operands. Without any pattern or operand the call lists files.
    """
    options: list[str] = []
    found = list(patterns)
    operands: list[str] = []
    kind = ""
    reference = False
    index = 0
    ended = False

    def take_value(name: str) -> str:
        nonlocal index
        if index >= len(tokens):
            raise ValueError(f"{name} requires a value in the next args item.")
        value = tokens[index]
        index += 1
        return value

    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token == "--" and not ended:
            ended = True
            continue
        if ended or not token.startswith("-") or token == "-":
            operands.append(token)
            continue
        # A long option's value may contain spaces; option names never do.
        option_text = token.partition("=")[0] if token.startswith("--") else token
        if any(char.isspace() for char in option_text):
            raise ValueError(
                f"args item {json.dumps(token)} holds several arguments; pass one per item, "
                f"for example {json.dumps(token.split())}."
            )
        if token in _ENTRY_SELECTORS:
            selected = _ENTRY_SELECTORS[token]
            if kind and kind != selected:
                raise ValueError("Choose one of --files, --dirs, or --entries.")
            kind = selected
            continue
        if token.startswith("--"):
            name, separator, long_value = token.partition("=")
            pending = [(name, long_value if separator else None)]
        else:
            pending = []
            position = 1
            while position < len(token):
                name = "-" + token[position]
                position += 1
                option = BY_NAME.get(name)
                takes_value = name == "-e" or (option is not None and option.argument)
                pending.append(
                    (name, token[position:] if takes_value and position < len(token) else None)
                )
                if takes_value:
                    break
        for name, inline in pending:
            if name in {"-e", "--regexp"}:
                found.append(inline if inline is not None else take_value(name))
                continue
            if name in _GREP_GLOBS:
                value = inline if inline is not None else take_value(name)
                options.extend(["-g", _GREP_GLOBS[name] + value.removeprefix("!")])
                continue
            if name == "-E" and inline is None and not _is_encoding(tokens[index : index + 1]):
                # grep -E selects extended regex syntax, which ripgrep always uses.
                continue
            option = BY_NAME.get(name)
            if option is None:
                raise ValueError(f"Unsupported search option {name!r}; use args=['--help'].")
            if option.key == "reference":
                reference = True
            if option.argument:
                options.extend([name, inline if inline is not None else take_value(name)])
            elif inline is not None:
                raise ValueError(f"{name} does not take a value.")
            else:
                options.append(name)

    operands.extend(roots)
    if kind and found:
        raise ValueError("Path discovery takes roots and -g name filters, not -e content patterns.")
    pattern_operand = False
    if not kind and not reference and not found:
        if operands and len(operands) > len(roots):
            found.append(operands.pop(0))
            pattern_operand = True
        else:
            kind = "files"
    if any(not root.strip() or root == "-" for root in operands):
        raise ValueError(
            "Search roots must be nonempty file or directory paths; stdin is unavailable."
        )
    return {
        "action": "paths" if kind else "content",
        "kind": kind or "files",
        "patterns": found,
        "paths": operands,
        "options": options,
        "pattern_operand": pattern_operand,
    }


def _is_encoding(value: list[str]) -> bool:
    if not value:
        return False
    if value[0].casefold() in {"auto", "none"}:
        return True
    try:
        return codecs.lookup(value[0])._is_text_encoding  # type: ignore[attr-defined]
    except LookupError:
        return False
