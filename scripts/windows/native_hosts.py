"""Compile the native Windows hosts from ``launcher.c`` with LLVM and the Windows SDK.

This module is the complete native-host recipe: ``payload.NATIVE_SOURCE_FILES``
fingerprints it together with the launcher sources, so an update recompiles
the hosts exactly when something here or in those sources changes. It imports
only the standard library and stdlib-only vBot helpers, because source updates
run it in the private build environment without application dependencies.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

from cli.application.payload import PayloadError
from core.utils.processes import subprocess_creation_flags

HOSTS = {
    "vBot.Server.exe": "server",
    "vBot.Desktop.exe": "desktop",
    "vBot.Update.exe": "update",
    "vBot.Python.exe": "python",
    "vBot.exe": "host",
    "vBot.GUI.exe": "gui",
}
# Stable bootstraps resolve the installation's active version on every launch.
STABLE_ROLES = frozenset({"host", "gui"})


def compile_hosts(source: Path, runtime: Path, *, version: str) -> None:
    """Compile every native host into a version's ``runtime`` directory."""

    for filename, role in HOSTS.items():
        compile_host(source, runtime / filename, role=role, version=version)


def compile_host(
    source: Path, output: Path, *, role: str, version: str, stable: bool = False
) -> None:
    # The GUI companion always resolves the installation pointer, including
    # when an older source updater invokes this compiler without the new flag.
    stable = stable or role in STABLE_ROLES
    windows = source / "scripts" / "windows"
    icon = (source / "desktop" / "icon.ico").resolve()
    manifest = (
        windows / ("desktop.manifest" if role == "desktop" else "launcher.manifest")
    ).resolve()
    numeric, display = _version_resource_values(version)
    # CREATE_NO_WINDOW only gives console applications an invisible console.
    # A GUI server makes pywinpty allocate and then hide a visible one on start.
    subsystem = "CONSOLE" if role in {"python", "host", "server"} else "WINDOWS"
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vbot-native-") as temporary:
        resource = Path(temporary) / "launcher.res"
        resource_command = [
            require_tool("llvm-rc"),
            "/nologo",
            f'/dVBOT_ICON_PATH="{icon}"',
            f'/dVBOT_MANIFEST_PATH="{manifest}"',
            f"/dVBOT_FILE_VERSION={numeric}",
            f'/dVBOT_FILE_VERSION_STRING="{display}"',
            f'/dVBOT_PRODUCT_NAME="{output.stem}"',
            f"/fo{resource}",
            str(windows / "launcher.rc"),
        ]
        run_tool(resource_command)
        object_path = Path(temporary) / "launcher.obj"
        compile_command = [
            require_tool("clang-cl"),
            "/nologo",
            "/c",
            "/std:c11",
            "/O2",
            "/DUNICODE",
            "/D_UNICODE",
            f'/DVBOT_ROLE=L"{role}"',
            str(windows / "launcher.c"),
            f"/Fo:{object_path}",
        ]
        if stable:
            compile_command.insert(8, "/DVBOT_STABLE_BOOTSTRAP")
        run_tool(compile_command)
        link_command = [
            require_tool("clang-cl"),
            "/nologo",
            str(object_path),
            str(resource),
            f"/Fe:{output}",
            "/link",
            f"/SUBSYSTEM:{subsystem}",
            "shell32.lib",
            "kernel32.lib",
            "user32.lib",
        ]
        run_tool(link_command)


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise PayloadError(f"required Windows build tool is unavailable: {name}")
    return path


def run_tool(command: Sequence[str]) -> None:
    """Run one build tool without a console window, raising its output on failure."""

    environment = os.environ.copy()
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    result = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
        env=environment,
        creationflags=subprocess_creation_flags(),
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise PayloadError(f"command failed ({command[0]}): {detail}")


def _version_resource_values(version: str) -> tuple[str, str]:
    parts = [int(value) for value in re.findall(r"\d+", version)[:4]]
    parts.extend([0] * (4 - len(parts)))
    return ",".join(map(str, parts)), ".".join(map(str, parts))
