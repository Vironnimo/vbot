"""Server startup entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import gc
import logging
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from time import perf_counter
from types import FrameType
from typing import Any, Literal

import psutil  # type: ignore[import-untyped]

from core.storage.layout import DataDirectoryLayout, initialize_data_directory
from core.utils.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    PORT_SETTING_KEYS,
    Config,
    ServerBind,
    resolve_port,
    resolve_server_bind,
)
from core.utils.logging import LogManager, build_uvicorn_log_config, get_logger
from core.utils.processes import activate_process_containment
from core.utils.server_control import (
    SHUTDOWN_FAILED_EXIT_CODE,
    STARTUP_FAILED_EXIT_CODE,
    UNKNOWN_STOP_INITIATOR,
    create_server_control,
    remove_server_control,
    server_control_claim,
)
from server.app import create_app

# Re-exported from core.utils.config so existing `from server.main import ...`
# consumers (and the server tests) keep working after the resolver moved to core.
__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "PORT_SETTING_KEYS",
    "ServerBind",
    "main",
    "parse_args",
    "resolve_port",
    "resolve_server_bind",
]

_LOGGER = get_logger("server")
# The directory holding this code: `app` inside an installed version of a packaged build.
_APP_ROOT = Path(__file__).resolve().parents[1]
# Fatal errors (a native crash, a fatal interpreter error) bypass logging; the
# fault handler writes them to this file in the log directory instead.
CRASH_LOG_NAME = "server-crash.log"
# Checked at each start: a larger crash log moves to its single previous generation.
_CRASH_LOG_ROTATE_BYTES = 1024 * 1024
# Bound transport draining before lifespan teardown cancels Runs and closes services.
_GRACEFUL_SHUTDOWN_SECONDS = 5

_UVICORN_IMPORT_ERROR: ModuleNotFoundError | None

try:
    import uvicorn  # type: ignore[import-not-found]
except ModuleNotFoundError as exc:  # pragma: no cover - exercised when server extra is absent.
    uvicorn = None  # type: ignore[assignment]
    _UVICORN_IMPORT_ERROR = exc
else:
    _UVICORN_IMPORT_ERROR = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse server CLI arguments."""
    parser = argparse.ArgumentParser(description="Start the vBot server")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--verification-only", action="store_true")
    modes.add_argument("--test-instance", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Start uvicorn for the vBot FastAPI app.

    Exits with ``STARTUP_FAILED_EXIT_CODE`` when the application failed to start and
    with ``SHUTDOWN_FAILED_EXIT_CODE`` after a stop whose Runtime shutdown failed.
    """
    if uvicorn is None:
        raise RuntimeError("uvicorn is required to start the server") from _UVICORN_IMPORT_ERROR
    args = parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else None
    config = Config(data_dir=data_dir)
    safe_startup_mode: Literal["verification", "test"] | None = (
        "verification" if args.verification_only else "test" if args.test_instance else None
    )
    server_bind = resolve_server_bind(config, host=args.host, explicit_port=args.port)
    activate_process_containment()
    if not config.data_dir.expanduser().exists():
        # The control claim writes into the data directory. A fresh root must come
        # from the canonical layout so it carries the Session store's bootstrap
        # marker; an existing root without that marker is refused at startup.
        initialize_data_directory(config.data_dir, resources_dir=config.get("RESOURCES_PATH"))
    with server_control_claim(config.data_dir, server_bind["listen_port"]):
        control = create_server_control(config.data_dir, server_bind["listen_port"])
        lifecycle = _ServerLifecycle(
            config=config,
            server_bind=server_bind,
            mode=safe_startup_mode or "normal",
            # Durations need the wall-clock creation time, not the record's identity.
            process_created=psutil.Process().create_time(),
        )
        log_manager: LogManager | None = None
        server_holder: dict[str, Any] = {}
        shutdown_event = asyncio.Event()
        try:

            def request_shutdown(initiator: str = UNKNOWN_STOP_INITIATOR) -> None:
                lifecycle.request_stop("control", initiator=initiator)
                server = server_holder.get("server")
                if server is not None:
                    shutdown_event.set()
                    server.should_exit = True

            restart_scheduled = False

            def request_restart() -> None:
                nonlocal restart_scheduled
                if restart_scheduled:
                    return
                from cli.server_management import resolve_instance, schedule_server_restart

                instance = resolve_instance(
                    host=server_bind["listen_host"],
                    port=server_bind["listen_port"],
                    data_dir=config.data_dir,
                )
                result = schedule_server_restart(instance, wait_pid=os.getpid())
                if not result.ok:
                    raise RuntimeError("restart_unavailable")
                restart_scheduled = True
                lifecycle.request_stop("restart")
                # Let the RPC response reach the client before cooperative teardown.
                asyncio.get_running_loop().call_later(0.5, request_shutdown)

            def on_ready(runtime: Any) -> None:
                _prepare_for_serving()
                lifecycle.started(runtime)

            def on_stopped(runtime_stopped_cleanly: bool) -> None:
                lifecycle.stopped(
                    listening=_server_listening(server_holder),
                    runtime_stopped_cleanly=runtime_stopped_cleanly,
                )

            app_kwargs: dict[str, Any] = {
                "config": config,
                "server_bind": server_bind,
                "shutdown_token": control.token,
                "request_shutdown": request_shutdown,
                "shutdown_event": shutdown_event,
                "request_restart": request_restart,
                "on_ready": on_ready,
                "on_stopped": on_stopped,
            }
            if safe_startup_mode is not None:
                app_kwargs["safe_startup_mode"] = safe_startup_mode
            app = create_app(**app_kwargs)
            uvicorn_config = uvicorn.Config(
                app,
                host=server_bind["listen_host"],
                port=server_bind["listen_port"],
                # Keep synchronous WebSocket compression off the shared Event Loop.
                ws_per_message_deflate=False,
                timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS,
                log_level="info",
                access_log=False,
                log_config=build_uvicorn_log_config(),
            )
            # uvicorn applies its logging configuration while building its config,
            # which closes every handler that exists at that moment.
            log_manager = LogManager(
                level=config.get("LOG_LEVEL", "INFO"), data_dir=config.data_dir
            )
            _enable_crash_log(config.data_dir)
            server = uvicorn.Server(uvicorn_config)
            uvicorn_handle_exit = server.handle_exit

            def handle_exit(sig: int, frame: FrameType | None) -> None:
                lifecycle.request_stop("signal", signal=_signal_name(sig))
                shutdown_event.set()
                uvicorn_handle_exit(sig, frame)

            server.handle_exit = handle_exit  # type: ignore[method-assign]
            server_holder["server"] = server
            server.run()
        finally:
            if log_manager is not None:
                # Covers a startup failure and an exit that skipped the app shutdown.
                lifecycle.stopped(listening=_server_listening(server_holder))
                log_manager.close()
            remove_server_control(control)
    # Current uvicorn itself exits with STARTUP_FAILED_EXIT_CODE when the application
    # fails to start; older versions return from run() instead.
    if lifecycle.startup_failed:
        raise SystemExit(STARTUP_FAILED_EXIT_CODE)
    if lifecycle.shutdown_failed:
        raise SystemExit(SHUTDOWN_FAILED_EXIT_CODE)


class _ServerLifecycle:
    """Write the server process's single start line and single stop line.

    The start line says which version runs in which mode and what it serves;
    the stop line says why the process stopped and how long it ran.
    """

    def __init__(
        self,
        *,
        config: Config,
        server_bind: ServerBind,
        mode: str,
        process_created: float,
    ) -> None:
        self._config = config
        self._server_bind = server_bind
        self._mode = mode
        self._process_created = process_created
        self._stop_cause: dict[str, str] | None = None
        self._ready = False
        self._stopped = False
        self.startup_failed = False
        self.shutdown_failed = False

    def request_stop(self, reason: str, **details: str) -> None:
        """Remember why the server stops; the first cause wins."""
        if self._stop_cause is None:
            self._stop_cause = {"reason": reason, **details}

    def started(self, runtime: Any) -> None:
        self._ready = True
        build = runtime.build
        fields: dict[str, object] = {"version": self._config.get("VBOT_VERSION") or build.version}
        if build.revision is not None:
            fields["revision"] = build.revision[:8]
        if build.release:
            fields["release"] = "yes"
        elif build.branch is not None:
            fields["branch"] = build.branch
        version_id = _installed_version_id()
        if version_id is not None:
            fields["version_id"] = version_id
        fields.update(
            mode=self._mode,
            pid=os.getpid(),
            listen=f"{self._server_bind['listen_host']}:{self._server_bind['listen_port']}",
            data_dir=self._config.data_dir,
            startup=f"{time.time() - self._process_created:.1f}s",
        )
        _LOGGER.info(
            "Server started (%s %s)", _format_fields(fields), runtime.startup_summary.describe()
        )

    def stopped(self, *, listening: bool, runtime_stopped_cleanly: bool = True) -> None:
        """Write the stop line once; a failed Runtime shutdown marks it ``shutdown=failed``.

        The Runtime logged each failed shutdown step; the stop line only records
        the outcome, which also sets the process exit status.
        """
        if self._stopped:
            return
        self._stopped = True
        self.shutdown_failed = not runtime_stopped_cleanly
        cause = self._stop_cause
        if cause is None:
            cause = {"reason": "unknown" if self._ready and listening else "startup_failed"}
        self.startup_failed = cause["reason"] == "startup_failed"
        fields: dict[str, object] = dict(cause)
        if self.shutdown_failed:
            fields["shutdown"] = "failed"
        fields["uptime"] = _format_duration(time.time() - self._process_created)
        _LOGGER.log(
            logging.WARNING
            if self.shutdown_failed or cause["reason"] in {"startup_failed", "unknown"}
            else logging.INFO,
            "Server stopped (%s)",
            _format_fields(fields),
        )


def _installed_version_id() -> str | None:
    """Name the installed version a packaged server runs from, ``None`` elsewhere."""
    # Payload layout: <install>/versions/<version id>/app is the running code.
    if _APP_ROOT.name != "app" or _APP_ROOT.parent.parent.name != "versions":
        return None
    return _APP_ROOT.parent.name


def _server_listening(server_holder: dict[str, Any]) -> bool:
    return bool(getattr(server_holder.get("server"), "started", False))


def _signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return str(number)


def _format_fields(fields: dict[str, object]) -> str:
    """Render ``key=value`` log fields, quoting values that contain whitespace."""
    rendered = []
    for key, value in fields.items():
        text = str(value)
        rendered.append(f'{key}="{text}"' if any(c.isspace() for c in text) else f"{key}={text}")
    return " ".join(rendered)


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rest = divmod(total, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h{minutes:02d}m"
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _enable_crash_log(data_dir: Path) -> None:
    """Record this process's fatal errors in the crash log of its log directory.

    Each start appends one line in the daily log format, so a crashed process's
    dump (every thread's Python stack, and the C stack where the platform
    provides one) follows the line of its own start, and the Logs view shows it
    as that line's continuation. The fault handler keeps the file object
    referenced, so it stays open until the interpreter has finalized; native
    libraries still crash there.
    """
    path = DataDirectoryLayout(data_dir).logs / CRASH_LOG_NAME
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    header = f"{started} [INFO] vbot.server - Server process started (pid={os.getpid()})\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A missing file needs nothing; a file another server process holds open
        # cannot move and keeps growing until a later start.
        with suppress(OSError):
            if path.stat().st_size > _CRASH_LOG_ROTATE_BYTES:
                os.replace(path, path.with_name(f"{path.name}.1"))
        crash_log = path.open("ab", buffering=0)
        try:
            crash_log.write(header.encode("utf-8"))
        except OSError:
            crash_log.close()
            raise
    except OSError as exc:
        _LOGGER.warning("Opening the crash log failed (path=%s error=%s)", path, exc)
        return
    faulthandler.enable(file=crash_log, all_threads=True, c_stack=True)


# A thread that computes holds the GIL until the waiting Event Loop asks for it,
# and the loop waits this long before asking, on every reacquisition after I/O.
# Windows rounds a wait of 1 ms or more up to its 15.6 ms timer tick, so with
# the default 5 ms a busy worker thread cuts the loop from thousands of I/O
# operations per second to about ten. Below 1 ms the loop asks at once.
_GIL_SWITCH_INTERVAL_SECONDS = 0.0005


def _prepare_for_serving() -> None:
    """Tune the process for serving once startup is complete.

    Modules, classes and Runtime services live as long as the process, yet every
    full collection would traverse them again while holding the GIL and so
    stall the Event Loop. Collecting first keeps startup garbage out of the
    frozen set. The shorter GIL switch interval lets the Event Loop take the GIL
    back promptly from worker threads that compute.
    """
    sys.setswitchinterval(_GIL_SWITCH_INTERVAL_SECONDS)
    started = perf_counter()
    gc.collect()
    gc.freeze()
    _LOGGER.debug(
        "Startup heap frozen (objects=%d collect_ms=%d)",
        gc.get_freeze_count(),
        round((perf_counter() - started) * 1000),
    )


if __name__ == "__main__":
    main()
