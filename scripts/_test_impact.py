"""Query the per-test dependency records for the commit check.

``tests/file_dependencies.py`` records while pytest-testmon collects data; this
module reads those records and testmon's own database outside pytest.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from tests.file_dependencies import DATA_FILE, TESTMON_DATA


def _query(data_file: Path, sql: str, values: list[str]) -> list[tuple[str, str]]:
    if not values or not data_file.is_file():
        return []
    placeholders = ", ".join("?" for _ in values)
    connection = sqlite3.connect(f"file:{data_file.as_posix()}?mode=ro", uri=True, timeout=60)
    try:
        return connection.execute(sql.format(placeholders=placeholders), values).fetchall()
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
        sorted(candidates),
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
    modules = sorted({test.partition("::")[0] for test in tests})
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
    """The tests pytest-testmon would run for the current working tree."""

    tests: frozenset[str]
    """Tests whose recorded code changed, and tests that failed in their last run."""
    recorded: frozenset[str]
    """Every recorded test."""
    seconds: float
    """Recorded duration of the selected tests."""
    complete: bool
    """No usable record exists (none yet, or installed packages changed)."""

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
        for test in sorted(self.recorded - self.tests):
            if test.partition("::")[0] in wanted:
                arguments += ["--deselect", test]
        return arguments


def select(root: Path) -> Selection:
    """Compute pytest-testmon's selection without starting pytest.

    Starting pytest costs seconds even when testmon then deselects every test, so
    callers use this to skip the run or to size it.
    """
    unusable = Selection(frozenset(), frozenset(), 0.0, complete=True)
    if not (root / TESTMON_DATA).is_file():
        return unusable
    from testmon.testmon_core import TestmonData  # type: ignore[import-untyped]

    data = TestmonData.for_local_run(rootdir=str(root))
    try:
        recorded = data.all_tests
        # testmon drops the records of an environment whose packages changed.
        if data.system_packages_change or not recorded:
            return unusable
        data.determine_stable()
        tests = frozenset(data.unstable_test_names) | frozenset(data.failing_tests)
        seconds = sum((recorded.get(test) or {}).get("duration") or 0.0 for test in tests)
        return Selection(tests, frozenset(recorded), seconds, complete=False)
    finally:
        data.db.con.close()


def copy_data(source_root: Path, target_root: Path) -> bool:
    """Copy one checkout's test-impact data into another; return whether any existed.

    A copy stays valid in the target: testmon compares each test's recorded code
    with the target's files, so data from an older state only selects more tests.
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
