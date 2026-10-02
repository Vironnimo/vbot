"""Package builds: the one supported Python version, and builds on the build machine's
bare Python without vBot's dependencies."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts.package_build import PYTHON_VERSION

ROOT = Path(__file__).resolve().parents[2]

# Refuses every import outside the standard library and the checkout's own packages.
_BARE_IMPORT = """
import importlib, sys
sys.path.insert(0, sys.argv[1])
own = {"cli", "core", "scripts", "server", "desktop"}

class Bare:
    def find_spec(self, name, path=None, target=None):
        top = name.partition(".")[0]
        if top in sys.stdlib_module_names or top in own:
            return None
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

sys.meta_path.insert(0, Bare())
importlib.import_module(sys.argv[2])
"""


@pytest.mark.parametrize("module", ["scripts.build_windows", "scripts.build_linux"])
def test_build_script_imports_with_only_the_standard_library(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", _BARE_IMPORT, str(ROOT), module],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_every_pin_names_the_one_python_version_the_packages_bundle() -> None:
    """Changing the Python version is one commit that updates every one of these pins."""
    major, minor = PYTHON_VERSION.split(".")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["requires-python"] == f">={PYTHON_VERSION},<{major}.{int(minor) + 1}"
    assert project["tool"]["mypy"]["python_version"] == PYTHON_VERSION

    runtime_lock = json.loads((ROOT / "scripts/linux/python.lock.json").read_text("utf-8"))
    assert runtime_lock["python"].startswith(f"{PYTHON_VERSION}.")
    archive = f"cpython-{runtime_lock['python']}%2B{runtime_lock['release']}-"
    assert all(archive in spec["url"] for spec in runtime_lock["runtimes"].values())

    locks = sorted((ROOT / "scripts").glob("*/requirements-*.lock"))
    assert len(locks) == 5
    for lock in locks:
        header = lock.read_text(encoding="utf-8").splitlines()[1]
        assert f" --python-version {PYTHON_VERSION} " in header, lock.name

    workflows = sorted(
        [
            *(ROOT / ".github/workflows").glob("*.yml"),
            *(ROOT / ".github/actions").glob("*/action.yml"),
        ]
    )
    assert any(workflow.name == "action.yml" for workflow in workflows)
    versions = {
        (workflow.name, value)
        for workflow in workflows
        for value in re.findall(
            r"^\s*python-version:\s*(\S+)", workflow.read_text("utf-8"), re.MULTILINE
        )
    }
    assert {value for _name, value in versions} == {f'"{PYTHON_VERSION}"'}, versions
