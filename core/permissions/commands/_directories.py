"""Working-directory tracking: ``cd``, ``pushd``/``popd``, ``cd -``, drive switches.

Every change may fail, so each candidate location keeps the old folder beside
the new one (``Location``); a dynamic or unplaceable target makes the folder
unknown. Each folder changed into is a ``start_dir`` access.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from core.permissions.commands import _paths
from core.permissions.commands._paths import Unresolvable
from core.permissions.commands._state import MAX_CANDIDATES, Context, Location, joined
from core.permissions.commands._syntax import RawCommand, Word

if TYPE_CHECKING:
    from core.permissions.commands._analysis import Analysis

WILDCARD_FOLDER = "it changes into a folder a wildcard picks when it runs"


def cmd_directory(
    analysis: Analysis, context: Context, raw: RawCommand, name: str, arguments: Sequence[Word]
) -> None:
    """cmd's ``cd``, ``chdir``, ``pushd`` and ``popd``; *name* is lower-case."""
    operands = [
        word for word in arguments if not analysis.is_switch(context, raw, word, slash=True)
    ]
    if name == "popd":
        change_directory(analysis, context, raw, "pop", None)
        return
    if not operands:
        return
    # cd takes the rest of the line, spaces included, as one folder.
    target = operands[0] if len(operands) == 1 else joined(operands)
    cross_drive = name in ("cd", "chdir") and not any(
        (analysis.value(context, word, analysis.first(context, raw)).text or "").lower() == "/d"
        for word in arguments
    )
    change_directory(
        analysis,
        context,
        raw,
        "push" if name == "pushd" else "set",
        target,
        keep_drive=cross_drive,
        wildcards=True,
    )


def bash_directory(
    analysis: Analysis, context: Context, raw: RawCommand, name: str, arguments: Sequence[Word]
) -> None:
    location = analysis.first(context, raw)
    texts = [analysis.value(context, word, location).text for word in arguments]
    operands = [
        word
        for word, text in zip(arguments, texts, strict=True)
        if not ((text or "x").startswith("-") and text != "-")
    ]
    operand_texts = [analysis.value(context, word, location).text or "" for word in operands]
    if name == "popd":
        mode = "unknown" if any(text.startswith(("+", "-")) for text in operand_texts) else "pop"
        change_directory(analysis, context, raw, mode, None)
        return
    if name == "pushd":
        if not operands or operand_texts[0].startswith(("+", "-")):
            change_directory(analysis, context, raw, "unknown", None)
        elif "-n" not in texts:
            change_directory(analysis, context, raw, "push", operands[0])
        return
    if not operands:
        home = analysis.env.get("HOME")
        change_directory(
            analysis, context, raw, "set", Word.literal(home or "~", source=raw.source)
        )
    elif operand_texts[0] == "-":
        change_directory(analysis, context, raw, "previous", None)
    else:
        change_directory(analysis, context, raw, "set", operands[0])


def change_directory(
    analysis: Analysis,
    context: Context,
    raw: RawCommand,
    mode: str,
    target: Word | None,
    *,
    keep_old: bool = True,
    keep_drive: bool = False,
    wildcards: bool = False,
) -> list[Path]:
    """Move every candidate location; the old one stays, since the change may fail.

    *mode* is ``set``, ``push``, ``pop``, ``previous`` or ``unknown``. Returns
    the folders changed into. *wildcards*: the command matches wildcards in
    *target* itself (cmd's ``cd``, PowerShell's ``-Path``); bash expands an
    unquoted one before any command runs. Such a target is unknown, like a
    computed one: its literal text names no folder the command enters.
    """
    locations = context.locations(raw.scope)
    moved: list[Location] = []
    entered: list[Path] = []
    for location in locations:
        if keep_old:
            moved.append(location)
        if mode == "pop":
            if location.stack:
                head, *rest = location.stack
                moved.append(replace(location.moved(head), stack=tuple(rest)))
                if head is not None:
                    analysis.add("start_dir", head, raw.source)
                    entered.append(head)
            continue
        if mode == "previous":
            previous = location.previous
            if previous is None and context.shell == "bash":
                previous = analysis.path_or_none(analysis.env.get("OLDPWD"))
            if previous is None:
                analysis.unresolve(
                    "cwd", raw.source, "it returns to a folder this analysis cannot determine"
                )
            else:
                analysis.add("start_dir", previous, raw.source)
                entered.append(previous)
            moved.append(location.moved(previous))
            continue
        if mode == "unknown" or target is None:
            analysis.unresolve(
                "cwd", raw.source, "it changes into a folder this analysis cannot determine"
            )
            moved.append(location.moved(None))
            continue
        value = analysis.value(context, target, location)
        if value.text is None:
            analysis.unresolve(
                "cwd", target.source, "it changes into a folder computed when it runs"
            )
            moved.append(location.moved(None))
            continue
        if context.shell == "powershell" and value.text in ("-", "+"):
            if value.text == "-" and location.previous is not None:
                analysis.add("start_dir", location.previous, raw.source)
                entered.append(location.previous)
                moved.append(location.moved(location.previous))
            elif value.text == "-":
                analysis.unresolve(
                    "cwd", raw.source, "it returns to a folder this analysis cannot determine"
                )
                moved.append(location.moved(None))
            continue
        if expands_wildcard(context.shell, target, value.text, wildcards=wildcards):
            analysis.unresolve("cwd", target.source, WILDCARD_FOLDER)
            moved.append(location.moved(None))
            continue
        resolved = _paths.resolve(
            value.text,
            location.cwd,
            analysis.home,
            tilde=target.tilde or context.shell == "powershell",
            msys=analysis.msys and context.shell == "bash",
            drives=dict(location.drives),
        )
        if not isinstance(resolved, Path):
            reason = (
                resolved.reason
                if isinstance(resolved, Unresolvable)
                else "is not a folder of the filesystem"
            )
            analysis.unresolve("cwd", target.source, f"the folder {reason}")
            moved.append(location.moved(None))
            continue
        analysis.add("start_dir", resolved, target.source)
        entered.append(resolved)
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
    set_locations(analysis, context, raw, moved)
    return entered


def expands_wildcard(shell: str, target: Word, text: str, *, wildcards: bool = False) -> bool:
    """Whether a wildcard in *target* (value *text*) picks the folder when the line runs.

    bash expands an unquoted wildcard in any word; elsewhere only a command that
    matches wildcards itself (*wildcards*) does: cmd ``*`` and ``?``, PowerShell
    also ``[``.
    """
    text = _paths.without_long_prefix(text)
    if shell == "bash":
        return target.glob and _paths.has_glob(text, brace=True)
    if shell == "cmd":
        return wildcards and any(character in text for character in "*?")
    return wildcards and _paths.has_glob(text)


def switch_drive(analysis: Analysis, context: Context, raw: RawCommand, letter: str) -> None:
    """``D:`` in cmd and PowerShell: the folder the line last used on that drive, or its root."""
    moved: list[Location] = []
    for location in context.locations(raw.scope):
        moved.append(location)
        if _paths.drive_of(location.cwd) == letter:
            continue
        remembered = dict(location.drives).get(letter)
        if remembered is None:
            remembered = analysis.path_or_none(analysis.env_folded.get(f"={letter}:")) or Path(
                f"{letter}:\\"
            )
        analysis.add("start_dir", remembered, raw.source)
        moved.append(location.moved(remembered))
    set_locations(analysis, context, raw, moved)


def set_locations(
    analysis: Analysis, context: Context, raw: RawCommand, moved: list[Location]
) -> None:
    unique = tuple(dict.fromkeys(moved))
    if len(unique) > MAX_CANDIDATES:
        analysis.unresolve(
            "cwd", raw.source, "the folder it runs in depends on too many earlier changes"
        )
        unique = (Location(None),)
    context.states[raw.scope] = unique
