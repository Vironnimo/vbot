"""Launch the installed Desktop without loading server or tray services."""

from __future__ import annotations

import json
import os
import subprocess

from cli.application.operations import child_environment
from cli.application.state import ApplicationError, Installation, exclusive
from core.utils.processes import subprocess_creation_flags

#: The relaunch contract of ``desktop.restart``: how the Desktop recognizes a
#: newer active version and starts it.
RELAUNCH_ENV = "VBOT_DESKTOP_RELAUNCH"


def open_desktop(
    install: Installation,
    *,
    host: str | None = None,
    port: int | None = None,
    open_session: tuple[str, str] | None = None,
) -> None:
    """Resolve the active version and launch under the removal-admission lock.

    ``open_session`` names an Agent address and Session id for the Desktop to
    show; an already running Desktop receives it through its own handoff.
    The Desktop also receives :data:`RELAUNCH_ENV`, so it can restart into a
    version activated while it runs.
    """
    from cli.application.state import ensure_not_removing

    # Serialize launch with the uninstaller's removal reservation.
    with exclusive(install.root, "dispatch", timeout=5):
        ensure_not_removing(install.root)
        if install.install_shape not in {"server-desktop", "desktop-client"}:
            raise ApplicationError("This installation does not include the Desktop app")
        version = install.version()
        arguments = [str(install.interpreter(version.name, role="Desktop")), "-m", "desktop.main"]
        if host is not None or port is not None:
            if host is not None:
                arguments.extend(("--host", host))
            if port is not None:
                arguments.extend(("--port", str(port)))
        elif install.owns_server:
            assert install.server_host is not None and install.server_port is not None
            arguments.extend(("--host", install.server_host, "--port", str(install.server_port)))
        if open_session is not None:
            arguments.extend(("--open-session", *open_session))
        subprocess.Popen(
            arguments,
            cwd=version / "app",
            env={
                **child_environment(install),
                RELAUNCH_ENV: relaunch_contract(install, version.name),
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess_creation_flags(new_process_group=True, breakaway=True),
            start_new_session=os.name != "nt",
        )


def relaunch_contract(install: Installation, version_id: str) -> str:
    """Return the JSON contract a Desktop running ``version_id`` restarts with.

    The command is the stable GUI companion with exactly ``desktop``, which
    resolves the active version again.
    """
    return json.dumps(
        {
            "version_file": str(install.root / "active-version"),
            "version": version_id,
            "command": [str(install.root / "vBot.GUI.exe"), "desktop"],
        }
    )
