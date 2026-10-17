"""Launch the installed Desktop, and read what a Desktop Client's tray follows.

The tray of a Desktop Client follows the server the Desktop last used and signs
in with the Desktop's Device: it reads the Desktop's settings and sign-ins
through the Desktop's own modules (stdlib-only; they import nothing else of vBot)
and never writes either.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass

from cli.application.operations import child_environment
from cli.application.state import ApplicationError, Installation, exclusive
from core.utils.processes import subprocess_creation_flags

#: The relaunch contract of ``desktop.restart``: how the Desktop recognizes a
#: newer active version and starts it.
RELAUNCH_ENV = "VBOT_DESKTOP_RELAUNCH"
#: Wildcard bind addresses: the Desktop reaches such a server over loopback.
_WILDCARD_HOSTS = frozenset({"", "*", "0.0.0.0", "::"})
_LOOPBACK_HOST = "127.0.0.1"


def open_desktop(
    install: Installation,
    *,
    host: str | None = None,
    port: int | None = None,
    open_session: tuple[str, str] | None = None,
) -> None:
    """Resolve the active version and launch under the removal-admission lock.

    Without ``host`` and ``port`` the Desktop of an installation with a server
    opens that server: over loopback when it binds a wildcard address, and with
    ``--server-data-dir``, so it signs in with the server's local access token
    instead of pairing. ``open_session`` names an Agent address and Session id
    for the Desktop to show; an already running Desktop receives it through its
    own handoff. The Desktop also receives :data:`RELAUNCH_ENV`, so it can
    restart into a version activated while it runs.
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
            server_host = (
                _LOOPBACK_HOST if install.server_host in _WILDCARD_HOSTS else install.server_host
            )
            arguments.extend(("--host", server_host, "--port", str(install.server_port)))
            if install.server_data_directory is not None:
                arguments.extend(("--server-data-dir", install.server_data_directory))
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


@dataclass(frozen=True)
class DesktopServer:
    """The server the Desktop last connected to, which a Desktop Client tray follows.

    ``host`` and ``port`` are what the Desktop's ``--host``/``--port`` take (an
    HTTPS server's host is its URL); ``origin`` is where the tray connects and
    the key of the Desktop's stored sign-in.
    """

    host: str
    port: int
    origin: str


def desktop_server() -> DesktopServer | None:
    """Read the Desktop's last-used server from its settings; ``None`` without one."""

    try:
        from desktop.server_address import parse_server_address
        from desktop.settings import read_last_used
    except ImportError:
        return None
    reference = read_last_used()
    if reference is None:
        return None
    host, port = reference["host"], reference["port"]
    try:
        address = parse_server_address(
            f"https://{host}:{port}" if reference.get("scheme") == "https" else host, port
        )
    except ValueError:
        return None
    return DesktopServer(address.bridge_host, address.port, address.origin)


def desktop_sign_in(origin: str) -> str | None:
    """The Device token the Desktop keeps for ``origin``, read fresh; ``None`` without one.

    The Desktop owns the file (``desktop/credentials.py``); the tray only reads
    it and never removes a token the server refused. The Desktop's own sign-in
    goes first; the CLI's sign-in of the same origin is the fallback.
    """

    from cli.server_client import cli_sign_in

    try:
        from desktop.credentials import DEVICE_TOKENS_FILE_NAME, DeviceTokenStore
        from desktop.settings import config_dir
    except ImportError:
        return cli_sign_in(origin)
    try:
        stored = DeviceTokenStore(config_dir() / DEVICE_TOKENS_FILE_NAME).get(origin)
    except OSError:
        stored = None
    if stored is not None and stored.token is not None:
        return stored.token
    return cli_sign_in(origin)
