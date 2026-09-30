"""Contract of the per-test dependency record that selects the tests of a commit.

The record is written by a real pytest-testmon run in a subprocess: the audit
hook, the xdist hand-over, and testmon's own database are the behavior under test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import _test_impact
from tests import file_dependencies

REPO_ROOT = Path(__file__).resolve().parents[1]

PROJECT_FILES = {
    "pytest.ini": "[pytest]\n",
    "conftest.py": 'pytest_plugins = ["tests.file_dependencies"]\n',
    "helper.py": "def shout(text):\n    return text.upper()\n",
    "data/prompt.txt": "hello",
    "data/imported.txt": "at import",
    "listed/a.txt": "",
    "listed/nested/b.txt": "",
    "test_reads.py": (
        "from pathlib import Path\n"
        "\n"
        "import helper\n"
        "\n"
        'IMPORTED = Path("data/imported.txt").read_text()\n'
        "\n"
        "\n"
        "def test_reads_file():\n"
        '    assert helper.shout(Path("data/prompt.txt").read_text()) == "HELLO"\n'
        "\n"
        "\n"
        "def test_lists_directory():\n"
        '    assert sorted(path.name for path in Path("listed").iterdir()) == ["a.txt", "nested"]\n'
        "\n"
        "\n"
        "def test_lists_nested_directory():\n"
        '    assert [path.name for path in Path("listed/nested").iterdir()] == ["b.txt"]\n'
        "\n"
        "\n"
        "def test_reads_nothing():\n"
        "    assert IMPORTED\n"
    ),
}


@pytest.fixture(scope="module")
def recorded_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("project")
    for name, content in PROJECT_FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content.encode("utf-8"))
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "COVERAGE_CORE": "ctrace"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--testmon", "-n", "2"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return root


def test_readers_are_the_tests_that_read_a_file_or_listed_its_directory(
    recorded_project: Path, tmp_path: Path
) -> None:
    lists = "test_reads.py::test_lists_directory"
    lists_nested = "test_reads.py::test_lists_nested_directory"
    assert _test_impact.readers(recorded_project, {"data/prompt.txt"}) == {
        "test_reads.py::test_reads_file"
    }
    # A file added to a listed directory changes what the listing test sees.
    assert _test_impact.readers(recorded_project, {"listed/new.txt"}) == {lists}
    assert _test_impact.readers(recorded_project, {"listed/nested/new.txt"}) == {lists_nested}
    # So does a new directory in it, however deep the added file lies.
    assert _test_impact.readers(recorded_project, {"listed/new/deeper/c.txt"}) == {lists}
    # A removed listed directory changes the listing of its own directory.
    (tmp_path / "listed").mkdir()
    assert _test_impact.readers(tmp_path, {"listed/nested/b.txt"}, recorded_project) == {
        lists,
        lists_nested,
    }
    assert _test_impact.readers(recorded_project, {"data/imported.txt"}) == {
        file_dependencies.COLLECTION
    }
    assert _test_impact.readers(recorded_project, {"helper.py", "unknown.txt"}) == set()


def test_dependencies_combine_executed_code_and_read_files(recorded_project: Path) -> None:
    tests = {"test_reads.py::test_reads_file", "test_reads.py::test_reads_nothing", "test_reads.py"}

    dependencies = _test_impact.dependencies(recorded_project, tests)

    assert {"test_reads.py", "helper.py", "data/prompt.txt"} <= dependencies[
        "test_reads.py::test_reads_file"
    ]
    assert "helper.py" not in dependencies["test_reads.py::test_reads_nothing"]
    # A collection error names only the module: it depends on what its tests use.
    assert {"helper.py", "data/prompt.txt"} <= dependencies["test_reads.py"]


def test_copied_data_answers_like_the_original(recorded_project: Path, tmp_path: Path) -> None:
    assert _test_impact.copy_data(recorded_project, tmp_path) is True

    assert _test_impact.readers(tmp_path, {"data/prompt.txt"}) == {"test_reads.py::test_reads_file"}
    assert _test_impact.copy_data(tmp_path / "missing", tmp_path) is False
