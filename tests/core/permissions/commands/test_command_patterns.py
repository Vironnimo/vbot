"""Shell(...) command patterns: normalization, allow matching and deny may-match."""

from __future__ import annotations

import sys

import pytest

from core.permissions.commands.commands import (
    CommandWord,
    ShellKind,
    SimpleCommand,
    command_pattern_matches,
    command_pattern_may_match,
    normalize_command_pattern,
)


def _command(text: str, shell: ShellKind = "bash") -> SimpleCommand:
    """Words in angle brackets are dynamic."""
    name, *words = text.split(" ")
    return SimpleCommand(
        name,
        tuple(
            CommandWord(word[1:-1], False) if word.startswith("<") else CommandWord(word, True)
            for word in words
        ),
        text,
        shell,
    )


@pytest.mark.parametrize(
    ("pattern", "command", "matches", "may_match"),
    [
        ("git status", "git status", True, True),
        ("git status", "git status -s", False, False),
        ("git *", "git", True, True),
        ("git *", "git push origin", True, True),
        ("git * main", "git push origin main", True, True),
        ("git*", "gitk", True, True),
        ("npm run test:*", "npm run test", True, True),
        ("npm run test:*", "npm run test --watch", True, True),
        ("Git Status", "git status", False, False),
        # A dynamic word matches only a wildcard; for deny rules it may match anything.
        ("rm *", "rm <$X>", True, True),
        ("rm -rf *", "rm <$X>", False, True),
        ("rm -rf /", "rm <$X>", False, True),
        ("rm -rf /", "rm <$X> safe", False, False),
        ("git push *", "git <$CMD> origin", False, True),
        ("git push", "git <$CMD> origin", False, False),
    ],
)
def test_bash_patterns(pattern: str, command: str, matches: bool, may_match: bool) -> None:
    subject = _command(command)
    assert command_pattern_matches(pattern, subject, case_insensitive=False) is matches
    assert command_pattern_may_match(pattern, subject, case_insensitive=False) is may_match


@pytest.mark.parametrize(
    ("pattern", "command", "matches"),
    [
        ("Remove-Item *", "Remove-Item x", True),
        ("remove-item *", "Remove-Item x", True),
        ("rm *", "Remove-Item -Recurse x", sys.platform == "win32"),
        ("gci", "Get-ChildItem", True),
    ],
)
def test_powershell_patterns_fold_case_and_aliases(
    pattern: str, command: str, matches: bool
) -> None:
    subject = _command(command, "powershell")
    assert command_pattern_matches(pattern, subject, case_insensitive=False) is matches


@pytest.mark.parametrize(
    ("pattern", "normalized"),
    [
        ("  git   push  * ", "git push *"),
        ("npm run test:*", "npm run test *"),
        ("C:\\tools\\rg.exe *", "rg *"),
        ("/usr/bin/git status", "git status"),
        ("*", "*"),
    ],
)
def test_normalize_command_pattern(pattern: str, normalized: str) -> None:
    assert normalize_command_pattern(pattern) == normalized


@pytest.mark.parametrize("pattern", ["", "   ", "git\nstatus"])
def test_normalize_command_pattern_refuses(pattern: str) -> None:
    with pytest.raises(ValueError):
        normalize_command_pattern(pattern)
