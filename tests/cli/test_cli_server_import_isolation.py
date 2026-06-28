"""Lock test: the CLI must import without the ``server`` package or fastapi.

A desktop/CLI-only install (``pip install -e ".[cli,desktop]"`` without the
``server`` extra) has no fastapi, so ``import server.app`` would fail. The CLI
must never depend on that — ``vbot --help`` and ``vbot desktop`` have to load
anyway. This test runs a fresh interpreter with a ``sys.meta_path`` blocker that
makes any import of ``fastapi`` or ``server`` fail hard, then asserts that
``import cli.main`` still succeeds. Reintroducing a ``from server ...`` import
anywhere in the CLI load path makes this subprocess exit non-zero, failing here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Runs in a clean child interpreter: install a blocker for the server stack, then
# import the CLI entrypoint. A blocked import surfacing through cli.main raises
# ModuleNotFoundError and aborts the child with a non-zero exit code.
_GUARDED_IMPORT_PROGRAM = """
import importlib.abc
import importlib.machinery
import sys

_BLOCKED_ROOTS = ("fastapi", "server")


class _BlockServerStack(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        root = fullname.split(".", 1)[0]
        if root in _BLOCKED_ROOTS:
            raise ModuleNotFoundError(f"blocked import of {fullname!r}", name=fullname)
        return None


sys.meta_path.insert(0, _BlockServerStack())

import cli.main  # noqa: F401  -- import is the assertion

print("cli.main imported without server/fastapi")
"""


def _run_guarded_import() -> subprocess.CompletedProcess[str]:
    """Import ``cli.main`` in a child interpreter with the server stack blocked."""

    return subprocess.run(
        [sys.executable, "-c", _GUARDED_IMPORT_PROGRAM],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_main_imports_without_server_package_or_fastapi() -> None:
    """``import cli.main`` succeeds even when fastapi and ``server`` are unimportable."""

    result = _run_guarded_import()

    assert result.returncode == 0, (
        "cli.main failed to import with the server stack blocked; the CLI must not "
        f"depend on the server package.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "cli.main imported without server/fastapi" in result.stdout


def test_import_blocker_actually_blocks_the_server_package() -> None:
    """Guard the guard: the blocker must really make ``import server`` fail.

    Without this, a no-op blocker would let the contract test pass vacuously even
    if the CLI did import the server package.
    """

    program = _GUARDED_IMPORT_PROGRAM.replace(
        "import cli.main  # noqa: F401  -- import is the assertion",
        "import server  # noqa: F401  -- must be blocked",
    )

    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert "ModuleNotFoundError" in result.stderr
