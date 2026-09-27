"""Record and query the files each test depends on, for commit-time test selection.

pytest-testmon records the Python code each test executes (``TESTMON_DATA``) and
selects the tests affected by a Python change. Tests also read repository files
that are not Python (prompts, Skill resources, manifests, fixtures), which testmon
does not track. While testmon collects data (``--testmon``), this plugin records
per test every such file the test opens and every repository directory it lists,
in ``DATA_FILE`` next to ``TESTMON_DATA``. ``scripts/commit_check.py`` uses both
records to run the tests a commit affects and to attribute their failures.

The module is a pytest plugin; ``tests/conftest.py`` registers it.

Files opened outside a test, while test modules are imported during collection,
are recorded under ``COLLECTION``: their readers cannot be attributed to single
tests, so a change to one of them selects the complete suite.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

TESTMON_DATA = ".testmondata"
DATA_FILE = ".testfiledeps"
COLLECTION = "<collection>"

_WORKER_OUTPUT_KEY = "vbot_file_dependencies"
_EVENTS = frozenset({"open", "os.listdir", "os.scandir"})
_IGNORED_SUFFIXES = (".py", ".pyc", ".pyi")
_COVERAGE_PACKAGE = f"{os.sep}coverage{os.sep}"


class _Recorder:
    """Audit-hook target that attributes repository reads to the running test."""

    def __init__(self, root: Path) -> None:
        self._root = os.path.normcase(str(root)) + os.sep
        self.current: str | None = COLLECTION
        self.records: dict[str, set[str]] = {}

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        if event not in _EVENTS or self.current is None:
            return
        target = args[0] if args else None
        if event != "open":
            # Directory listings matter when a test enumerates files. The import
            # system lists every package it imports from, coverage lists directories
            # to resolve file name case on Windows, and collection lists test
            # directories; testmon already tracks new modules and new tests.
            if self.current == COLLECTION or _listed_by_infrastructure():
                return
            if target is None:
                target = "."  # os.listdir() and os.scandir() default to the working directory
        if target is None or isinstance(target, int):
            return  # file descriptors carry no path
        try:
            full = os.path.normcase(os.path.abspath(os.fsdecode(target)))
        except (TypeError, ValueError):
            return
        if not full.startswith(self._root):
            return
        relative = full[len(self._root) :].replace("\\", "/")
        if (
            not relative
            or relative.endswith(_IGNORED_SUFFIXES)
            or relative.startswith(".")
            or "/." in relative
            or "__pycache__" in relative
        ):
            return
        self.records.setdefault(self.current, set()).add(relative)


def _listed_by_infrastructure() -> bool:
    # Frame 0 is this helper, 1 the audit hook, 2 the caller of os.listdir/os.scandir.
    caller = sys._getframe(2).f_code.co_filename
    return caller.startswith("<frozen importlib") or _COVERAGE_PACKAGE in caller


_recorder: _Recorder | None = None
_controller_records: dict[str, set[str]] = {}


def _collecting(config: pytest.Config) -> bool:
    testmon_config = getattr(config, "testmon_config", None)
    return bool(testmon_config is not None and testmon_config.collect)


def _is_xdist_controller(config: pytest.Config) -> bool:
    return not hasattr(config, "workerinput") and bool(getattr(config.option, "numprocesses", 0))


def pytest_sessionstart(session: pytest.Session) -> None:
    global _recorder
    config = session.config
    if not _collecting(config) or _is_xdist_controller(config):
        return
    if _recorder is None:
        _recorder = _Recorder(config.rootpath)
        sys.addaudithook(_recorder.audit)
    _recorder.current = COLLECTION
    _recorder.records = {}


def pytest_runtest_logstart(nodeid: str, location: tuple[str, int | None, str]) -> None:
    if _recorder is not None:
        _recorder.current = nodeid
        # A test that reads nothing still replaces its earlier record.
        _recorder.records.setdefault(nodeid, set())


def pytest_runtest_logfinish(nodeid: str, location: tuple[str, int | None, str]) -> None:
    if _recorder is not None:
        _recorder.current = None


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    payload = getattr(node, "workeroutput", {}).get(_WORKER_OUTPUT_KEY)
    if payload is None:
        return
    for test, paths in json.loads(payload).items():
        _controller_records.setdefault(test, set()).update(paths)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    if not _collecting(config):
        return
    if _is_xdist_controller(config):
        _store(config.rootpath, _controller_records)
        _controller_records.clear()
        return
    if _recorder is None:
        return
    _recorder.current = None
    if hasattr(config, "workerinput"):
        records = {test: sorted(paths) for test, paths in _recorder.records.items()}
        config.workeroutput[_WORKER_OUTPUT_KEY] = json.dumps(records)  # type: ignore[attr-defined]
        return
    _store(config.rootpath, _recorder.records)


def _store(root: Path, records: dict[str, set[str]]) -> None:
    """Replace the recorded files of every test that ran; collection reads accumulate."""
    connection = sqlite3.connect(root / DATA_FILE, timeout=60)
    try:
        with connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS reads (test TEXT NOT NULL, path TEXT NOT NULL, "
                "PRIMARY KEY (test, path))"
            )
            connection.executemany(
                "DELETE FROM reads WHERE test = ?",
                [(test,) for test in records if test != COLLECTION],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO reads (test, path) VALUES (?, ?)",
                [(test, path) for test, paths in records.items() for path in paths],
            )
    finally:
        connection.close()


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
