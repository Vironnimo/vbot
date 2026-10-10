"""Package builds: the one supported Python version, and builds on the build machine's
bare Python without vBot's dependencies."""

from __future__ import annotations

import importlib.util
import json
import marshal
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import CodeType

import pytest

from cli.application.packages import validate_release
from cli.application.state import current_platform
from scripts import package_build
from scripts.package_build import (
    PYTHON_VERSION,
    compile_bytecode,
    write_archive,
    write_manifest,
)

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


def test_packaged_bytecode_is_deterministic_verified_and_loads_after_extraction(
    tmp_path: Path,
) -> None:
    root = tmp_path / "build" / "version"
    sources = {
        "app/cli/main.py": "def value():\n    return 42\n",
        "app/server/main.py": "value = 43\n",
        "app/resources/extensions/example/main.py": "value = 44\n",
        "runtime/Lib/example.py": "value = 45\n",
        f"runtime/lib/python{PYTHON_VERSION}/example.py": "value = 46\n",
        # These belong to other interpreters or deliberately invalid fixtures.
        "app/resources/skills/example/scripts/old.py": "print 'python 2'\n",
        "runtime/Lib/test/bad.py": "invalid syntax\n",
    }
    for name, source in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    (root / "app/pyproject.toml").write_text("[project]\nname='vbot'\n", encoding="utf-8")
    (root / "app/webui/dist").mkdir(parents=True)
    (root / "app/webui/dist/index.html").write_text("ready", encoding="utf-8")
    stale = root / "app/cli/__pycache__/main.cpython-313.pyc"
    stale.parent.mkdir()
    stale.write_bytes(b"stale")

    compile_bytecode(root, Path(sys.executable))
    assert not stale.exists()
    caches = {path.relative_to(root): path.read_bytes() for path in root.rglob("*.pyc")}
    assert len(caches) == 5
    for data in caches.values():
        assert data[:4] == importlib.util.MAGIC_NUMBER
        assert int.from_bytes(data[4:8], "little") == 1  # unchecked hash
        code = marshal.loads(data[16:])
        assert isinstance(code, CodeType)
        assert code.co_filename in sources
        assert data[8:16] == importlib.util.source_hash((root / code.co_filename).read_bytes())
        assert not Path(code.co_filename).is_absolute()
        for nested in code.co_consts:
            if isinstance(nested, CodeType):
                assert nested.co_filename == code.co_filename

    # Neither build-directory names nor source timestamps enter a cache's bytes.
    other = tmp_path / "other" / "version"
    shutil.copytree(root, other)
    for path in other.rglob("*.py"):
        os.utime(path, (1000000000, 1000000000))
    compile_bytecode(other, Path(sys.executable))
    assert {path.relative_to(other): path.read_bytes() for path in other.rglob("*.pyc")} == caches
    manifest = write_manifest(
        other,
        version="1.0.0",
        revision="abcdef",
        platform=current_platform(),
        shape="server",
        channel="main",
    )
    assert all(relative.as_posix() in manifest["files"] for relative in caches)
    archive = tmp_path / "package.zip"
    write_archive(other, archive)
    extracted = tmp_path / "extracted"
    shutil.unpack_archive(archive, extracted)
    validate_release(extracted, shape="server", remove_bytecode_caches=True)
    probe = """
import importlib.machinery, sys
sys.path.insert(0, sys.argv[1])
original = importlib.machinery.SourceFileLoader.get_data
def read(self, path):
    if path.endswith('.py'):
        raise AssertionError('startup reread source: ' + path)
    return original(self, path)
importlib.machinery.SourceFileLoader.get_data = read
import main
assert main.value() == 42
"""
    loaded = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", probe, str(extracted / "app/cli")],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert loaded.returncode == 0, loaded.stderr
    assert {
        path.relative_to(extracted): path.read_bytes() for path in extracted.rglob("*.pyc")
    } == caches


@pytest.mark.parametrize("problem", ["syntax", "python_version"])
def test_bytecode_build_fails_before_publication_for_invalid_sources_or_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    root = tmp_path / "version"
    (root / "app/core").mkdir(parents=True)
    (root / "app/core/example.py").write_text(
        "invalid syntax\n" if problem == "syntax" else "value = 42\n",
        encoding="utf-8",
    )
    if problem == "python_version":
        monkeypatch.setattr(package_build, "PYTHON_VERSION", "0.0")
    with pytest.raises(package_build.BuildError):
        compile_bytecode(root, Path(sys.executable))
