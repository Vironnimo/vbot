"""Tests for server startup argument and port handling."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import uvicorn.server

from core.access.config import access_config
from core.access.local_token import local_token_path, read_local_access_token
from core.database import DataStoreMarker, read_marker
from core.utils.log_viewer import LOG_LINE_PATTERN
from core.utils.logging import resolve_daily_log_path
from core.utils.processes import ForeignJob
from core.utils.server_control import (
    SHUTDOWN_FAILED_EXIT_CODE,
    STARTUP_FAILED_EXIT_CODE,
    create_server_control,
)
from core.utils.version import BuildIdentity
from server import main as server_main
from server.main import main, parse_args

_READY_RUNTIME = SimpleNamespace(
    build=BuildIdentity("1.2.3", revision="a" * 40, branch="main"),
    startup_summary=SimpleNamespace(describe=lambda: "tools=3"),
)


def _fake_create_app(
    *,
    config,
    server_bind,
    shutdown_token,
    request_shutdown,
    request_restart,
    on_ready,
    on_stopped,
    **options,
) -> dict[str, Any]:
    return {
        "config": config,
        "server_bind": server_bind,
        "shutdown_token": shutdown_token,
        "request_shutdown": request_shutdown,
        "request_restart": request_restart,
        "on_ready": on_ready,
        "on_stopped": on_stopped,
        **options,
    }


def _patch_serving(
    monkeypatch: pytest.MonkeyPatch, run: Callable[[Any], None]
) -> list[dict[str, Any]]:
    """Replace uvicorn with a server whose ``run`` plays *run*; return its configs."""
    calls: list[dict[str, Any]] = []

    class FakeConfig:
        def __init__(self, app, **kwargs) -> None:
            self.app = app
            calls.append({"app": app, **kwargs})

    class FakeServer:
        def __init__(self, config: FakeConfig) -> None:
            self.config = config
            self.should_exit = False
            self.started = False
            self.bind_failure = False

        async def startup(self, sockets=None) -> None:
            self.config.app["on_ready"](_READY_RUNTIME)
            log_path = resolve_daily_log_path(self.config.app["config"].data_dir)
            if log_path.exists():
                assert " - Server started (" not in log_path.read_text(encoding="utf-8")
            if self.bind_failure:
                self.config.app["on_stopped"](True)
                raise SystemExit(uvicorn.server.STARTUP_FAILURE)
            self.started = True

        def handle_exit(self, sig: int, frame: object) -> None:
            self.should_exit = True

        def run(self) -> None:
            run(self)

    monkeypatch.setattr(
        server_main, "uvicorn", SimpleNamespace(Config=FakeConfig, Server=FakeServer)
    )
    monkeypatch.setattr(server_main, "activate_process_containment", lambda: None)
    # The test process may run inside a coding agent's or CI runner's Windows Job.
    monkeypatch.setattr(server_main, "foreign_job", lambda: None)
    monkeypatch.setattr(server_main, "create_app", _fake_create_app)
    # The real hook would freeze the test process heap for the rest of the session.
    monkeypatch.setattr(server_main.gc, "collect", lambda: None)
    monkeypatch.setattr(server_main.gc, "freeze", lambda: None)
    monkeypatch.setattr(server_main.sys, "setswitchinterval", lambda _interval: None)
    # The real fault handler would take over the test process's own.
    monkeypatch.setattr(server_main, "faulthandler", SimpleNamespace(enable=_close_crash_log))
    return calls


def _close_crash_log(*, file: Any, **_options: Any) -> None:
    file.close()


def test_parse_args_accepts_data_dir_port_and_one_safe_startup_mode() -> None:
    args = parse_args(["--data-dir", "dev-data", "--port", "9000"])

    assert (args.data_dir, args.port) == ("dev-data", 9000)
    assert parse_args(["--verification-only"]).verification_only is True
    assert parse_args(["--test-instance"]).test_instance is True
    with pytest.raises(SystemExit):
        parse_args(["--verification-only", "--test-instance"])


@pytest.mark.parametrize("action", ["shutdown", "restart", "restart_failure"])
def test_main_starts_uvicorn_with_configured_app(tmp_path: Path, monkeypatch, action: str) -> None:
    from cli import server_management

    schedule = MagicMock(return_value=SimpleNamespace(ok=action != "restart_failure"))
    loop = MagicMock()
    monkeypatch.setattr(server_management, "schedule_server_restart", schedule)
    monkeypatch.setattr(server_main.asyncio, "get_running_loop", lambda: loop)

    served_tokens: list[str | None] = []

    def run(server: Any) -> None:
        app = server.config.app
        served_tokens.append(read_local_access_token(tmp_path / "data", 8765))
        asyncio.run(server.startup())
        if action == "restart_failure":
            with pytest.raises(RuntimeError):
                app["request_restart"]()
            assert not server.should_exit
            loop.call_later.assert_not_called()
        elif action == "restart":
            app["request_restart"]()
            app["request_restart"]()
            schedule.assert_called_once()
            instance = schedule.call_args.args[0]
            assert instance.port == 8765 and instance.host == "127.0.0.1"
            assert instance.data_dir == tmp_path / "data"
            assert schedule.call_args.kwargs == {"wait_pid": server_main.os.getpid()}
            assert not server.should_exit
            assert not app["shutdown_event"].is_set()
            delay, callback = loop.call_later.call_args.args
            assert delay > 0
            callback()
            assert server.should_exit
            assert app["shutdown_event"].is_set()
        app["request_shutdown"]()
        assert server.should_exit is True
        assert app["shutdown_event"].is_set()

    calls = _patch_serving(monkeypatch, run)
    heap: list[str] = []
    monkeypatch.setattr(server_main.gc, "collect", lambda: heap.append("collect"))
    monkeypatch.setattr(server_main.gc, "freeze", lambda: heap.append("freeze"))
    intervals: list[float] = []
    monkeypatch.setattr(server_main.sys, "setswitchinterval", intervals.append)

    main(["--data-dir", str(tmp_path / "data"), "--port", "8765"])

    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 8765
    # Only the access guard reads forwarding headers, and only from trusted proxies.
    assert calls[0]["proxy_headers"] is False
    assert "http" not in calls[0]
    assert calls[0]["app"]["access"] == access_config("local")
    # The local access token exists exactly while this server runs.
    assert served_tokens == [calls[0]["app"]["local_access_token"]]
    assert not local_token_path(tmp_path / "data", 8765).exists()
    assert calls[0]["ws_per_message_deflate"] is False
    assert calls[0]["timeout_graceful_shutdown"] == 5
    assert calls[0]["log_level"] == "info"
    assert calls[0]["access_log"] is False
    assert calls[0]["log_config"]["handlers"]["vbot_proxy"] == {
        "class": "core.utils.logging.ManagedLoggerProxyHandler",
        "target_logger_name": "vbot.server.uvicorn",
        "filters": ["quiet_logs_websocket_lifecycle"],
    }
    assert calls[0]["log_config"]["filters"]["quiet_logs_websocket_lifecycle"] == {
        "()": "core.utils.logging.QuietLogsWebSocketLifecycleFilter",
    }
    assert calls[0]["log_config"]["loggers"]["uvicorn.access"] == {
        "handlers": ["null"],
        "level": "INFO",
        "propagate": False,
    }
    assert calls[0]["app"]["server_bind"] == {
        "listen_host": "127.0.0.1",
        "listen_port": 8765,
        "port_source": "cli",
    }
    assert calls[0]["app"]["shutdown_token"]
    # Once ready, the server freezes its collected startup heap and lets the
    # Event Loop take the GIL back from computing threads below Windows' 1 ms wait.
    assert heap == ["collect", "freeze"]
    assert intervals and intervals[0] < 0.001
    assert not (tmp_path / "data" / "runtime" / "server-8765.json").exists()
    # A restart the server scheduled for itself is its stop reason.
    log = resolve_daily_log_path(tmp_path / "data").read_text(encoding="utf-8")
    assert ("Server stopped (reason=restart " in log) is (action == "restart")


@pytest.mark.parametrize("existing", [False, True])
def test_main_initializes_only_a_missing_data_directory(
    tmp_path: Path, monkeypatch, existing: bool
) -> None:
    data_dir = tmp_path / "data"
    if existing:
        data_dir.mkdir()
    markers: list[DataStoreMarker | None] = []

    def run(server: Any) -> None:
        markers.append(read_marker(data_dir))
        _serve_until("control")(server)

    _patch_serving(monkeypatch, run)

    main(["--data-dir", str(data_dir), "--port", "8765"])

    assert len(markers) == 1
    if existing:
        # An existing root is never authorized here; Runtime startup decides.
        assert markers[0] is None
        assert not (data_dir / "agents").exists()
    else:
        assert markers[0] is not None
        assert markers[0].databases == {}


def _serve_until(cause: str) -> Callable[[Any], None]:
    """Play one way a served process ends, as uvicorn and the app lifespan would."""

    def run(server: Any) -> None:
        app = server.config.app
        if cause == "startup_failed":
            # Current uvicorn exits itself when the application fails to start.
            raise SystemExit(uvicorn.server.STARTUP_FAILURE)
        if cause == "startup_failed_returning":
            # Older uvicorn versions return from run() instead.
            return
        server.bind_failure = cause == "startup_failed_bind"
        asyncio.run(server.startup())
        if cause == "signal":
            server.handle_exit(signal.SIGINT, None)
        else:
            app["request_shutdown"]("tray_quit")
        assert server.should_exit
        assert app["shutdown_event"].is_set()
        app["on_stopped"](cause != "shutdown_failed")

    return run


@pytest.mark.parametrize(
    ("cause", "stop_fields"),
    [
        ("control", "[INFO] vbot.server - Server stopped (reason=control initiator=tray_quit "),
        ("signal", "[INFO] vbot.server - Server stopped (reason=signal signal=SIGINT "),
        ("startup_failed", "[WARN] vbot.server - Server stopped (reason=startup_failed "),
        ("startup_failed_bind", "[WARN] vbot.server - Server stopped (reason=startup_failed "),
        (
            "startup_failed_returning",
            "[WARN] vbot.server - Server stopped (reason=startup_failed ",
        ),
        (
            "shutdown_failed",
            "[WARN] vbot.server - Server stopped (reason=control initiator=tray_quit "
            "shutdown=failed ",
        ),
    ],
)
def test_main_logs_one_start_line_and_one_stop_line_with_its_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str, stop_fields: str
) -> None:
    clock = [100.0]

    def create_app(**kwargs: Any) -> dict[str, Any]:
        clock[0] += 2.0
        return _fake_create_app(**kwargs)

    def serve(server: Any) -> None:
        clock[0] += 3.0
        _serve_until(cause)(server)

    def collect() -> None:
        clock[0] += 1.0

    _patch_serving(monkeypatch, serve)
    # Started inside another application's Job, the server says so: closing that
    # application ends it without the stop line below.
    job = ForeignJob("codex.exe", 4711, confines_children=True) if cause == "signal" else None
    monkeypatch.setattr(server_main, "foreign_job", lambda: job)
    monkeypatch.setattr(server_main, "create_app", create_app)
    monkeypatch.setattr(server_main, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(server_main.gc, "collect", collect)
    monkeypatch.setattr(
        server_main,
        "time",
        SimpleNamespace(time=lambda: 1_000.0, strftime=server_main.time.strftime),
    )
    monkeypatch.setattr(
        server_main,
        "psutil",
        SimpleNamespace(Process=lambda: SimpleNamespace(create_time=lambda: 995.0)),
    )
    data_dir = tmp_path / "data"
    # Supervisors such as the generated systemd unit do not restart for these statuses.
    exit_code = {
        "startup_failed": STARTUP_FAILED_EXIT_CODE,
        "startup_failed_bind": STARTUP_FAILED_EXIT_CODE,
        "startup_failed_returning": STARTUP_FAILED_EXIT_CODE,
        "shutdown_failed": SHUTDOWN_FAILED_EXIT_CODE,
    }.get(cause)

    if exit_code is not None:
        with pytest.raises(SystemExit) as exited:
            main(["--data-dir", str(data_dir), "--port", "8765", "--verification-only"])
        # A failed startup or Runtime shutdown, already logged, still fails the process.
        assert exited.value.code == exit_code
        assert not (data_dir / "runtime" / "server-8765.json").exists()
    else:
        main(["--data-dir", str(data_dir), "--port", "8765", "--verification-only"])

    lines = resolve_daily_log_path(data_dir).read_text(encoding="utf-8").splitlines()
    started = [line for line in lines if " - Server started (" in line]
    stopped = [line for line in lines if " - Server stopped (" in line]
    if cause.startswith("startup_failed"):
        assert started == []
    else:
        # The start line names the build, the mode and the Runtime's startup summary.
        assert len(started) == 1
        assert "(version=1.2.3 revision=aaaaaaaa branch=main " in started[0]
        assert " mode=verification access=local pid=" in started[0]
        assert started[0].endswith(" tools=3)")
        # Readiness includes the heap preparation; the phases account for the
        # whole startup, distinguishing interpreter/import cost from app work.
        assert " startup=11.0s pre_main=5.000s setup=2.000s app=4.000s " in started[0]
    assert len(stopped) == 1
    assert stop_fields in stopped[0]
    assert " uptime=" in stopped[0]
    enclosed = [line for line in lines if "owner=codex.exe owner_pid=4711" in line]
    assert len(enclosed) == (1 if job else 0)
    assert all("[WARN] vbot.server - " in line for line in enclosed)


def test_main_names_a_previous_server_process_that_ended_without_shutting_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_serving(monkeypatch, _serve_until("control"))
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # A killed server leaves its control record; its pid now names another process.
    create_server_control(data_dir, 8765, pid=os.getppid(), process_create_time=1.0, token="old")

    main(["--data-dir", str(data_dir), "--port", "8765"])

    lines = resolve_daily_log_path(data_dir).read_text(encoding="utf-8").splitlines()
    [line] = [line for line in lines if f"pid={os.getppid()})" in line]
    assert "[WARN] vbot.server - " in line


@pytest.mark.parametrize("oversized", [False, True])
def test_main_records_fatal_errors_in_a_crash_log_kept_across_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oversized: bool
) -> None:
    _patch_serving(monkeypatch, _serve_until("control"))
    enabled: list[dict[str, Any]] = []

    def enable(*, file: Any, **options: Any) -> None:
        enabled.append({"path": Path(file.name).resolve(), **options})
        _close_crash_log(file=file)

    monkeypatch.setattr(server_main, "faulthandler", SimpleNamespace(enable=enable))
    monkeypatch.setattr(server_main, "_CRASH_LOG_ROTATE_BYTES", 64)
    crash_log = tmp_path / "data" / "logs" / server_main.CRASH_LOG_NAME
    crash_log.parent.mkdir(parents=True)
    previous = b"Fatal Python error: Segmentation fault\n" * (2 if oversized else 1)
    crash_log.write_bytes(previous)

    main(["--data-dir", str(tmp_path / "data"), "--port", "8765"])

    # Every thread's Python stack, plus the C stack where the platform provides one.
    assert enabled == [{"path": crash_log.resolve(), "all_threads": True, "c_stack": True}]
    if oversized:
        # A start keeps one previous generation of an oversized crash log.
        assert crash_log.with_name(f"{crash_log.name}.1").read_bytes() == previous
        started = crash_log.read_text(encoding="utf-8")
    else:
        # A start after a crash keeps what the crashed process wrote.
        content = crash_log.read_bytes()
        assert content.startswith(previous)
        started = content.removeprefix(previous).decode("utf-8")
    # One line per start in the log format names the process whose dump follows it.
    header = LOG_LINE_PATTERN.match(started.removesuffix("\n"))
    assert header is not None and started.count("\n") == 1
    assert f"pid={os.getpid()}" in header["message"]


def test_main_starts_without_a_crash_log_it_cannot_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_serving(monkeypatch, _serve_until("control"))
    enabled: list[dict[str, Any]] = []
    monkeypatch.setattr(
        server_main,
        "faulthandler",
        SimpleNamespace(enable=lambda **options: enabled.append(options)),
    )
    data_dir = tmp_path / "data"
    (data_dir / "logs" / server_main.CRASH_LOG_NAME).mkdir(parents=True)

    main(["--data-dir", str(data_dir), "--port", "8765"])

    assert enabled == []
    lines = resolve_daily_log_path(data_dir).read_text(encoding="utf-8").splitlines()
    assert any(
        "[WARN] vbot.server - " in line and server_main.CRASH_LOG_NAME in line for line in lines
    )
    assert any(" - Server started (" in line for line in lines)


@pytest.mark.parametrize(
    ("argv", "host", "mode"),
    [
        pytest.param([], "127.0.0.1", "local", id="default"),
        # Launchers that predate access modes pass only a host.
        pytest.param(["--host", "0.0.0.0"], "0.0.0.0", "lan", id="network-host"),
        pytest.param(["--access", "lan"], "0.0.0.0", "lan", id="lan"),
        pytest.param(
            [
                "--access",
                "authenticated",
                "--public-url",
                "https://vbot.example.org",
                "--trusted-proxy",
                "127.0.0.1",
            ],
            "127.0.0.1",
            "authenticated",
            id="behind-proxy",
        ),
    ],
)
def test_main_serves_the_access_mode_its_flags_or_bind_host_select(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], host: str, mode: str
) -> None:
    calls = _patch_serving(monkeypatch, _serve_until("control"))

    main(["--data-dir", str(tmp_path / "data"), "--port", "8765", *argv])

    assert calls[0]["host"] == host
    assert calls[0]["app"]["server_bind"]["listen_host"] == host
    assert calls[0]["app"]["access"].mode == mode


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        pytest.param(
            ["--access", "local", "--host", "0.0.0.0"],
            "must bind a loopback address",
            id="local-on-the-network",
        ),
        pytest.param(
            ["--access", "authenticated", "--trusted-proxy", "127.0.0.1"],
            "needs --public-url",
            id="no-public-url",
        ),
        pytest.param(
            ["--access", "authenticated", "--public-url", "http://vbot.example.org"],
            "must start with https://",
            id="plain-public-url",
        ),
        pytest.param(
            ["--access", "authenticated", "--public-url", "https://vbot.example.org"],
            "needs HTTPS",
            id="no-tls-and-no-proxy",
        ),
        pytest.param(
            ["--access", "lan", "--public-url", "https://vbot.example.org"],
            "applies only to --access authenticated",
            id="login-option-without-login",
        ),
        pytest.param(
            ["--access", "lan", "--allow-network", "0.0.0.0/0"],
            "every address",
            id="whole-internet-without-login",
        ),
        pytest.param(
            [
                "--access",
                "authenticated",
                "--public-url",
                "https://vbot.example.org",
                "--tls-cert",
                "missing-cert.pem",
                "--tls-key",
                "missing-key.pem",
            ],
            "does not exist",
            id="missing-certificate",
        ),
    ],
)
def test_main_refuses_to_start_with_an_unsafe_or_incomplete_access_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    message: str,
) -> None:
    calls = _patch_serving(monkeypatch, _serve_until("control"))
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"

    with pytest.raises(SystemExit) as exited:
        main(["--data-dir", str(data_dir), "--port", "8765", *argv])

    # Supervisors do not restart a server that cannot start this way.
    assert exited.value.code == STARTUP_FAILED_EXIT_CODE
    err = capsys.readouterr().err
    assert err.startswith("vBot server cannot start: ") and message in err
    assert calls == [] and not data_dir.exists()


def test_a_packaged_server_serves_the_access_mode_its_installation_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.application import state as application_state

    recorded = access_config("lan", allow_networks=["100.64.0.0/10"])
    install = SimpleNamespace(
        owns_server=True, server_port=8765, server_host="0.0.0.0", access=recorded
    )
    monkeypatch.setattr(application_state, "discover", lambda: install)
    calls = _patch_serving(monkeypatch, _serve_until("control"))

    def serve(name: str, *argv: str, port: int = 8765) -> Path:
        data_dir = tmp_path / name
        install.server_data_directory = str(data_dir)
        main(["--data-dir", str(data_dir), "--port", str(port), *argv])
        return data_dir

    # Its launcher may predate access modes and pass the host it knew.
    old_launcher = serve("old-launcher", "--host", "127.0.0.1")
    # Flags that repeat the record are fine; contradicting ones are refused.
    serve("same-flags", "--access", "lan", "--allow-network", "100.64.0.0/10")
    with pytest.raises(SystemExit) as refused:
        serve("other-flags", "--access", "local")
    # Another port of the recorded data directory is not the recorded server.
    serve("other-port", port=9000)

    assert [(call["host"], call["app"]["access"]) for call in calls] == [
        ("0.0.0.0", recorded),
        ("0.0.0.0", recorded),
        ("127.0.0.1", access_config("local")),
    ]
    assert refused.value.code == STARTUP_FAILED_EXIT_CODE
    assert "vbot application access set" in capsys.readouterr().err
    log = resolve_daily_log_path(old_launcher).read_text(encoding="utf-8")
    assert "[WARN] vbot.server - Server binds the installation's recorded address" in log


def test_a_packaged_server_with_an_unreadable_installation_record_does_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli.application import state as application_state

    def discover() -> None:
        raise application_state.ApplicationError("Installation record is damaged")

    monkeypatch.setattr(application_state, "discover", discover)
    calls = _patch_serving(monkeypatch, _serve_until("control"))

    with pytest.raises(SystemExit) as exited:
        main(["--data-dir", str(tmp_path / "data"), "--port", "8765"])

    # Without the record the server cannot know which access mode it must enforce.
    assert exited.value.code == STARTUP_FAILED_EXIT_CODE and calls == []
    assert "holds the access mode, cannot be read" in capsys.readouterr().err
