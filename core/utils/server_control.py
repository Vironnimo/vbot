"""Authenticated local control record for one running vBot server process."""

from __future__ import annotations

import importlib
import json
import math
import os
import secrets
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]

from core.utils.atomic import atomic_write_bytes

CONTROL_DIRECTORY_NAME = "runtime"
CONTROL_TOKEN_HEADER = "X-VBot-Control-Token"
CONTROL_SHUTDOWN_PATH = "/_vbot/control/shutdown"
CONTROL_RECORD_VERSION = 2
CONTROL_RECORD_MAX_BYTES = 16_384
CONTROL_TOKEN_BYTES = 32
# The control record carries the shutdown authority token; only the owner reads it.
CONTROL_RECORD_MODE = 0o600


@contextmanager
def server_control_claim(data_dir: str | Path, port: int):
    """Hold the OS-owned lifetime claim for one data-directory/port authority."""
    record_path = control_record_path(data_dir, port)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = record_path.with_suffix(".lock")
    handle = lock_path.open("a+b")
    locked = False
    try:
        if os.name == "nt":
            msvcrt = importlib.import_module("msvcrt")

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(  # type: ignore[attr-defined]
                handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
            )
        locked = True
        existing = read_server_control(data_dir, port)
        if existing is not None and existing.pid != os.getpid():
            try:
                process = psutil.Process(existing.pid)
                alive = (
                    abs(process.create_time() - existing.process_create_time) < 0.001
                    and process.is_running()
                )
            except (OSError, psutil.Error):
                alive = False
            if alive:
                raise RuntimeError("Another live server owns this control authority")
        yield
    except OSError as exc:
        if not locked:
            raise RuntimeError("Another server process holds this control authority") from exc
        raise
    finally:
        if locked:
            with suppress(OSError):
                handle.seek(0)
                if os.name == "nt":
                    msvcrt = importlib.import_module("msvcrt")

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(  # type: ignore[attr-defined]
                        handle.fileno(),
                        fcntl.LOCK_UN,  # type: ignore[attr-defined]
                    )
        handle.close()


@dataclass(frozen=True, slots=True)
class ServerControlRecord:
    """Per-process authority required to request cooperative server shutdown."""

    pid: int
    process_create_time: float
    port: int
    token: str
    path: Path


def control_record_path(data_dir: str | Path, port: int) -> Path:
    """Return the target-specific local control-record path."""

    if not 1 <= port <= 65_535:
        raise ValueError("Server control port must be between 1 and 65535")
    return Path(data_dir).expanduser().resolve() / CONTROL_DIRECTORY_NAME / f"server-{port}.json"


def create_server_control(
    data_dir: str | Path,
    port: int,
    *,
    pid: int | None = None,
    process_create_time: float | None = None,
    token: str | None = None,
) -> ServerControlRecord:
    """Create and atomically persist fresh authority for the current server."""

    resolved_pid = os.getpid() if pid is None else pid
    if resolved_pid <= 0:
        raise ValueError("Server control PID must be positive")
    resolved_create_time = (
        psutil.Process(resolved_pid).create_time()
        if process_create_time is None
        else process_create_time
    )
    if (
        isinstance(resolved_create_time, bool)
        or not isinstance(resolved_create_time, (int, float))
        or not math.isfinite(resolved_create_time)
        or resolved_create_time <= 0
    ):
        raise ValueError("Server control process creation time must be positive and finite")
    resolved_token = token or secrets.token_urlsafe(CONTROL_TOKEN_BYTES)
    if not resolved_token:
        raise ValueError("Server control token must not be empty")
    path = control_record_path(data_dir, port)
    payload = json.dumps(
        {
            "version": CONTROL_RECORD_VERSION,
            "pid": resolved_pid,
            "process_create_time": resolved_create_time,
            "port": port,
            "token": resolved_token,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    atomic_write_bytes(path, payload, mode=CONTROL_RECORD_MODE)
    return ServerControlRecord(
        pid=resolved_pid,
        process_create_time=float(resolved_create_time),
        port=port,
        token=resolved_token,
        path=path,
    )


def read_server_control(data_dir: str | Path, port: int) -> ServerControlRecord | None:
    """Read one valid control record without trusting arbitrary persisted fields."""

    path = control_record_path(data_dir, port)
    try:
        if path.stat().st_size > CONTROL_RECORD_MAX_BYTES:
            return None
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != CONTROL_RECORD_VERSION:
        return None
    pid = payload.get("pid")
    process_create_time = payload.get("process_create_time")
    stored_port = payload.get("port")
    token = payload.get("token")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or isinstance(process_create_time, bool)
        or not isinstance(process_create_time, (int, float))
        or not math.isfinite(process_create_time)
        or process_create_time <= 0
        or stored_port != port
        or not isinstance(token, str)
        or not token
    ):
        return None
    return ServerControlRecord(
        pid=pid,
        process_create_time=float(process_create_time),
        port=port,
        token=token,
        path=path,
    )


def remove_server_control(record: ServerControlRecord) -> None:
    """Remove the record only while it still belongs to this exact process."""

    current = read_server_control(record.path.parents[1], record.port)
    if current is None or current.pid != record.pid:
        return
    if not secrets.compare_digest(current.token, record.token):
        return
    with suppress(OSError):
        record.path.unlink()


def is_authorized_control_token(provided: str | None, expected: str | None) -> bool:
    """Compare a supplied control token without exposing timing differences."""

    if not provided or not expected:
        return False
    return secrets.compare_digest(provided, expected)


__all__ = [
    "CONTROL_SHUTDOWN_PATH",
    "CONTROL_TOKEN_HEADER",
    "ServerControlRecord",
    "control_record_path",
    "create_server_control",
    "is_authorized_control_token",
    "read_server_control",
    "remove_server_control",
    "server_control_claim",
]
