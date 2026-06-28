"""Per-user Desktop settings store for the vBot pywebview accessor.

Desktop preferences live in the OS per-user config directory, never beside the
program (a real install puts the program inside a package/venv that is not
user-writable and is replaced on update) and never in the shared server
``data_dir`` (that directory belongs to the selected vBot instance).

The on-disk schema is::

    {
      "servers": [{"host": "...", "port": 8420, "label": "..."}],
      "last_used": {"host": "...", "port": 8420},
      "wakeword": {...}
    }

``servers`` is the list of remembered targets, ``last_used`` points at the
target to auto-connect on launch (a ``{host, port}`` reference, not an index, so
it survives list reordering), and ``wakeword`` holds the local voice pipeline
configuration. Reads tolerate a malformed file by returning defaults; writes
preserve unrelated top-level keys so one concern never clobbers another.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

APP_CONFIG_DIR_NAME = "vbot"
SETTINGS_FILE_NAME = "settings.json"
SERVERS_KEY = "servers"
LAST_USED_KEY = "last_used"
WAKEWORD_KEY = "wakeword"
# Read and write both retry a few times on transient I/O errors (e.g. a
# Windows file lock from antivirus or another accessor) before giving up.
_IO_RETRY_ATTEMPTS = 3
_IO_RETRY_BASE_DELAY_SECONDS = 0.05

DEFAULT_WAKEWORD_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "engine": "openwakeword",
    "microphone": None,
    "sensitivity": 0.5,
    "target_agent_id": None,
    "session_behavior": "active",
    "wake_phrase": "hey_jarvis",
}


def resolve_config_dir(
    os_name: str,
    environ: Mapping[str, str],
    home: PurePath,
) -> PurePath:
    """Resolve the per-user config dir from explicit platform inputs.

    Windows (``os_name == "nt"``) uses ``%APPDATA%\\vbot`` (falling back to
    ``<home>\\AppData\\Roaming\\vbot`` when ``APPDATA`` is unset); every other
    platform follows the XDG base directory convention — ``$XDG_CONFIG_HOME/vbot``
    when set, else ``~/.config/vbot``. macOS falls into the XDG branch until a Mac
    installer exists. Pure inputs make both branches testable on any host without
    mutating the global ``os.name`` (which would break ``pathlib`` flavor
    selection).
    """

    if os_name == "nt":
        appdata = environ.get("APPDATA")
        base: PurePath = PureWindowsPath(appdata) if appdata else home / "AppData" / "Roaming"
        return base / APP_CONFIG_DIR_NAME

    xdg_config_home = environ.get("XDG_CONFIG_HOME")
    base = PurePosixPath(xdg_config_home) if xdg_config_home else home / ".config"
    return base / APP_CONFIG_DIR_NAME


def config_dir() -> Path:
    """Return the per-user Desktop config directory for the current host.

    Thin binding of :func:`resolve_config_dir` to the live platform; the policy
    (Windows ``%APPDATA%`` vs XDG) lives there. The directory is not created here
    — writers create it on demand.
    """

    return Path(resolve_config_dir(os.name, os.environ, Path.home()))


def settings_path(base_dir: Path | None = None) -> Path:
    """Return the Desktop settings file path inside the per-user config dir."""

    return (base_dir if base_dir is not None else config_dir()) / SETTINGS_FILE_NAME


def read_settings(path: Path | None = None) -> dict[str, Any]:
    """Read Desktop settings, defaulting to empty settings.

    A missing file, an unreadable file, or malformed/non-object JSON all yield
    an empty dict rather than raising, so a corrupt file never crashes launch.
    """

    resolved_path = path if path is not None else settings_path()
    if not resolved_path.exists():
        return {}

    for attempt in range(_IO_RETRY_ATTEMPTS):
        try:
            data = json.loads(resolved_path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt < _IO_RETRY_ATTEMPTS - 1:
                time.sleep(_IO_RETRY_BASE_DELAY_SECONDS * (attempt + 1))
                continue
            return {}
        except (OSError, json.JSONDecodeError):
            return {}
        else:
            break
    if not isinstance(data, dict):
        return {}
    return data


def write_settings(settings: dict[str, Any], path: Path | None = None) -> None:
    """Persist Desktop settings with a same-directory atomic replace.

    The config directory is created on demand. The write goes to a temporary
    file in the same directory and is then atomically renamed into place, so a
    reader never observes a half-written file.
    """

    resolved_path = path if path is not None else settings_path()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(settings, indent=2, sort_keys=True) + "\n"

    temporary_path: Path | None = None
    for attempt in range(_IO_RETRY_ATTEMPTS):
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=resolved_path.parent,
                delete=False,
                prefix=f".{resolved_path.name}.",
                suffix=".tmp",
            ) as temporary_file:
                temporary_file.write(payload)
                temporary_path = Path(temporary_file.name)
            temporary_path.replace(resolved_path)
            return
        except PermissionError:
            if temporary_path is not None and temporary_path.exists():
                with suppress(OSError):
                    temporary_path.unlink()
            if attempt < _IO_RETRY_ATTEMPTS - 1:
                time.sleep(_IO_RETRY_BASE_DELAY_SECONDS * (attempt + 1))
                continue
        except OSError:
            if temporary_path is not None and temporary_path.exists():
                with suppress(OSError):
                    temporary_path.unlink()
            if attempt < _IO_RETRY_ATTEMPTS - 1:
                time.sleep(_IO_RETRY_BASE_DELAY_SECONDS * (attempt + 1))
                continue
            return


def read_servers(path: Path | None = None) -> list[dict[str, Any]]:
    """Return the remembered-servers list, ignoring malformed entries.

    Each kept entry is a dict with a string ``host`` and an integer ``port``; an
    optional ``label`` string is carried through when present. Anything that does
    not fit that shape is dropped rather than raising, so a hand-edited file with
    one bad entry still yields the usable ones.
    """

    full = read_settings(path)
    raw_servers = full.get(SERVERS_KEY)
    if not isinstance(raw_servers, list):
        return []
    servers: list[dict[str, Any]] = []
    for entry in raw_servers:
        server = _normalize_server_entry(entry)
        if server is not None:
            servers.append(server)
    return servers


def write_servers(servers: list[dict[str, Any]], path: Path | None = None) -> None:
    """Persist the remembered-servers list, preserving other settings keys."""

    full = read_settings(path)
    full[SERVERS_KEY] = servers
    write_settings(full, path)


def read_last_used(path: Path | None = None) -> dict[str, Any] | None:
    """Return the last-used target reference, or ``None`` when unset/malformed."""

    full = read_settings(path)
    return _normalize_target_reference(full.get(LAST_USED_KEY))


def write_last_used(host: str, port: int, path: Path | None = None) -> None:
    """Persist the last-used target reference, preserving other settings keys."""

    full = read_settings(path)
    full[LAST_USED_KEY] = {"host": host, "port": port}
    write_settings(full, path)


def read_wakeword_settings(path: Path | None = None) -> dict[str, Any]:
    """Read wakeword config from Desktop settings, merged with defaults.

    A missing or non-dict ``wakeword`` key falls back to the defaults.
    """

    full = read_settings(path)
    wakeword_data = full.get(WAKEWORD_KEY)
    if not isinstance(wakeword_data, dict):
        wakeword_data = {}
    merged = dict(DEFAULT_WAKEWORD_SETTINGS)
    merged.update(wakeword_data)
    return merged


def write_wakeword_settings(wakeword_config: dict[str, Any], path: Path | None = None) -> None:
    """Merge wakeword config into full Desktop settings and persist atomically."""

    full = read_settings(path)
    full[WAKEWORD_KEY] = wakeword_config
    write_settings(full, path)


def _normalize_server_entry(entry: Any) -> dict[str, Any] | None:
    """Return a clean ``{host, port[, label]}`` dict, or ``None`` when invalid."""

    if not isinstance(entry, dict):
        return None
    host = entry.get("host")
    port = entry.get("port")
    if not isinstance(host, str) or not host:
        return None
    # bool is an int subclass; an accidental True/False port is not a valid port.
    if not isinstance(port, int) or isinstance(port, bool):
        return None
    server: dict[str, Any] = {"host": host, "port": port}
    label = entry.get("label")
    if isinstance(label, str):
        server["label"] = label
    return server


def _normalize_target_reference(reference: Any) -> dict[str, Any] | None:
    """Return a clean ``{host, port}`` reference, or ``None`` when invalid."""

    server = _normalize_server_entry(reference)
    if server is None:
        return None
    return {"host": server["host"], "port": server["port"]}
