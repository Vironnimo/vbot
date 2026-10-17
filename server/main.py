"""Server startup entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import faulthandler
import gc
import logging
import os
import signal
import socket
import sys
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from types import FrameType
from typing import Any, Literal, NoReturn

import psutil  # type: ignore[import-untyped]

from core.access.config import (
    ACCESS_MODES,
    AccessConfig,
    AccessConfigError,
    access_config,
    check_bind_host,
    default_host,
    inferred_access_mode,
)
from core.access.local_token import create_local_access_token, remove_local_access_token
from core.access.server_record import (
    RecordedServerAccess,
    read_server_access,
    write_server_access,
)
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
from core.utils.processes import ForeignJob, activate_process_containment, foreign_job
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
    parser.add_argument(
        "--host",
        help="Bind address; default 127.0.0.1 for local and authenticated behind a proxy, "
        "else 0.0.0.0. Without access options the data directory's recorded address wins.",
    )
    parser.add_argument("--port", type=int)
    parser.add_argument("--data-dir")
    parser.add_argument(
        "--access",
        choices=ACCESS_MODES,
        help="Who may connect: local (this computer only), lan (home network without "
        "login) or authenticated (with login, from the internet). Omitted: what the data "
        "directory records; without a record local for a loopback bind, else lan. A "
        "packaged installation's record decides for its own server.",
    )
    parser.add_argument(
        "--allow-network",
        action="append",
        default=[],
        metavar="CIDR",
        help="lan: also admit this network (repeatable)",
    )
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="NAME",
        help="also accept this name in a request's Host header, such as a DNS name of "
        "this machine (repeatable)",
    )
    parser.add_argument("--public-url", help="authenticated: the HTTPS address browsers use")
    parser.add_argument(
        "--trusted-proxy",
        action="append",
        default=[],
        metavar="IP_OR_CIDR",
        help="authenticated: the reverse proxy that terminates HTTPS (repeatable)",
    )
    parser.add_argument("--tls-cert", help="authenticated: certificate chain file for HTTPS")
    parser.add_argument("--tls-key", help="authenticated: private key file for HTTPS")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--verification-only", action="store_true")
    modes.add_argument("--test-instance", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Start uvicorn for the vBot FastAPI app.

    Exits with ``STARTUP_FAILED_EXIT_CODE`` when the application failed to start and
    with ``SHUTDOWN_FAILED_EXIT_CODE`` after a stop whose Runtime shutdown failed.
    """
    main_entered_at = time.time()
    setup_started = perf_counter()
    if uvicorn is None:
        raise RuntimeError("uvicorn is required to start the server") from _UVICORN_IMPORT_ERROR
    args = parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else None
    config = Config(data_dir=data_dir)
    safe_startup_mode: Literal["verification", "test"] | None = (
        "verification" if args.verification_only else "test" if args.test_instance else None
    )
    port_bind = resolve_server_bind(config, host=DEFAULT_HOST, explicit_port=args.port)
    try:
        served = _resolve_access(args, config.data_dir, port_bind["listen_port"])
        tls = _tls_holder(served.access)
    except AccessConfigError as exc:
        _refuse_start(str(exc))
    access = served.access
    server_bind: ServerBind = {**port_bind, "listen_host": served.host}
    # Before containment, which nests this process in a Job of its own.
    enclosing_job = _enclosing_foreign_job()
    activate_process_containment()
    if not config.data_dir.expanduser().exists():
        # The control claim writes into the data directory. A fresh root must come
        # from the canonical layout so it carries the Session store's bootstrap
        # marker; an existing root without that marker is refused at startup.
        initialize_data_directory(config.data_dir)
    with server_control_claim(config.data_dir, server_bind["listen_port"]) as previous:
        if served.record is not None:
            try:
                write_server_access(config.data_dir, served.record)
            except (OSError, ValueError) as exc:
                _refuse_start(f"recording the access configuration failed: {exc}")
        control = create_server_control(config.data_dir, server_bind["listen_port"])
        try:
            local_token = create_local_access_token(config.data_dir, server_bind["listen_port"])
        except BaseException:
            remove_server_control(control)
            raise
        lifecycle = _ServerLifecycle(
            config=config,
            server_bind=server_bind,
            mode=safe_startup_mode or "normal",
            access=access.mode,
            # Durations need the wall-clock creation time, not the record's identity.
            process_created=psutil.Process().create_time(),
            main_entered_at=main_entered_at,
            setup_started=setup_started,
        )
        log_manager: LogManager | None = None
        server_holder: dict[str, Any] = {}
        ready_runtime: Any = None
        shutdown_event = asyncio.Event()
        certificate_watch: list[asyncio.Task[None]] = []
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
                nonlocal ready_runtime
                _prepare_for_serving()
                ready_runtime = runtime
                if tls is not None:
                    certificate_watch.append(
                        asyncio.get_running_loop().create_task(
                            tls.watch(), name="tls-certificate-watch"
                        )
                    )

            def on_stopped(runtime_stopped_cleanly: bool) -> None:
                for task in certificate_watch:
                    task.cancel()
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
                "access": access,
                "local_access_token": local_token.value,
            }
            if safe_startup_mode is not None:
                app_kwargs["safe_startup_mode"] = safe_startup_mode
            app = create_app(**app_kwargs)
            protocol: dict[str, Any] = {}
            if tls is not None:
                from server.access.tls import sniffing_protocol_class

                protocol["http"] = sniffing_protocol_class(tls)
            uvicorn_config = uvicorn.Config(
                app,
                host=server_bind["listen_host"],
                port=server_bind["listen_port"],
                # The access guard alone resolves client addresses and schemes from
                # forwarding headers, and only from configured trusted proxies.
                proxy_headers=False,
                # Keep synchronous WebSocket compression off the shared Event Loop.
                ws_per_message_deflate=False,
                **protocol,
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
            if previous is not None:
                # Its own shutdown removes the record last, after its stop line.
                _LOGGER.warning(
                    "Previous server process ended without shutting down (pid=%s)", previous.pid
                )
            _log_enclosing_job(enclosing_job)
            for warning in served.warnings:
                _LOGGER.warning("%s", warning)
            if served.record is not None:
                _LOGGER.info(
                    "Server access recorded for the data directory (access=%s listen=%s)",
                    access.mode,
                    served.host,
                )
            server = uvicorn.Server(uvicorn_config)
            uvicorn_startup = server.startup
            uvicorn_handle_exit = server.handle_exit

            async def startup(sockets: list[socket.socket] | None = None) -> None:
                await uvicorn_startup(sockets=sockets)
                # Lifespan readiness precedes binding; only a bound server serves.
                if server.started:
                    lifecycle.started(ready_runtime)

            def handle_exit(sig: int, frame: FrameType | None) -> None:
                lifecycle.request_stop("signal", signal=_signal_name(sig))
                shutdown_event.set()
                uvicorn_handle_exit(sig, frame)

            server.startup = startup  # type: ignore[method-assign]
            server.handle_exit = handle_exit  # type: ignore[method-assign]
            server_holder["server"] = server
            lifecycle.starting_app()
            server.run()
        finally:
            if log_manager is not None:
                # Covers a startup failure and an exit that skipped the app shutdown.
                lifecycle.stopped(listening=_server_listening(server_holder))
                log_manager.close()
            remove_local_access_token(local_token)
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
        access: str,
        process_created: float,
        main_entered_at: float,
        setup_started: float,
    ) -> None:
        self._config = config
        self._server_bind = server_bind
        self._mode = mode
        self._access = access
        self._process_created = process_created
        # Before main includes interpreter startup and the full import graph.
        # Later phases use a monotonic clock, including the final heap freeze.
        self._startup_phases = {"pre_main": max(0.0, main_entered_at - process_created)}
        self._phase_started = setup_started
        self._stop_cause: dict[str, str] | None = None
        self._ready = False
        self._stopped = False
        self.startup_failed = False
        self.shutdown_failed = False

    def request_stop(self, reason: str, **details: str) -> None:
        """Remember why the server stops; the first cause wins."""
        if self._stop_cause is None:
            self._stop_cause = {"reason": reason, **details}

    def starting_app(self) -> None:
        """Separate entrypoint setup from uvicorn and application readiness."""
        now = perf_counter()
        self._startup_phases["setup"] = now - self._phase_started
        self._phase_started = now

    def started(self, runtime: Any) -> None:
        self._ready = True
        self._startup_phases["app"] = perf_counter() - self._phase_started
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
            access=self._access,
            pid=os.getpid(),
            listen=f"{self._server_bind['listen_host']}:{self._server_bind['listen_port']}",
            data_dir=self._config.data_dir,
            startup=f"{sum(self._startup_phases.values()):.1f}s",
        )
        fields.update({phase: f"{seconds:.3f}s" for phase, seconds in self._startup_phases.items()})
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


@dataclass(frozen=True)
class _ServedAccess:
    """What the server serves, what it records for its data directory and what it warns."""

    access: AccessConfig
    host: str
    #: Written to the data directory before serving; ``None`` keeps the record as it is.
    record: RecordedServerAccess | None
    warnings: tuple[str, ...] = ()


def _resolve_access(args: argparse.Namespace, data_dir: Path, port: int) -> _ServedAccess:
    """Decide the access configuration and bind host; raise ``AccessConfigError`` to refuse.

    Precedence: the server of a packaged installation runs with its installation
    record, whatever its launcher passes (launchers that predate access modes pass
    only ``--host``), and mirrors it into the data directory. Any other server
    takes explicit access options and records them; without them it serves what
    the data directory records, and only a data directory without a record lets
    the mode follow the bind host. A record that cannot be read refuses the start.
    """
    explicit = args.access is not None or any(
        (
            args.allow_network,
            args.allow_host,
            args.public_url,
            args.trusted_proxy,
            args.tls_cert,
            args.tls_key,
        )
    )
    install = _recorded_installation(data_dir, port)
    if install is not None:
        return _installation_access(args, data_dir, install, explicit=explicit)
    recorded = read_server_access(data_dir)
    if recorded is not None and not explicit:
        warnings: tuple[str, ...] = ()
        if args.host is not None and args.host != recorded.host:
            warnings = (
                "Server binds the data directory's recorded address, not the requested one "
                f"(requested={args.host} recorded={recorded.host}); start it with --access "
                "and the access options to change it",
            )
        return _ServedAccess(recorded.access, recorded.host, None, warnings)
    if recorded is not None and args.access is None:
        raise AccessConfigError(
            f"this data directory records its server's access mode ({recorded.access.mode}); "
            "give --access together with every access option to replace it"
        )
    access = _flagged_access(args, args.host or DEFAULT_HOST)
    host = args.host or default_host(access)
    check_bind_host(access, host)
    served = RecordedServerAccess(access=access, host=host)
    return _ServedAccess(access, host, None if served == recorded else served)


def _installation_access(
    args: argparse.Namespace, data_dir: Path, install: Any, *, explicit: bool
) -> _ServedAccess:
    access: AccessConfig = install.access
    host: str = install.server_host
    if explicit and _flagged_access(args, host) != access:
        raise AccessConfigError(
            "this installation records its server's access mode "
            f"({access.mode}); change it with: vbot application access set"
        )
    warnings: list[str] = []
    if args.host is not None and args.host != host:
        warnings.append(
            "Server binds the installation's recorded address, not the requested one "
            f"(requested={args.host} recorded={host})"
        )
    try:
        mirrored = read_server_access(data_dir)
    except AccessConfigError as exc:
        # The installation record is the authority; the mirror only guards other launchers.
        warnings.append(f"Replacing the data directory's access record ({exc})")
        mirrored = None
    served = RecordedServerAccess(access=access, host=host)
    return _ServedAccess(access, host, None if served == mirrored else served, tuple(warnings))


def _flagged_access(args: argparse.Namespace, host: str) -> AccessConfig:
    return access_config(
        args.access or inferred_access_mode(args.host or host),
        allow_networks=args.allow_network,
        allow_hosts=args.allow_host,
        public_url=args.public_url,
        trusted_proxies=args.trusted_proxy,
        tls_cert=args.tls_cert,
        tls_key=args.tls_key,
    )


def _refuse_start(reason: str) -> NoReturn:
    print(f"vBot server cannot start: {reason}", file=sys.stderr)
    raise SystemExit(STARTUP_FAILED_EXIT_CODE)


def _recorded_installation(data_dir: Path, port: int) -> Any:
    """The packaged installation whose recorded server this process is, if any."""
    from cli.application.state import ApplicationError, discover

    try:
        install = discover()
    except ApplicationError as exc:
        raise AccessConfigError(
            f"the installation record, which holds the access mode, cannot be read: {exc}"
        ) from exc
    if install is None or not install.owns_server or install.server_port != port:
        return None
    recorded_data = Path(install.server_data_directory or "").expanduser().resolve()
    if recorded_data != data_dir.expanduser().resolve():
        return None
    return install


def _tls_holder(access: AccessConfig) -> Any:
    """The reloading TLS context of direct TLS; ``None`` without it."""
    if not access.direct_tls:
        return None
    from server.access.tls import TlsContextHolder

    assert access.tls_cert is not None and access.tls_key is not None
    return TlsContextHolder(access.tls_cert, access.tls_key)


def _enclosing_foreign_job() -> ForeignJob | OSError | None:
    """Another application's Windows Job this server started in, or why that is unknown."""
    try:
        return foreign_job()
    except OSError as exc:
        return exc


def _log_enclosing_job(job: ForeignJob | OSError | None) -> None:
    """Say why this server may later vanish without a stop line.

    Closing the application that owns the Job ends every process in it, this
    server included, before the server can log anything.
    """
    if isinstance(job, ForeignJob):
        _LOGGER.warning(
            "Server started inside another application's Windows Job; closing that "
            "application ends the server without a stop line (owner=%s owner_pid=%s)",
            job.name,
            job.pid,
        )
    elif job is not None:
        _LOGGER.debug("Inspecting the Windows Job of the server failed (error=%s)", job)


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
