"""Script entry points import project packages from their own checkout."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
PROJECT_PACKAGES = ("cli", "core", "desktop", "scripts", "server")
_MODULE_LEVEL_PROJECT_IMPORT = re.compile(
    rf"^(?:from|import) (?:{'|'.join(PROJECT_PACKAGES)})\b", re.MULTILINE
)
# Runs the script's module-level code (its imports) without its __main__ block.
_IMPORT_SCRIPT = "import runpy, sys; runpy.run_path(sys.argv[1], run_name='checkout_import_check')"


def _entry_points_importing_project_packages() -> list[str]:
    """Return runnable scripts (not private helpers) with module-level project imports."""
    return sorted(
        path.name
        for path in SCRIPTS_DIR.glob("*.py")
        if not path.name.startswith("_")
        and _MODULE_LEVEL_PROJECT_IMPORT.search(path.read_text(encoding="utf-8"))
    )


def test_entry_point_discovery_finds_project_importing_scripts():
    assert {
        "probe_background_completion.py",
        "test-env.py",
        "validate_model_db.py",
        "worktree.py",
    } <= set(_entry_points_importing_project_packages())


@pytest.mark.parametrize("script", _entry_points_importing_project_packages())
def test_script_imports_its_own_checkout_before_another_installation(script, tmp_path):
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

    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_SCRIPT, str(SCRIPTS_DIR / script)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
