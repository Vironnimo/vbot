"""Git's metadata operations and explicit paths outside its ordinary work tree.

The handler owns command interpretation; this internal unit keeps configuration,
credential files and environment redirects together with their read/write modes.
It never opens a repository or reads configuration while analyzing a command.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.permissions.commands._state import UNRESOLVED_KIND, Context, joined_path
from core.permissions.commands._syntax import Part, RawCommand, Word
from core.permissions.commands.commands import PathAccessKind

if TYPE_CHECKING:
    from core.permissions.commands._analysis import Analysis


_CONFIG_VALUES = frozenset(
    {"-f", "--file", "-t", "--type", "--default", "--value", "--url", "--comment", "--blob"}
)
_CONFIG_READS = frozenset(
    {
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--get-color",
        "--get-colorbool",
        "--list",
        "-l",
    }
)
_CONFIG_WRITES = frozenset(
    {
        "--add",
        "--replace-all",
        "--unset",
        "--unset-all",
        "--rename-section",
        "--remove-section",
        "--edit",
        "-e",
    }
)
_CONFIG_ACTIONS = frozenset(
    {"get", "list", "set", "unset", "rename-section", "remove-section", "edit"}
)
_CONFIG_OPTIONS = (
    _CONFIG_VALUES
    | _CONFIG_READS
    | _CONFIG_WRITES
    | frozenset(
        {
            "--global",
            "--system",
            "--local",
            "--worktree",
            "--includes",
            "--all",
            "--regexp",
            "--fixed-value",
            "--show-origin",
            "--show-scope",
            "--name-only",
            "--null",
            "-z",
            "--bool",
            "--int",
            "--bool-or-int",
            "--bool-or-str",
            "--path",
            "--expiry-date",
        }
    )
)
_LIST_VALUES = frozenset(
    {
        "--format",
        "--sort",
        "--contains",
        "--no-contains",
        "--merged",
        "--no-merged",
        "--points-at",
        "--column",
        "--color",
        "-m",
    }
)


def _arguments(
    arguments: Sequence[str | None], values: frozenset[str] = frozenset()
) -> tuple[set[str], list[int], dict[str, int | str | None]]:
    """Options, operand positions, and option values (position or attached text)."""
    options: set[str] = set()
    operands: list[int] = []
    named: dict[str, int | str | None] = {}
    index = 0
    ended = False
    while index < len(arguments):
        text = arguments[index]
        if not ended and text == "--":
            ended = True
        elif not ended and text is not None and text.startswith("-") and text != "-":
            option, equals, attached = text.partition("=")
            if text.startswith("-f") and len(text) > 2 and "-f" in values:
                option, equals, attached = "-f", "=", text[2:]
            if option.startswith("--no-"):
                options.discard("--" + option[5:])
            else:
                options.add(option)
            if option in values:
                if equals:
                    named[option] = attached
                else:
                    index += 1
                    named[option] = index if index < len(arguments) else None
        else:
            operands.append(index)
        index += 1
    return options, operands, named


def writes(subcommand: str, arguments: Sequence[str | None], *, default: bool) -> bool:
    """Distinguish metadata queries from mutations, including old/new config syntax."""
    if subcommand == "config":
        options, indices, _ = _arguments(arguments, _CONFIG_VALUES)
        operands = [arguments[index] for index in indices]
        if options & _CONFIG_WRITES:
            return True
        if options & _CONFIG_READS:
            return False
        if operands and operands[0] in _CONFIG_ACTIONS:
            return operands[0] not in {"get", "list"}
        return len(operands) > 1 or any(value is None for value in arguments)
    options, indices, _ = _arguments(arguments, _LIST_VALUES)
    operands = [arguments[index] for index in indices]
    if subcommand in {"branch", "tag"}:
        letters = "dDmMcCf" if subcommand == "branch" else "dafsu"
        mutations = {
            "--delete",
            "--force",
            "--move",
            "--copy",
            "--edit-description",
            "--set-upstream-to",
            "--unset-upstream",
            "--annotate",
            "--sign",
            "--local-user",
        }
        if options & mutations or any(
            not flag.startswith("--") and any(letter in flag[1:] for letter in letters)
            for flag in options
        ):
            return True
        if options & {"--list", "-l", "--show-current"} or (
            subcommand == "tag" and options & {"--verify", "-v"}
        ):
            return False
        if options & {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at"}:
            return False
        return bool(operands)
    if subcommand == "symbolic-ref":
        return bool(options & {"--delete", "-d"}) or len(operands) > 1
    if subcommand == "reflog":
        return bool(operands) and operands[0] in {None, "expire", "delete", "drop", "write"}
    if subcommand == "remote":
        return bool(operands) and operands[0] not in {"show", "get-url"}
    if subcommand in {"stash", "worktree", "notes", "lfs", "replace"}:
        read_actions = {
            "stash": {"list", "show"},
            "worktree": {"list"},
            "notes": {"list", "show", "get-ref"},
            "lfs": {"env", "ls-files", "status", "version"},
            "replace": set(),
        }
        if operands and operands[0] in read_actions[subcommand]:
            return False
        if subcommand in {"notes", "replace"} and not operands:
            return False
        if subcommand == "replace" and options & {"-l", "--list"}:
            return False
        return default
    if subcommand == "hash-object":
        return "-w" in options
    if subcommand == "bundle":
        return bool(operands) and operands[0] in {None, "unbundle"}
    return default


def environment_paths(
    analysis: Analysis, context: Context, raw: RawCommand, *, writing: bool
) -> None:
    """An environment redirect is as real a target as --git-dir/--work-tree."""
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
    ):
        word = analysis.environment_value(context, name)
        if word is not None:
            analysis.access(
                context,
                raw,
                "write" if writing else "read",
                word,
                glob=False,
                tier="a",
                subtree=name != "GIT_INDEX_FILE",
            )


def handle_files(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    subcommand: str,
    arguments: Sequence[Word],
    texts: Sequence[str | None],
    *,
    writing: bool,
) -> bool:
    """Handle commands whose operands are metadata values, not work-tree pathspecs."""
    if subcommand == "config":
        _config(analysis, context, raw, arguments, texts, writing=writing)
        return True
    if subcommand == "lfs" and texts and texts[0] in {"install", "uninstall"}:
        options, _, named = _arguments(texts[1:], frozenset({"--file"}))
        if "--file" in options:
            target = _option_word(arguments[1:], named["--file"], raw.source)
            analysis.access(context, raw, "write", target, glob=False, tier="a")
        else:
            scope = next(
                (
                    scope
                    for flags, scope in (
                        ({"--system"}, "--system"),
                        ({"--local", "-l"}, "--local"),
                        ({"--worktree", "-w"}, "--worktree"),
                    )
                    if flags & options
                ),
                "--global",
            )
            _config(analysis, context, raw, [Word.literal(scope)], [scope], writing=True)
        return False  # It can also install or remove the repository's hook.
    if subcommand == "credential-store":
        options, operands, named = _arguments(texts, frozenset({"--file"}))
        action = texts[operands[0]] if operands else None
        access: PathAccessKind = "read" if action == "get" else "write"
        explicit = _option_word(arguments, named.get("--file"), raw.source)
        targets = (
            [explicit]
            if "--file" in options
            else _home_files(analysis, context, raw, ".git-credentials", "credentials")
        )
        for word in targets:
            analysis.access(context, raw, access, word, glob=False, tier="a")
        return True
    if subcommand == "bundle":
        _, operands, _ = _arguments(texts, frozenset({"--version", "--filter"}))
        if len(operands) >= 2:
            action, target = texts[operands[0]], arguments[operands[1]]
            if action is None:
                analysis.unresolve("path_write", raw.source, "the path is computed when it runs")
            access = "write" if action in {None, "create"} else "read"
            if analysis.literal_of(context, raw, target) != "-":
                analysis.access(context, raw, access, target, glob=False, tier="a")
            if action == "unbundle":
                analysis.access(context, raw, "write", Word.literal("."), glob=False, tier="a")
        return True
    return False


def _option_word(arguments: Sequence[Word], value: int | str | None, source: str) -> Word:
    if isinstance(value, int):
        return arguments[value]
    if isinstance(value, str):
        return Word.literal(value, source=source)
    # A missing argument cannot identify a path, even if git would reject it too.
    return Word((Part("dynamic", source),), source)


def _home_files(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    home_file: str,
    config_file: str,
) -> list[Word]:
    home = analysis.environment_value(context, "HOME") or Word.literal(str(analysis.home))
    xdg = analysis.environment_value(context, "XDG_CONFIG_HOME") or joined_path(
        home, Word.literal(".config")
    )
    return [
        joined_path(home, Word.literal(home_file)),
        joined_path(xdg, Word.literal(f"git/{config_file}")),
    ]


def _config(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    arguments: Sequence[Word],
    texts: Sequence[str | None],
    *,
    writing: bool,
) -> None:
    options, _, named = _arguments(texts, _CONFIG_VALUES)
    access: PathAccessKind = "write" if writing else "read"
    if any(text is None for text in texts) or options - _CONFIG_OPTIONS:
        analysis.unresolve(
            UNRESOLVED_KIND[access],
            raw.source,
            "the Git configuration options do not identify one file",
        )
    if "--file" in options or "-f" in options:
        word = _option_word(arguments, named.get("--file", named.get("-f")), raw.source)
        if analysis.literal_of(context, raw, word) != "-":
            analysis.access(context, raw, access, word, glob=False, tier="a")
        return
    if "--global" in options:
        explicit = analysis.environment_value(context, "GIT_CONFIG_GLOBAL")
        targets = (
            [explicit]
            if explicit is not None
            else _home_files(analysis, context, raw, ".gitconfig", "config")
        )
        for word in targets:
            analysis.access(context, raw, access, word, glob=False, tier="a")
        return
    if "--system" in options:
        explicit = analysis.environment_value(context, "GIT_CONFIG_SYSTEM")
        if explicit is None:
            analysis.unresolve(
                UNRESOLVED_KIND[access], raw.source, "the system Git configuration path is unknown"
            )
        else:
            analysis.access(context, raw, access, explicit, glob=False, tier="a")
        return
    if "--blob" in options:
        return  # A Git object, not a filesystem path.
    config = analysis.environment_value(context, "GIT_CONFIG")
    if config is not None and not options & {"--local", "--worktree"}:
        analysis.access(context, raw, access, config, glob=False, tier="a")
        return
    default_read = not writing and not options & {"--local", "--worktree"}
    if default_read:
        global_config = analysis.environment_value(context, "GIT_CONFIG_GLOBAL")
        targets = (
            [global_config]
            if global_config is not None
            else _home_files(analysis, context, raw, ".gitconfig", "config")
        )
        for word in targets:
            analysis.access(context, raw, "read", word, glob=False, tier="a")
        no_system = analysis.environment_value(context, "GIT_CONFIG_NOSYSTEM")
        value = analysis.literal_of(context, raw, no_system)
        skips_system = value is not None and (
            value.lower() in {"true", "yes", "on"} or (value.isdigit() and int(value) != 0)
        )
        if not skips_system:
            system_config = analysis.environment_value(context, "GIT_CONFIG_SYSTEM")
            if system_config is None:
                analysis.unresolve(
                    "path_read",
                    raw.source,
                    "the system Git configuration path is unknown; "
                    "select --local or --file to read a known configuration",
                )
            else:
                analysis.access(context, raw, "read", system_config, glob=False, tier="a")
    # --worktree falls back to config unless extensions.worktreeConfig is enabled.
    # The analyzer does not read configuration, so both possible targets must pass.
    config_names = (
        ("config", "config.worktree") if "--worktree" in options or default_read else ("config",)
    )
    git_dir = analysis.environment_value(context, "GIT_DIR")
    for config_name in config_names:
        if git_dir is not None:
            analysis.access(
                context,
                raw,
                access,
                joined_path(git_dir, Word.literal(config_name)),
                glob=False,
                tier="a",
            )
        else:
            for location in context.locations(raw.scope):
                if location.cwd is None:
                    analysis.unresolve(
                        UNRESOLVED_KIND[access], raw.source, "the folder git works in is unknown"
                    )
                else:
                    analysis.add(
                        access, location.cwd, raw.source, work_tree=True, glob=f".git/{config_name}"
                    )
