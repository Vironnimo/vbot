"""cmd.exe command lines (``cmd /c ...``) lowered into a ``Script``.

cmd has no syntax tree to borrow, so this is a small tokenizer: commands split
on ``&``, ``&&``, ``||``, ``|`` and parentheses outside quotes, ``^`` escapes,
redirections, ``%VAR%`` from the environment, ``!VAR!`` and ``for`` variables
as dynamic parts, and the commands after ``if`` conditions, ``for ... do`` and
``else``.
"""

from __future__ import annotations

import itertools
import re

from core.permissions.commands._syntax import (
    Part,
    Pipeline,
    RawCommand,
    Redirect,
    RedirectKind,
    Script,
    Word,
)

# %%i and %i (for variables), %~dp0 and %1 (batch arguments), %VAR%, !VAR!.
_VARIABLE = re.compile(
    r"%%~?[A-Za-z]|%~[^%\s]*[0-9A-Za-z]|%[0-9*]|%([^%\s=]+)%|!([^!\s]+)!"
    r"|%[A-Za-z](?=[^A-Za-z0-9_%]|$)"
)
_REDIRECT = re.compile(r"^([0-9]?)(>>|>|<)(&[0-9])?")
_IF_UNARY = frozenset({"exist", "defined", "errorlevel", "cmdextversion"})


def parse_cmd(text: str, *, delayed_expansion: bool = False) -> Script:
    script = Script()
    pipelines = itertools.count(1)
    pipeline_id: int | None = None
    pipeline_index = 0
    for segment, operator in _segments(text):
        tokens = _tokens(segment)
        words: list[Word] = []
        redirects: list[Redirect] = []
        index = 0
        while index < len(tokens):
            token, quoted = tokens[index]
            redirect = None if quoted else _REDIRECT.match(token)
            if redirect is not None:
                rest = token[redirect.end() :]
                index += 1
                if redirect.group(3):
                    continue
                if not rest and index < len(tokens):
                    rest = tokens[index][0]
                    index += 1
                if rest:
                    kind: RedirectKind = "read" if redirect.group(2) == "<" else "write"
                    redirects.append(Redirect(kind, _word(rest, delayed_expansion)))
                continue
            words.append(_word(token, delayed_expansion, quoted=quoted))
            index += 1
        words = _strip_conditions(words, script)
        pipeline = None
        if operator == "|" or pipeline_id is not None:
            if pipeline_id is None:
                pipeline_id, pipeline_index = next(pipelines), 0
            pipeline = Pipeline(pipeline_id, pipeline_index)
            pipeline_index += 1
            if operator != "|":
                pipeline_id = None
        if words or redirects:
            script.commands.append(RawCommand(words, segment.strip(), redirects, pipeline))
    return script


def _segments(text: str) -> list[tuple[str, str]]:
    """The commands of a line with the operator that ends each (``&``, ``|``, ...).

    A parenthesis opens a block only where a command starts (also after
    ``do``, ``else`` and an ``if`` condition); elsewhere, as in
    ``for %i in (*.txt)`` or ``echo (x)``, it is text up to its partner.
    """
    segments: list[tuple[str, str]] = []
    current: list[str] = []
    quoted = False
    blocks = literal = 0
    index = 0
    while index < len(text):
        character = text[index]
        if character == '"':
            quoted = not quoted
        elif not quoted and character == "^" and index + 1 < len(text):
            current.append(text[index : index + 2])
            index += 2
            continue
        elif not quoted and character == "(" and (literal or not _opens_block("".join(current))):
            literal += 1
        elif not quoted and character == ")" and literal:
            literal -= 1
        elif not quoted and character == ")" and not blocks:
            pass  # a stray parenthesis is text
        elif not quoted and character in "&|()\n":
            if character == "(":
                blocks += 1
            elif character == ")":
                blocks -= 1
            pair = text[index : index + 2]
            operator = pair if pair in ("&&", "||") else character
            segments.append(("".join(current), operator))
            current = []
            index += len(operator)
            continue
        current.append(character)
        index += 1
    segments.append(("".join(current), ""))
    return [
        (segment, operator) for segment, operator in segments if segment.strip() or operator == "|"
    ]


def _opens_block(before: str) -> bool:
    words = (
        before.replace("@", " ", 1).split() if before.lstrip().startswith("@") else before.split()
    )
    if not words:
        return True
    first, last = words[0].lower(), words[-1].lower()
    return last in ("do", "else") or first in ("if", "else")


def _tokens(segment: str) -> list[tuple[str, bool]]:
    """Whitespace-separated tokens with quotes and escapes removed, and whether one was quoted."""
    tokens: list[tuple[str, bool]] = []
    current: list[str] = []
    quoted = in_quotes = False
    started = False
    index = 0
    segment = segment.lstrip()
    if segment.startswith("@"):
        segment = segment[1:]
    while index < len(segment):
        character = segment[index]
        if character == '"':
            in_quotes = not in_quotes
            quoted = started = True
        elif character == "^" and not in_quotes and index + 1 < len(segment):
            index += 1
            current.append(segment[index])
            started = True
        elif character in " \t" and not in_quotes:
            if started:
                tokens.append(("".join(current), quoted))
            current, quoted, started = [], False, False
        elif character in "<>" and not in_quotes and started and not "".join(current).isdigit():
            tokens.append(("".join(current), quoted))
            current, quoted, started = [character], False, True
        else:
            current.append(character)
            started = True
        index += 1
    if started:
        tokens.append(("".join(current), quoted))
    return tokens


def _word(text: str, delayed_expansion: bool, *, quoted: bool = False) -> Word:
    parts: list[Part] = []
    position = 0
    for match in _VARIABLE.finditer(text):
        if match.start() > position:
            parts.append(Part("text", text[position : match.start()]))
        if match.group(1):
            parts.append(Part("variable", "env:" + match.group(1).lower()))
        elif match.group(2) and not delayed_expansion:
            parts.append(Part("text", match.group(0)))
        else:
            parts.append(Part("dynamic", match.group(0)))
        position = match.end()
    if position < len(text):
        parts.append(Part("text", text[position:]))
    # A quoted word keeps its quotes in the source: ``start "title" program``.
    source = f'"{text}"' if quoted else text
    return Word(tuple(parts), source, glob=not quoted and any(c in text for c in "*?"))


def _strip_conditions(words: list[Word], script: Script) -> list[Word]:
    """The command an ``if``, ``for ... do`` or ``else`` runs; ``set`` assigns a variable."""
    while words:
        first = _static(words[0]).lower()
        if first == "if":
            rest = words[1:]
            while rest and _static(rest[0]).lower() in ("/i", "not"):
                rest = rest[1:]
            if rest and _static(rest[0]).lower() in _IF_UNARY:
                rest = rest[2:]
            elif len(rest) >= 3 and _static(rest[1]).lower() in (
                "equ",
                "neq",
                "lss",
                "leq",
                "gtr",
                "geq",
            ):
                rest = rest[3:]
            elif rest and "==" in _static(rest[0]):
                left, _, right = _static(rest[0]).partition("==")
                rest = rest[1:] if right else rest[2:]
            elif len(rest) >= 2 and _static(rest[1]) == "==":
                rest = rest[3:]
            words = rest
        elif first == "for":
            positions = [index for index, word in enumerate(words) if _static(word).lower() == "do"]
            words = words[positions[0] + 1 :] if positions else []
        elif first == "else":
            words = words[1:]
        else:
            break
    if words and _static(words[0]).lower() == "set" and len(words) > 1:
        assignment = next((word for word in words[1:] if "=" in _static(word)), None)
        if assignment is not None:
            name = _static(assignment).split("=", 1)[0].strip('"').lower()
            script.assigned.add("env:" + name)
    return words


def _static(word: Word) -> str:
    return "".join(part.value for part in word.parts if part.kind == "text") if word.parts else ""
