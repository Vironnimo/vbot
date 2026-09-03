"""Per-user Desktop settings store for the vBot pywebview accessor.

Desktop preferences live in the OS per-user config directory, never beside the
program (a real install puts the program inside a package/venv that is not
user-writable and is replaced on update) and never in the shared server
``data_dir`` (that directory belongs to the selected vBot instance).

The on-disk schema is::

    {
      "servers": [{"host": "...", "port": 8420, "label": "..."}],
      "last_used": {"host": "...", "port": 8420},
      "window": {"width": 1280, "height": 800},
      "wakeword": {...}
    }

``servers`` is the list of remembered targets, ``last_used`` points at the
target to auto-connect on launch (a ``{host, port}`` reference, not an index, so
it survives list reordering), and ``wakeword`` holds the local voice pipeline
configuration. Reads tolerate a malformed file by returning defaults; writes
preserve unrelated top-level keys so one concern never clobbers another.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, TypeGuard

logger = logging.getLogger("vbot.desktop.settings")

APP_CONFIG_DIR_NAME = "vbot"
SETTINGS_FILE_NAME = "settings.json"
SERVERS_KEY = "servers"
LAST_USED_KEY = "last_used"
WINDOW_KEY = "window"
WAKEWORD_KEY = "wakeword"
DEFAULT_WAKEWORD_MODEL_IDS = ("builtin/okay_nabu", "builtin/hey_nabu")
# Read and write both retry a few times on transient I/O errors (e.g. a
# Windows file lock from antivirus or another accessor) before giving up.
_IO_RETRY_ATTEMPTS = 3
_IO_RETRY_BASE_DELAY_SECONDS = 0.05
_MIN_WAKEWORD_SENSITIVITY = 0.05
_MAX_WAKEWORD_SENSITIVITY = 0.95

# pywebview may dispatch bridge calls on different threads. All sections share
# one JSON document, so each section update must hold the same per-file lock for
# its complete read-modify-write transaction. The registry also coordinates
# callers that independently resolve the default settings path.
_SETTINGS_LOCKS_GUARD = threading.Lock()
_SETTINGS_LOCKS: dict[str, threading.RLock] = {}

DEFAULT_WAKEWORD_SETTINGS: dict[str, Any] = {
    "enabled": False,
    "microphone": None,
    "active_model_ids": list(DEFAULT_WAKEWORD_MODEL_IDS),
    # Sensitivity is calibrated and preserved independently per installed model.
    "model_sensitivities": {},
    # Agent/session routing is server-specific. A Desktop can switch between
    # unrelated vBot servers, where the same bare agent id may name a different
    # identity. Keeping the target beside the server URL prevents commands from
    # silently crossing that boundary after a switch.
    "server_profiles": {},
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

    resolved_path = _resolve_settings_path(path)
    with _settings_lock(resolved_path):
        return _read_settings_unlocked(resolved_path)


def _read_settings_unlocked(resolved_path: Path) -> dict[str, Any]:
    """Read a resolved settings path while its caller owns the file lock."""

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

    resolved_path = _resolve_settings_path(path)
    with _settings_lock(resolved_path):
        _write_settings_unlocked(settings, resolved_path)


def _write_settings_unlocked(settings: dict[str, Any], resolved_path: Path) -> None:
    """Write a resolved settings path while its caller owns the file lock."""

    payload = json.dumps(settings, indent=2, sort_keys=True) + "\n"

    for attempt in range(_IO_RETRY_ATTEMPTS):
        temporary_path: Path | None = None
        try:
            resolved_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=resolved_path.parent,
                delete=False,
                prefix=f".{resolved_path.name}.",
                suffix=".tmp",
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(payload)
            temporary_path.replace(resolved_path)
            return
        except OSError:
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)
            if attempt < _IO_RETRY_ATTEMPTS - 1:
                time.sleep(_IO_RETRY_BASE_DELAY_SECONDS * (attempt + 1))
                continue
            logger.error(
                "Desktop settings could not be persisted after %s attempts: %s",
                _IO_RETRY_ATTEMPTS,
                resolved_path,
                exc_info=True,
            )
            raise


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

    _write_section(SERVERS_KEY, servers, path)


def read_last_used(path: Path | None = None) -> dict[str, Any] | None:
    """Return the last-used target reference, or ``None`` when unset/malformed."""

    full = read_settings(path)
    return _normalize_target_reference(full.get(LAST_USED_KEY))


def write_last_used(host: str, port: int, path: Path | None = None) -> None:
    """Persist the last-used target reference, preserving other settings keys."""

    _write_section(LAST_USED_KEY, {"host": host, "port": port}, path)


def clear_last_used(path: Path | None = None) -> None:
    """Remove the last-used target reference while preserving other settings."""

    resolved_path = _resolve_settings_path(path)
    with _settings_lock(resolved_path):
        full = _read_settings_unlocked(resolved_path)
        if LAST_USED_KEY not in full:
            return
        del full[LAST_USED_KEY]
        _write_settings_unlocked(full, resolved_path)


def read_window_size(path: Path | None = None) -> tuple[int, int] | None:
    """Return the last Desktop window size, or ``None`` when unset/malformed."""

    full = read_settings(path)
    window = full.get(WINDOW_KEY)
    if not isinstance(window, dict):
        return None
    width = window.get("width")
    height = window.get("height")
    if not _valid_window_dimension(width) or not _valid_window_dimension(height):
        return None
    return int(width), int(height)


def write_window_size(width: int, height: int, path: Path | None = None) -> None:
    """Persist the Desktop window size while preserving other settings keys."""

    if not _valid_window_dimension(width) or not _valid_window_dimension(height):
        raise ValueError("window width and height must be positive integers")
    _write_section(WINDOW_KEY, {"width": width, "height": height}, path)


def read_wakeword_settings(path: Path | None = None) -> dict[str, Any]:
    """Read wakeword config from Desktop settings, merged with defaults.

    A missing or non-dict ``wakeword`` key falls back to the defaults.
    """

    full = read_settings(path)
    wakeword_data = full.get(WAKEWORD_KEY)
    if not isinstance(wakeword_data, dict):
        wakeword_data = {}
    merged = copy.deepcopy(DEFAULT_WAKEWORD_SETTINGS)
    for key in DEFAULT_WAKEWORD_SETTINGS:
        if key in wakeword_data:
            merged[key] = copy.deepcopy(wakeword_data[key])
    if not isinstance(merged.get("enabled"), bool):
        merged["enabled"] = False
    merged["microphone"] = _normalize_microphone_descriptor(merged.get("microphone"))
    active_model_ids = merged.get("active_model_ids")
    if _valid_active_model_ids(active_model_ids) and isinstance(active_model_ids, list):
        merged["active_model_ids"] = [model_id.strip() for model_id in active_model_ids]
    else:
        merged["active_model_ids"] = list(DEFAULT_WAKEWORD_MODEL_IDS)
    merged["model_sensitivities"] = _normalize_model_sensitivities(
        merged.get("model_sensitivities")
    )
    merged["server_profiles"] = _normalize_server_profiles(merged.get("server_profiles"))
    return merged


def write_wakeword_settings(wakeword_config: dict[str, Any], path: Path | None = None) -> None:
    """Merge wakeword config into full Desktop settings and persist atomically."""

    _write_section(WAKEWORD_KEY, wakeword_config, path)


def _write_section(key: str, value: Any, path: Path | None) -> None:
    """Update one top-level section as a serialized read-modify-write transaction."""

    resolved_path = _resolve_settings_path(path)
    with _settings_lock(resolved_path):
        full = _read_settings_unlocked(resolved_path)
        full[key] = value
        _write_settings_unlocked(full, resolved_path)


def _resolve_settings_path(path: Path | None) -> Path:
    """Resolve an explicit or default path once for a complete store operation."""

    return path if path is not None else settings_path()


def _settings_lock(resolved_path: Path) -> threading.RLock:
    """Return the process-wide transaction lock for one settings file."""

    key = os.path.normcase(os.path.abspath(os.fspath(resolved_path)))
    with _SETTINGS_LOCKS_GUARD:
        lock = _SETTINGS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _SETTINGS_LOCKS[key] = lock
        return lock


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


def _valid_window_dimension(value: Any) -> TypeGuard[int]:
    """Return whether a persisted window dimension has the supported shape."""

    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_active_model_ids(value: Any) -> bool:
    """Return whether persisted active model IDs have the supported shape."""
    if not isinstance(value, list) or not 1 <= len(value) <= 2:
        return False
    if any(not isinstance(model_id, str) or not model_id.strip() for model_id in value):
        return False
    return len({model_id.strip() for model_id in value}) == len(value)


def _normalize_microphone_descriptor(value: Any) -> dict[str, Any] | None:
    """Return the supported stable microphone identity, never a stale index."""
    if not isinstance(value, dict):
        return None
    index = value.get("index")
    name = value.get("name")
    host_api = value.get("host_api")
    if (
        not isinstance(index, int)
        or isinstance(index, bool)
        or index < 0
        or not isinstance(name, str)
        or not name.strip()
        or not isinstance(host_api, str)
    ):
        return None
    return {"index": index, "name": name.strip(), "host_api": host_api.strip()}


def _normalize_model_sensitivities(value: Any) -> dict[str, float]:
    """Drop malformed persisted model sensitivity entries."""
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, float] = {}
    for model_id, sensitivity in value.items():
        if (
            not isinstance(model_id, str)
            or not model_id.strip()
            or isinstance(sensitivity, bool)
            or not isinstance(sensitivity, (int, float))
        ):
            continue
        numeric = float(sensitivity)
        if _MIN_WAKEWORD_SENSITIVITY <= numeric <= _MAX_WAKEWORD_SENSITIVITY:
            normalized[model_id.strip()] = numeric
    return normalized


def _normalize_server_profiles(value: Any) -> dict[str, dict[str, Any]]:
    """Keep only valid server-scoped Voice routing fields."""
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for server_url, profile in value.items():
        if (
            not isinstance(server_url, str)
            or not server_url.strip()
            or not isinstance(profile, dict)
        ):
            continue
        target_agent_id = profile.get("target_agent_id")
        session_behavior = profile.get("session_behavior")
        if (
            "target_agent_id" in profile
            and target_agent_id is not None
            and (not isinstance(target_agent_id, str) or not target_agent_id.strip())
        ):
            continue
        if "session_behavior" in profile and session_behavior not in {"active", "new"}:
            continue
        normalized_profile: dict[str, Any] = {}
        if "target_agent_id" in profile:
            normalized_profile["target_agent_id"] = (
                target_agent_id.strip() if isinstance(target_agent_id, str) else None
            )
        if "session_behavior" in profile:
            normalized_profile["session_behavior"] = session_behavior
        if normalized_profile:
            normalized[server_url.strip()] = normalized_profile
    return normalized
