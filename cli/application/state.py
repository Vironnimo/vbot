"""Validated installation facts and durable operations for packaged applications."""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.utils.atomic import atomic_write_text
from core.utils.ids import is_safe_id, new_id

SHAPES = frozenset({"server", "server-desktop", "desktop-client"})
TERMINAL = frozenset({"completed", "failed", "rolled_back", "needs_attention", "prepared"})
PHASES = TERMINAL | {
    "queued",
    "preparing",
    "waiting_for_idle",
    "stopping",
    "activating",
    "verifying",
}
_NATIVE_HOST_NAMES = frozenset(
    {
        "vbot.exe",
        "vbot.python.exe",
        "vbot.server.exe",
        "vbot.desktop.exe",
        "vbot.update.exe",
    }
)


class ApplicationError(ValueError):
    """An installation cannot safely perform the requested operation."""


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def safe_id(value: Any) -> str:
    if not is_safe_id(value):
        raise ApplicationError("Invalid application version or operation identifier")
    return str(value)


def contained(root: Path, relative: str) -> Path:
    """Reject traversal and Windows reparse points before using managed paths."""
    if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
        raise ApplicationError("Application root must not be a link or junction")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ApplicationError("Application path escapes its installation")
    current = path
    while current != root and current != current.parent:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ApplicationError("Application paths must not contain links or junctions")
        current = current.parent
    return path


def read_json(path: Path, *, limit: int = 1024 * 1024) -> dict[str, Any]:
    try:
        if path.stat().st_size > limit:
            raise ApplicationError(f"Application record is too large: {path.name}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApplicationError(f"Cannot read application record: {path.name}") from exc
    if not isinstance(value, dict):
        raise ApplicationError(f"Application record must be an object: {path.name}")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode=0o600)


@dataclass(frozen=True)
class Installation:
    root: Path
    install_shape: str
    server_host: str | None
    server_port: int | None
    server_data_directory: str | None
    release_url: str = "https://api.github.com/repos/Vironnimo/vbot/releases/latest"
    release_public_key: str = ""

    @property
    def owns_server(self) -> bool:
        return self.install_shape != "desktop-client"

    def version(self, version_id: str | None = None) -> Path:
        if version_id is None:
            try:
                version_id = (
                    contained(self.root, "active-version").read_text(encoding="ascii").strip()
                )
            except (OSError, UnicodeError) as exc:
                raise ApplicationError("The active application version is unavailable") from exc
        return contained(self.root, f"versions/{safe_id(version_id)}")

    def interpreter(self, version_id: str | None = None, role: str = "Python") -> Path:
        if role not in {"Python", "Server", "Desktop", "Update"}:
            raise ApplicationError("Unknown application process role")
        version = self.version(version_id)
        executable = (
            version / "runtime" / (f"vBot.{role}.exe" if os.name == "nt" else "bin/python3")
        )
        if not executable.is_file():
            raise ApplicationError(f"Application runtime is missing: {executable.name}")
        return executable

    def activate(self, version_id: str) -> None:
        candidate = self.version(version_id)
        if not (candidate / "release.json").is_file():
            raise ApplicationError("Cannot activate an incomplete application version")
        atomic_write_text(contained(self.root, "active-version"), safe_id(version_id) + "\n")

    def save(self) -> None:
        value = asdict(self)
        value.pop("root")
        value["schema_version"] = 1
        write_json(self.root / "application.json", value)


def load_installation(root: Path) -> Installation:
    root = root.expanduser().absolute()
    value = read_json(contained(root, "application.json"))
    if value.get("schema_version") != 1 or value.get("install_shape") not in SHAPES:
        raise ApplicationError("Unsupported application installation record")
    shape = value["install_shape"]
    host, port, data = (
        value.get(name) for name in ("server_host", "server_port", "server_data_directory")
    )
    if shape == "desktop-client":
        if any(item is not None for item in (host, port, data)):
            raise ApplicationError("A Desktop Client must not own a server target")
    elif (
        not isinstance(host, str)
        or not host
        or type(port) is not int
        or not 1 <= port <= 65535
        or not isinstance(data, str)
        or not Path(data).is_absolute()
    ):
        raise ApplicationError("Application server target is incomplete")
    url = value.get("release_url", "https://api.github.com/repos/Vironnimo/vbot/releases/latest")
    key = value.get("release_public_key", "")
    if not isinstance(url, str) or not url.startswith("https://") or not isinstance(key, str):
        raise ApplicationError("Invalid application release configuration")
    return Installation(root, shape, host, port, data, url, key)


def _module_install_root(source: Path) -> Path | None:
    """Return the recorded install containing one loaded application payload."""

    app = source.resolve().parents[2]
    if app.name != "app" or app.parent.parent.name != "versions":
        return None
    root = app.parent.parent.parent
    return root if (root / "application.json").is_file() else None


def _native_install_root(executable: Path) -> Path | None:
    """Return the install recorded by a native vBot host path, when available."""

    executable = executable.resolve()
    if executable.name.casefold() not in _NATIVE_HOST_NAMES:
        return None
    candidates = (executable.parent, executable.parent.parent.parent.parent)
    return next((path for path in candidates if (path / "application.json").is_file()), None)


def discover(root: Path | None = None) -> Installation | None:
    if root is not None:
        return load_installation(root)
    # Loaded packaged code and its native host are stronger provenance than an
    # inherited environment. In particular, a source CLI spawned by a packaged
    # server must remain a source CLI even though Bash preserves server context.
    module_root = _module_install_root(Path(__file__))
    if module_root is not None:
        return load_installation(module_root)
    executable = Path(sys.executable)
    native_root = _native_install_root(executable)
    if native_root is not None:
        return load_installation(native_root)
    # Temporary native payloads used during install do not yet have a recorded
    # ancestor, so their native host may use the explicit destination root.
    if executable.name.casefold() in _NATIVE_HOST_NAMES:
        explicit = os.environ.get("VBOT_INSTALL_ROOT")
        if explicit:
            return load_installation(Path(explicit))
    return None


def ensure_not_removing(root: Path) -> None:
    if contained(root, "removal-pending.json").exists():
        raise ApplicationError(
            "Application removal is pending. Finish the uninstaller, or after an interrupted "
            "removal inspect the installation and run vbot application removal-reset"
        )


@contextmanager
def exclusive(
    root: Path, name: str = "operation", *, timeout: float = 0, allow_removal: bool = False
) -> Iterator[None]:
    """An OS-owned lock: process death releases it; age never breaks it."""
    path = contained(root, f".{safe_id(name)}.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    msvcrt = importlib.import_module("msvcrt")

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl = importlib.import_module("fcntl")
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ApplicationError("Another application operation is running") from exc
                time.sleep(0.1)
        try:
            if name in {"operation", "host"} and not allow_removal:
                ensure_not_removing(root)
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt = importlib.import_module("msvcrt")

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl = importlib.import_module("fcntl")
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass
class Operation:
    id: str
    phase: str = "queued"
    message: str = "Update accepted"
    created_at: str = field(default_factory=timestamp)
    updated_at: str = field(default_factory=timestamp)
    previous_version: str | None = None
    candidate_version: str | None = None
    package: str | None = None
    local_package: bool = False
    restart: bool = True
    server_was_running: bool = False
    handoff_ticket: str | None = None
    error: str | None = None
    worker_pid: int | None = None
    worker_created: float | None = None

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL

    def save(self, install: Installation) -> None:
        self.updated_at = timestamp()
        value = asdict(self)
        value["schema_version"] = 1
        write_json(contained(install.root, f"operations/{safe_id(self.id)}.json"), value)

    def transition(self, install: Installation, phase: str, message: str) -> None:
        if phase not in PHASES:
            raise ApplicationError("Unknown update phase")
        self.phase, self.message = phase, message
        self.save(install)


def load_operation(install: Installation, operation_id: str) -> Operation:
    value = read_json(contained(install.root, f"operations/{safe_id(operation_id)}.json"))
    if value.pop("schema_version", None) != 1 or value.get("id") != operation_id:
        raise ApplicationError("Unsupported update operation record")
    if value.get("phase") not in PHASES:
        raise ApplicationError("Invalid update operation phase")
    for name in ("previous_version", "candidate_version"):
        if value.get(name) is not None:
            safe_id(value[name])
    for name in ("local_package", "restart", "server_was_running"):
        if type(value.get(name)) is not bool:
            raise ApplicationError("Invalid update operation policy")
    for name in ("message", "created_at", "updated_at"):
        if not isinstance(value.get(name), str):
            raise ApplicationError("Invalid update operation metadata")
    for name in ("package", "handoff_ticket", "error"):
        if value.get(name) is not None and not isinstance(value[name], str):
            raise ApplicationError("Invalid update operation reference")
    if value.get("worker_pid") is not None and (
        type(value["worker_pid"]) is not int or value["worker_pid"] <= 0
    ):
        raise ApplicationError("Invalid update worker identity")
    if value.get("worker_created") is not None and (
        type(value["worker_created"]) not in {int, float} or value["worker_created"] <= 0
    ):
        raise ApplicationError("Invalid update worker creation time")
    try:
        return Operation(**value)
    except TypeError as exc:
        raise ApplicationError("Invalid update operation fields") from exc


def operations(install: Installation) -> list[Operation]:
    directory = contained(install.root, "operations")
    return sorted(
        (load_operation(install, path.stem) for path in directory.glob("*.json")),
        key=lambda value: value.created_at,
        reverse=True,
    )


def create_operation(install: Installation, **kwargs: Any) -> Operation:
    # Caller owns the dispatch lock; the exclusive claim additionally protects id reuse.
    directory = contained(install.root, "operations")
    directory.mkdir(parents=True, exist_ok=True)

    def claim(candidate: str) -> bool:
        if (directory / f"{candidate}.json").exists():
            return False
        # Dispatch lock prevents concurrent claims; atomic complete JSON prevents
        # readers and crash recovery observing an empty placeholder record.
        Operation(id=candidate, **kwargs).save(install)
        return True

    operation = Operation(id=new_id("upd", claim=claim), **kwargs)
    operation.save(install)
    return operation
