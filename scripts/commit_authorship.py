#!/usr/bin/env python
"""Keep AI tool attribution out of vBot's history: commits carry only the user's authorship.

Every commit, merge commit and push in a checkout that enabled the tracked hooks
(``git config core.hooksPath .githooks``) passes through here; Claude Code sessions
in this repository, cloud sessions included, enable them at session start
(``.claude/settings.json``). Three commands, one per hook:

- ``identity`` (``pre-commit``, ``pre-merge-commit``) refuses a commit whose author or
  committer is an AI tool identity such as ``Claude <noreply@anthropic.com>``.
- ``message <file>`` (``commit-msg``) removes attribution lines from the message:
  ``Co-Authored-By`` and ``Claude-Session`` trailers and "Generated with" footers.
- ``push <remote>`` (``pre-push``) refuses to push a commit that carries either, so a
  commit made without the hooks, or history merged in from elsewhere, never reaches
  the remote.

``history`` checks every commit reachable from HEAD; CI runs it on every push to main.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

# An author or committer whose name or email matches is an AI tool, not the user.
TOOL_IDENTITY = re.compile(
    r"\bclaude\b|anthropic\.com|\bcodex\b|openai\.com|\bcopilot\b", re.IGNORECASE
)
# A message line that attributes the commit to an AI tool.
ATTRIBUTION_LINE = re.compile(
    r"^\s*(?:co-authored-by:|claude-session:|.*generated (?:with|by) \[?(?:claude|codex))",
    re.IGNORECASE,
)
NO_COMMIT = "0" * 40
FIELD = "\x1f"
RECORD = "\x1e"
REMEDY = (
    "Commits in vBot carry only the user's authorship: no AI tool as author or committer, "
    "no Co-Authored-By or Claude-Session trailers, no 'Generated with' footers."
)


class Commit(NamedTuple):
    sha: str
    author: str
    committer: str
    message: str


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout


def tool_identity(ident: str) -> bool:
    """Return whether ``Name <email>`` names an AI tool."""
    return bool(TOOL_IDENTITY.search(ident))


def clean_message(message: str) -> str:
    """Return ``message`` without attribution lines and the blank lines they leave behind."""
    lines = [line for line in message.splitlines() if not ATTRIBUTION_LINE.match(line)]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) + "\n" if lines else ""


def problems(commit: Commit) -> list[str]:
    """Return what makes ``commit`` carry attribution other than the user's."""
    found = [
        f"{role} {ident}"
        for role, ident in (("author", commit.author), ("committer", commit.committer))
        if tool_identity(ident)
    ]
    found += [
        f"message line {line.strip()!r}"
        for line in commit.message.splitlines()
        if ATTRIBUTION_LINE.match(line)
    ]
    return found


def commits(*revisions: str) -> list[Commit]:
    """Return the commits that ``git log`` selects with these revision arguments."""
    output = _git(
        "log", f"--format=%H{FIELD}%an <%ae>{FIELD}%cn <%ce>{FIELD}%B{RECORD}", *revisions
    )
    found = []
    for record in output.split(RECORD):
        record = record.lstrip("\n")
        if record:
            sha, author, committer, message = record.split(FIELD, 3)
            found.append(Commit(sha, author, committer, message))
    return found


def _report(offending: Iterable[Commit]) -> int:
    lines = [
        f"  {commit.sha[:12]}: {'; '.join(problems(commit))}"
        for commit in offending
        if problems(commit)
    ]
    if not lines:
        return 0
    print("These commits carry AI tool attribution:", *lines, sep="\n", file=sys.stderr)
    print(
        f"\n{REMEDY}\nRewrite them with the user's identity and without those lines "
        "(for example git commit --amend --reset-author, or git rebase with --exec), "
        "then try again. Do not bypass this check with --no-verify.",
        file=sys.stderr,
    )
    return 1


def check_identity() -> int:
    # git var prints "Name <email> <timestamp> <zone>".
    idents = {
        role: _git("var", variable).rsplit(">", 1)[0] + ">"
        for role, variable in (("author", "GIT_AUTHOR_IDENT"), ("committer", "GIT_COMMITTER_IDENT"))
    }
    refused = [f"  {role}: {ident}" for role, ident in idents.items() if tool_identity(ident)]
    if not refused:
        return 0
    print("Commit blocked: an AI tool identity would author this commit.", *refused, sep="\n")
    print(
        f"\n{REMEDY}\nCommit as the user: git config user.name 'Julian B.' and "
        "git config user.email virojb@web.de in this repository (never --global), and "
        "unset GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL, GIT_COMMITTER_NAME and "
        "GIT_COMMITTER_EMAIL when they name a tool. Then commit again."
    )
    return 1


def strip_message(path: Path) -> int:
    message = path.read_text(encoding="utf-8")
    cleaned = clean_message(message)
    if cleaned != message:
        path.write_text(cleaned, encoding="utf-8")
    return 0


def check_push(remote: str, updates: str) -> int:
    offending: list[Commit] = []
    for update in updates.splitlines():
        parts = update.split()
        if len(parts) != 4 or parts[1] == NO_COMMIT:
            continue
        offending += commits(parts[1], "--not", f"--remotes={remote}")
    return _report(offending)


def main(argv: list[str]) -> int:
    command, *rest = argv or [""]
    if command == "identity":
        return check_identity()
    if command == "message" and len(rest) == 1:
        return strip_message(Path(rest[0]))
    if command == "push" and rest:
        return check_push(rest[0], sys.stdin.read())
    if command == "history":
        return _report(commits("HEAD"))
    print("usage: commit_authorship.py identity | message <file> | push <remote> | history")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
