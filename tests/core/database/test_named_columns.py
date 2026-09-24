"""Runtime SQL names its columns.

An older vBot keeps reading and writing a database a newer one extended only
while it never selects columns implicitly: ``SELECT *`` or ``alias.*`` would
hand it columns it does not know. This guard scans every string literal in the
runtime packages for such reads.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[3]
_RUNTIME_PACKAGES = ("core", "server", "resources/extensions")

_SQL = re.compile(r"\bSELECT\b", re.IGNORECASE)
_IMPLICIT_COLUMNS = (
    re.compile(r"\bSELECT\s+(?:DISTINCT\s+|ALL\s+)?\*", re.IGNORECASE),
    re.compile(r"(?<![\w.])[A-Za-z_]\w*\.\*"),
    re.compile(r",\s*\*\s*(?:,|\bFROM\b)", re.IGNORECASE),
)

# Implicit reads that remain only in the Session store query modules, which
# the sessions.db Generation 1 rewrite replaces. Counts may only shrink:
# remove an entry once its module names every column.
_ALLOWED = {
    "core/sessions/_store_continuation.py": 3,
    "core/sessions/_store_fts.py": 1,
    "core/sessions/_store_mutations.py": 1,
    "core/sessions/_store_owned.py": 5,
    "core/sessions/_store_queries.py": 1,
    "core/sessions/_store_runs.py": 2,
    "core/sessions/_store_values.py": 4,
}


def _literals(tree: ast.AST) -> list[tuple[int, str]]:
    """Every string literal, with f-string text parts joined around placeholders."""
    literals: list[tuple[int, str]] = []
    formatted_parts: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = ""
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    formatted_parts.add(id(part))
                    text += part.value
                else:
                    text += " {} "
            literals.append((node.lineno, text))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in formatted_parts
        ):
            literals.append((node.lineno, node.value))
    return literals


def _implicit_column_reads() -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for package in _RUNTIME_PACKAGES:
        for source_path in sorted((_REPO_ROOT / package).rglob("*.py")):
            source = source_path.read_text(encoding="utf-8")
            if not _SQL.search(source):
                continue
            relative = source_path.relative_to(_REPO_ROOT).as_posix()
            for line, text in _literals(ast.parse(source, filename=str(source_path))):
                if not _SQL.search(text):
                    continue
                for pattern in _IMPLICIT_COLUMNS:
                    for match in pattern.finditer(text):
                        findings.append((relative, line, match.group(0)))
    return findings


def test_runtime_sql_never_selects_columns_implicitly() -> None:
    findings = _implicit_column_reads()
    counts = Counter(path for path, _line, _match in findings)

    unexpected = [
        f"{path}:{line}: {match!r}"
        for path, line, match in findings
        if counts[path] > _ALLOWED.get(path, 0)
    ]
    stale = sorted(
        f"{path} allows {allowed}, found {counts[path]}"
        for path, allowed in _ALLOWED.items()
        if counts[path] < allowed
    )

    assert unexpected == [], "name the selected columns instead of '*'"
    assert stale == [], "lower or remove the allowlist entry"


def test_the_guard_recognizes_implicit_reads() -> None:
    samples = {
        "SELECT * FROM notes": True,
        "select distinct * from notes": True,
        "SELECT n.*, t.label FROM notes n JOIN tags t": True,
        "SELECT note_id, * FROM notes": True,
        "SELECT COUNT(*) FROM notes": False,
        "SELECT note_id, body FROM notes WHERE body GLOB '*.md'": False,
        "SELECT width * height AS area FROM shapes": False,
    }
    for text, implicit in samples.items():
        assert any(pattern.search(text) for pattern in _IMPLICIT_COLUMNS) is implicit, text
