"""Select the tests a change affects from the per-test dependency records.

``tests/file_dependencies.py`` records while pytest-testmon collects data; this
module reads those records and testmon's own database outside pytest, for
``scripts/commit_check.py``.

The records describe a tested state: the git tree a commit check last tested, plus
the paths that differed from it in the working tree then. ``DATA_FILE`` stores it
beside the file reads, so a copy of the records carries the state they describe.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.file_dependencies import COLLECTION, DATA_FILE, TESTMON_DATA

# Changes that can affect any test: pytest and plugin configuration.
FULL_SUITE_TRIGGERS = frozenset({"pyproject.toml"})
_CODE_SUFFIXES = (".py", ".pyi")
# Stay below SQLite's limit on the parameters of one statement.
_CHUNK = 500


def _query(data_file: Path, sql: str, values: Iterable[str] = ()) -> list[tuple[Any, ...]]:
    """Run *sql* read-only; ``{placeholders}`` takes *values*, in chunks when there are many."""
    values = sorted(values)
    if not data_file.is_file() or ("{placeholders}" in sql and not values):
        return []
    connection = sqlite3.connect(f"file:{data_file.as_posix()}?mode=ro", uri=True, timeout=60)
    try:
        if "{placeholders}" not in sql:
            return connection.execute(sql).fetchall()
        rows: list[tuple[Any, ...]] = []
        for start in range(0, len(values), _CHUNK):
            chunk = values[start : start + _CHUNK]
            statement = sql.format(placeholders=", ".join("?" for _ in chunk))
            rows += connection.execute(statement, chunk).fetchall()
        return rows
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def readers(root: Path, paths: set[str]) -> set[str]:
    """Return the tests that read one of *paths* or listed a directory containing one.

    The result contains ``COLLECTION`` when a path was read outside any test.
    """
    candidates = set(paths)
    for path in paths:
        parent = path.rpartition("/")[0]
        if parent:
            candidates.add(parent)
    rows = _query(
        root / DATA_FILE,
        "SELECT DISTINCT test, path FROM reads WHERE path IN ({placeholders})",
        candidates,
    )
    return {test for test, _path in rows}


def dependencies(root: Path, tests: set[str]) -> dict[str, set[str]]:
    """Return the repository files each test depends on, as far as recorded.

    That is every Python file whose code the test executed (testmon) and every data
    file it read or directory it listed. Each test also depends on its own module.
    A test module id without ``::``, as a collection error reports it, depends on
    the files of all recorded tests of that module.
    """
    result = {test: {test.partition("::")[0]} for test in tests}
    modules = {test.partition("::")[0] for test in tests}
    module_of = "substr({column}, 1, instr({column} || '::', '::') - 1) IN ({{placeholders}})"
    executed = _query(
        root / TESTMON_DATA,
        "SELECT execution.test_name, fingerprint.filename FROM test_execution execution "
        "JOIN test_execution_file_fp link ON link.test_execution_id = execution.id "
        "JOIN file_fp fingerprint ON fingerprint.id = link.fingerprint_id "
        "WHERE " + module_of.format(column="execution.test_name"),
        modules,
    )
    read = _query(
        root / DATA_FILE,
        "SELECT test, path FROM reads WHERE " + module_of.format(column="test"),
        modules,
    )
    for recorded, path in [*executed, *read]:
        module = recorded.partition("::")[0]
        for test in (recorded, module):
            if test in result:
                result[test].add(path.replace("\\", "/"))
    return result


@dataclass(frozen=True)
class Selection:
    """The tests a change affects, judged against recorded test runs."""

    tests: frozenset[str]
    """Selected tests: their recorded code changed, or they read a changed file."""
    failed: frozenset[str]
    """Tests that failed in their last recorded run."""
    durations: Mapping[str, float]
    """Recorded duration of every recorded test."""
    complete: bool
    """Every test is selected: no usable record, or a change that can affect any test."""

    def selects(self, test: str) -> bool:
        """Whether *test* lacks a passing run with the code and files it has now."""
        return (
            self.complete or test in self.tests or test in self.failed or test not in self.durations
        )

    def __and__(self, other: Selection) -> Selection:
        """Return the tests both selections select.

        Each selection judges the same working tree against the records of a
        different tested state. A test one of them leaves out passed in that state
        with the code and files it depends on now, so it need not run again.
        """
        durations = {**other.durations, **self.durations}
        if self.complete and other.complete:
            return Selection(frozenset(), frozenset(), durations, complete=True)
        candidates = self.tests | other.tests | self.failed | other.failed
        for selection in (self, other):
            if selection.complete:
                candidates |= set(selection.durations)
        tests = frozenset(test for test in candidates if self.selects(test) and other.selects(test))
        return Selection(tests, frozenset(), durations, complete=False)

    @property
    def seconds(self) -> float:
        """Recorded duration of the selected tests."""
        return sum(self.durations.get(test) or 0.0 for test in self.tests)

    def pytest_arguments(self, root: Path, extra_modules: Iterable[str]) -> list[str]:
        """Return pytest arguments that run the selected tests and *extra_modules*.

        pytest gets whole test modules and deselects their recorded, unselected
        tests: tests a module gained since its record run too, and a stale
        ``--deselect`` is harmless, while a stale node id would make pytest run
        nothing. Every worker then collects the same tests.
        """
        modules = {test.partition("::")[0] for test in self.tests} | set(extra_modules)
        existing = sorted(module for module in modules if (root / module).is_file())
        wanted = set(existing)
        arguments = list(existing)
        for test in sorted(set(self.durations) - self.tests):
            if test.partition("::")[0] in wanted:
                arguments += ["--deselect", test]
        return arguments


def select(root: Path, changed: Iterable[str] | None, records: Path | None = None) -> Selection:
    """Select the tests of *root*'s working tree that *changed* affects.

    *records* is the checkout whose test runs describe the tested state (default
    *root*); *changed* are the paths that differ from that state, None when that
    state is unknown. Changed Python code selects through pytest-testmon's record
    of the code each test executed; any other changed file selects the tests that
    read it. testmon also selects the tests that failed in their last run.
    """
    records = records or root
    rows = _query(records / TESTMON_DATA, "SELECT test_name, duration, failed FROM test_execution")
    durations = {test: duration or 0.0 for test, duration, _failed in rows}
    failed = frozenset(test for test, _duration, test_failed in rows if test_failed)
    if changed is None:
        return Selection(frozenset(), failed, durations, complete=True)
    changed = set(changed)
    data_files = {path for path in changed if not path.endswith(_CODE_SUFFIXES)}
    tests = readers(records, data_files)
    if FULL_SUITE_TRIGGERS & data_files or COLLECTION in tests:
        return Selection(frozenset(), failed, durations, complete=True)
    if len(data_files) < len(changed):
        affected = _affected_by_code(root, records)
        if affected is None:
            return Selection(frozenset(), failed, durations, complete=True)
        tests |= affected
    return Selection(frozenset(tests), failed, durations, complete=False)


def _affected_by_code(root: Path, records: Path) -> set[str] | None:
    """Return testmon's selection, or None when *records* holds no usable record."""
    data_file = records / TESTMON_DATA
    if not data_file.is_file():
        return None
    from testmon import db  # type: ignore[import-untyped]
    from testmon.testmon_core import TestmonData  # type: ignore[import-untyped]

    data = TestmonData.for_local_run(rootdir=str(root), database=db.DB(str(data_file)))
    try:
        # testmon drops the records of an environment whose packages changed.
        if data.system_packages_change or not data.all_tests:
            return None
        data.determine_stable()
        return set(data.unstable_test_names) | set(data.failing_tests)
    finally:
        data.db.con.close()


def adopt(source: Path, target: Path, tests: Iterable[str]) -> None:
    """Replace *target*'s records of *tests* with *source*'s, where *source* has one.

    Adopt only the record of a test whose code and files are as they were when
    *source* recorded it: *target* then selects the test when they change again.
    """
    rows = _query(
        source / TESTMON_DATA,
        "SELECT execution.test_name, execution.duration, execution.failed, execution.forced, "
        "fingerprint.filename, fingerprint.fsha, fingerprint.method_checksums "
        "FROM test_execution execution "
        "LEFT JOIN test_execution_file_fp link ON link.test_execution_id = execution.id "
        "LEFT JOIN file_fp fingerprint ON fingerprint.id = link.fingerprint_id "
        "WHERE execution.test_name IN ({placeholders})",
        tests,
    )
    if not rows:
        return
    from testmon.process_code import blob_to_checksums  # type: ignore[import-untyped]
    from testmon.testmon_core import TestmonData  # type: ignore[import-untyped]

    executions: dict[str, dict[str, Any]] = {}
    for test, duration, failed, forced, filename, fsha, checksums in rows:
        execution = executions.setdefault(
            test, {"deps": [], "duration": duration, "failed": failed, "forced": forced}
        )
        if filename is not None:
            execution["deps"].append(
                {
                    "filename": filename,
                    "fsha": fsha,
                    "method_checksums": blob_to_checksums(checksums),
                }
            )
    data = TestmonData.for_local_run(rootdir=str(target))
    try:
        data.save_test_execution_file_fps(executions)
    finally:
        data.db.con.close()

    reads = _query(
        source / DATA_FILE,
        "SELECT test, path FROM reads WHERE test IN ({placeholders})",
        executions,
    )
    connection = sqlite3.connect(target / DATA_FILE, timeout=60)
    try:
        with connection:
            # The table tests/file_dependencies.py creates.
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reads (test TEXT NOT NULL, path TEXT NOT NULL, "
                "PRIMARY KEY (test, path))"
            )
            connection.executemany("DELETE FROM reads WHERE test = ?", [(t,) for t in executions])
            connection.executemany("INSERT OR IGNORE INTO reads (test, path) VALUES (?, ?)", reads)
    finally:
        connection.close()


def tested_state(checkout: Path) -> tuple[str, frozenset[str]] | None:
    """Return the tree *checkout*'s records describe and the paths that differed from it.

    None when no commit check recorded one.
    """
    rows = _query(checkout / DATA_FILE, "SELECT tree, dirty FROM tested_state")
    if not rows:
        return None
    tree, dirty = rows[0]
    return tree, frozenset(json.loads(dirty))


def record_tested_state(checkout: Path, tree: str, dirty: Iterable[str]) -> None:
    """Record that *checkout*'s records describe *tree*, except for the *dirty* paths."""
    connection = sqlite3.connect(checkout / DATA_FILE, timeout=60)
    try:
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS tested_state (tree TEXT NOT NULL, dirty TEXT NOT NULL)"
            )
            connection.execute("DELETE FROM tested_state")
            connection.execute(
                "INSERT INTO tested_state (tree, dirty) VALUES (?, ?)",
                (tree, json.dumps(sorted(dirty))),
            )
    finally:
        connection.close()


def copy_data(source_root: Path, target_root: Path) -> bool:
    """Copy one checkout's test-impact data into another; return whether any existed.

    A copy stays valid in the target: it carries the tested state it describes,
    and testmon compares each test's recorded code with the target's files.
    """
    copied = False
    for name in (TESTMON_DATA, DATA_FILE):
        source = source_root / name
        if not source.is_file():
            continue
        reader = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True, timeout=60)
        writer = sqlite3.connect(target_root / name)
        try:
            reader.backup(writer)
        finally:
            writer.close()
            reader.close()
        copied = True
    return copied
