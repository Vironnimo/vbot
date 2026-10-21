"""Commands that run other commands: wrappers, runners and nested shells.

Wrappers (``env``, ``timeout``, ``xargs``, ``sudo``) and runners (``uv run``,
``pipx run``) hand their inner command words back to the analysis; nested
shells (``pwsh -Command``, ``bash -c``, ``cmd /c``, ``eval``) analyze their
script recursively, up to ``MAX_NESTING_DEPTH``. What cannot be told apart is
an unresolved ``command``.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from core.permissions.commands._directories import change_directory
from core.permissions.commands._specs import POWERSHELLS, RunnerSpec
from core.permissions.commands._state import Context, Value, environment_key
from core.permissions.commands._syntax import RawCommand, Word
from core.permissions.commands.commands import ShellKind

if TYPE_CHECKING:
    from core.permissions.commands._analysis import Analysis

_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_POWERSHELL_VALUE_OPTIONS = frozenset(
    {
        "executionpolicy", "ep", "ex", "configurationname", "config", "version", "v",
        "inputformat", "if", "inp", "outputformat", "of", "o", "windowstyle", "w",
        "settingsfile", "settings", "custompipename", "psconsolefile", "encodedarguments",
        "ea", "args", "sshserver",
    }
)  # fmt: skip


def unwrap(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    wrapper: str,
    options: Mapping[str, bool],
    arguments: Sequence[Word],
) -> list[Word] | None:
    """The inner command of a wrapper, [] when it runs none, None when it is no wrapper here.

    A wrapper whose inner command cannot be told is an unresolved command.
    """
    rest = list(arguments)
    location = analysis.first(context, raw)
    hidden = f"{wrapper} runs a command this analysis cannot tell apart from its options"
    while rest:
        text = analysis.value(context, rest[0], location).text
        if text is None:
            if wrapper in ("timeout", "env"):
                analysis.unresolve("command", raw.source, hidden)
                return []
            return rest
        if text == "--":
            rest = rest[1:]
            break
        if wrapper == "env" and _ASSIGNMENT.match(text):
            analysis.environment_dynamic.add(environment_key(text.split("=", 1)[0]))
            rest = rest[1:]
            continue
        if not text.startswith("-") or text == "-":
            break
        option, _, attached = text.partition("=")
        takes_value = options.get(option) or (
            len(option) > 2 and not option.startswith("--") and options.get(option[:2])
        )
        if option in ("-C", "--chdir") and wrapper == "env":
            target = (
                Word.literal(attached, source=rest[0].source)
                if attached
                else (rest[1] if len(rest) > 1 else None)
            )
            if target is not None:
                change_directory(analysis, context, raw, "set", target, keep_old=False)
        if wrapper == "env" and option in ("-S", "--split-string"):
            analysis.unresolve("command", raw.source, "env -S splits its command when it runs")
            return []
        rest = rest[1:]
        if takes_value and not attached and rest and (len(option) <= 2 or option.startswith("--")):
            rest = rest[1:]
    if wrapper == "timeout" and rest:
        duration = analysis.value(context, rest[0], location).text
        if duration is None:
            analysis.unresolve("command", raw.source, hidden)
            return []
        if not _DURATION.match(duration) or len(rest) < 2:
            return None  # Windows' timeout.exe waits and runs nothing
        rest = rest[1:]
    return rest


def unwrap_runner(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    runner: str,
    spec: RunnerSpec,
    arguments: Sequence[Word],
) -> list[Word]:
    """The command a runner runs (``uv run --with x pytest``), [] when it runs none.

    Options come before and after the subcommand; an option of ``directories``
    moves the inner command into its folder. An option this table does not
    know counts as a flag.
    """
    location = analysis.first(context, raw)
    texts = [analysis.value(context, word, location).text for word in arguments]
    index = 0
    matched = not spec.subcommands
    while index < len(arguments):
        text = texts[index]
        if text is None:
            if matched:
                break
            return []
        if text == "--":
            index += 1
            if matched:
                break
            continue
        if text.startswith("-") and text != "-":
            option, equals, attached = text.partition("=")
            if option in spec.directories:
                target = (
                    Word.literal(attached, source=arguments[index].source)
                    if equals
                    else (arguments[index + 1] if index + 1 < len(arguments) else None)
                )
                if target is not None:
                    change_directory(analysis, context, raw, "set", target, keep_old=False)
            index += 2 if option in spec.values and not equals else 1
            continue
        if matched:
            break
        found = next(
            (
                words
                for words in spec.subcommands
                if [item.lower() if item else item for item in texts[index : index + len(words)]]
                == list(words)
            ),
            None,
        )
        if found is None:
            return []  # another subcommand (uv pip, pipx install): runs no command
        index += len(found)
        matched = True
    if not matched:
        return []
    rest = list(arguments[index:])
    if spec.environment_prefix and rest:
        # hatch run test:pytest: the environment before the colon.
        first = analysis.value(context, rest[0], location).text
        if first is not None and re.match(r"^[\w.-]+:[^\\/]", first):
            rest[0] = Word.literal(first.split(":", 1)[1], source=rest[0].source)
    return rest


# Nested shells -------------------------------------------------------------------


def nested_shell(
    analysis: Analysis, context: Context, raw: RawCommand, name: str, arguments: Sequence[Word]
) -> None:
    location = analysis.first(context, raw)
    values = [analysis.value(context, word, location) for word in arguments]
    if name == "wsl":
        analysis.unresolve(
            "command", raw.source, "it runs commands in the Windows Subsystem for Linux"
        )
        return
    if name in ("eval", "watch"):
        if name == "watch":
            values = _skip_options(values, {"-n", "--interval", "-d", "--differences"})
        if not values:
            return
        if any(not value.static for value in values):
            analysis.unresolve(
                "command", raw.source, f"{name} runs a command computed when it runs"
            )
            return
        nested(analysis, context, raw, "bash", " ".join(value.text or "" for value in values))
        return
    if name == "cmd":
        _nested_cmd(analysis, context, raw, values)
        return
    if name in POWERSHELLS:
        _nested_powershell(analysis, context, raw, name, arguments, values)
        return
    _nested_posix(analysis, context, raw, name, arguments, values)


def nested(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    shell: ShellKind,
    text: str,
    *,
    cwds: Sequence[Path | None] | None = None,
    delayed_expansion: bool = False,
) -> None:
    """Analyze *text* as a script of *shell* run by *raw*, in its folders unless *cwds*."""
    if cwds is None:
        cwds = list(dict.fromkeys(location.cwd for location in context.locations(raw.scope)))
    if shell == "powershell" and not analysis.has_powershell:
        analysis.unresolve(
            "command", raw.source, "PowerShell is not available to analyze the command it runs"
        )
        return
    analysis.run(text, shell, cwds, depth=context.depth + 1, delayed_expansion=delayed_expansion)


def _reads_input(raw: RawCommand) -> bool:
    return (raw.pipeline is not None and raw.pipeline.index > 0) or any(
        redirect.kind == "read" for redirect in raw.redirects
    )


def _nested_posix(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
    values: list[Value],
) -> None:
    index = 0
    command_next = False
    while index < len(values):
        text = values[index].text
        if text is None:
            break
        if command_next or not text.startswith(("-", "+")) or text in ("-", "--"):
            break
        if text in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            index += 2
            continue
        if not text.startswith("--") and "c" in text[1:]:
            command_next = True
        index += 1
    if command_next:
        if index >= len(values) or not values[index].static:
            analysis.unresolve(
                "command", raw.source, f"{name} -c runs a command computed when it runs"
            )
            return
        if name == "fish":
            analysis.unresolve("command", raw.source, "fish scripts are not analyzed")
            return
        nested(analysis, context, raw, "bash", values[index].text or "")
        return
    if index < len(values) and values[index].text not in (None, "-", "--"):
        analysis.access(context, raw, "read", arguments[index], glob=False, tier="a")
        return
    if _reads_input(raw):
        analysis.unresolve("command", raw.source, f"{name} runs commands it reads from its input")


def _nested_cmd(analysis: Analysis, context: Context, raw: RawCommand, values: list[Value]) -> None:
    delayed = False
    for index, value in enumerate(values):
        text = value.text
        if text is None:
            analysis.unresolve("command", raw.source, "cmd runs a command computed when it runs")
            return
        lowered = text.lower()
        if lowered.startswith("/v:on"):
            delayed = True
        if lowered[:2] in ("/c", "/k") and (len(lowered) == 2 or lowered[2] in '"'):
            rest = [text[2:]] if len(text) > 2 else []
            tail = values[index + 1 :]
            if any(not item.static for item in tail):
                analysis.unresolve(
                    "command", raw.source, "cmd runs a command computed when it runs"
                )
                return
            line = " ".join([*rest, *(item.text or "" for item in tail)])
            nested(analysis, context, raw, "cmd", line, delayed_expansion=delayed)
            return
    if _reads_input(raw):
        analysis.unresolve("command", raw.source, "cmd runs commands it reads from its input")


def _nested_powershell(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
    values: list[Value],
) -> None:
    index = 0
    cwds: list[Path | None] | None = None
    while index < len(values):
        word = arguments[index]
        text = values[index].text
        if word.block:
            return  # pwsh -Command { ... }: the block is part of this script
        if text is None:
            analysis.unresolve(
                "command", raw.source, f"{name} runs a command computed when it runs"
            )
            return
        lowered = text.lower()
        option = lowered.lstrip("-/") if lowered.startswith(("-", "/")) else None
        if option is None or lowered in ("-", "--"):
            if lowered == "-":
                analysis.unresolve(
                    "command", raw.source, f"{name} runs commands it reads from its input"
                )
                return
            if name == "powershell":
                _nested_powershell_text(
                    analysis, context, raw, name, arguments[index:], values[index:], cwds
                )
            else:
                analysis.access(context, raw, "read", word, glob=False, tier="a")
            return
        if option and "command".startswith(option) and option not in ("co",) or option == "c":
            _nested_powershell_text(
                analysis, context, raw, name, arguments[index + 1 :], values[index + 1 :], cwds
            )
            return
        if option in ("e", "ec", "en", "enc", "enco", "encod", "encode", "encoded") or (
            len(option) >= 2 and "encodedcommand".startswith(option)
        ):
            encoded = values[index + 1].text if index + 1 < len(values) else None
            decoded = decode_utf16(encoded)
            if decoded is None:
                analysis.unresolve(
                    "command", raw.source, f"{name} -EncodedCommand holds no readable command"
                )
                return
            nested(analysis, context, raw, "powershell", decoded, cwds=cwds)
            return
        if option and ("file".startswith(option)):
            if index + 1 < len(arguments):
                analysis.access(context, raw, "read", arguments[index + 1], glob=False, tier="a")
            return
        if option in ("wd", "workingdirectory") or (
            len(option) >= 2 and "workingdirectory".startswith(option)
        ):
            if index + 1 < len(arguments):
                accesses = analysis.access(
                    context, raw, "start_dir", arguments[index + 1], glob=False, tier="a"
                )
                cwds = [path for path, _ in accesses] or [None]
            index += 2
            continue
        if option in _POWERSHELL_VALUE_OPTIONS or any(
            len(option) >= 3 and full.startswith(option) for full in _POWERSHELL_VALUE_OPTIONS
        ):
            index += 2
            continue
        index += 1
    if raw.pipeline is not None and raw.pipeline.index > 0:
        analysis.unresolve("command", raw.source, f"{name} runs commands it reads from its input")


def _nested_powershell_text(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    name: str,
    arguments: Sequence[Word],
    values: Sequence[Value],
    cwds: list[Path | None] | None,
) -> None:
    if not values:
        return
    if len(arguments) == 1 and arguments[0].block:
        return
    if any(not value.static for value in values):
        analysis.unresolve("command", raw.source, f"{name} runs a command computed when it runs")
        return
    text = " ".join(value.text or "" for value in values)
    if text.strip() == "-":
        analysis.unresolve("command", raw.source, f"{name} runs commands it reads from its input")
        return
    nested(analysis, context, raw, "powershell", text, cwds=cwds)


def _skip_options(values: list[Value], with_value: set[str]) -> list[Value]:
    index = 0
    while index < len(values) and (values[index].text or "").startswith("-"):
        index += 2 if values[index].text in with_value else 1
    return values[index:]


def decode_utf16(text: str | None) -> str | None:
    """The command of ``-EncodedCommand`` (UTF-16LE base64), else None."""
    if not text:
        return None
    try:
        return base64.b64decode(text, validate=True).decode("utf-16-le")
    except binascii.Error, UnicodeDecodeError, ValueError:
        return None


def split_windows_arguments(text: str) -> list[str]:
    """Split a Windows command line the way CommandLineToArgvW does (quotes and backslashes)."""
    arguments: list[str] = []
    current: list[str] = []
    quoted = started = False
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\\":
            run = len(text) - index - len(text[index:].lstrip("\\"))
            if index + run < len(text) and text[index + run] == '"':
                current.append("\\" * (run // 2))
                if run % 2:
                    current.append('"')
                    index += run + 1
                else:
                    index += run
                started = True
                continue
            current.append("\\" * run)
            index += run
            started = True
            continue
        if character == '"':
            quoted = not quoted
            started = True
        elif character in " \t" and not quoted:
            if started:
                arguments.append("".join(current))
            current, started = [], False
        else:
            current.append(character)
            started = True
        index += 1
    if started:
        arguments.append("".join(current))
    return arguments
