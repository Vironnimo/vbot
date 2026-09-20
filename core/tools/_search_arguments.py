"""Parse one ripgrep argument vector without invoking or emulating a shell."""

from __future__ import annotations

from typing import Any

from core.tools._search_options import BY_NAME

_ENTRY_SELECTORS = {"--files": "files", "--dirs": "directories", "--entries": "all"}


def parse_search_args(tokens: list[str]) -> dict[str, Any]:
    """Separate option values, content patterns, and literal roots by rg grammar."""
    options: list[str] = []
    patterns: list[str] = []
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
                patterns.append(inline if inline is not None else take_value(name))
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

    if kind and patterns:
        raise ValueError("Path discovery takes roots and -g name filters, not -e content patterns.")
    if not kind and not reference and not patterns:
        if not operands:
            raise ValueError("Supply a content pattern, or use --files, --dirs, or --entries.")
        patterns.append(operands.pop(0))
    if any(not root.strip() or root == "-" for root in operands):
        raise ValueError(
            "Search roots must be nonempty file or directory paths; stdin is unavailable."
        )
    return {
        "action": "paths" if kind else "content",
        "kind": kind or "files",
        "patterns": patterns,
        "paths": operands,
        "options": options,
    }
