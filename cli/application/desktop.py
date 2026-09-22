"""Launch the installed Desktop without loading server or tray services."""

from __future__ import annotations

import os
import subprocess

from cli.application.operations import child_environment
from cli.application.state import ApplicationError, Installation, exclusive
from core.utils.processes import subprocess_creation_flags


def open_desktop(
    install: Installation, *, host: str | None = None, port: int | None = None
) -> None:
    """Resolve the active version and launch under the removal-admission lock."""
    from cli.application.state import ensure_not_removing

    # Serialize launch with the uninstaller's removal reservation.
    with exclusive(install.root, "dispatch", timeout=5):
        ensure_not_removing(install.root)
        if install.install_shape not in {"server-desktop", "desktop-client"}:
            raise ApplicationError("This installation does not include the Desktop app")
        arguments = [str(install.interpreter(role="Desktop")), "-m", "desktop.main"]
        if host is not None or port is not None:
            if host is not None:
                arguments.extend(("--host", host))
            if port is not None:
                arguments.extend(("--port", str(port)))
        elif install.owns_server:
            assert install.server_host is not None and install.server_port is not None
            arguments.extend(("--host", install.server_host, "--port", str(install.server_port)))
        subprocess.Popen(
            arguments,
            cwd=install.version() / "app",
            env=child_environment(install),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess_creation_flags(new_process_group=True, breakaway=True),
            start_new_session=os.name != "nt",
        )
