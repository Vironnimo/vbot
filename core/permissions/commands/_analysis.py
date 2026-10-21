"""From lowered scripts to a ``CommandPlan``: the walk, programs, words and paths.

One ``Analysis`` per analyzed command line. It walks the lowered commands in
source order and keeps, per bash subshell scope, the set of folders the shell
may be in (``_directories.py``; a ``cd`` that may fail keeps the old folder as
a candidate). Each command is canonicalized (aliases, program basenames,
wrappers, runners, global options), nested shells are analyzed recursively
(``_nesting.py``), and paths come from the tier A specs of known commands
(``_specs.py``, ``_handlers.py``, ``_cmdlets.py``, ``_dotnet.py``) or, for
other commands, from words that are clearly paths (tier B).

The analysis is synchronous and runs on a worker thread (``commands.py``);
``powershell`` is the blocking call that has the PowerShell helper parse a
script.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.permissions.commands import _cmdlets, _dotnet, _paths
from core.permissions.commands._directories import (
    bash_directory,
    change_directory,
    cmd_directory,
    switch_drive,
)
from core.permissions.commands._handlers import (
    HANDLERS,
    PYTHONS,
    add_network_target,
    git_writes,
    handle_python,
    program_outputs,
)
from core.permissions.commands._nesting import nested, nested_shell, unwrap, unwrap_runner
from core.permissions.commands._paths import Unresolvable
from core.permissions.commands._specs import (
    CMD_BUILTINS,
    CMD_INTERNALS,
    ELEVATING_WRAPPERS,
    GLOBAL_OPTIONS,
    LISTING_PROGRAMS,
    POSIX_SHELLS,
    POWERSHELLS,
    PROGRAMS,
    PRUNE_OPTIONS,
    RUNNERS,
    TRANSPARENT_WRAPPERS,
    AccessKind,
    GlobalOptions,
    ProgramSpec,
)
from core.permissions.commands._state import (
    SOURCE_LIMIT,
    UNRESOLVED_KIND,
    Context,
    ListedRoot,
    Listing,
    Location,
    Value,
    environment_key,
    items,
)
from core.permissions.commands._syntax import Part, Pipeline, RawCommand, Script, Word
from core.permissions.commands.commands import (
    MAX_COMMAND_LINE_CHARS,
    MAX_NESTING_DEPTH,
    CommandPlan,
    CommandWord,
    PathAccessKind,
    PlannedPathAccess,
    ShellKind,
    SimpleCommand,
    UnresolvedElement,
    UnresolvedKind,
)

_URL = re.compile(r"https?://[^\s'\"<>`]+", re.IGNORECASE)
_DRIVE_FUNCTION = re.compile(r"^([A-Za-z]):$")
_REMOTE = re.compile(r"^[\w.@-]{2,}:")
# cmd ends a built-in command's name at the first of these (``cd..``, ``cd\``, ``echo.``).
_CMD_NAME_END = re.compile(r"[.\\/:=;,+(]")
# A name an exclusion leaves out exactly: no wildcard, no separator inside.
_PRUNED_NAME = re.compile(r"^[^\\/*?\[\]{}!]+/?$")

type PowerShellParse = Callable[[str], Mapping[str, Any]]


class Analysis:
    """One analysis of a command line; ``plan`` returns what it found."""

    def __init__(
        self, *, env: Mapping[str, str], home: Path, powershell: PowerShellParse | None
    ) -> None:
        self.env = dict(env)
        self.env_folded = {name.upper(): value for name, value in env.items()}
        self.home = home
        self.msys = _paths.WINDOWS
        # Environment variables a script changed, visible to the shells it starts.
        self.environment_dynamic: set[str] = set()
        self._powershell = powershell
        self._commands: list[SimpleCommand] = []
        self._paths: dict[tuple[object, ...], PlannedPathAccess] = {}
        self._unresolved: dict[tuple[str, str, str], UnresolvedElement] = {}
        self._urls: dict[str, None] = {}
        self._hosts: dict[str, None] = {}

    @property
    def has_powershell(self) -> bool:
        return self._powershell is not None

    def plan(self) -> CommandPlan:
        return CommandPlan(
            tuple(self._commands),
            tuple(self._paths.values()),
            tuple(self._unresolved.values()),
            tuple(self._urls),
            tuple(self._hosts),
        )

    # Scripts ----------------------------------------------------------------

    def run(
        self,
        text: str,
        shell: ShellKind,
        cwds: Sequence[Path | None],
        *,
        depth: int,
        delayed_expansion: bool = False,
    ) -> None:
        if len(text) > MAX_COMMAND_LINE_CHARS:
            self.unresolve("command", text, "the command line is too long to analyze")
            return
        if depth > MAX_NESTING_DEPTH:
            self.unresolve("command", text, "it nests shells too deeply to analyze")
            return
        script = self._parse(text, shell, delayed_expansion)
        if script is None:
            return
        if script.error is not None:
            self.unresolve("command", text, script.error)
            return
        start = tuple(dict.fromkeys(Location(cwd) for cwd in cwds))
        context = Context(
            shell=shell,
            depth=depth,
            assigned=set(script.assigned),
            environment_changed=script.environment_changed,
            states={(): start},
            delayed_expansion=delayed_expansion,
            start=start,
        )
        self._prepare(context, script)
        for command in script.commands:
            self._command(context, command)

    def run_argv(self, argv: Sequence[str], cwd: Path) -> None:
        """Analyze starting the program *argv* directly, its words literal."""
        if not argv:
            return
        raw = RawCommand(words=[Word.literal(item) for item in argv], source=" ".join(argv))
        context = Context(
            shell="bash",
            depth=0,
            assigned=set(),
            environment_changed=False,
            states={(): (Location(cwd),)},
        )
        self._program_command(context, raw)

    def _parse(self, text: str, shell: ShellKind, delayed_expansion: bool) -> Script | None:
        if shell == "bash":
            from core.permissions.commands._bash import parse_bash

            return parse_bash(text)
        if shell == "cmd":
            from core.permissions.commands._cmd import parse_cmd

            return parse_cmd(text, delayed_expansion=delayed_expansion)
        if self._powershell is None:
            self.unresolve("command", text, "PowerShell is not available to analyze it")
            return None
        from core.permissions.commands._powershell import lower

        return lower(dict(self._powershell(text)))

    def _prepare(self, context: Context, script: Script) -> None:
        """Variables and environment the script itself changes, before it is read."""
        for name in script.assigned:
            if context.shell == "bash":
                self.environment_dynamic.add(environment_key(name))
            elif name.startswith("env:"):
                self.environment_dynamic.add(environment_key(name[4:]))
        if context.shell == "powershell":
            _cmdlets.prepare(self, context, script)

    # Commands ---------------------------------------------------------------

    def _command(self, context: Context, raw: RawCommand) -> None:
        if raw.call is not None:
            _dotnet.member_call(self, context, raw)
            return
        if raw.expression is not None:
            _dotnet.expression_string(self, context, raw)
            return
        if raw.words:
            if context.shell == "powershell":
                _cmdlets.powershell_command(self, context, raw)
            elif context.shell == "cmd":
                self._cmd_command(context, raw)
            else:
                self._program_command(context, raw)
        for redirect in raw.redirects:
            if (
                redirect.target.parts == (Part("variable", "null"),)
                and context.shell == "powershell"
            ):
                continue
            self.access(context, raw, redirect.kind, redirect.target, glob=False, tier="a")

    def _program_command(self, context: Context, raw: RawCommand) -> None:
        name_word = raw.words[0]
        name = self.value(context, name_word, self.first(context, raw)).text
        if name is None:
            self.unresolve("command", raw.source, "the command name is computed when it runs")
            return
        if _paths.is_clearly_path(name, windows_switches=False):
            self.access(context, raw, "read", name_word, glob=False, tier="a")
        self.program(context, raw, _paths.program_name(name), raw.words[1:])

    def _cmd_command(self, context: Context, raw: RawCommand) -> None:
        name = self.value(context, raw.words[0], self.first(context, raw)).text
        if name is None:
            self.unresolve("command", raw.source, "the command name is computed when it runs")
            return
        arguments = list(raw.words[1:])
        drive = _DRIVE_FUNCTION.match(name)
        if drive is not None:
            self.emit(context, name, [], raw)
            switch_drive(self, context, raw, drive.group(1).upper())
            return
        end = _CMD_NAME_END.search(name)
        if end is not None and name[: end.start()].lower() in CMD_INTERNALS:
            # cmd reads cd.. as cd .. and cd\x as cd \x; = ; , separate like a space.
            rest = name[end.start() :]
            name = name[: end.start()]
            if rest[0] in "=;,":
                rest = rest[1:]
            if rest:
                arguments.insert(0, Word.literal(rest, source=raw.words[0].source))
        lowered = name.lower()
        if lowered in ("cd", "chdir", "pushd", "popd"):
            self.emit(context, lowered, arguments, raw)
            cmd_directory(self, context, raw, lowered, arguments)
            return
        if lowered == "mklink":
            self.emit(context, name, arguments, raw)
            operands = [
                word for word in arguments if not self.is_switch(context, raw, word, slash=True)
            ]
            for index, word in enumerate(operands[:2]):
                kind: AccessKind = "write" if index == 0 else "read"
                self.access(context, raw, kind, word, glob=False, tier="a")
            return
        if lowered == "start":
            self.emit(context, name, arguments, raw)
            self._cmd_start(context, raw, arguments)
            return
        spec = CMD_BUILTINS.get(lowered)
        if spec is not None:
            self.emit(context, name, arguments, raw)
            self.apply_spec(context, raw, lowered, spec, arguments)
            return
        self.program(context, raw, _paths.program_name(name), arguments)

    def _cmd_start(self, context: Context, raw: RawCommand, arguments: Sequence[Word]) -> None:
        rest = list(arguments)
        if rest and rest[0].source.startswith('"'):
            rest = rest[1:]  # the window title
        saved = context.locations(raw.scope)
        location = self.first(context, raw)
        while rest and (self.value(context, rest[0], location).text or "").startswith("/"):
            option = (self.value(context, rest[0], location).text or "").lower()
            rest = rest[1:]
            if option == "/d" and rest:
                change_directory(self, context, raw, "set", rest[0], keep_old=False)
                rest = rest[1:]
        if rest:
            self.words_command(context, raw, rest, fed=False)
        context.states[raw.scope] = saved

    def words_command(
        self, context: Context, raw: RawCommand, words: Sequence[Word], *, fed: bool
    ) -> None:
        """The command *words* name, as a wrapper or runner runs it."""
        name = self.value(context, words[0], self.first(context, raw)).text
        if name is None:
            self.unresolve("command", raw.source, "the command name is computed when it runs")
            return
        self.program(context, raw, _paths.program_name(name), list(words[1:]), fed=fed)

    def program(
        self,
        context: Context,
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
            inner = unwrap(self, context, raw, lowered, wrapper, arguments)
            if inner is not None:
                if inner:
                    self.words_command(context, raw, inner, fed=fed or lowered == "xargs")
                # env -C changes the folder of its command only.
                context.states[raw.scope] = saved
                return
        runner = RUNNERS.get(lowered)
        if runner is not None:
            saved = context.locations(raw.scope)
            inner = unwrap_runner(self, context, raw, lowered, runner, arguments)
            if inner:
                # Both are commands: rules may name the runner or what it runs.
                self.emit(context, name, arguments, raw)
                self.words_command(context, raw, inner, fed=fed)
                context.states[raw.scope] = saved
                return
            context.states[raw.scope] = saved
        options = GLOBAL_OPTIONS.get(lowered)
        if options is not None:
            self._global_options(context, raw, name, options, arguments, fed=fed)
            return
        self.emit(context, name, arguments, raw)
        self._program_body(context, raw, lowered, arguments, fed=fed)

    def _global_options(
        self,
        context: Context,
        raw: RawCommand,
        name: str,
        options: GlobalOptions,
        arguments: Sequence[Word],
        *,
        fed: bool,
    ) -> None:
        """``git -C dir push``: strip the options before the subcommand, then run it.

        The command is emitted without them, so ``Shell(git push *)`` matches; a
        directory option moves the subcommand into its folder.
        """
        location = self.first(context, raw)
        saved = context.locations(raw.scope)
        rest = list(arguments)
        writes: list[Word] = []
        while rest:
            word = rest[0]
            text = self.value(context, word, location).text
            if text is None or not text.startswith("-") or text == "-":
                break
            option, equals, attached = (
                text.partition("=") if text.startswith("--") else (text, "", "")
            )
            if len(text) > 2 and not text.startswith("--") and text[:2] in options.attached:
                option, equals, attached = text[:2], "=", text[2:]
            if option in options.flags:
                rest = rest[1:]
                continue
            if option not in options.values:
                break
            if equals:
                value: Word | None = Word.literal(attached, source=word.source)
                rest = rest[1:]
            else:
                value = rest[1] if len(rest) > 1 else None
                rest = rest[2:]
            if value is None:
                break
            if option in options.directories:
                change_directory(self, context, raw, "set", value, keep_old=False)
            elif option in options.reads:
                self.access(context, raw, "read", value, glob=False, tier="a")
            elif option in options.writes:
                writes.append(value)
        lowered = name.lower()
        self.emit(context, name, rest, raw)
        subcommand = [self.value(context, word, location).text for word in rest[:1]]
        if writes and (lowered != "git" or git_writes(subcommand)):
            for value in writes:
                self.access(context, raw, "write", value, glob=False, tier="a", subtree=True)
        self._program_body(context, raw, lowered, rest, fed=fed)
        context.states[raw.scope] = saved

    def _program_body(
        self,
        context: Context,
        raw: RawCommand,
        lowered: str,
        arguments: Sequence[Word],
        *,
        fed: bool,
    ) -> None:
        """What an emitted program does with its arguments."""
        elevating = ELEVATING_WRAPPERS.get(lowered)
        if elevating is not None:
            inner = unwrap(self, context, raw, lowered, elevating, arguments)
            if inner:
                self.words_command(context, raw, inner, fed=fed)
            return
        if (
            lowered in POWERSHELLS
            or lowered in POSIX_SHELLS
            or lowered in ("cmd", "fish", "wsl", "eval", "watch")
        ):
            nested_shell(self, context, raw, lowered, arguments)
            return
        if context.shell == "bash" and lowered in ("cd", "pushd", "popd"):
            bash_directory(self, context, raw, lowered, arguments)
            return
        if context.shell == "bash" and lowered == "trap":
            location = self.first(context, raw)
            if arguments and self.value(context, arguments[0], location).static:
                nested(
                    self,
                    context,
                    raw,
                    "bash",
                    self.value(context, arguments[0], location).text or "",
                )
            return
        handler = HANDLERS.get(lowered)
        if handler is not None:
            handler(self, context, raw, lowered, arguments, fed)
            return
        if PYTHONS.match(lowered):
            handle_python(self, context, raw, lowered, arguments, fed)
            return
        spec = PROGRAMS.get(lowered)
        if spec is not None:
            self.apply_spec(context, raw, lowered, spec, arguments, fed=fed)
            return
        if fed:
            # xargs CMD: the program gets operands from its input; unknown ones are only read.
            self.pipeline_targets(context, raw, "read")
        # Unknown programs: their positional paths count as reads (tier B); only
        # the output options OUTPUT_OPTIONS lists are writes.
        program_outputs(self, context, raw, lowered, arguments)

    # Program specs ----------------------------------------------------------

    def apply_spec(
        self,
        context: Context,
        raw: RawCommand,
        name: str,
        spec: ProgramSpec,
        arguments: Sequence[Word],
        *,
        fed: bool = False,
    ) -> None:
        location = self.first(context, raw)
        operands: list[Word] = []
        flags: set[str] = set()
        values: list[tuple[str, Word, AccessKind | None]] = []
        index = 0
        ended = False
        while index < len(arguments):
            word = arguments[index]
            text = self.value(context, word, location).text
            index += 1
            if ended or text is None or not self.looks_like_option(text, spec):
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
                        literal = Word.literal(attached, source=word.source)
                        values.append((option, literal, spec.values[option]))
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
                        literal = Word.literal(rest, source=word.source)
                        values.append((short, literal, spec.values[short]))
                    elif index < len(arguments):
                        values.append((short, arguments[index], spec.values[short]))
                        index += 1
                    break
        subtree = spec.subtree or bool(flags & spec.subtree_flags)
        pruned = self._pruned(context, raw, name, values)
        operand_kind = spec.operands
        if spec.write_flags and (
            flags & spec.write_flags
            or any(flag.startswith("-i") for flag in flags if name == "sed")
        ):
            operand_kind = "write"
        for option, value, kind in values:
            if kind is not None:
                self.access(context, raw, kind, value, glob=True, tier="a", subtree=subtree)
            if option in spec.skip_cancel:
                spec = replace(spec, skip=0)
        if flags & spec.skip_cancel:
            spec = replace(spec, skip=0)
        paths = operands[spec.skip :]
        if name in ("rsync", "scp"):
            # host:path names another machine; what it reaches there is not a local path.
            local = []
            for word in paths:
                text = self.value(context, word, location).text or ""
                if _REMOTE.match(text) or "://" in text:
                    add_network_target(self, text, word.source)
                else:
                    local.append(word)
            paths = local
        if operand_kind is not None:
            for position, word in enumerate(paths):
                kind = operand_kind
                if spec.first is not None and position == 0:
                    kind = spec.first
                if spec.last is not None and len(paths) >= 2 and position == len(paths) - 1:
                    kind = spec.last
                self.access(
                    context, raw, kind, word, glob=True, tier="a", subtree=subtree, pruned=pruned
                )
            if spec.last is not None and len(paths) == 1 and spec.slash:
                # copy x / xcopy x: the copy lands in the working directory.
                here = Word.literal(".", source=raw.source)
                self.access(context, raw, spec.last, here, glob=False, tier="a")
        if not paths and spec.default is not None and not fed:
            here = Word.literal(".", source=raw.source)
            self.access(
                context,
                raw,
                spec.default,
                here,
                glob=False,
                tier="a",
                subtree=subtree,
                pruned=pruned,
            )
        if fed:
            self.pipeline_targets(
                context, raw, "write" if "write" in (spec.operands, spec.last) else "read"
            )
        if raw.pipeline is not None:
            if (
                spec.listing
                and name in LISTING_PROGRAMS
                and not (fed or (raw.pipeline.index > 0 and not paths))
            ):
                roots = paths or [Word.literal(".", source=raw.source)]
                listing = self.resolve_listing(
                    context, raw, roots, recursive=subtree, children=name in ("ls", "dir")
                )
                context.listings[(raw.pipeline.id, raw.pipeline.index)] = listing
            elif spec.filter and raw.pipeline.index > 0 and not paths:
                context.listings[(raw.pipeline.id, raw.pipeline.index)] = Listing(filter=True)
        consumed = {id(word) for word in paths} | {id(value) for _, value, _ in values}
        self.tier_b(context, raw, [word for word in arguments if id(word) not in consumed])

    def _pruned(
        self,
        context: Context,
        raw: RawCommand,
        name: str,
        values: Sequence[tuple[str, Word, AccessKind | None]],
    ) -> tuple[str, ...]:
        """Folder names an exclusion option leaves out at every depth, where exact.

        A case-sensitive exclusion is not exact where file names ignore case
        (Windows): ``rg -g '!secret'`` would still read ``Secret``.
        """
        table = PRUNE_OPTIONS.get(name)
        if table is None:
            return ()
        location = self.first(context, raw)
        names: list[str] = []
        for option, value, _ in values:
            how = table.get(option)
            if how is None:
                continue
            negated, folded = how
            text = self.value(context, value, location).text
            if text is None or (_paths.WINDOWS and not folded):
                continue
            if negated:
                if not text.startswith("!"):
                    continue
                text = text[1:]
            if _PRUNED_NAME.match(text):
                names.append(text.rstrip("/").lower() if folded else text.rstrip("/"))
        return tuple(dict.fromkeys(names))

    def looks_like_option(self, text: str, spec: ProgramSpec) -> bool:
        if spec.slash:
            return bool(re.match(r"^/[A-Za-z?+-][^\\/]*$", text))
        return text.startswith("-") and text != "-"

    def is_switch(self, context: Context, raw: RawCommand, word: Word, *, slash: bool) -> bool:
        text = self.value(context, word, self.first(context, raw)).text
        if text is None:
            return False
        return bool(re.match(r"^/[A-Za-z?]", text)) if slash else text.startswith("-")

    # Pipelines --------------------------------------------------------------

    def listing_before(self, context: Context, pipeline: Pipeline) -> Listing | None:
        """What the pipeline hands the element at *pipeline*, through filters; None if unknown."""
        index = pipeline.index - 1
        while index >= 0:
            listing = context.listings.get((pipeline.id, index))
            if listing is None:
                return None
            if not listing.filter:
                return listing if listing.roots is not None else None
            index -= 1
        return None

    def pipeline_targets(self, context: Context, raw: RawCommand, kind: AccessKind) -> None:
        """A command that takes its targets from its input: bounded by the listing before it."""
        if raw.pipeline is None:
            self.unresolve(UNRESOLVED_KIND[kind], raw.source, "it takes its targets from its input")
            return
        listing = self.listing_before(context, raw.pipeline)
        if listing is None:
            self.unresolve(
                UNRESOLVED_KIND[kind],
                raw.source,
                "it takes its targets from the output of another command",
            )
            return
        self.listed_targets(listing, kind)

    def listed_targets(self, listing: Listing, kind: AccessKind) -> None:
        """Record *kind* for each entry *listing* names; changing a folder reaches below it."""
        for path, source, glob in listing.roots or ():
            self.add(kind, path, source, subtree=listing.recursive or kind == "write", glob=glob)

    # Words and paths --------------------------------------------------------

    def first(self, context: Context, raw: RawCommand) -> Location:
        return context.locations(raw.scope)[0]

    def value(self, context: Context, word: Word, location: Location) -> Value:
        if word.parameter is not None:
            if word.argument is None:
                return Value(word.source, word.source)
            argument = self.value(context, word.argument, location)
            if argument.text is None:
                return Value(None, word.source)
            return Value(f"-{word.parameter}:{argument.text}", word.source)
        if word.items is not None:
            values = [self.value(context, item, location) for item in word.items]
            if any(item.text is None for item in values):
                return Value(None, word.source)
            return Value(",".join(item.text or "" for item in values), word.source)
        pieces: list[str] = []
        for part in word.parts:
            if part.kind == "text":
                pieces.append(part.value)
                continue
            if part.kind == "dynamic":
                return Value(None, word.source)
            resolved = self.variable(context, part.value, location)
            if resolved is None:
                return Value(None, word.source)
            pieces.append(resolved)
        return Value("".join(pieces), word.source)

    def variable(self, context: Context, name: str, location: Location) -> str | None:
        if context.shell == "bash":
            if name in context.assigned or environment_key(name) in self.environment_dynamic:
                return None
            if name == "PWD":
                return None if location.cwd is None else str(location.cwd)
            if name == "OLDPWD" and location.previous is not None:
                return str(location.previous)
            if name == "HOME":
                return self.env.get("HOME") or str(self.home)
            return self.environment(name)
        if context.environment_changed or name in context.assigned:
            return None
        if name.startswith("env:"):
            variable = name[4:]
            if environment_key(variable) in self.environment_dynamic:
                return None
            if context.shell == "cmd" and variable == "cd":
                return None if location.cwd is None else str(location.cwd)
            return self.environment(variable)
        if name == "home":
            return str(self.home)
        if name == "pwd":
            return None if location.cwd is None else str(location.cwd)
        return None

    def environment(self, name: str) -> str | None:
        if _paths.WINDOWS:
            return self.env_folded.get(name.upper())
        return self.env.get(name)

    def literal_of(self, context: Context, raw: RawCommand, word: Word | None) -> str | None:
        if word is None:
            return None
        return self.value(context, word, self.first(context, raw)).text

    def resolve_word(
        self, context: Context, raw: RawCommand, word: Word, *, glob: bool
    ) -> list[ListedRoot] | None:
        """The paths a listing word names for every candidate location, None when unknown.

        Each is ``(path, source, pattern)``: a wildcard's literal folder and the
        pattern below it, or the path itself and None.
        """
        resolved: list[ListedRoot] = []
        brace = context.shell == "bash"
        for location in context.locations(raw.scope):
            value = self.value(context, word, location)
            if value.text is None:
                return None
            text = _paths.without_long_prefix(value.text)
            pattern = None
            if glob and _paths.has_glob(text, brace=brace):
                split = _paths.glob_split(text, brace=brace)
                if isinstance(split, Unresolvable):
                    return None
                text, pattern = split
            path = _paths.resolve(
                text,
                location.cwd,
                self.home,
                tilde=word.tilde or context.shell != "bash",
                msys=self.msys and context.shell == "bash",
                drives=dict(location.drives),
            )
            if not isinstance(path, Path):
                return None
            resolved.append((path, word.source, pattern))
        return resolved

    def resolve_listing(
        self,
        context: Context,
        raw: RawCommand,
        roots: Sequence[Word],
        *,
        recursive: bool,
        children: bool = False,
    ) -> Listing:
        """What a listing of *roots* hands on: with *children*, the entries in each folder."""
        found: list[ListedRoot] = []
        for root in roots:
            resolved = self.resolve_word(context, raw, root, glob=True)
            if resolved is None:
                return Listing(roots=None)
            for path, source, pattern in resolved:
                if pattern is not None and recursive:
                    pattern = None  # a recursive listing reaches the whole folder
                elif pattern is None and children and not recursive:
                    pattern = "*"
                found.append((path, source, pattern))
        return Listing(roots=found, recursive=recursive)

    def access(
        self,
        context: Context,
        raw: RawCommand,
        kind: PathAccessKind,
        word: Word,
        *,
        glob: bool,
        tier: str,
        subtree: bool = False,
        pruned: tuple[str, ...] = (),
        deep: bool = False,
    ) -> list[tuple[Path, str]]:
        """Record the access *word* makes from every candidate location; return the paths.

        A wildcard reaches the entries it matches in its literal folder; with
        *deep* (``Get-ChildItem *.md -Recurse``) the whole folder.
        """
        found: list[tuple[Path, str]] = []
        brace = context.shell == "bash"
        for item in items(word):
            for location in context.locations(raw.scope):
                value = self.value(context, item, location)
                self.collect_urls(value)
                if value.text is None:
                    if tier == "a":
                        self.unresolve(
                            UNRESOLVED_KIND[kind], item.source, "the path is computed when it runs"
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
                pattern: str | None = None
                reaches_below = subtree
                if (
                    glob
                    and (context.shell != "bash" or item.glob)
                    and _paths.has_glob(text, brace=brace)
                ):
                    split = _paths.glob_split(text, brace=brace)
                    if isinstance(split, Unresolvable):
                        self.unresolve(
                            UNRESOLVED_KIND[kind], item.source, f"the path {split.reason}"
                        )
                        continue
                    text, pattern = split
                    if deep:
                        pattern, reaches_below = None, True
                path = _paths.resolve(
                    text,
                    location.cwd,
                    self.home,
                    tilde=item.tilde or context.shell == "powershell",
                    msys=self.msys and context.shell == "bash",
                    drives=dict(location.drives),
                )
                if path is None:
                    continue
                if isinstance(path, Unresolvable):
                    if tier == "a" or location.cwd is None:
                        self.unresolve(
                            UNRESOLVED_KIND[kind], item.source, f"the path {path.reason}"
                        )
                    continue
                self.add(
                    kind, path, item.source, subtree=reaches_below, glob=pattern, pruned=pruned
                )
                found.append((path, item.source))
        return found

    def tier_b(self, context: Context, raw: RawCommand, words: Iterable[Word]) -> None:
        location = self.first(context, raw)
        for word in words:
            if word.block:
                continue
            target = word
            text = self.value(context, word, location).text
            if word.parameter is not None:
                if word.argument is None:
                    continue
                target = word.argument
            elif text is not None and re.match(r"^-{0,2}[A-Za-z_][\w.-]*=", text):
                # --output=../x, DESTDIR=/opt
                target = Word.literal(text.split("=", 1)[1], source=word.source)
            self.access(context, raw, "read", target, glob=True, tier="b")

    def collect_urls(self, value: Value) -> None:
        for match in _URL.finditer(value.text if value.text is not None else value.source):
            self._urls.setdefault(match.group(0).rstrip(".,;)"), None)

    # Output -----------------------------------------------------------------

    def emit(self, context: Context, name: str, arguments: Sequence[Word], raw: RawCommand) -> None:
        location = self.first(context, raw)
        words = []
        for word in arguments:
            value = self.value(context, word, location)
            self.collect_urls(value)
            words.append(
                CommandWord(value.text if value.text is not None else word.source, value.static)
            )
        self._commands.append(
            SimpleCommand(name, tuple(words), raw.source[:SOURCE_LIMIT] or name, context.shell)
        )

    def add(
        self,
        kind: PathAccessKind,
        path: Path,
        source: str,
        *,
        subtree: bool = False,
        glob: str | None = None,
        pruned: tuple[str, ...] = (),
    ) -> None:
        whole = (kind, path, None, True, ())
        if whole in self._paths:
            return  # the whole subtree is already in the plan
        key = (kind, path, glob, subtree, pruned)
        if key not in self._paths:
            self._paths[key] = PlannedPathAccess(
                kind, path, source[:SOURCE_LIMIT], subtree, glob, pruned
            )

    def add_url(self, url: str, source: str) -> None:
        """An http(s) URL the command fetches that its words do not spell out."""
        del source
        self._urls.setdefault(url, None)

    def add_host(self, host: str, source: str) -> None:
        """A host the command connects to other than by an http(s) URL."""
        del source
        self._hosts.setdefault(host.strip("[]").lower(), None)

    def unresolve(self, kind: UnresolvedKind, source: str, reason: str) -> None:
        source = source[:SOURCE_LIMIT]
        self._unresolved.setdefault((kind, source, reason), UnresolvedElement(kind, source, reason))

    def path_or_none(self, text: str | None) -> Path | None:
        if not text:
            return None
        resolved = _paths.resolve(text, None, self.home)
        return resolved if isinstance(resolved, Path) else None
