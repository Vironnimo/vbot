"""The ``/secret`` command syntax, shared by the command and the hints that name it.

``/secret NAME [options] VALUE`` stores a Secret; options come before the value,
and everything after them is the value, spaces included. ``--`` ends the
options when a value starts with ``--``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from core.secrets._agent_secret_model import SECRET_KINDS, Lifetime, SecretKind

__all__ = [
    "SecretCommand",
    "SecretCommandError",
    "parse_secret_command",
    "secret_command_example",
]

SECRET_COMMAND_USAGE: Final = (
    "/secret NAME VALUE stores a Secret for this session. Options before the value: "
    "--permanent, --for 2h (or 30m, 7d), --once, --kind login --user NAME, "
    "--kind text, --description TEXT."
)
_DURATION = re.compile(r"(\d+)\s*([mhdw])", re.IGNORECASE)
_DURATION_UNITS: Final = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_VALUE_OPTIONS: Final = frozenset({"--for", "--kind", "--user", "--description"})
# Any whitespace separates the name and the options, line breaks included.
_SEPARATOR = re.compile(r"\s+")


class SecretCommandError(ValueError):
    """The command text cannot be read; the message says how to write it."""


@dataclass(frozen=True)
class SecretCommand:
    name: str | None
    value: str
    kind: SecretKind | None = None
    username: str | None = None
    lifetime: Lifetime = "session"
    duration: timedelta | None = None
    description: str = ""


def parse_secret_command(arguments: str) -> SecretCommand:
    """Read the text after ``/secret``; a bare command has neither name nor value."""
    text = arguments.strip()
    if not text:
        return SecretCommand(name=None, value="")
    name, rest = _first_word(text)
    if name.startswith("--"):
        raise SecretCommandError(f"Name the Secret first. {SECRET_COMMAND_USAGE}")
    options: dict[str, str] = {}
    flags: set[str] = set()
    rest = rest.lstrip()
    while rest.startswith("--"):
        token, rest = _first_word(rest)
        if token == "--":
            break
        option = token.lower()
        if option in _VALUE_OPTIONS:
            if option == "--description" and rest.startswith('"'):
                closing = rest.find('"', 1)
                if closing < 0:
                    raise SecretCommandError('Close the --description text with ".')
                options[option] = rest[1:closing]
                rest = rest[closing + 1 :].lstrip()
                continue
            argument, rest = _first_word(rest)
            if not argument:
                raise SecretCommandError(f"{option} needs a value. {SECRET_COMMAND_USAGE}")
            options[option] = argument
        elif option in {"--permanent", "--once"}:
            flags.add(option)
        else:
            # The token may be a value that starts with --: never repeat it.
            raise SecretCommandError(
                "An option before the value is unknown; write -- before a value that "
                f"starts with --. {SECRET_COMMAND_USAGE}"
            )
    lifetime: Lifetime = "session"
    duration = None
    chosen = [item for item in ("--permanent", "--once", "--for") if item in flags | set(options)]
    if len(chosen) > 1:
        raise SecretCommandError(f"Use only one of {', '.join(chosen)}.")
    if "--permanent" in flags:
        lifetime = "permanent"
    elif "--once" in flags:
        lifetime = "once"
    elif "--for" in options:
        lifetime = "until"
        duration = _duration(options["--for"])
    kind = options.get("--kind")
    if kind is not None:
        kind = kind.lower()
        if kind not in SECRET_KINDS:
            raise SecretCommandError(f"--kind takes one of: {', '.join(SECRET_KINDS)}.")
    username = options.get("--user")
    if username is not None and kind is None:
        kind = "login"
    return SecretCommand(
        name=name,
        value=rest,
        kind=kind,  # type: ignore[arg-type]
        username=username,
        lifetime=lifetime,
        duration=duration,
        description=options.get("--description", ""),
    )


def secret_command_example(name: str, kind: SecretKind, lifetime: Lifetime) -> str:
    """The command a user sends to store ``name``, with placeholders for the values."""
    parts = ["/secret", name]
    if lifetime == "permanent":
        parts.append("--permanent")
    elif lifetime == "once":
        parts.append("--once")
    if kind == "login":
        parts += ["--kind", "login", "--user", "<username>", "<password>"]
    else:
        if kind == "text":
            parts += ["--kind", "text"]
        parts.append("<value>")
    return " ".join(parts)


def _first_word(text: str) -> tuple[str, str]:
    """The text up to the first whitespace, and the rest after that whitespace."""
    match = _SEPARATOR.search(text)
    if match is None:
        return text, ""
    return text[: match.start()], text[match.end() :]


def _duration(text: str) -> timedelta:
    match = _DURATION.fullmatch(text.strip())
    if match is None:
        # The argument may be the value itself: never repeat it.
        raise SecretCommandError("--for takes a duration such as 30m, 2h, 7d or 2w.")
    amount = int(match.group(1))
    if amount <= 0:
        raise SecretCommandError("--for needs a duration above zero.")
    return timedelta(**{_DURATION_UNITS[match.group(2).lower()]: amount})
