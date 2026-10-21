"""From lowered scripts to a ``CommandPlan``: names, words, paths, directories, nesting.

One ``Analysis`` per analyzed command line. It walks the lowered commands in
source order and keeps, per bash subshell scope, the set of folders the shell
may be in (a ``cd`` that may fail keeps the old folder as a candidate). Each
command is canonicalized (aliases, program basenames, wrappers), nested shells
are analyzed recursively, and paths come from the tier A specs of known
commands or, for other commands, from words that are clearly paths (tier B).
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from core.permissions.commands import _paths
from core.permissions.commands._paths import Unresolvable
from core.permissions.commands._specs import (
    CMD_BUILTINS,
    CMDLETS,
    ELEVATING_WRAPPERS,
    FILTER_CMDLETS,
    LISTING_CMDLETS,
    LISTING_PROGRAMS,
    LOCATION_FUNCTIONS,
    POSIX_SHELLS,
    POWERSHELLS,
    PROGRAMS,
    TRANSPARENT_WRAPPERS,
    AccessKind,
    ProgramSpec,
    cmdlet_parameters,
)
from core.permissions.commands._syntax import Binding, Part, RawCommand, Script, Word
from core.permissions.commands.commands import (
    MAX_COMMAND_LINE_CHARS,
    MAX_NESTING_DEPTH,
    CommandPlan,
    CommandWord,
    PathAccess,
    PathAccessKind,
    ShellKind,
    SimpleCommand,
    UnresolvedElement,
    UnresolvedKind,
)

if TYPE_CHECKING:
    from core.permissions.commands._powershell import PowerShellHelper

_MAX_CANDIDATES = 16
_URL = re.compile(r"https?://[^\s'\"<>`]+", re.IGNORECASE)
_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_DRIVE_FUNCTION = re.compile(r"^([A-Za-z]):$")
_REMOTE = re.compile(r"^[\w.@-]{2,}:")
_SOURCE_LIMIT = 500
_UNRESOLVED_KIND: Mapping[str, UnresolvedKind] = {
    "read": "path_read",
    "write": "path_write",
    "start_dir": "cwd",
}


@dataclass(frozen=True, slots=True)
class _Location:
    """One folder the shell may be in, with what ``cd -``, ``popd`` and drive switches return to."""

    cwd: Path | None
    previous: Path | None = None
    stack: tuple[Path | None, ...] = ()
    drives: tuple[tuple[str, Path], ...] = ()

    def moved(self, target: Path | None) -> _Location:
        drives = dict(self.drives)
        old_drive, new_drive = _paths.drive_of(self.cwd), _paths.drive_of(target)
        if self.cwd is not None and old_drive is not None and old_drive != new_drive:
            drives[old_drive] = self.cwd
        return _Location(target, self.cwd, self.stack, tuple(sorted(drives.items())))


@dataclass(slots=True)
class _Listing:
    """What a pipeline element hands on: the folders below which it lists, or a pass-through."""

    roots: list[tuple[Path, str]] | None = None
    filter: bool = False


@dataclass(slots=True)
class _Context:
    """One script (a command line or a nested shell's script) being analyzed."""

    shell: ShellKind
    depth: int
    assigned: set[str]
    environment_changed: bool
    states: dict[tuple[int, ...], tuple[_Location, ...]]
    listings: dict[tuple[int, int], _Listing] = field(default_factory=dict)
    redefined: set[str] = field(default_factory=set)
    delayed_expansion: bool = False

    def locations(self, scope: tuple[int, ...]) -> tuple[_Location, ...]:
        for length in range(len(scope), -1, -1):
            state = self.states.get(scope[:length])
            if state is not None:
                if length != len(scope):
                    self.states[scope] = state
                return state
        raise AssertionError("the root scope always has a state")


@dataclass(frozen=True, slots=True)
class _Value:
    text: str | None  # None: dynamic
    source: str

    @property
    def static(self) -> bool:
        return self.text is not None


class Analysis:
    def __init__(
        self, *, env: Mapping[str, str], home: Path, powershell: PowerShellHelper | None
    ) -> None:
        self._env = dict(env)
        self._env_folded = {name.upper(): value for name, value in env.items()}
        self._home = home
        self._powershell = powershell
        self._commands: list[SimpleCommand] = []
        self._paths: dict[tuple[str, Path, bool], PathAccess] = {}
        self._unresolved: dict[tuple[str, str, str], UnresolvedElement] = {}
        self._urls: dict[str, None] = {}
        # Environment variables a script changed, visible to the shells it starts.
        self._environment_dynamic: set[str] = set()
        self._msys = _paths.WINDOWS

    def plan(self) -> CommandPlan:
        return CommandPlan(
            tuple(self._commands),
            tuple(self._paths.values()),
            tuple(self._unresolved.values()),
            tuple(self._urls),
        )

    # Scripts ----------------------------------------------------------------

    async def run(
        self,
        text: str,
        shell: ShellKind,
        cwds: Sequence[Path | None],
        *,
        depth: int,
        delayed_expansion: bool = False,
    ) -> None:
        if len(text) > MAX_COMMAND_LINE_CHARS:
            self._unresolve("command", text, "the command line is too long to analyze")
            return
        if depth > MAX_NESTING_DEPTH:
            self._unresolve("command", text, "it nests shells too deeply to analyze")
            return
        script = await self._parse(text, shell, delayed_expansion)
        if script is None:
            return
        if script.error is not None:
            self._unresolve("command", text, script.error)
            return
        context = _Context(
            shell=shell,
            depth=depth,
            assigned=set(script.assigned),
            environment_changed=script.environment_changed,
            states={(): tuple(dict.fromkeys(_Location(cwd) for cwd in cwds))},
            delayed_expansion=delayed_expansion,
        )
        self._prepare(context, script)
        for command in script.commands:
            await self._command(context, command)

    async def _parse(self, text: str, shell: ShellKind, delayed_expansion: bool) -> Script | None:
        if shell == "bash":
            from core.permissions.commands._bash import parse_bash

            return parse_bash(text)
        if shell == "cmd":
            from core.permissions.commands._cmd import parse_cmd

            return parse_cmd(text, delayed_expansion=delayed_expansion)
        if self._powershell is None:
            self._unresolve("command", text, "PowerShell is not available to analyze it")
            return None
        from core.permissions.commands._powershell import lower

        return lower(await self._powershell.analyze(text))

    def _prepare(self, context: _Context, script: Script) -> None:
        """Variables, environment and aliases the script itself changes, before it is read."""
        for name in script.assigned:
            if context.shell == "bash":
                self._environment_dynamic.add(_environment_key(name))
            elif name.startswith("env:"):
                self._environment_dynamic.add(_environment_key(name[4:]))
        if context.shell != "powershell":
            return
        for command in script.commands:
            name = (command.resolved or "").lower()
            binding = command.binding
            if binding is None:
                continue
            if name in ("set-variable", "new-variable", "remove-variable", "clear-variable"):
                for value in _items(binding.bound.get("Name")):
                    text = _literal(value)
                    if text is None:
                        context.environment_changed = True
                    else:
                        context.assigned.add(text.lower())
            elif name in ("set-alias", "new-alias"):
                for value in _items(binding.bound.get("Name")):
                    text = _literal(value)
                    if text is not None:
                        context.redefined.add(text.lower())
            elif name in (
                "set-item",
                "new-item",
                "remove-item",
                "clear-item",
                "set-content",
                "rename-item",
            ):
                for parameter in ("Path", "LiteralPath"):
                    for value in _items(binding.bound.get(parameter)):
                        text = _literal(value) or ""
                        if text.lower().startswith(("env:", "variable:")):
                            variable = text.split(":", 1)[1].lstrip("\\/").lower()
                            prefix = "env:" if text.lower().startswith("env:") else ""
                            context.assigned.add(prefix + variable)
                            if prefix:
                                self._environment_dynamic.add(_environment_key(variable))

    # Commands ---------------------------------------------------------------

    async def _command(self, context: _Context, raw: RawCommand) -> None:
        if raw.words:
            if context.shell == "powershell":
                await self._powershell_command(context, raw)
            elif context.shell == "cmd":
                await self._cmd_command(context, raw)
            else:
                await self._program_command(context, raw)
        for redirect in raw.redirects:
            if (
                redirect.target.parts == (Part("variable", "null"),)
                and context.shell == "powershell"
            ):
                continue
            self._access(context, raw, redirect.kind, redirect.target, glob=False, tier="a")

    async def _powershell_command(self, context: _Context, raw: RawCommand) -> None:
        name_word = raw.words[0]
        if name_word.block:
            # & { ... } and . { ... }: the block's commands are part of the script.
            return
        name = self._value(context, name_word, self._first(context, raw)).text
        if name is None:
            self._unresolve("command", raw.source, "the command name is computed when it runs")
            return
        if raw.stop_parsing:
            self._emit(context, raw.resolved or _paths.program_name(name), raw.words[1:], raw)
            self._unresolve(
                "command", raw.source, "--% passes the rest of the line to the program unparsed"
            )
            return
        lowered = name.lower()
        if lowered in context.redefined:
            self._unresolve("command", raw.source, f"the command line redefines {name} itself")
            return
        if raw.script_function:
            self._emit(context, name, raw.words[1:], raw)
            self._tier_b(context, raw, raw.words[1:])
            return
        if raw.resolved is not None:
            resolved = raw.resolved
            location = LOCATION_FUNCTIONS.get(resolved.lower())
            if location is not None:
                self._emit(context, "Set-Location", [Word.literal(location, source=name)], raw)
                self._change_directory(context, raw, "set", Word.literal(location, source=name))
                return
            drive = _DRIVE_FUNCTION.match(resolved)
            if drive is not None:
                self._emit(context, resolved, [], raw)
                self._switch_drive(context, raw, drive.group(1).upper())
                return
            if raw.binding is not None:
                await self._cmdlet(context, raw, resolved, raw.binding)
                return
            self._emit(context, resolved, raw.words[1:], raw)
            self._tier_b(context, raw, raw.words[1:])
            return
        if raw.invocation == "." or (
            raw.invocation == "&" and _paths.is_clearly_path(name, windows_switches=True)
        ):
            self._access(context, raw, "read", name_word, glob=False, tier="a")
        await self._program(context, raw, _paths.program_name(name), raw.words[1:])

    async def _program_command(self, context: _Context, raw: RawCommand) -> None:
        name_word = raw.words[0]
        name = self._value(context, name_word, self._first(context, raw)).text
        if name is None:
            self._unresolve("command", raw.source, "the command name is computed when it runs")
            return
        if _paths.is_clearly_path(name, windows_switches=False):
            self._access(context, raw, "read", name_word, glob=False, tier="a")
        await self._program(context, raw, _paths.program_name(name), raw.words[1:])

    async def _cmd_command(self, context: _Context, raw: RawCommand) -> None:
        name = self._value(context, raw.words[0], self._first(context, raw)).text
        if name is None:
            self._unresolve("command", raw.source, "the command name is computed when it runs")
            return
        lowered = name.lower().rstrip(".")
        arguments = raw.words[1:]
        drive = _DRIVE_FUNCTION.match(name)
        if drive is not None:
            self._emit(context, name, [], raw)
            self._switch_drive(context, raw, drive.group(1).upper())
            return
        if lowered in ("cd", "chdir", "pushd", "popd"):
            self._emit(context, lowered, arguments, raw)
            operands = [
                word for word in arguments if not self._is_switch(context, raw, word, slash=True)
            ]
            if lowered == "popd":
                self._change_directory(context, raw, "pop", None)
            elif operands:
                # cd takes the rest of the line, spaces included, as one folder.
                target = operands[0] if len(operands) == 1 else _joined(operands)
                cross_drive = lowered in ("cd", "chdir") and not any(
                    (self._value(context, word, self._first(context, raw)).text or "").lower()
                    == "/d"
                    for word in arguments
                )
                self._change_directory(
                    context,
                    raw,
                    "push" if lowered == "pushd" else "set",
                    target,
                    keep_drive=cross_drive,
                )
            return
        if lowered == "mklink":
            self._emit(context, name, arguments, raw)
            operands = [
                word for word in arguments if not self._is_switch(context, raw, word, slash=True)
            ]
            for index, word in enumerate(operands[:2]):
                self._access(
                    context, raw, "write" if index == 0 else "read", word, glob=False, tier="a"
                )
            return
        if lowered == "start":
            self._emit(context, name, arguments, raw)
            rest = list(arguments)
            if rest and rest[0].source.startswith('"'):
                rest = rest[1:]  # the window title
            saved = context.locations(raw.scope)
            while rest and (
                self._value(context, rest[0], self._first(context, raw)).text or ""
            ).startswith("/"):
                option = (
                    self._value(context, rest[0], self._first(context, raw)).text or ""
                ).lower()
                rest = rest[1:]
                if option == "/d" and rest:
                    self._change_directory(context, raw, "set", rest[0], keep_old=False)
                    rest = rest[1:]
            if rest:
                await self._words_as_command(context, raw, rest)
            context.states[raw.scope] = saved
            return
        spec = CMD_BUILTINS.get(lowered)
        if spec is not None:
            self._emit(context, name, arguments, raw)
            self._apply_spec(context, raw, lowered, spec, arguments)
            return
        await self._program(context, raw, _paths.program_name(name), arguments)

    async def _words_as_command(
        self, context: _Context, raw: RawCommand, words: Sequence[Word]
    ) -> None:
        name = self._value(context, words[0], self._first(context, raw)).text
        if name is None:
            self._unresolve("command", raw.source, "the command name is computed when it runs")
            return
        await self._program(context, raw, _paths.program_name(name), list(words[1:]))

    async def _program(
        self,
        context: _Context,
        raw: RawCommand,
        name: str,
        arguments: Sequence[Word],
        *,
        fed: bool = False,
    ) -> None:
        """A program (not a PowerShell cmdlet) and its arguments, its name canonical."""
        lowered = name.lower()
        wrapper = TRANSPARENT_WRAPPERS.get(lowered)
        if wrapper is not None:
            saved = context.locations(raw.scope)
            inner = self._unwrap(context, raw, lowered, wrapper, arguments)
            if inner is not None:
                if inner:
                    await self._words_command(context, raw, inner, fed=fed or lowered == "xargs")
                # env -C changes the folder of its command only.
                context.states[raw.scope] = saved
                return
        self._emit(context, name, arguments, raw)
        elevating = ELEVATING_WRAPPERS.get(lowered)
        if elevating is not None:
            inner = self._unwrap(context, raw, lowered, elevating, arguments)
            if inner:
                await self._words_command(context, raw, inner, fed=fed)
            return
        if (
            lowered in POWERSHELLS
            or lowered in POSIX_SHELLS
            or lowered in ("cmd", "fish", "wsl", "eval", "watch")
        ):
            await self._nested_shell(context, raw, lowered, arguments)
            return
        if context.shell == "bash" and lowered in ("cd", "pushd", "popd"):
            self._bash_directory(context, raw, lowered, arguments)
            return
        if context.shell == "bash" and lowered == "trap":
            if arguments and self._value(context, arguments[0], self._first(context, raw)).static:
                await self._nested(
                    context,
                    raw,
                    "bash",
                    self._value(context, arguments[0], self._first(context, raw)).text or "",
                )
            return
        handler = _HANDLERS.get(lowered)
        if handler is not None:
            handler(self, context, raw, arguments, fed)
            return
        spec = PROGRAMS.get(lowered)
        if spec is not None:
            self._apply_spec(context, raw, lowered, spec, arguments, fed=fed)
            return
        if fed:
            # xargs CMD: the program gets operands from its input; unknown ones are only read.
            self._pipeline_targets(context, raw, "read")
        self._tier_b(context, raw, arguments)

    async def _words_command(
        self, context: _Context, raw: RawCommand, words: Sequence[Word], *, fed: bool
    ) -> None:
        name = self._value(context, words[0], self._first(context, raw)).text
        if name is None:
            self._unresolve("command", raw.source, "the command name is computed when it runs")
            return
        await self._program(context, raw, _paths.program_name(name), list(words[1:]), fed=fed)

    def _unwrap(
        self,
        context: _Context,
        raw: RawCommand,
        wrapper: str,
        options: Mapping[str, bool],
        arguments: Sequence[Word],
    ) -> list[Word] | None:
        """The inner command of a wrapper, [] when it runs none, None when it is no wrapper here.

        A wrapper whose inner command cannot be told is an unresolved command.
        """
        rest = list(arguments)
        location = self._first(context, raw)
        hidden = f"{wrapper} runs a command this analysis cannot tell apart from its options"
        while rest:
            text = self._value(context, rest[0], location).text
            if text is None:
                if wrapper in ("timeout", "env"):
                    self._unresolve("command", raw.source, hidden)
                    return []
                return rest
            if text == "--":
                rest = rest[1:]
                break
            if wrapper == "env" and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", text):
                self._environment_dynamic.add(_environment_key(text.split("=", 1)[0]))
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
                    self._change_directory(context, raw, "set", target, keep_old=False)
            if wrapper == "env" and option in ("-S", "--split-string"):
                self._unresolve("command", raw.source, "env -S splits its command when it runs")
                return []
            rest = rest[1:]
            if (
                takes_value
                and not attached
                and rest
                and (len(option) <= 2 or option.startswith("--"))
            ):
                rest = rest[1:]
        if wrapper == "timeout" and rest:
            duration = self._value(context, rest[0], location).text
            if duration is None:
                self._unresolve("command", raw.source, hidden)
                return []
            if not _DURATION.match(duration) or len(rest) < 2:
                return None  # Windows' timeout.exe waits and runs nothing
            rest = rest[1:]
        return rest

    # Nested shells ----------------------------------------------------------

    async def _nested_shell(
        self, context: _Context, raw: RawCommand, name: str, arguments: Sequence[Word]
    ) -> None:
        location = self._first(context, raw)
        values = [self._value(context, word, location) for word in arguments]
        if name == "wsl":
            self._unresolve(
                "command", raw.source, "it runs commands in the Windows Subsystem for Linux"
            )
            return
        if name in ("eval", "watch"):
            if name == "watch":
                values = _skip_options(values, {"-n", "--interval", "-d", "--differences"})
            if not values:
                return
            if any(not value.static for value in values):
                self._unresolve(
                    "command", raw.source, f"{name} runs a command computed when it runs"
                )
                return
            await self._nested(context, raw, "bash", " ".join(value.text or "" for value in values))
            return
        if name == "cmd":
            await self._nested_cmd(context, raw, arguments, values)
            return
        if name in POWERSHELLS:
            await self._nested_powershell(context, raw, name, arguments, values)
            return
        await self._nested_posix(context, raw, name, arguments, values)

    async def _nested_posix(
        self,
        context: _Context,
        raw: RawCommand,
        name: str,
        arguments: Sequence[Word],
        values: list[_Value],
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
                self._unresolve(
                    "command", raw.source, f"{name} -c runs a command computed when it runs"
                )
                return
            if name == "fish":
                self._unresolve("command", raw.source, "fish scripts are not analyzed")
                return
            await self._nested(context, raw, "bash", values[index].text or "")
            return
        if index < len(values) and values[index].text not in (None, "-", "--"):
            self._access(context, raw, "read", arguments[index], glob=False, tier="a")
            return
        if (
            raw.pipeline is not None
            and raw.pipeline.index > 0
            or any(r.kind == "read" for r in raw.redirects)
        ):
            self._unresolve("command", raw.source, f"{name} runs commands it reads from its input")

    async def _nested_cmd(
        self, context: _Context, raw: RawCommand, arguments: Sequence[Word], values: list[_Value]
    ) -> None:
        delayed = False
        for index, value in enumerate(values):
            text = value.text
            if text is None:
                self._unresolve("command", raw.source, "cmd runs a command computed when it runs")
                return
            lowered = text.lower()
            if lowered.startswith("/v:on"):
                delayed = True
            if lowered[:2] in ("/c", "/k") and (len(lowered) == 2 or lowered[2] in '"'):
                rest = [text[2:]] if len(text) > 2 else []
                tail = values[index + 1 :]
                if any(not item.static for item in tail):
                    self._unresolve(
                        "command", raw.source, "cmd runs a command computed when it runs"
                    )
                    return
                line = " ".join([*rest, *(item.text or "" for item in tail)])
                await self._nested(context, raw, "cmd", line, delayed_expansion=delayed)
                return
        if (
            raw.pipeline is not None
            and raw.pipeline.index > 0
            or any(r.kind == "read" for r in raw.redirects)
        ):
            self._unresolve("command", raw.source, "cmd runs commands it reads from its input")

    async def _nested_powershell(
        self,
        context: _Context,
        raw: RawCommand,
        name: str,
        arguments: Sequence[Word],
        values: list[_Value],
    ) -> None:
        index = 0
        cwds: list[Path | None] | None = None
        while index < len(values):
            word = arguments[index]
            text = values[index].text
            if word.block:
                return  # pwsh -Command { ... }: the block is part of this script
            if text is None:
                self._unresolve(
                    "command", raw.source, f"{name} runs a command computed when it runs"
                )
                return
            lowered = text.lower()
            option = lowered.lstrip("-/") if lowered.startswith(("-", "/")) else None
            if option is None or lowered in ("-", "--"):
                if lowered == "-":
                    self._unresolve(
                        "command", raw.source, f"{name} runs commands it reads from its input"
                    )
                    return
                if name == "powershell":
                    await self._nested_powershell_text(
                        context, raw, name, arguments[index:], values[index:], cwds
                    )
                else:
                    self._access(context, raw, "read", word, glob=False, tier="a")
                return
            if option and "command".startswith(option) and option not in ("co",) or option == "c":
                await self._nested_powershell_text(
                    context, raw, name, arguments[index + 1 :], values[index + 1 :], cwds
                )
                return
            if option in ("e", "ec", "en", "enc", "enco", "encod", "encode", "encoded") or (
                len(option) >= 2 and "encodedcommand".startswith(option)
            ):
                encoded = values[index + 1].text if index + 1 < len(values) else None
                decoded = _decode_utf16(encoded)
                if decoded is None:
                    self._unresolve(
                        "command", raw.source, f"{name} -EncodedCommand holds no readable command"
                    )
                    return
                await self._nested(context, raw, "powershell", decoded, cwds=cwds)
                return
            if option and ("file".startswith(option)):
                if index + 1 < len(arguments):
                    self._access(context, raw, "read", arguments[index + 1], glob=False, tier="a")
                return
            if option in ("wd", "workingdirectory") or (
                len(option) >= 2 and "workingdirectory".startswith(option)
            ):
                if index + 1 < len(arguments):
                    accesses = self._access(
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
            self._unresolve("command", raw.source, f"{name} runs commands it reads from its input")

    async def _nested_powershell_text(
        self,
        context: _Context,
        raw: RawCommand,
        name: str,
        arguments: Sequence[Word],
        values: Sequence[_Value],
        cwds: list[Path | None] | None,
    ) -> None:
        if not values:
            return
        if len(arguments) == 1 and arguments[0].block:
            return
        if any(not value.static for value in values):
            self._unresolve("command", raw.source, f"{name} runs a command computed when it runs")
            return
        text = " ".join(value.text or "" for value in values)
        if text.strip() == "-":
            self._unresolve("command", raw.source, f"{name} runs commands it reads from its input")
            return
        await self._nested(context, raw, "powershell", text, cwds=cwds)

    async def _nested(
        self,
        context: _Context,
        raw: RawCommand,
        shell: ShellKind,
        text: str,
        *,
        cwds: Sequence[Path | None] | None = None,
        delayed_expansion: bool = False,
    ) -> None:
        if cwds is None:
            cwds = list(dict.fromkeys(location.cwd for location in context.locations(raw.scope)))
        if shell == "powershell" and self._powershell is None:
            self._unresolve(
                "command", raw.source, "PowerShell is not available to analyze the command it runs"
            )
            return
        await self.run(
            text, shell, cwds, depth=context.depth + 1, delayed_expansion=delayed_expansion
        )

    # PowerShell cmdlets -----------------------------------------------------

    async def _cmdlet(
        self, context: _Context, raw: RawCommand, name: str, binding: Binding
    ) -> None:
        lowered = name.lower()
        self._emit(context, name, raw.words[1:], raw)
        if lowered in ("set-location", "push-location"):
            target = _first_bound(binding, "Path", "LiteralPath")
            mode = "push" if lowered == "push-location" else "set"
            history = [word for word in raw.words[1:] if _literal(word) in ("-", "+")]
            if target is None and history:
                target = history[0]
            if target is None and not binding.bound:
                self._change_directory(context, raw, mode, Word.literal("~", source=raw.source))
            elif target is not None:
                self._change_directory(context, raw, mode, target)
            return
        if lowered == "pop-location":
            self._change_directory(context, raw, "pop", None)
            return
        if lowered == "invoke-expression":
            command = _first_bound(binding, "Command")
            value = (
                None
                if command is None
                else self._value(context, command, self._first(context, raw))
            )
            if value is None or value.text is None:
                self._unresolve(
                    "command", raw.source, "Invoke-Expression runs a command computed when it runs"
                )
                return
            await self._nested(context, raw, "powershell", value.text)
            return
        if lowered == "start-process":
            await self._start_process(context, raw, binding)
            return
        parameters = cmdlet_parameters(lowered)
        if parameters is None:
            self._tier_b(context, raw, raw.words[1:])
            return
        kinds = {spec.kind for spec in parameters.values()}
        strongest: AccessKind = "write" if "write" in kinds else "read"
        if binding.errors or any(word.splat for word in raw.words[1:]):
            self._unresolve(
                _UNRESOLVED_KIND[strongest],
                raw.source,
                "its parameters cannot be bound before it runs",
            )
        for parameter, spec in parameters.items():
            if parameter not in binding.bound:
                continue
            subtree = spec.subtree is True or (
                isinstance(spec.subtree, str) and spec.subtree in binding.bound
            )
            for item in _items(binding.bound[parameter]):
                if (
                    lowered == "new-item"
                    and parameter in ("Path", "LiteralPath")
                    and "Name" in binding.bound
                ):
                    item = _joined_path(item, binding.bound["Name"])
                self._access(
                    context, raw, spec.kind, item, glob=not spec.literal, tier="a", subtree=subtree
                )
        if lowered == "rename-item":
            self._rename_target(context, raw, binding)
        if lowered == "new-item":
            item_type = self._literal_of(context, raw, binding.bound.get("ItemType"))
            if item_type is not None and item_type.lower() in (
                "symboliclink",
                "junction",
                "hardlink",
            ):
                for link in _items(binding.bound.get("Value")):
                    self._access(context, raw, "read", link, glob=False, tier="a")
        has_path = any(name in parameters for name in ("Path", "LiteralPath", "FilePath"))
        bound_path = any(name in binding.bound for name in ("Path", "LiteralPath", "FilePath"))
        if has_path and not bound_path:
            if raw.pipeline is not None and raw.pipeline.index > 0 and lowered in CMDLETS:
                primary = (
                    parameters.get("Path")
                    or parameters.get("LiteralPath")
                    or parameters["FilePath"]
                )
                self._pipeline_targets(context, raw, primary.kind)
            elif lowered in ("get-childitem", "expand-archive"):
                default_kind: AccessKind = "read" if lowered == "get-childitem" else "write"
                self._access(
                    context,
                    raw,
                    default_kind,
                    Word.literal(".", source=raw.source),
                    glob=False,
                    tier="a",
                    subtree=lowered == "expand-archive" or "Recurse" in binding.bound,
                )
        if lowered in LISTING_CMDLETS:
            self._register_listing(context, raw, binding, parameters)
        elif lowered in FILTER_CMDLETS and raw.pipeline is not None:
            context.listings[(raw.pipeline.id, raw.pipeline.index)] = _Listing(filter=True)
        self._tier_b(context, raw, binding.unbound)

    def _register_listing(
        self, context: _Context, raw: RawCommand, binding: Binding, parameters: Mapping[str, object]
    ) -> None:
        if raw.pipeline is None:
            return
        roots: list[tuple[Path, str]] | None = []
        values = [
            value for name in ("Path", "LiteralPath") for value in _items(binding.bound.get(name))
        ]
        if not values:
            values = [Word.literal(".", source=raw.source)]
        for value in values:
            accesses = self._resolve_word(context, raw, value, glob=True)
            if accesses is None:
                roots = None
                break
            assert roots is not None
            roots.extend(accesses)
        context.listings[(raw.pipeline.id, raw.pipeline.index)] = _Listing(roots=roots)

    def _rename_target(self, context: _Context, raw: RawCommand, binding: Binding) -> None:
        new_name = binding.bound.get("NewName")
        source = _first_bound(binding, "Path", "LiteralPath")
        if new_name is None:
            return
        value = self._value(context, new_name, self._first(context, raw))
        if value.text is None:
            self._unresolve("path_write", raw.source, "the new name is computed when it runs")
            return
        if re.search(r"[\\/]", value.text) or source is None:
            self._access(context, raw, "write", new_name, glob=False, tier="a")
            return
        self._access(context, raw, "write", _sibling(source, value.text), glob=False, tier="a")

    async def _start_process(self, context: _Context, raw: RawCommand, binding: Binding) -> None:
        for parameter, kind in (
            ("RedirectStandardOutput", "write"),
            ("RedirectStandardError", "write"),
            ("RedirectStandardInput", "read"),
        ):
            for value in _items(binding.bound.get(parameter)):
                self._access(context, raw, kind, value, glob=False, tier="a")  # type: ignore[arg-type]
        program = binding.bound.get("FilePath")
        if program is None:
            return
        location = self._first(context, raw)
        name = self._value(context, program, location).text
        if name is None:
            self._unresolve(
                "command", raw.source, "Start-Process starts a program computed when it runs"
            )
            return
        if _paths.is_clearly_path(name, windows_switches=True):
            self._access(context, raw, "read", program, glob=False, tier="a")
        arguments: list[Word] = []
        for value in _items(binding.bound.get("ArgumentList")):
            text = self._value(context, value, location).text
            if text is None:
                arguments.append(value)
            elif len(_items(binding.bound.get("ArgumentList"))) == 1:
                arguments.extend(Word.literal(item) for item in _split_windows_arguments(text))
            else:
                arguments.append(Word.literal(text, source=value.source))
        saved = context.locations(raw.scope)
        directory = binding.bound.get("WorkingDirectory")
        if directory is not None:
            self._change_directory(context, raw, "set", directory, keep_old=False)
        await self._program(context, raw, _paths.program_name(name), arguments)
        context.states[raw.scope] = saved

    # Program specs ----------------------------------------------------------

    def _apply_spec(
        self,
        context: _Context,
        raw: RawCommand,
        name: str,
        spec: ProgramSpec,
        arguments: Sequence[Word],
        *,
        fed: bool = False,
    ) -> None:
        location = self._first(context, raw)
        operands: list[Word] = []
        flags: set[str] = set()
        values: list[tuple[str, Word, AccessKind | None]] = []
        index = 0
        ended = False
        while index < len(arguments):
            word = arguments[index]
            text = self._value(context, word, location).text
            index += 1
            if ended or text is None or not self._looks_like_option(text, spec):
                operands.append(word)
                continue
            if text == "--" and not spec.slash:
                ended = True
                continue
            option, equals, attached = text.partition("=" if text.startswith("--") else "\x00")
            if spec.slash:
                option, _, attached = text.partition(":")
                flags.add(option.lower())
                flags.add(option)
                if option in spec.values or option.lower() in spec.values:
                    kind = spec.values.get(option, spec.values.get(option.lower()))
                    if attached:
                        values.append((option, Word.literal(attached, source=word.source), kind))
                    elif index < len(arguments):
                        values.append((option, arguments[index], kind))
                        index += 1
                continue
            if option.startswith("--"):
                flags.add(option)
                if option in spec.values:
                    if equals:
                        values.append(
                            (
                                option,
                                Word.literal(attached, source=word.source),
                                spec.values[option],
                            )
                        )
                    elif index < len(arguments):
                        values.append((option, arguments[index], spec.values[option]))
                        index += 1
                continue
            # A bundle of short options; one taking a value takes the rest or the next word.
            letters = option[1:]
            for position, letter in enumerate(letters):
                short = "-" + letter
                flags.add(short)
                if short in spec.values:
                    rest = letters[position + 1 :]
                    if rest:
                        values.append(
                            (short, Word.literal(rest, source=word.source), spec.values[short])
                        )
                    elif index < len(arguments):
                        values.append((short, arguments[index], spec.values[short]))
                        index += 1
                    break
        subtree = spec.subtree or bool(flags & spec.subtree_flags)
        operand_kind = spec.operands
        if spec.write_flags and (
            flags & spec.write_flags
            or any(flag.startswith("-i") for flag in flags if name == "sed")
        ):
            operand_kind = "write"
        for option, value, kind in values:
            if kind is not None:
                self._access(context, raw, kind, value, glob=True, tier="a", subtree=subtree)
            if option in spec.skip_cancel:
                spec = replace(spec, skip=0)
        if flags & spec.skip_cancel:
            spec = replace(spec, skip=0)
        paths = operands[spec.skip :]
        if name in ("rsync", "scp"):
            paths = [
                word
                for word in paths
                if not _REMOTE.match(self._value(context, word, location).text or "")
            ]
        if operand_kind is not None:
            for position, word in enumerate(paths):
                kind = operand_kind
                if spec.last is not None and len(paths) >= 2 and position == len(paths) - 1:
                    kind = spec.last
                self._access(context, raw, kind, word, glob=True, tier="a", subtree=subtree)
            if spec.last is not None and len(paths) == 1 and spec.slash:
                # copy x / xcopy x: the copy lands in the working directory.
                self._access(
                    context,
                    raw,
                    spec.last,
                    Word.literal(".", source=raw.source),
                    glob=False,
                    tier="a",
                )
        if not paths and spec.default is not None and not fed:
            self._access(
                context,
                raw,
                spec.default,
                Word.literal(".", source=raw.source),
                glob=False,
                tier="a",
                subtree=subtree,
            )
        if fed:
            self._pipeline_targets(
                context, raw, "write" if "write" in (spec.operands, spec.last) else "read"
            )
        if raw.pipeline is not None:
            if (
                spec.listing
                and name in LISTING_PROGRAMS
                and not (fed or (raw.pipeline.index > 0 and not paths))
            ):
                roots: list[tuple[Path, str]] | None = []
                for word in paths or [Word.literal(".", source=raw.source)]:
                    resolved = self._resolve_word(context, raw, word, glob=True)
                    if resolved is None:
                        roots = None
                        break
                    assert roots is not None
                    roots.extend(resolved)
                context.listings[(raw.pipeline.id, raw.pipeline.index)] = _Listing(roots=roots)
            elif spec.filter and raw.pipeline.index > 0 and not paths:
                context.listings[(raw.pipeline.id, raw.pipeline.index)] = _Listing(filter=True)
        consumed = {id(word) for word in paths} | {id(value) for _, value, _ in values}
        self._tier_b(context, raw, [word for word in arguments if id(word) not in consumed])

    def _looks_like_option(self, text: str, spec: ProgramSpec) -> bool:
        if spec.slash:
            return bool(re.match(r"^/[A-Za-z?+-][^\\/]*$", text))
        return text.startswith("-") and text != "-"

    def _is_switch(self, context: _Context, raw: RawCommand, word: Word, *, slash: bool) -> bool:
        text = self._value(context, word, self._first(context, raw)).text
        if text is None:
            return False
        return bool(re.match(r"^/[A-Za-z?]", text)) if slash else text.startswith("-")

    # Directories ------------------------------------------------------------

    def _bash_directory(
        self, context: _Context, raw: RawCommand, name: str, arguments: Sequence[Word]
    ) -> None:
        location = self._first(context, raw)
        operands = [
            word
            for word in arguments
            if not (
                (self._value(context, word, location).text or "x").startswith("-")
                and self._value(context, word, location).text != "-"
            )
        ]
        if name == "popd":
            if any(
                (self._value(context, word, location).text or "").startswith(("+", "-"))
                for word in operands
            ):
                self._change_directory(context, raw, "unknown", None)
            else:
                self._change_directory(context, raw, "pop", None)
            return
        if name == "pushd":
            if not operands or (self._value(context, operands[0], location).text or "").startswith(
                ("+", "-")
            ):
                self._change_directory(context, raw, "unknown", None)
            elif not any(self._value(context, word, location).text == "-n" for word in arguments):
                self._change_directory(context, raw, "push", operands[0])
            return
        if not operands:
            home = self._env.get("HOME")
            self._change_directory(
                context, raw, "set", Word.literal(home or "~", source=raw.source)
            )
        elif self._value(context, operands[0], location).text == "-":
            self._change_directory(context, raw, "previous", None)
        else:
            self._change_directory(context, raw, "set", operands[0])

    def _change_directory(
        self,
        context: _Context,
        raw: RawCommand,
        mode: str,
        target: Word | None,
        *,
        keep_old: bool = True,
        keep_drive: bool = False,
    ) -> None:
        """Move every candidate location; the old one stays, since the change may fail."""
        locations = context.locations(raw.scope)
        moved: list[_Location] = []
        for location in locations:
            if keep_old:
                moved.append(location)
            if mode == "pop":
                if location.stack:
                    head, *rest = location.stack
                    moved.append(replace(location.moved(head), stack=tuple(rest)))
                    if head is not None:
                        self._add("start_dir", head, raw.source)
                continue
            if mode == "previous":
                previous = location.previous
                if previous is None and context.shell == "bash":
                    previous = self._path_or_none(self._env.get("OLDPWD"))
                if previous is None:
                    self._unresolve(
                        "cwd", raw.source, "it returns to a folder this analysis cannot determine"
                    )
                else:
                    self._add("start_dir", previous, raw.source)
                moved.append(location.moved(previous))
                continue
            if mode == "unknown" or target is None:
                self._unresolve(
                    "cwd", raw.source, "it changes into a folder this analysis cannot determine"
                )
                moved.append(location.moved(None))
                continue
            value = self._value(context, target, location)
            if value.text is None:
                self._unresolve(
                    "cwd", target.source, "it changes into a folder computed when it runs"
                )
                moved.append(location.moved(None))
                continue
            if context.shell == "powershell" and value.text in ("-", "+"):
                if value.text == "-" and location.previous is not None:
                    self._add("start_dir", location.previous, raw.source)
                    moved.append(location.moved(location.previous))
                elif value.text == "-":
                    self._unresolve(
                        "cwd", raw.source, "it returns to a folder this analysis cannot determine"
                    )
                    moved.append(location.moved(None))
                continue
            resolved = _paths.resolve(
                value.text,
                location.cwd,
                self._home,
                tilde=target.tilde or context.shell == "powershell",
                msys=self._msys and context.shell == "bash",
                drives=dict(location.drives),
            )
            if not isinstance(resolved, Path):
                reason = (
                    resolved.reason
                    if isinstance(resolved, Unresolvable)
                    else "is not a folder of the filesystem"
                )
                self._unresolve("cwd", target.source, f"the folder {reason}")
                moved.append(location.moved(None))
                continue
            self._add("start_dir", resolved, target.source)
            if keep_drive and _paths.drive_of(resolved) != _paths.drive_of(location.cwd):
                # cmd's cd to another drive without /d only sets that drive's folder.
                drives = dict(location.drives)
                if (letter := _paths.drive_of(resolved)) is not None:
                    drives[letter] = resolved
                moved.append(replace(location, drives=tuple(sorted(drives.items()))))
                continue
            next_location = location.moved(resolved)
            if mode == "push":
                next_location = replace(next_location, stack=(location.cwd, *location.stack))
            moved.append(next_location)
        self._set_locations(context, raw, moved)

    def _switch_drive(self, context: _Context, raw: RawCommand, letter: str) -> None:
        moved: list[_Location] = []
        for location in context.locations(raw.scope):
            moved.append(location)
            if _paths.drive_of(location.cwd) == letter:
                continue
            remembered = dict(location.drives).get(letter)
            if remembered is None:
                remembered = self._path_or_none(self._env_folded.get(f"={letter}:")) or Path(
                    f"{letter}:\\"
                )
            self._add("start_dir", remembered, raw.source)
            moved.append(location.moved(remembered))
        self._set_locations(context, raw, moved)

    def _set_locations(self, context: _Context, raw: RawCommand, moved: list[_Location]) -> None:
        unique = tuple(dict.fromkeys(moved))
        if len(unique) > _MAX_CANDIDATES:
            self._unresolve(
                "cwd", raw.source, "the folder it runs in depends on too many earlier changes"
            )
            unique = (_Location(None),)
        context.states[raw.scope] = unique

    # Pipelines --------------------------------------------------------------

    def _pipeline_targets(self, context: _Context, raw: RawCommand, kind: AccessKind) -> None:
        """A command that takes its targets from its input: bounded by the listing before it."""
        if raw.pipeline is None:
            self._unresolve(
                _UNRESOLVED_KIND[kind], raw.source, "it takes its targets from its input"
            )
            return
        index = raw.pipeline.index - 1
        while index >= 0:
            listing = context.listings.get((raw.pipeline.id, index))
            if listing is None:
                break
            if listing.filter:
                index -= 1
                continue
            if listing.roots is None:
                break
            for path, source in listing.roots:
                self._add(kind, path, source, subtree=True)
            return
        self._unresolve(
            _UNRESOLVED_KIND[kind],
            raw.source,
            "it takes its targets from the output of another command",
        )

    # Words and paths --------------------------------------------------------

    def _first(self, context: _Context, raw: RawCommand) -> _Location:
        return context.locations(raw.scope)[0]

    def _value(self, context: _Context, word: Word, location: _Location) -> _Value:
        if word.parameter is not None:
            if word.argument is None:
                return _Value(word.source, word.source)
            argument = self._value(context, word.argument, location)
            if argument.text is None:
                return _Value(None, word.source)
            return _Value(f"-{word.parameter}:{argument.text}", word.source)
        if word.items is not None:
            items = [self._value(context, item, location) for item in word.items]
            if any(item.text is None for item in items):
                return _Value(None, word.source)
            return _Value(",".join(item.text or "" for item in items), word.source)
        pieces: list[str] = []
        for part in word.parts:
            if part.kind == "text":
                pieces.append(part.value)
                continue
            if part.kind == "dynamic":
                return _Value(None, word.source)
            resolved = self._variable(context, part.value, location)
            if resolved is None:
                return _Value(None, word.source)
            pieces.append(resolved)
        return _Value("".join(pieces), word.source)

    def _variable(self, context: _Context, name: str, location: _Location) -> str | None:
        if context.shell == "bash":
            if name in context.assigned or _environment_key(name) in self._environment_dynamic:
                return None
            if name == "PWD":
                return None if location.cwd is None else str(location.cwd)
            if name == "OLDPWD" and location.previous is not None:
                return str(location.previous)
            if name == "HOME":
                return self._env.get("HOME") or str(self._home)
            return self._environment(name)
        if context.environment_changed or name in context.assigned:
            return None
        if name.startswith("env:"):
            variable = name[4:]
            if _environment_key(variable) in self._environment_dynamic:
                return None
            if context.shell == "cmd" and variable == "cd":
                return None if location.cwd is None else str(location.cwd)
            return self._environment(variable)
        if name == "home":
            return str(self._home)
        if name == "pwd":
            return None if location.cwd is None else str(location.cwd)
        return None

    def _environment(self, name: str) -> str | None:
        if _paths.WINDOWS:
            return self._env_folded.get(name.upper())
        return self._env.get(name)

    def _literal_of(self, context: _Context, raw: RawCommand, word: Word | None) -> str | None:
        if word is None:
            return None
        return self._value(context, word, self._first(context, raw)).text

    def _resolve_word(
        self, context: _Context, raw: RawCommand, word: Word, *, glob: bool
    ) -> list[tuple[Path, str]] | None:
        """The folders a listing word names for every candidate location, None when unknown."""
        resolved: list[tuple[Path, str]] = []
        for location in context.locations(raw.scope):
            value = self._value(context, word, location)
            if value.text is None:
                return None
            text = _paths.without_long_prefix(value.text)
            if glob and _paths.has_glob(text, brace=context.shell == "bash"):
                prefix = _paths.glob_prefix(text, brace=context.shell == "bash")
                if isinstance(prefix, Unresolvable):
                    return None
                text = prefix
            path = _paths.resolve(
                text,
                location.cwd,
                self._home,
                tilde=word.tilde or context.shell != "bash",
                msys=self._msys and context.shell == "bash",
                drives=dict(location.drives),
            )
            if not isinstance(path, Path):
                return None
            resolved.append((path, word.source))
        return resolved

    def _resolve_listing(
        self, context: _Context, raw: RawCommand, roots: Sequence[Word]
    ) -> _Listing:
        found: list[tuple[Path, str]] = []
        for root in roots:
            resolved = self._resolve_word(context, raw, root, glob=True)
            if resolved is None:
                return _Listing(roots=None)
            found.extend(resolved)
        return _Listing(roots=found)

    def _access(
        self,
        context: _Context,
        raw: RawCommand,
        kind: PathAccessKind,
        word: Word,
        *,
        glob: bool,
        tier: str,
        subtree: bool = False,
    ) -> list[tuple[Path, str]]:
        """Record the access *word* makes from every candidate location; return the paths."""
        found: list[tuple[Path, str]] = []
        for item in _items(word):
            for location in context.locations(raw.scope):
                value = self._value(context, item, location)
                self._collect_urls(value)
                if value.text is None:
                    if tier == "a":
                        self._unresolve(
                            _UNRESOLVED_KIND[kind], item.source, "the path is computed when it runs"
                        )
                    continue
                text = value.text
                if tier == "b":
                    file_url = _paths.file_url_path(text)
                    if file_url is not None:
                        text = file_url
                    elif not _paths.is_clearly_path(text, windows_switches=context.shell != "bash"):
                        continue
                elif _paths.is_url(text):
                    continue
                text = _paths.without_long_prefix(text)
                reaches_below = subtree
                use_glob = glob and (context.shell != "bash" or item.glob)
                if use_glob and _paths.has_glob(text, brace=context.shell == "bash"):
                    prefix = _paths.glob_prefix(text, brace=context.shell == "bash")
                    if isinstance(prefix, Unresolvable):
                        self._unresolve(
                            _UNRESOLVED_KIND[kind], item.source, f"the path {prefix.reason}"
                        )
                        continue
                    text, reaches_below = prefix, True
                path = _paths.resolve(
                    text,
                    location.cwd,
                    self._home,
                    tilde=item.tilde or context.shell == "powershell",
                    msys=self._msys and context.shell == "bash",
                    drives=dict(location.drives),
                )
                if path is None:
                    continue
                if isinstance(path, Unresolvable):
                    if tier == "a" or location.cwd is None:
                        self._unresolve(
                            _UNRESOLVED_KIND[kind], item.source, f"the path {path.reason}"
                        )
                    continue
                self._add(kind, path, item.source, subtree=reaches_below)
                found.append((path, item.source))
        return found

    def _tier_b(self, context: _Context, raw: RawCommand, words: Iterable[Word]) -> None:
        for word in words:
            if word.block:
                continue
            target = word
            location = self._first(context, raw)
            text = self._value(context, word, location).text
            if word.parameter is not None:
                if word.argument is None:
                    continue
                target = word.argument
            elif text is not None and re.match(r"^-{0,2}[A-Za-z_][\w.-]*=", text):
                # --output=../x, DESTDIR=/opt
                target = Word.literal(text.split("=", 1)[1], source=word.source)
            self._access(context, raw, "read", target, glob=True, tier="b")

    def _collect_urls(self, value: _Value) -> None:
        for match in _URL.finditer(value.text if value.text is not None else value.source):
            self._urls.setdefault(match.group(0).rstrip(".,;)"), None)

    # Output -----------------------------------------------------------------

    def _emit(
        self, context: _Context, name: str, arguments: Sequence[Word], raw: RawCommand
    ) -> None:
        location = self._first(context, raw)
        words = []
        for word in arguments:
            value = self._value(context, word, location)
            self._collect_urls(value)
            words.append(
                CommandWord(value.text if value.text is not None else word.source, value.static)
            )
        self._commands.append(
            SimpleCommand(name, tuple(words), raw.source[:_SOURCE_LIMIT] or name, context.shell)
        )

    def _add(self, kind: PathAccessKind, path: Path, source: str, *, subtree: bool = False) -> None:
        key = (kind, path, subtree)
        if key not in self._paths and (kind, path, True) not in self._paths:
            self._paths[key] = PathAccess(kind, path, source[:_SOURCE_LIMIT], subtree)

    def _unresolve(self, kind: UnresolvedKind, source: str, reason: str) -> None:
        source = source[:_SOURCE_LIMIT]
        self._unresolved.setdefault((kind, source, reason), UnresolvedElement(kind, source, reason))

    def _path_or_none(self, text: str | None) -> Path | None:
        if not text:
            return None
        resolved = _paths.resolve(text, None, self._home)
        return resolved if isinstance(resolved, Path) else None


# Special programs --------------------------------------------------------------


def _handle_find(
    analysis: Analysis, context: _Context, raw: RawCommand, arguments: Sequence[Word], fed: bool
) -> None:
    location = analysis._first(context, raw)
    texts = [analysis._value(context, word, location).text for word in arguments]
    roots: list[Word] = []
    index = 0
    while (
        index < len(arguments) and texts[index] is not None and texts[index] in ("-H", "-L", "-P")
    ):
        index += 1
    while index < len(arguments):
        text = texts[index]
        if text is not None and (text.startswith("-") or text in ("(", "!", ")")):
            break
        roots.append(arguments[index])
        index += 1
    roots = roots or [Word.literal(".", source=raw.source)]
    writes = False
    expression = list(zip(arguments[index:], texts[index:], strict=True))
    position = 0
    while position < len(expression):
        word, text = expression[position]
        position += 1
        if text == "-delete":
            writes = True
        elif text in ("-fprint", "-fprint0", "-fprintf", "-fls"):
            if position < len(expression):
                analysis._access(
                    context, raw, "write", expression[position][0], glob=False, tier="a"
                )
        elif text in ("-exec", "-execdir", "-ok", "-okdir"):
            inner: list[Word] = []
            while position < len(expression) and expression[position][1] not in (";", "+"):
                inner.append(expression[position][0])
                position += 1
            position += 1
            if inner:
                inner_name = analysis._value(context, inner[0], location).text
                if inner_name is None:
                    analysis._unresolve(
                        "command", raw.source, "find -exec runs a command computed when it runs"
                    )
                    continue
                program = _paths.program_name(inner_name).lower()
                spec = PROGRAMS.get(program)
                analysis._emit(
                    context,
                    _paths.program_name(inner_name),
                    [w for w in inner[1:] if analysis._value(context, w, location).text != "{}"],
                    raw,
                )
                if spec is None or "write" in (spec.operands, spec.last) or program in POSIX_SHELLS:
                    writes = True
                others = [
                    w for w in inner[1:] if analysis._value(context, w, location).text != "{}"
                ]
                analysis._tier_b(context, raw, others)
        del word
    found = []
    for root in roots:
        found += analysis._access(context, raw, "read", root, glob=True, tier="a", subtree=True)
        if writes:
            analysis._access(context, raw, "write", root, glob=True, tier="a", subtree=True)
    if raw.pipeline is not None:
        listing = analysis._resolve_listing(context, raw, roots)
        context.listings[(raw.pipeline.id, raw.pipeline.index)] = listing
    del found


def _handle_tar(
    analysis: Analysis, context: _Context, raw: RawCommand, arguments: Sequence[Word], fed: bool
) -> None:
    location = analysis._first(context, raw)
    archive: Word | None = None
    directory: Word | None = None
    mode = "list"
    operands: list[Word] = []
    index = 0
    while index < len(arguments):
        word = arguments[index]
        text = analysis._value(context, word, location).text
        index += 1
        if text is None:
            operands.append(word)
            continue
        letters = ""
        if text.startswith("--"):
            option, _, attached = text.partition("=")
            if option in ("--file", "--directory"):
                value = (
                    Word.literal(attached, source=word.source)
                    if attached
                    else (arguments[index] if index < len(arguments) else None)
                )
                if not attached:
                    index += 1
                if option == "--file":
                    archive = value
                else:
                    directory = value
            elif option in ("--extract", "--get"):
                mode = "extract"
            elif option in ("--create", "--append", "--update", "--concatenate"):
                mode = "create"
            continue
        if text.startswith("-") or (index == 1 and re.match(r"^[A-Za-z]+$", text)):
            letters = text.lstrip("-")
            for position, letter in enumerate(letters):
                if letter == "x":
                    mode = "extract"
                elif letter in "cru":
                    mode = "create"
                elif letter in "fC":
                    rest = letters[position + 1 :]
                    value = (
                        Word.literal(rest, source=word.source)
                        if rest
                        else (arguments[index] if index < len(arguments) else None)
                    )
                    if not rest:
                        index += 1
                    if letter == "f":
                        archive = value
                    else:
                        directory = value
                    break
            continue
        operands.append(word)
    target = directory or Word.literal(".", source=raw.source)
    if mode == "extract":
        if archive is not None:
            analysis._access(context, raw, "read", archive, glob=False, tier="a")
        analysis._access(context, raw, "write", target, glob=False, tier="a", subtree=True)
    elif mode == "create":
        if archive is not None:
            analysis._access(context, raw, "write", archive, glob=False, tier="a")
        for operand in operands:
            analysis._access(
                context,
                raw,
                "read",
                _relative_to(operand, directory),
                glob=True,
                tier="a",
                subtree=True,
            )
    elif archive is not None:
        analysis._access(context, raw, "read", archive, glob=False, tier="a")


def _handle_dd(
    analysis: Analysis, context: _Context, raw: RawCommand, arguments: Sequence[Word], fed: bool
) -> None:
    location = analysis._first(context, raw)
    for word in arguments:
        text = analysis._value(context, word, location).text
        if text is None:
            if word.source.startswith(("of=", "if=")):
                analysis._unresolve(
                    "path_write" if word.source.startswith("of=") else "path_read",
                    word.source,
                    "the path is computed when it runs",
                )
            continue
        key, equals, value = text.partition("=")
        if equals and key in ("if", "of"):
            analysis._access(
                context,
                raw,
                "read" if key == "if" else "write",
                Word.literal(value, source=word.source),
                glob=False,
                tier="a",
            )


def _handle_unzip(
    analysis: Analysis, context: _Context, raw: RawCommand, arguments: Sequence[Word], fed: bool
) -> None:
    location = analysis._first(context, raw)
    texts = [analysis._value(context, word, location).text for word in arguments]
    destination: Word | None = None
    listing_only = False
    archive: Word | None = None
    index = 0
    while index < len(arguments):
        text = texts[index]
        if text == "-d" and index + 1 < len(arguments):
            destination = arguments[index + 1]
            index += 2
            continue
        if text is not None and text.startswith("-"):
            listing_only = (
                listing_only
                or any(letter in text for letter in "lvtZ")
                and not text.startswith("--")
            )
            if text == "-x":
                break
        elif archive is None:
            archive = arguments[index]
        index += 1
    if archive is not None:
        analysis._access(context, raw, "read", archive, glob=True, tier="a")
    if not listing_only:
        analysis._access(
            context,
            raw,
            "write",
            destination or Word.literal(".", source=raw.source),
            glob=False,
            tier="a",
            subtree=True,
        )


def _handle_download(
    analysis: Analysis, context: _Context, raw: RawCommand, arguments: Sequence[Word], fed: bool
) -> None:
    """curl and wget: where downloads land and which files they upload."""
    location = analysis._first(context, raw)
    program = _paths.program_name(
        analysis._value(context, raw.words[0], location).text or ""
    ).lower()
    writes = _CURL_WRITES if program == "curl" else _WGET_WRITES
    reads = _CURL_READS if program == "curl" else _WGET_READS
    remote_name = False
    index = 0
    while index < len(arguments):
        word = arguments[index]
        text = analysis._value(context, word, location).text
        index += 1
        if text is None or not text.startswith("-"):
            continue
        option, equals, attached = text.partition("=")
        if program == "curl" and not option.startswith("--") and len(option) > 2:
            option, attached, equals = option[:2], option[2:], "="
        if option in ("-O", "--remote-name", "--remote-name-all", "-J"):
            remote_name = True
            continue
        kind: AccessKind | None = (
            "write" if option in writes else "read" if option in reads else None
        )
        if option in _DOWNLOAD_VALUES or kind is not None:
            value = (
                Word.literal(attached, source=word.source)
                if equals
                else (arguments[index] if index < len(arguments) else None)
            )
            if not equals:
                index += 1
            if kind is None or value is None:
                continue
            text_value = analysis._value(context, value, location).text
            if (
                text_value is not None
                and program == "curl"
                and option in ("-d", "--data", "--data-binary", "--data-urlencode", "-F", "--form")
            ):
                if "@" not in text_value:
                    continue
                text_value = text_value.split("@", 1)[1].split(";", 1)[0]
                value = Word.literal(text_value, source=value.source)
            analysis._access(context, raw, kind, value, glob=False, tier="a")
    if program == "wget" and not any(
        (analysis._value(context, word, location).text or "").startswith(
            ("-O", "--output-document", "-P", "--directory-prefix")
        )
        for word in arguments
    ):
        remote_name = True
    if remote_name:
        analysis._access(
            context, raw, "write", Word.literal(".", source=raw.source), glob=False, tier="a"
        )
    analysis._tier_b(
        context,
        raw,
        [
            word
            for word in arguments
            if not (analysis._value(context, word, location).text or "-").startswith("-")
        ],
    )


_CURL_WRITES = frozenset(
    {
        "-o",
        "--output",
        "-c",
        "--cookie-jar",
        "-D",
        "--dump-header",
        "--trace",
        "--trace-ascii",
        "--output-dir",
        "--stderr",
        "--etag-save",
        "--hsts",
        "--alt-svc",
    }
)
_CURL_READS = frozenset(
    {
        "-T",
        "--upload-file",
        "-K",
        "--config",
        "-d",
        "--data",
        "--data-binary",
        "--data-urlencode",
        "-F",
        "--form",
        "--cacert",
        "--cert",
        "--key",
        "--netrc-file",
        "-b",
        "--cookie",
    }
)
_WGET_WRITES = frozenset(
    {
        "-O",
        "--output-document",
        "-P",
        "--directory-prefix",
        "-o",
        "--output-file",
        "-a",
        "--append-output",
        "--save-cookies",
    }
)
_WGET_READS = frozenset(
    {"-i", "--input-file", "--load-cookies", "--post-file", "--body-file", "--ca-certificate"}
)
_DOWNLOAD_VALUES = frozenset(
    {
        "-H",
        "--header",
        "-X",
        "--request",
        "-u",
        "--user",
        "-A",
        "--user-agent",
        "-e",
        "--referer",
        "-m",
        "--max-time",
        "-x",
        "--proxy",
        "-w",
        "--write-out",
        "-r",
        "--range",
        "--connect-timeout",
        "--retry",
        "-U",
        "--proxy-user",
        "--resolve",
        "-t",
        "--tries",
        "-T",
        "--timeout",
        "--password",
        "-Q",
        "--quota",
        "-l",
        "--level",
    }
)


def _handle_acl(
    analysis: Analysis, context: _Context, raw: RawCommand, arguments: Sequence[Word], fed: bool
) -> None:
    """icacls and attrib: the files they change; switches, their values and flags are no paths."""
    location = analysis._first(context, raw)
    program = _paths.program_name(analysis._value(context, raw.words[0], location).text or "")
    icacls = program.lower() == "icacls"
    texts = [analysis._value(context, word, location).text for word in arguments]
    recursive = any((text or "").lower() in ("/t", "/s") for text in texts)
    index = 0
    files = 0
    while index < len(arguments):
        word, text = arguments[index], texts[index]
        index += 1
        lowered = (text or "").lower()
        if icacls and lowered in ("/save", "/restore") and index < len(arguments):
            kind: AccessKind = "write" if lowered == "/save" else "read"
            analysis._access(context, raw, kind, arguments[index], glob=False, tier="a")
            index += 1
            continue
        if icacls and lowered.split(":", 1)[0] in _ICACLS_VALUES:
            index += 1
            continue
        if text is not None and (
            text.startswith(("/", "+")) or (text.startswith("-") and not icacls)
        ):
            continue
        if icacls and files:
            continue  # icacls names one file or folder; later words are values
        files += 1
        analysis._access(context, raw, "write", word, glob=True, tier="a", subtree=recursive)


_ICACLS_VALUES = frozenset(
    {"/grant", "/deny", "/remove", "/setintegritylevel", "/setowner", "/findsid", "/substitute"}
)

_HANDLERS = {
    "icacls": _handle_acl,
    "attrib": _handle_acl,
    "find": _handle_find,
    "tar": _handle_tar,
    "bsdtar": _handle_tar,
    "dd": _handle_dd,
    "unzip": _handle_unzip,
    "curl": _handle_download,
    "wget": _handle_download,
}

_POWERSHELL_VALUE_OPTIONS = frozenset(
    {
        "executionpolicy",
        "ep",
        "ex",
        "configurationname",
        "config",
        "version",
        "v",
        "inputformat",
        "if",
        "inp",
        "outputformat",
        "of",
        "o",
        "windowstyle",
        "w",
        "settingsfile",
        "settings",
        "custompipename",
        "psconsolefile",
        "encodedarguments",
        "ea",
        "args",
        "sshserver",
    }
)


# Helpers -----------------------------------------------------------------------


def _items(word: Word | None) -> tuple[Word, ...]:
    if word is None:
        return ()
    if word.items is not None:
        return tuple(item for element in word.items for item in _items(element))
    return (word,)


def _first_bound(binding: Binding, *names: str) -> Word | None:
    for name in names:
        value = binding.bound.get(name)
        if value is not None:
            return value
    return None


def _literal(word: Word) -> str | None:
    if all(part.kind == "text" for part in word.parts) and word.items is None:
        return "".join(part.value for part in word.parts)
    return None


def _joined(words: Sequence[Word]) -> Word:
    parts: list[Part] = []
    for index, word in enumerate(words):
        if index:
            parts.append(Part("text", " "))
        parts.extend(word.parts)
    return Word(tuple(parts), " ".join(word.source for word in words))


def _joined_path(folder: Word, name: Word | None) -> Word:
    if name is None:
        return folder
    separator = Part("text", "\\" if _paths.WINDOWS else "/")
    return Word(
        (*folder.parts, separator, *name.parts),
        f"{folder.source} {name.source}",
        tilde=folder.tilde,
    )


def _sibling(source: Word, name: str) -> Word:
    parts = list(source.parts)
    text = (
        "".join(part.value for part in parts)
        if all(part.kind == "text" for part in parts)
        else None
    )
    if text is None:
        return Word((Part("dynamic", source.source),), source.source)
    cut = max(text.rfind("/"), text.rfind("\\"))
    folder = text[: cut + 1] if cut >= 0 else ""
    return Word.literal(folder + name, source=f"{source.source} -> {name}")


def _relative_to(word: Word, directory: Word | None) -> Word:
    if directory is None:
        return word
    return _joined_path(directory, word)


def _skip_options(values: list[_Value], with_value: set[str]) -> list[_Value]:
    index = 0
    while index < len(values) and (values[index].text or "").startswith("-"):
        index += 2 if values[index].text in with_value else 1
    return values[index:]


def _decode_utf16(text: str | None) -> str | None:
    if not text:
        return None
    try:
        return base64.b64decode(text, validate=True).decode("utf-16-le")
    except binascii.Error, UnicodeDecodeError, ValueError:
        return None


def _split_windows_arguments(text: str) -> list[str]:
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


def _environment_key(name: str) -> str:
    return name.upper() if _paths.WINDOWS else name
