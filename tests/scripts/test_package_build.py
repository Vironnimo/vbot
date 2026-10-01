"""Package builds run on the build machine's bare Python, without vBot's dependencies."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

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
