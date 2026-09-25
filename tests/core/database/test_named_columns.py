"""Runtime SQL names its columns.

An older vBot keeps reading and writing a database a newer one extended only
while its SQL never relies on the full column set. ``SELECT *`` or ``alias.*``
would hand it columns it does not know, and an ``INSERT`` without a column list
fails once a table gains a column. This guard scans every string literal in the
runtime packages for such reads and writes; tables of the ``temp`` schema are
private to one connection and exempt.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

_REPO_ROOT = Path(__file__).parents[3]
_RUNTIME_PACKAGES = ("core", "server", "resources/extensions")
# Writers also include the command line and the converters, which build the
# databases the runtime then opens.
_WRITING_PACKAGES = ("core", "server", "cli", "resources/extensions", "scripts/converters")

_SQL = re.compile(r"\bSELECT\b", re.IGNORECASE)
_IMPLICIT_COLUMNS = (
    re.compile(r"\bSELECT\s+(?:DISTINCT\s+|ALL\s+)?\*", re.IGNORECASE),
    re.compile(r"(?<![\w.])[A-Za-z_]\w*\.\*"),
    re.compile(r",\s*\*\s*(?:,|\bFROM\b)", re.IGNORECASE),
)
_WRITE = re.compile(r"\b(?:INSERT|REPLACE)\b", re.IGNORECASE)
# The target runs up to what follows it: a column list, or the row source of
# an INSERT that has none.
_INSERT = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?|REPLACE)\s+INTO\s+(?P<target>.+?)\s*"
    r"(?P<next>\(|\bVALUES\b|\bSELECT\b|\bWITH\b|\bDEFAULT\s+VALUES\b)",
    re.IGNORECASE | re.DOTALL,
)
_TEMP_TABLE = re.compile(
    r"\bCREATE\s+TEMP(?:ORARY)?\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`\[]?(?P<name>\w+)",
    re.IGNORECASE,
)
_TEMP_SCHEMA = re.compile(r"^[\"`\[]?temp(?:orary)?[\"`\]]?\.", re.IGNORECASE)


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


def _sources(packages: tuple[str, ...], marker: re.Pattern[str]) -> Iterator[tuple[str, str]]:
    """Each Python source under *packages* that mentions *marker*, by relative path."""
    for package in packages:
        for source_path in sorted((_REPO_ROOT / package).rglob("*.py")):
            source = source_path.read_text(encoding="utf-8")
            if marker.search(source):
                yield source_path.relative_to(_REPO_ROOT).as_posix(), source


def _implicit_column_reads() -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for relative, source in _sources(_RUNTIME_PACKAGES, _SQL):
        for line, text in _literals(ast.parse(source, filename=relative)):
            if not _SQL.search(text):
                continue
            for pattern in _IMPLICIT_COLUMNS:
                for match in pattern.finditer(text):
                    findings.append((relative, line, match.group(0)))
    return findings


def _unnamed_inserts(text: str, temp_tables: set[str]) -> list[str]:
    """The INSERTs in *text* that list no columns for a table outside ``temp``."""
    findings: list[str] = []
    for match in _INSERT.finditer(text):
        target = match.group("target").strip()
        if match.group("next") == "(" or _TEMP_SCHEMA.match(target):
            continue
        if target.strip('"`[]').lower() in temp_tables:
            continue
        findings.append(" ".join(match.group(0).split()))
    return findings


def _implicit_column_writes() -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    for relative, source in _sources(_WRITING_PACKAGES, _WRITE):
        literals = _literals(ast.parse(source, filename=relative))
        temp_tables = {
            match.group("name").lower()
            for _line, text in literals
            for match in _TEMP_TABLE.finditer(text)
        }
        for line, text in literals:
            findings.extend(
                (relative, line, insert) for insert in _unnamed_inserts(text, temp_tables)
            )
    return findings


def test_runtime_sql_never_selects_columns_implicitly() -> None:
    unexpected = [f"{path}:{line}: {match!r}" for path, line, match in _implicit_column_reads()]

    assert unexpected == [], "name the selected columns instead of '*'"


def test_sql_never_inserts_without_a_column_list() -> None:
    unexpected = [f"{path}:{line}: {match!r}" for path, line, match in _implicit_column_writes()]

    assert unexpected == [], "name the inserted columns"


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


def test_the_guard_recognizes_implicit_writes() -> None:
    samples = {
        "INSERT INTO notes VALUES (?, ?)": True,
        "insert or ignore into notes values (?, ?)": True,
        "REPLACE INTO notes VALUES (?, ?)": True,
        "INSERT INTO main.notes SELECT note_id, body FROM staged": True,
        "INSERT INTO  {}  VALUES (?)": True,
        'INSERT INTO "notes"\nVALUES (?)': True,
        "INSERT INTO notes DEFAULT VALUES": True,
        "INSERT INTO notes (note_id, body) VALUES (?, ?)": False,
        "INSERT INTO notes(note_id) SELECT note_id FROM staged": False,
        "INSERT OR REPLACE INTO notes (note_id) VALUES (?)": False,
        "INSERT INTO  {}  ( {} ) VALUES (?)": False,
        "INSERT INTO temp.ranks VALUES (?)": False,
        "INSERT INTO scratch VALUES (?)": False,
    }
    for text, implicit in samples.items():
        assert bool(_unnamed_inserts(text, {"scratch"})) is implicit, text
    created = "CREATE TEMP TABLE IF NOT EXISTS scratch (value)"
    assert [match.group("name") for match in _TEMP_TABLE.finditer(created)] == ["scratch"]
