#!/usr/bin/env python
"""Measure and check the domain maps under ``.vorch/domain-maps/``.

A root map is the always-read layer of its domain: every agent working in the
domain reads all of it, so its size is a budget (``ROOT_BUDGET``) that routine
updates must not keep raising. ``.vorch/PROJECT.md``, which every agent reads in
every session, has its own budget (``PROJECT_BUDGET``). The rules behind these
checks live in ``.vorch/workflows/domain-map-workflow.md``.

``python scripts/domain_maps.py`` prints the corpus report a domain-map audit
starts from: root map sizes against the budget, routing defects and punctuation
findings. The commit check calls ``staged_findings`` for the staged map files.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import override

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAPS_DIR = ".vorch/domain-maps"
INDEX_FILE = ".vorch/PROJECT.md"
ROOT_BUDGET = 16_000
"""Characters a root map may hold (about 4,000 tokens)."""
PROJECT_BUDGET = 30_000
"""Characters ``INDEX_FILE`` may hold (about 7,500 tokens)."""
AUDIT_THRESHOLD = 5
"""Root maps over budget at which the report calls for a corpus audit."""

_CODE_SPAN = re.compile(r"(`+).+?\1")
_MD_TOKEN = re.compile(r"`([^`\s]+\.md)`")
_INDEX_ROW = re.compile(r"^\|\s*([\w.-]+\.md)\s*\|", re.MULTILINE)


@dataclass(frozen=True)
class Finding:
    path: str
    message: str
    blocking: bool

    @override
    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


def is_map(path: str) -> bool:
    return path.startswith(MAPS_DIR + "/") and path.endswith(".md")


def is_root_map(path: str) -> bool:
    return is_map(path) and PurePosixPath(path).parent.as_posix() == MAPS_DIR


def is_checked(path: str) -> bool:
    """Return whether the commit check looks at *path*: a map or ``INDEX_FILE``."""
    return is_map(path) or path == INDEX_FILE


def budget_for(path: str) -> int | None:
    """Return the size budget of an always-read file, or None for any other file."""
    if path == INDEX_FILE:
        return PROJECT_BUDGET
    return ROOT_BUDGET if is_root_map(path) else None


def prose_non_ascii(text: str) -> list[int]:
    """Return the line numbers with non-ASCII characters outside code spans and blocks.

    Literal product strings (a UI label, a rendered fragment) keep their characters
    inside a code span; everything else uses ASCII punctuation.
    """
    lines: list[int] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if any(ord(char) > 127 for char in _CODE_SPAN.sub("", line)):
            lines.append(number)
    return lines


def references_section(text: str) -> str:
    """Return the body of the file's ``## References`` section, or an empty string."""
    match = re.search(r"^## References[ \t]*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def _resolve(root: Path, token: str) -> str | None:
    """Return the repository path a Markdown reference names, or None when nothing is there."""
    for candidate in (f"{MAPS_DIR}/{token}", token):
        if (root / candidate).is_file():
            return PurePosixPath(candidate).as_posix()
    return None


def broken_references(root: Path, path: str, text: str) -> list[Finding]:
    """Return a blocking finding for each References entry that names no file."""
    return [
        Finding(path, f"References entry `{token}` names no file", True)
        for token in dict.fromkeys(_MD_TOKEN.findall(references_section(text)))
        if _resolve(root, token) is None
    ]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _committed_size(root: Path, path: str) -> int:
    """Return the characters of *path* at HEAD; 0 when HEAD does not hold it."""
    shown = _git(root, "show", f"HEAD:{path}")
    return len(shown.stdout) if shown.returncode == 0 else 0


def _read(root: Path, path: str) -> str:
    return (root / path).read_text(encoding="utf-8")


def staged_findings(root: Path, staged: Iterable[str]) -> list[Finding]:
    """Check the staged map files as they are in the working tree.

    Non-ASCII prose and References entries that name no file block. A root map
    or ``INDEX_FILE`` above its budget that the change makes larger is reported
    without blocking.
    """
    findings: list[Finding] = []
    for path in staged:
        if not is_checked(path):
            continue
        text = _read(root, path)
        lines = prose_non_ascii(text) if is_map(path) else []
        if lines:
            shown = ", ".join(str(number) for number in lines[:10])
            more = f" and {len(lines) - 10} more" if len(lines) > 10 else ""
            findings.append(
                Finding(
                    path,
                    f"non-ASCII characters outside code spans on lines {shown}{more}; use "
                    "ASCII punctuation, or put a literal product string in a code span",
                    True,
                )
            )
        if is_map(path):
            findings.extend(broken_references(root, path, text))
        budget = budget_for(path)
        if budget is not None and len(text) > budget:
            before = _committed_size(root, path)
            if len(text) > before:
                remedy = (
                    "move facts whose use is confined to one domain into that domain's map"
                    if path == INDEX_FILE
                    else "move task-gated material of this domain into a supplementary file"
                )
                findings.append(
                    Finding(
                        path,
                        f"grows from {before} to {len(text)} characters, above its budget of "
                        f"{budget}; {remedy} so the file does not grow "
                        "(domain-map-workflow.md -> Size budget)",
                        False,
                    )
                )
    return findings


@dataclass(frozen=True)
class Report:
    sizes: dict[str, int]
    """Characters per root map, largest first."""
    supplementary: dict[str, int]
    """Characters per supplementary file."""
    findings: list[Finding]
    project_size: int
    """Characters of ``INDEX_FILE``."""


def corpus_report(root: Path) -> Report:
    """Measure every map and collect the corpus-wide findings."""
    maps_dir = root / MAPS_DIR
    files = sorted(path.relative_to(root).as_posix() for path in maps_dir.rglob("*.md"))
    texts = {path: _read(root, path) for path in files}
    roots = [path for path in files if is_root_map(path)]
    supplementary = [path for path in files if not is_root_map(path)]

    findings: list[Finding] = []
    for path, text in texts.items():
        findings.extend(broken_references(root, path, text))
        lines = prose_non_ascii(text)
        if lines:
            findings.append(
                Finding(
                    path, f"non-ASCII characters outside code spans on {len(lines)} lines", True
                )
            )

    # A supplementary file is reached through its root map, directly or through
    # another reached supplementary file.
    reached: set[str] = set()
    pending = list(roots)
    while pending:
        for token in _MD_TOKEN.findall(texts[pending.pop()]):
            target = _resolve(root, token)
            if target in texts and target not in reached and not is_root_map(target):
                reached.add(target)
                pending.append(target)
    findings.extend(
        Finding(path, "no map references this supplementary file", True)
        for path in supplementary
        if path not in reached
    )

    index_text = _read(root, INDEX_FILE) if (root / INDEX_FILE).is_file() else ""
    indexed = {f"{MAPS_DIR}/{name}" for name in _INDEX_ROW.findall(index_text)}
    findings.extend(
        Finding(path, f"root map is missing from the Domain Maps index in {INDEX_FILE}", True)
        for path in roots
        if path not in indexed
    )
    findings.extend(
        Finding(INDEX_FILE, f"Domain Maps index lists `{path}`, which is no root map", True)
        for path in sorted(indexed - set(roots))
    )

    sizes = dict(sorted(((path, len(texts[path])) for path in roots), key=lambda item: -item[1]))
    return Report(
        sizes, {path: len(texts[path]) for path in supplementary}, findings, len(index_text)
    )


def format_report(report: Report) -> str:
    over = [path for path, size in report.sizes.items() if size > ROOT_BUDGET]
    total = sum(report.sizes.values())
    lines = [f"Root maps (budget {ROOT_BUDGET} characters each):"]
    for path, size in report.sizes.items():
        marker = "  OVER" if size > ROOT_BUDGET else ""
        lines.append(f"  {size:>7}  {PurePosixPath(path).name}{marker}")
    lines += [
        "",
        f"{len(report.sizes)} root maps, {total} characters (~{total // 4} tokens); "
        f"{len(over)} over budget.",
        f"{len(report.supplementary)} supplementary files, "
        f"{sum(report.supplementary.values())} characters.",
        f"{INDEX_FILE}: {report.project_size} characters (budget {PROJECT_BUDGET})"
        + ("  OVER" if report.project_size > PROJECT_BUDGET else ""),
    ]
    if len(over) >= AUDIT_THRESHOLD:
        lines.append(
            f"Corpus audit due: {len(over)} root maps are over budget "
            "(.vorch/workflows/domain-map-audit-workflow.md)."
        )
    if report.findings:
        lines += ["", "Findings:", *(f"  {finding}" for finding in report.findings)]
    return "\n".join(lines)


def main(root: Path = PROJECT_ROOT) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    print(format_report(corpus_report(root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
