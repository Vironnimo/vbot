"""Recognize file-writing commands in an inline sed script without running it."""

from __future__ import annotations


def writes_file(script: str) -> bool:
    """Whether a command or substitution flag can write a separate file.

    Skip addresses, delimited expressions and literal text: a ``w`` in the
    replacement or a filename is not a command. Script files stay subject to
    the same documented limit as other scripts a command reads.
    """
    index = 0
    while index < len(script):
        char = script[index]
        if char.isspace() or char in ";{},!$~" or char.isdigit():
            index += 1
            continue
        if char == "/" or (char == "\\" and index + 1 < len(script)):
            # /regexp/ or GNU sed's alternate-address delimiter, e.g. \%regexp%.
            if char == "\\":
                index += 1
            index = _delimited(script, index + 1, script[index])
            continue
        index += 1
        if char in "wW":
            return True
        if char in "sy" and index < len(script):
            delimiter = script[index]
            index = _delimited(script, index + 1, delimiter)
            index = _delimited(script, index, delimiter)
            if char == "s":
                while index < len(script) and script[index] not in ";\n}":
                    if script[index] == "w":
                        return True
                    index += 1
            continue
        if char in "#aicrR":
            # Comments, literal output text and read filenames extend to newline.
            index = _line_end(script, index)
        elif char in ":btT":
            # A label can itself contain w; only the next command matters.
            while index < len(script) and script[index] not in ";\n}":
                index += 1
    return False


def _delimited(script: str, index: int, delimiter: str) -> int:
    while index < len(script):
        char = script[index]
        index += 1
        if char == "\\":
            index += 1
        elif char == delimiter:
            return index
    return index


def _line_end(script: str, index: int) -> int:
    while index < len(script):
        char = script[index]
        index += 1
        if char == "\\":
            index += 1
        elif char == "\n":
            break
    return index
