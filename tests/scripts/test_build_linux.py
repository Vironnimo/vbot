from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from scripts import build_linux

_BOOTSTRAP = Path(build_linux.__file__).parent / "linux" / "vbot"
_PYTHON = build_linux.PYTHON  # python3.<minor>, as python-build-standalone names it


def _runtime_archive(path: Path) -> None:
    files = {
        f"python/bin/{_PYTHON}": b"interpreter",
        "python/bin/pip3": b"#!/build/machine/python\n",
        f"python/lib/{_PYTHON}/os.py": b"os",
        f"python/lib/{_PYTHON}/test/test_os.py": b"test",
        f"python/lib/{_PYTHON}/tkinter/__init__.py": b"tk",
        f"python/lib/lib{_PYTHON}.so.1.0": b"embedding",
        "python/lib/tcl9.0/init.tcl": b"tcl",
        f"python/include/{_PYTHON}/Python.h": b"header",
    }
    links = {
        "python/bin/python3": _PYTHON,
        "python/bin/python": _PYTHON,
        f"python/lib/{_PYTHON}/linked.py": "os.py",
    }
    with tarfile.open(path, "w:gz") as bundle:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(content), 0o755
            bundle.addfile(info, io.BytesIO(content))
        for name, target in links.items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = tarfile.SYMTYPE, target
            bundle.addfile(info)


def test_runtime_is_a_pruned_link_free_tree_with_one_interpreter(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.gz"
    _runtime_archive(archive)
    runtime = tmp_path / "runtime"

    build_linux.extract_runtime(archive, runtime)

    files = sorted(
        path.relative_to(runtime).as_posix() for path in runtime.rglob("*") if not path.is_dir()
    )
    assert files == ["bin/python3", f"lib/{_PYTHON}/linked.py", f"lib/{_PYTHON}/os.py"]
    assert not any(path.is_symlink() for path in runtime.rglob("*"))
    assert (runtime / "bin" / "python3").read_bytes() == b"interpreter"
    assert (runtime / "lib" / _PYTHON / "linked.py").read_bytes() == b"os"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="POSIX bootstrap")
@pytest.mark.parametrize("active", ["v1", "", "../escape", None], ids=str)
def test_bootstrap_runs_the_active_version_cli_or_server_through_any_link(
    tmp_path: Path, active: str | None
) -> None:
    root = tmp_path / "application"
    app = root / "versions" / "v1" / "app"
    interpreter = root / "versions" / "v1" / "runtime" / "bin" / "python3"
    app.mkdir(parents=True)
    interpreter.parent.mkdir(parents=True)
    # The fake interpreter reports where and how the bootstrap ran it.
    interpreter.write_text('#!/bin/sh\necho "$PWD|$VBOT_INSTALL_ROOT|$*"\n', encoding="utf-8")
    interpreter.chmod(0o755)
    shutil.copyfile(_BOOTSTRAP, root / "vbot")
    (root / "vbot").chmod(0o755)
    if active is not None:
        (root / "active-version").write_text(f"{active}\n", encoding="ascii")
    link = tmp_path / "bin" / "vbot"
    link.parent.mkdir()
    link.symlink_to(root / "vbot")

    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(link), *arguments], cwd=tmp_path, capture_output=True, text=True, timeout=10
        )

    cli, server = run("server", "status"), run("--server", "--port", "8420")

    if active != "v1":
        assert (cli.returncode, server.returncode) == (111, 111)
        assert "no valid active version" in cli.stderr
        return
    flags = "-I -B -X utf8 -m"
    assert cli.stdout.strip() == f"{tmp_path}|{root}|{flags} cli.main server status"
    assert server.stdout.strip() == f"{app}|{root}|{flags} server.main --port 8420"
