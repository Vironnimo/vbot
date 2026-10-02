"""Restart the packaged Desktop into a newly activated application version.

A packaged launch (``cli.application.desktop.open_desktop``) passes the relaunch
contract in :data:`RELAUNCH_ENV`: the application's active-version file, the
version this process runs, and the command that launches the active version.
Source runs have no contract, and nothing here is active for them.

:class:`DesktopRestart` watches the version file. Once another version is
active, the Desktop is *pending*. Every activation comes from an update the
user started, so the restart is due right away:

- Voice recording or calibrating delays it until that ends, for at most
  :data:`RESTART_GRACE_SECONDS`;
- then the page is asked first (``vbot-desktop-restart``). A page that takes the
  request saves its edits and calls ``restartDesktop`` itself; without that call
  within :data:`RESTART_GRACE_SECONDS` the Desktop restarts on its own. An
  unhandled request restarts at once.

The user may also restart earlier through ``restartDesktop``.

A restart is a handoff, so a new version that cannot start never costs the
open window:

1. this process writes :data:`RESTART_REQUEST_FILE_NAME` (a nonce, the shown
   server, the page location and the window placement) into the Desktop config
   directory and runs the relaunch command;
2. the new Desktop takes the request and, finding this instance running, signals
   it with an activation request ``{"handoff": nonce}``, then waits for the
   single-instance guard to become free (``_windows.claim_desktop_instance``);
3. on that signal this process closes its window and exits; the new Desktop
   claims the guard and opens the same server, location and placement.

Without the signal within :data:`HANDOFF_TIMEOUT_SECONDS` (or when the command
fails) the restart is abandoned, the window stays and the page shows the
failure. The next automatic attempt waits :data:`FAILED_RETRY_SECONDS`, doubling
with every further failure up to :data:`FAILED_RETRY_MAX_SECONDS`; the user may
retry at once.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, TypeGuard

logger = logging.getLogger("vbot.desktop.restart")

RELAUNCH_ENV = "VBOT_DESKTOP_RELAUNCH"
RESTART_REQUEST_FILE_NAME = "restart-request.json"
#: A restart request older than this is a leftover of an abandoned restart.
RESTART_REQUEST_MAX_AGE_SECONDS = 120.0
#: How long the running Desktop waits for its successor to report.
HANDOFF_TIMEOUT_SECONDS = 60.0
VERSION_POLL_SECONDS = 5.0
#: How often the watcher looks again while an automatic restart is due.
DUE_POLL_SECONDS = 1.0
#: How long a due restart waits for Voice to finish, and then for the page that
#: took the request to call ``restartDesktop``.
RESTART_GRACE_SECONDS = 15.0
#: After a first failed restart, before the next automatic attempt; it doubles
#: with every further failure up to :data:`FAILED_RETRY_MAX_SECONDS`.
FAILED_RETRY_SECONDS = 60.0
FAILED_RETRY_MAX_SECONDS = 10 * 60.0
MAX_LOCATION_LENGTH = 2048
_MAX_VERSION_LENGTH = 128
_MAX_REQUEST_BYTES = 16 * 1024
_WINDOW_STATES = frozenset({"normal", "minimized", "maximized"})

WindowState = Literal["normal", "minimized", "maximized"]


class RestartError(Exception):
    """A restart request the Desktop cannot accept; ``error_code`` reaches the page."""

    def __init__(self, message: str, *, error_code: str) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class RelaunchContract:
    """What a packaged launch tells the Desktop about its installation."""

    version_file: Path
    version: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class WindowPlacement:
    """The window's size, state and, in the normal state, its position (logical pixels)."""

    state: WindowState
    width: int
    height: int
    x: int | None = None
    y: int | None = None


@dataclass(frozen=True)
class RestartRequest:
    """What a restarting Desktop hands to its successor."""

    nonce: str
    server: tuple[str, int] | None = None
    location: str | None = None
    placement: WindowPlacement | None = None


def relaunch_contract(environ: Mapping[str, str] = os.environ) -> RelaunchContract | None:
    """Return the packaged relaunch contract, or ``None`` for a source run or a broken value."""

    raw = environ.get(RELAUNCH_ENV)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    version_file = value.get("version_file") if isinstance(value, dict) else None
    version = value.get("version") if isinstance(value, dict) else None
    command = value.get("command") if isinstance(value, dict) else None
    if (
        not isinstance(version_file, str)
        or not Path(version_file).is_absolute()
        or not _is_version_id(version)
        or not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
        or not Path(command[0]).is_absolute()
    ):
        logger.warning("Ignoring an invalid Desktop relaunch contract; restarts are unavailable")
        return None
    assert isinstance(version, str)
    return RelaunchContract(Path(version_file), version, tuple(command))


def is_location(value: Any) -> bool:
    """Whether ``value`` is a URL fragment the successor may restore."""

    return (
        isinstance(value, str)
        and value.startswith("#")
        and len(value) <= MAX_LOCATION_LENGTH
        and value.isprintable()
        and not any(character.isspace() for character in value)
    )


def write_restart_request(
    config_directory: Path, request: RestartRequest, *, created_at: float
) -> None:
    """Write the request for the successor in one atomic replace; raises ``OSError``."""

    document: dict[str, Any] = {"created_at": created_at, "nonce": request.nonce}
    if request.server is not None:
        document["server"] = {"host": request.server[0], "port": request.server[1]}
    if request.location is not None:
        document["location"] = request.location
    if request.placement is not None:
        placement = request.placement
        document["window"] = {
            "state": placement.state,
            "width": placement.width,
            "height": placement.height,
            "x": placement.x,
            "y": placement.y,
        }
    path = Path(config_directory) / RESTART_REQUEST_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    # Leaving the block removes the temporary file unless it replaced *path*.
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete_on_close=False,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as temporary_file:
        temporary_file.write(json.dumps(document))
        temporary_file.close()
        Path(temporary_file.name).replace(path)


def discard_restart_request(config_directory: Path, nonce: str) -> None:
    """Delete the request file while it still holds this restart's ``nonce``."""

    path = Path(config_directory) / RESTART_REQUEST_FILE_NAME
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return
    if isinstance(document, dict) and document.get("nonce") == nonce:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def take_restart_request(config_directory: Path, *, now: float) -> RestartRequest | None:
    """Read and delete a predecessor's request; ``None`` when absent, stale or malformed.

    Invalid optional parts (server, location, placement) are dropped
    individually; the nonce alone still completes the handoff.
    """

    path = Path(config_directory) / RESTART_REQUEST_FILE_NAME
    try:
        if path.stat().st_size > _MAX_REQUEST_BYTES:
            raise ValueError("restart request is too large")
        payload: str | None = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, ValueError):
        logger.warning("The restart request of the previous Desktop could not be read")
        payload = None
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
    if payload is None:
        return None
    try:
        document = json.loads(payload)
    except ValueError:
        document = None
    if not isinstance(document, dict):
        logger.warning("Ignoring a malformed restart request of the previous Desktop")
        return None
    created_at = document.get("created_at")
    nonce = document.get("nonce")
    if (
        not isinstance(created_at, int | float)
        or isinstance(created_at, bool)
        or not isinstance(nonce, str)
        or not 16 <= len(nonce) <= 64
        or not nonce.isalnum()
    ):
        logger.warning("Ignoring a malformed restart request of the previous Desktop")
        return None
    age = now - created_at
    if not math.isfinite(age) or abs(age) > RESTART_REQUEST_MAX_AGE_SECONDS:
        logger.info("Ignoring a stale restart request of an earlier Desktop")
        return None
    location = document.get("location")
    return RestartRequest(
        nonce=nonce,
        server=_server(document.get("server")),
        location=location if is_location(location) else None,
        placement=_placement(document.get("window")),
    )


def _server(value: Any) -> tuple[str, int] | None:
    if not isinstance(value, dict):
        return None
    host, port = value.get("host"), value.get("port")
    if (
        isinstance(host, str)
        and host
        and host.isprintable()
        and isinstance(port, int)
        and not isinstance(port, bool)
        and 1 <= port <= 65535
    ):
        return host, port
    return None


def _placement(value: Any) -> WindowPlacement | None:
    if not isinstance(value, dict):
        return None
    state, width, height = value.get("state"), value.get("width"), value.get("height")
    if state not in _WINDOW_STATES or not _is_dimension(width) or not _is_dimension(height):
        return None
    x, y = value.get("x"), value.get("y")
    position = state == "normal" and _is_coordinate(x) and _is_coordinate(y)
    return WindowPlacement(
        state=state,
        width=width,
        height=height,
        x=x if position else None,
        y=y if position else None,
    )


def _is_dimension(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 100_000


def _is_coordinate(value: Any) -> TypeGuard[int]:
    # Windows parks minimized windows at -32000; such a position is never kept.
    return isinstance(value, int) and not isinstance(value, bool) and -20_000 < value < 100_000


def _is_version_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_VERSION_LENGTH
        and all(character.isalnum() or character in "-_." for character in value)
    )


class RestartPage(Protocol):
    """The page side: update status pushes and the restart request."""

    def publish_update(self, status: Mapping[str, Any]) -> None: ...

    def request_restart(self, on_result: Callable[[bool], None]) -> None: ...


class RelaunchProcess(Protocol):
    def poll(self) -> int | None: ...


def _spawn(command: tuple[str, ...]) -> RelaunchProcess:
    """Run the relaunch command detached, windowless and outside this process's job."""

    arguments = list(command)
    streams: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform != "win32":
        return subprocess.Popen(arguments, start_new_session=True, **streams)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.Popen(
            arguments, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **streams
        )
    except OSError:
        # A job that forbids breakaway still lets the relaunch run inside it.
        return subprocess.Popen(arguments, creationflags=flags, **streams)


class DesktopRestart:
    """Watch for a newer active version and hand the Desktop over to it.

    The window-facing callables are wired by the launcher: ``active_server``
    (the shown server), ``location`` (the page's URL fragment), ``placement``,
    ``shell_busy`` (Voice recording or calibrating) and ``close_window``. Every
    callable may run on the watcher or a restart thread.
    """

    def __init__(
        self,
        contract: RelaunchContract,
        *,
        config_directory: Path,
        page: RestartPage,
        active_server: Callable[[], tuple[str, int] | None],
        location: Callable[[], str | None],
        placement: Callable[[], WindowPlacement | None],
        shell_busy: Callable[[], bool],
        close_window: Callable[[], None],
        spawn: Callable[[tuple[str, ...]], RelaunchProcess] = _spawn,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        poll_seconds: float = VERSION_POLL_SECONDS,
    ) -> None:
        self._contract = contract
        self._config_directory = Path(config_directory)
        self._page = page
        self._active_server = active_server
        self._location = location
        self._placement = placement
        self._shell_busy = shell_busy
        self._close_window = close_window
        self._spawn = spawn
        self._clock = clock
        self._wall_clock = wall_clock
        self._poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._handoff = threading.Event()
        self._thread: threading.Thread | None = None
        self._version_signature: tuple[int, int] | None = None
        self._active_version: str | None = None
        self._pending = False
        self._restarting = False
        self._failed = False
        self._nonce: str | None = None
        self._next_automatic = clock()
        self._failures = 0
        # The current due period: when it began, whether it waits for Voice,
        # and when the page that was asked must have restarted.
        self._due_since: float | None = None
        self._waiting_for_voice = False
        self._page_deadline: float | None = None

    @property
    def running_version(self) -> str:
        return self._contract.version

    def status(self) -> dict[str, bool]:
        """Return ``{pending, restarting, failed}`` for the page."""

        with self._lock:
            return self._status_locked()

    def start(self) -> None:
        """Start watching once; checks the version file right away."""

        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._watch, name="vbot-desktop-restart", daemon=True
            )
            self._thread.start()

    def close(self) -> None:
        """Stop watching; an ongoing handoff is abandoned."""

        self._stop.set()
        self._handoff.set()

    def restart(self) -> None:
        """Start the handoff to the active version; raises :class:`RestartError`."""

        self._begin("request")

    def accept_handoff(self, nonce: Any) -> bool:
        """Accept a successor's signal; ``True`` when it belongs to the current restart."""

        with self._lock:
            matches = self._restarting and isinstance(nonce, str) and nonce == self._nonce
        if matches:
            self._handoff.set()
        else:
            logger.warning("Ignoring a Desktop handoff that belongs to no current restart")
        return matches

    def check(self) -> None:
        """Run one watcher step: the version file, then a due automatic restart."""

        self._check_version()
        now = self._clock()
        with self._lock:
            if not self._pending or self._restarting or now < self._next_automatic:
                self._due_since, self._page_deadline = None, None
                self._waiting_for_voice = False
                return
            if self._due_since is None:
                self._due_since = now
            due_since, page_deadline = self._due_since, self._page_deadline
        if page_deadline is not None:
            if now >= page_deadline:
                self._begin_automatic("page_timeout")
            return
        if now - due_since < RESTART_GRACE_SECONDS and self._voice_busy():
            with self._lock:
                waiting, self._waiting_for_voice = self._waiting_for_voice, True
            if not waiting:
                logger.info(
                    "Desktop restart waits for Voice to finish (timeout=%ds)",
                    RESTART_GRACE_SECONDS,
                )
            return
        with self._lock:
            self._page_deadline = now + RESTART_GRACE_SECONDS
        self._page.request_restart(self._page_answered)

    def _voice_busy(self) -> bool:
        try:
            return self._shell_busy()
        except Exception:
            # Counted as busy, so the grace still bounds the delay; logged once per due period.
            level = logging.DEBUG if self._waiting_for_voice else logging.WARNING
            logger.log(level, "Voice state is unavailable; counting it as busy", exc_info=True)
            return True

    def _page_answered(self, handled: bool) -> None:
        if handled:
            logger.info("The page took the restart request (timeout=%ds)", RESTART_GRACE_SECONDS)
            return
        self._begin_automatic("update")

    def _begin_automatic(self, reason: str) -> None:
        try:
            self._begin(reason)
        except RestartError:
            logger.debug("The automatic restart no longer applies", exc_info=True)

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:
                logger.warning("Desktop update check failed", exc_info=True)
            with self._lock:
                due = self._due_since is not None
            self._stop.wait(
                min(self._poll_seconds, DUE_POLL_SECONDS) if due else self._poll_seconds
            )

    def _check_version(self) -> None:
        path = self._contract.version_file
        try:
            stat = path.stat()
        except OSError:
            return
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature == self._version_signature:
            return
        try:
            active = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return
        self._version_signature = signature
        if not _is_version_id(active):
            return
        with self._lock:
            pending = active != self._contract.version
            changed = pending != self._pending or (pending and active != self._active_version)
            self._pending, self._active_version = pending, active
            if changed:
                # A failure and its retry delay belong to the version it tried to start.
                self._failed, self._failures = False, 0
                self._next_automatic = self._clock()
            status = self._status_locked()
        if not changed:
            return
        if pending:
            logger.info(
                "A newer vBot version is active (running=%s active=%s); the Desktop "
                "restarts into it now",
                self._contract.version,
                active,
            )
        self._page.publish_update(status)

    def _begin(self, reason: str) -> None:
        with self._lock:
            if not self._pending:
                raise RestartError(
                    "The Desktop already runs the active vBot version",
                    error_code="no_update_pending",
                )
            if self._restarting:
                return
            self._restarting, self._failed = True, False
            self._nonce = secrets.token_hex(16)
            nonce = self._nonce
            self._handoff.clear()
            status = self._status_locked()
        self._page.publish_update(status)
        threading.Thread(
            target=self._hand_over,
            args=(nonce, reason),
            name="vbot-desktop-handoff",
            daemon=True,
        ).start()

    def _hand_over(self, nonce: str, reason: str) -> None:
        try:
            request = RestartRequest(
                nonce=nonce,
                server=self._safe(self._active_server),
                location=self._safe(self._location),
                placement=self._safe(self._placement),
            )
            if request.location is not None and not is_location(request.location):
                request = RestartRequest(nonce, request.server, None, request.placement)
            write_restart_request(self._config_directory, request, created_at=self._wall_clock())
            logger.info(
                "Desktop restart started (reason=%s running=%s active=%s)",
                reason,
                self._contract.version,
                self._active_version,
            )
            process = self._spawn(self._contract.command)
        except OSError as exc:
            self._fail(nonce, f"the relaunch could not start ({exc})")
            return
        deadline = self._clock() + HANDOFF_TIMEOUT_SECONDS
        while not self._handoff.wait(0.25):
            code = process.poll()
            if code not in {None, 0}:
                self._fail(nonce, f"the relaunch command failed with exit code {code}")
                return
            if self._clock() >= deadline:
                self._fail(nonce, "the new Desktop did not report in time")
                return
        if self._stop.is_set():
            discard_restart_request(self._config_directory, nonce)
            return
        logger.info("Desktop handing over to version %s; closing this window", self._active_version)
        try:
            self._close_window()
        except Exception:
            logger.exception("The Desktop window could not close for the restart")

    def _fail(self, nonce: str, reason: str) -> None:
        discard_restart_request(self._config_directory, nonce)
        with self._lock:
            if self._nonce != nonce:
                return
            self._restarting, self._failed, self._nonce = False, True, None
            self._failures += 1
            retry = min(FAILED_RETRY_SECONDS * 2 ** (self._failures - 1), FAILED_RETRY_MAX_SECONDS)
            self._next_automatic = self._clock() + retry
            status = self._status_locked()
        logger.warning(
            "Desktop restart failed: %s; the window stays open (retry_in=%ds)", reason, retry
        )
        self._page.publish_update(status)

    @staticmethod
    def _safe(read: Callable[[], Any]) -> Any:
        try:
            return read()
        except Exception:
            logger.warning("Desktop state for the restart is unavailable", exc_info=True)
            return None

    def _status_locked(self) -> dict[str, bool]:
        return {"pending": self._pending, "restarting": self._restarting, "failed": self._failed}
