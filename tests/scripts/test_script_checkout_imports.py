"""Script entry points import project packages from their own checkout."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
PROJECT_PACKAGES = ("cli", "core", "desktop", "scripts", "server")
_MODULE_LEVEL_PROJECT_IMPORT = re.compile(
    rf"^(?:from|import) (?:{'|'.join(PROJECT_PACKAGES)})\b", re.MULTILINE
)
# One interpreter checks every script. First, each script's module-level code runs
# until its first project import, which reports where that package would come from
# and stops the script; nothing of the project is loaded, so each script starts
# clean. Then every script's module-level code runs in full (its imports, not its
# __main__ block) from the checkout; the scripts share one import of the project, so
# this takes about as long as the heaviest script (perf_bench), one to a few seconds.
_CHECK_SCRIPTS = """
import importlib.machinery, json, runpy, sys

packages = set(sys.argv[1].split(","))
scripts = sys.argv[2:]


class FirstProjectImport(BaseException):
    pass


class StopAtFirstProjectImport:
    @staticmethod
    def find_spec(name, path=None, target=None):
        if path is None and name in packages:
            spec = importlib.machinery.PathFinder.find_spec(name, sys.path)
            raise FirstProjectImport(None if spec is None else spec.origin)
        return None


origins = {}
original_path = list(sys.path)
sys.meta_path.insert(0, StopAtFirstProjectImport)
for script in scripts:
    try:
        runpy.run_path(script, run_name="checkout_import_check")
    except FirstProjectImport as stop:
        origins[script] = stop.args[0]
    finally:
        sys.path[:] = original_path
sys.meta_path.remove(StopAtFirstProjectImport)
for script in scripts:
    runpy.run_path(script, run_name="checkout_import_check")
print(json.dumps(origins))
"""


def _entry_points_importing_project_packages() -> list[str]:
    """Return runnable scripts (not private helpers) with module-level project imports."""
    return sorted(
        path.name
        for path in SCRIPTS_DIR.glob("*.py")
        if not path.name.startswith("_")
        and _MODULE_LEVEL_PROJECT_IMPORT.search(path.read_text(encoding="utf-8"))
    )


def test_entry_point_discovery_finds_project_importing_scripts() -> None:
    assert {
        "probe_background_completion.py",
        "test-env.py",
        "validate_model_db.py",
        "worktree.py",
    } <= set(_entry_points_importing_project_packages())


def test_scripts_import_their_own_checkout_before_another_installation(tmp_path: Path) -> None:
    # From a linked worktree, the editable install (or a PYTHONPATH workaround)
    # names another checkout; a shadow copy of every project package that fails
    # on import stands in for it.
    shadow = tmp_path / "shadow"
    for package in PROJECT_PACKAGES:
        (shadow / package).mkdir(parents=True)
        (shadow / package / "__init__.py").write_text(
            f"raise ImportError('{package} was imported from another checkout')\n",
            encoding="utf-8",
        )
    python_path = [str(shadow), *filter(None, [os.environ.get("PYTHONPATH")])]
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(python_path)}
    scripts = {str(SCRIPTS_DIR / name): name for name in _entry_points_importing_project_packages()}

    result = subprocess.run(
        [sys.executable, "-c", _CHECK_SCRIPTS, ",".join(PROJECT_PACKAGES), *scripts],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    origins: dict[str, str | None] = json.loads(result.stdout.splitlines()[-1])
    assert set(origins) == set(scripts)
    foreign = {
        scripts[script]: origin
        for script, origin in origins.items()
        if origin is None or not Path(origin).resolve().is_relative_to(PROJECT_ROOT)
    }
    assert foreign == {}
