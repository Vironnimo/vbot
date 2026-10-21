"""What git subcommands change and read in the work tree and index, for ``handle_git``.

Pathspec operands are subtree accesses of what they name; subcommands that
replace the whole work tree record a ``work_tree`` write the check places at
the repository's top folder; index paths (``:path``) count from that top too.

Operations that bring tracked files to a committed state or a patch (``pull``,
``restore``, ``apply``) and reads that list or compare them (``add``,
``diff``, ``status``) are ``git`` accesses: deny rules and rule files below
what they reach do not refuse them, only a path that names one. Operations
that delete or move files the history may not hold (``clean``, ``rm``, ``mv``,
``stash -u``), wildcard pathspecs, and reads that print file contents
(``grep``, ``blame``) stay ordinary subtree accesses.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from core.permissions.commands import _paths
from core.permissions.commands._state import Context
from core.permissions.commands._syntax import RawCommand, Word

if TYPE_CHECKING:
    from core.permissions.commands._analysis import Analysis


# Subcommands that replace files across the repository's whole work tree.
_GIT_WORK_TREE = frozenset(
    {
        "am", "bisect", "cherry-pick", "filter-branch", "merge", "pull", "read-tree", "rebase",
        "revert", "sparse-checkout", "submodule", "switch",
    }
)  # fmt: skip
# Options that take a separate value, per subcommand whose operands are files.
_GIT_VALUES: Mapping[str, frozenset[str]] = {
    "add": frozenset({"--chmod", "--pathspec-from-file"}),
    "apply": frozenset(
        {"-p", "-C", "--exclude", "--include", "--directory", "--whitespace",
         "--build-fake-ancestor"}
    ),
    "blame": frozenset({"-L", "-S", "--contents", "--ignore-rev", "--ignore-revs-file"}),
    "checkout": frozenset({"-b", "-B", "--orphan", "--conflict", "--pathspec-from-file"}),
    "checkout-index": frozenset({"--prefix", "--stage"}),
    "clean": frozenset({"-e", "--exclude"}),
    "commit": frozenset(
        {"-m", "--message", "-F", "--file", "-C", "--reuse-message", "-c", "--reedit-message",
         "--author", "--date", "-t", "--template", "--fixup", "--squash", "--trailer",
         "--cleanup", "--pathspec-from-file"}
    ),
    "diff": frozenset({"-O", "-S", "-G", "-l", "--src-prefix", "--dst-prefix"}),
    "grep": frozenset(
        {"-e", "-f", "-m", "--max-count", "-A", "-B", "-C", "--after-context",
         "--before-context", "--context", "--max-depth", "--threads", "-O"}
    ),
    "ls-files": frozenset({"-x", "--exclude", "-X", "--exclude-from", "--format"}),
    "rebase": frozenset({"-x", "--exec", "--onto", "-s", "--strategy", "-X"}),
    "reset": frozenset({"--pathspec-from-file"}),
    "restore": frozenset({"-s", "--source", "--conflict", "--pathspec-from-file"}),
    "rm": frozenset({"--pathspec-from-file"}),
    "stash": frozenset({"-m", "--message", "--pathspec-from-file"}),
}  # fmt: skip
# Subcommands that read the working-tree files their operands name. An operand
# that is a revision names no file there, so reading it refuses nothing.
_GIT_READS = frozenset(
    {"add", "annotate", "blame", "commit", "diff", "difftool", "grep", "hash-object",
     "ls-files", "stage", "status"}
)  # fmt: skip
# Reads that print the contents of the files they name, checked like any read.
_GIT_CONTENT_READS = frozenset({"annotate", "blame", "grep", "hash-object"})
# Subcommands whose pathspecs delete or move files, also ones git does not track.
_GIT_UNTRACKED = frozenset({"clean", "mv", "rm", "worktree"})
_GIT_STASH_TREE = frozenset({"push", "save", "pop", "apply", "branch"})
_GIT_STASH_UNTRACKED = frozenset({"-u", "--include-untracked", "-a", "--all"})
_GIT_APPLY_READ_ONLY = frozenset({"--check", "--stat", "--numstat", "--summary"})


def changed_files(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    subcommand: str,
    rest: Sequence[tuple[Word, str | None]],
) -> set[int]:
    """The files a writing git subcommand changes; the ids of the words it consumed.

    Pathspec operands are subtree writes of what they name (``git restore .``
    the whole folder); a subcommand that replaces the work tree is a
    ``work_tree`` write; one that writes only the index or ``.git`` writes the
    folder it works in, without what is below.
    """
    options, before, after = _git_arguments(rest, _GIT_VALUES.get(subcommand, frozenset()))
    operands = before + (after or [])
    action = before[0][1] if before else None
    flags = {option.partition("=")[0] for option in options}
    pathspecs: list[tuple[Word, str | None]] = []
    consumed: set[int] = set()
    work_tree = subcommand in _GIT_WORK_TREE
    if (
        (subcommand, action) in (("submodule", "foreach"), ("bisect", "run"))
        or (subcommand == "rebase" and flags & {"-x", "--exec"})
        or (subcommand == "filter-branch" and any(flag.endswith("-filter") for flag in flags))
    ):
        analysis.unresolve("command", raw.source, "git runs a command its arguments name")
    if flags & {"--pathspec-from-file", "--prefix", "--unsafe-paths"}:
        analysis.unresolve("path_write", raw.source, "git takes the paths it changes elsewhere")
    if subcommand == "checkout":
        # Without "--" the first operand may be a branch or a path: both.
        pathspecs = after if after is not None else operands
        work_tree = after is None
    elif subcommand == "checkout-index":
        pathspecs = operands
        work_tree = bool(flags & {"-a", "--all"})
    elif subcommand == "restore":
        staged_only = any(_short_flag(option, "S") or option == "--staged" for option in options)
        worktree = any(_short_flag(option, "W") or option == "--worktree" for option in options)
        pathspecs = operands if worktree or not staged_only else []
    elif subcommand == "rm":
        pathspecs = [] if "--cached" in flags else operands
    elif subcommand in ("mv", "clean"):
        pathspecs = operands or [(Word.literal(".", source=raw.source), ".")]
    elif subcommand == "reset":
        work_tree = bool(flags & {"--hard", "--merge", "--keep"})
    elif subcommand == "stash":
        work_tree = action is None or action in _GIT_STASH_TREE
        if action == "push":
            pathspecs = before[1:] + (after or [])
        elif action is None:
            pathspecs = after or []
    elif subcommand == "worktree" and action in ("remove", "move"):
        pathspecs = before[1:]
    elif subcommand == "apply":
        for word, _ in operands:
            analysis.access(context, raw, "read", word, glob=False, tier="a")
        consumed = {id(word) for word, _ in operands}
        if not (flags & _GIT_APPLY_READ_ONLY or "--cached" in flags) or "--apply" in flags:
            # From inside a repository it changes only files below its folder.
            pathspecs = [(Word.literal(".", source=raw.source), ".")]
    tracked = subcommand not in _GIT_UNTRACKED and not (
        subcommand == "stash" and flags & _GIT_STASH_UNTRACKED
    )
    for word, text in pathspecs:
        _git_pathspec(analysis, context, raw, word, text, tracked=tracked)
    if work_tree:
        analysis.access(
            context,
            raw,
            "write",
            Word.literal(".", source=raw.source),
            glob=False,
            tier="a",
            subtree=True,
            work_tree=True,
            git=tracked,
        )
    elif not pathspecs:
        analysis.access(
            context, raw, "write", Word.literal(".", source=raw.source), glob=False, tier="a"
        )
    return consumed | {id(word) for word, _ in pathspecs}


def read_files(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    subcommand: str,
    rest: Sequence[tuple[Word, str | None]],
) -> set[int]:
    """The working-tree and index files a git subcommand reads; the ids of the words it consumed.

    An operand of a reading subcommand is a subtree read of what it names
    (``git diff -- secrets``, ``git add .``); ``rev:path`` reads history,
    which is not checked. An index path (``:path``, ``:0:path``) is read in
    every subcommand. A pathless ``git grep`` searches the folder it works in.
    """
    options, before, after = _git_arguments(rest, _GIT_VALUES.get(subcommand, frozenset()))
    consumed: set[int] = set()
    if (
        subcommand == "grep"
        and before
        and not any(option.partition("=")[0] in ("-e", "-f", "--file") for option in options)
    ):
        consumed.add(id(before[0][0]))  # the pattern
        before = before[1:]
    paths: list[tuple[Word, str | None]] = []
    for word, text in before + (after or []):
        if text is not None and text.startswith(":"):
            _git_index_read(analysis, context, raw, word, text)
            consumed.add(id(word))
        elif text is None or ":" not in text or re.match(r"^[A-Za-z]:[\\/]", text):
            paths.append((word, text))
    if subcommand not in _GIT_READS:
        return consumed
    if subcommand == "grep" and not before and not after:
        paths = [(Word.literal(".", source=raw.source), ".")]
    listing = subcommand not in _GIT_CONTENT_READS and "--no-index" not in options
    for word, text in paths:
        target = word
        git = listing
        if text is not None and _paths.has_glob(text):
            prefix = _paths.glob_prefix(text)
            if isinstance(prefix, _paths.Unresolvable):
                analysis.unresolve("path_read", word.source, f"the path {prefix.reason}")
                continue
            target = Word.literal(prefix, source=word.source)
            git = False
        analysis.access(context, raw, "read", target, glob=False, tier="a", subtree=True, git=git)
        consumed.add(id(word))
    return consumed


def _git_index_read(
    analysis: Analysis, context: Context, raw: RawCommand, word: Word, text: str
) -> None:
    """``:path`` (or ``:0:path``) names a file from the repository's top folder.

    Pathspec magic that leaves files out (``:!x``, ``:^x``, ``:(exclude)x``)
    reads nothing more; other magic is not analyzed.
    """
    spec = text[1:]
    if spec.startswith(("!", "^", "(exclude", "(exclude,")):
        return
    if spec.startswith("("):
        analysis.unresolve("path_read", word.source, "git pathspec magic is not analyzed")
        return
    spec = re.sub(r"^[0-3]:", "", spec).removeprefix("/").replace("\\", "/")
    if spec.startswith(("./", "../")):
        analysis.access(
            context, raw, "read", Word.literal(spec, source=word.source), glob=False,
            tier="a", subtree=True,
        )  # fmt: skip
        return
    parts = [part for part in spec.split("/") if part not in ("", ".")]
    if ".." in parts:
        analysis.unresolve("path_read", word.source, "the index path climbs out of a folder")
        return
    for location in context.locations(raw.scope):
        if location.cwd is None:
            analysis.unresolve("path_read", word.source, "the folder git works in is unknown")
            continue
        analysis.add(
            "read",
            location.cwd,
            word.source,
            subtree=True,
            glob="/".join(parts) or None,
            work_tree=True,
        )


def _git_arguments(
    rest: Sequence[tuple[Word, str | None]], values: frozenset[str]
) -> tuple[list[str], list[tuple[Word, str | None]], list[tuple[Word, str | None]] | None]:
    """A git subcommand's options, its operands before ``--`` and after it (None without)."""
    options: list[str] = []
    before: list[tuple[Word, str | None]] = []
    after: list[tuple[Word, str | None]] | None = None
    index = 0
    while index < len(rest):
        word, text = rest[index]
        index += 1
        if after is not None:
            after.append((word, text))
        elif text == "--":
            after = []
        elif text is not None and text.startswith("-") and text != "-":
            options.append(text)
            if "=" not in text and text in values:
                index += 1
        else:
            before.append((word, text))
    return options, before, after


def _short_flag(option: str, letter: str) -> bool:
    return not option.startswith("--") and letter in option[1:]


def _git_pathspec(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    word: Word,
    text: str | None,
    *,
    tracked: bool,
) -> None:
    """A pathspec git changes the files of: everything below what it names.

    *tracked*: the subcommand changes only files to a committed state or a
    patch, so a literal pathspec is a ``git`` access. A git wildcard also
    matches across folders (``*.pem`` matches ``keys/a.pem``), so it reaches
    its whole literal folder as an ordinary subtree access.
    """
    if text is not None and text.startswith(":"):
        analysis.unresolve("path_write", word.source, "git pathspec magic is not analyzed")
        return
    target = word
    if text is not None and _paths.has_glob(text):
        prefix = _paths.glob_prefix(text)
        if isinstance(prefix, _paths.Unresolvable):
            analysis.unresolve("path_write", word.source, f"the path {prefix.reason}")
            return
        target = Word.literal(prefix, source=word.source)
        tracked = False
    analysis.access(context, raw, "write", target, glob=False, tier="a", subtree=True, git=tracked)
