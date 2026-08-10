#!/usr/bin/env python
"""Test environment manager — starts and stops a vBot server for live testing.

Usage:
    python scripts/test-env.py start [--host HOST] [--port PORT] [--data-dir DIR]
    python scripts/test-env.py stop [--host HOST] [--port PORT] [--data-dir DIR]

``start`` builds the frontend, starts the server in the background, and waits
until the health check passes. Prints the resolved URL and exits.

``stop`` stops a running server and confirms it is down.

Both subcommands delegate to the existing CLI (``cli/main.py``) for server
lifecycle management. This script adds the frontend-build step and structured
output that is easy for the Tester agent to parse.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

import psutil  # type: ignore[import-untyped]

from cli.server_management import CommandResult, ServerInstance, resolve_instance
from cli.server_management import start_server as start_server_command
from cli.server_management import stop_server as stop_server_command

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBUI_DIR = PROJECT_ROOT / "webui"
WEBUI_DIST = WEBUI_DIR / "dist" / "index.html"
WEBUI_NODE_MODULES = WEBUI_DIR / "node_modules"
STARTUP_TIMEOUT_SECONDS = 15
FAKE_PROVIDER_ID = "fake"
FAKE_PROVIDER_SERVICE = "vbot-e2e-fake-provider"
FAKE_PROVIDER_ENTRY = PROJECT_ROOT / "tests" / "e2e" / "fake-provider.js"
FAKE_PROVIDER_STARTUP_TIMEOUT_SECONDS = 5
FAKE_PROVIDER_POLL_SECONDS = 0.1
FAKE_PROVIDER_PID_FILE = "fake-provider.pid"
FAKE_PROVIDER_LOG_FILE = "fake-provider.log"


class FakeProviderInstance(NamedTuple):
    """Resolved local fake Provider process owned by one test data directory."""

    host: str
    port: int
    pid_path: Path
    log_path: Path

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


class FakeProviderProcess(NamedTuple):
    """Persisted identity that remains safe across operating-system PID reuse."""

    pid: int
    create_time: float


def _run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command and return the result."""
    # npm/vite emit UTF-8 regardless of the console code page.
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        encoding="utf-8",
        errors="replace",
    )


def _resolve_fake_provider(instance: ServerInstance) -> FakeProviderInstance | None:
    """Resolve the seeded local fake Provider, if this test instance has one."""

    settings_path = instance.data_dir / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        providers = settings["providers"]
        custom = providers["custom"]
        provider = custom[FAKE_PROVIDER_ID]
        base_url = provider["base_url"]
    except (KeyError, OSError, TypeError, json.JSONDecodeError):
        return None

    if not isinstance(provider, dict) or provider.get("adapter") != "openai_compatible":
        return None
    if provider.get("auth") != "none" or not isinstance(base_url, str):
        return None

    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None:
        return None

    processes_dir = instance.data_dir / "processes"
    return FakeProviderInstance(
        host="127.0.0.1",
        port=port,
        pid_path=processes_dir / FAKE_PROVIDER_PID_FILE,
        log_path=processes_dir / FAKE_PROVIDER_LOG_FILE,
    )


def _fake_provider_is_ready(instance: FakeProviderInstance) -> bool:
    """Return whether the configured endpoint is the project fake Provider."""

    connection = http.client.HTTPConnection(instance.host, instance.port, timeout=0.5)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    finally:
        connection.close()

    return response.status == 200 and payload.get("service") == FAKE_PROVIDER_SERVICE


def _fake_provider_port_is_bound(instance: FakeProviderInstance) -> bool:
    try:
        with socket.create_connection((instance.host, instance.port), timeout=0.2):
            return True
    except OSError:
        return False


def _read_fake_provider_process(instance: FakeProviderInstance) -> FakeProviderProcess | None:
    try:
        payload = json.loads(instance.pid_path.read_text(encoding="utf-8"))
        pid = payload["pid"]
        create_time = payload["create_time"]
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    if isinstance(create_time, bool) or not isinstance(create_time, (int, float)):
        return None
    return FakeProviderProcess(pid=pid, create_time=float(create_time))


def _owned_fake_provider_process(identity: FakeProviderProcess) -> psutil.Process | None:
    """Resolve an exact still-live fake Provider process from persisted identity."""
    try:
        process = psutil.Process(identity.pid)
        if abs(process.create_time() - identity.create_time) > 0.001:
            return None
        expected_entry = FAKE_PROVIDER_ENTRY.resolve()
        command_paths = []
        for argument in process.cmdline()[1:]:
            try:
                command_paths.append(Path(argument).resolve())
            except (OSError, ValueError):
                continue
        if expected_entry not in command_paths:
            return None
        return process
    except (psutil.Error, OSError):
        return None


def _write_fake_provider_process(instance: FakeProviderInstance, pid: int) -> bool:
    try:
        create_time = psutil.Process(pid).create_time()
        instance.pid_path.write_text(
            json.dumps({"pid": pid, "create_time": create_time}) + "\n",
            encoding="utf-8",
        )
    except (OSError, psutil.Error):
        return False
    return True


def _wait_for_fake_provider(instance: FakeProviderInstance, *, ready: bool) -> bool:
    deadline = time.monotonic() + FAKE_PROVIDER_STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _fake_provider_is_ready(instance) is ready:
            return True
        time.sleep(FAKE_PROVIDER_POLL_SECONDS)
    return False


def start_fake_provider(instance: FakeProviderInstance | None) -> bool:
    """Start the seeded fake Provider when the test instance configures it."""

    if instance is None:
        return True

    identity = _read_fake_provider_process(instance)
    owned_process = _owned_fake_provider_process(identity) if identity is not None else None
    if _fake_provider_is_ready(instance):
        if owned_process is None:
            print("provider.... FAILED")
            print(
                "  result: fake Provider is running without a verified owned process "
                f"at {instance.url}"
            )
            return False
        print("provider.... yes")
        print(f"provider-url {instance.url}")
        print(f"provider-log {instance.log_path}")
        return True

    if owned_process is not None and not _terminate_fake_provider_process(owned_process):
        print("provider.... FAILED")
        print("  result: the previous owned fake Provider process could not be stopped")
        return False
    instance.pid_path.unlink(missing_ok=True)
    if _fake_provider_port_is_bound(instance):
        print("provider.... FAILED")
        print(f"  result: port {instance.port} is occupied by another service")
        return False

    node = shutil.which("node")
    if node is None:
        print("provider.... FAILED")
        print("  result: Node.js is required for the seeded fake Provider")
        return False

    instance.pid_path.parent.mkdir(parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "VBOT_E2E_PROVIDER_HOST": instance.host,
        "VBOT_E2E_PROVIDER_PORT": str(instance.port),
    }
    with instance.log_path.open("w", encoding="utf-8") as log_file:
        if sys.platform == "win32":
            process = subprocess.Popen(
                [node, str(FAKE_PROVIDER_ENTRY)],
                cwd=PROJECT_ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS),
            )
        else:
            process = subprocess.Popen(
                [node, str(FAKE_PROVIDER_ENTRY)],
                cwd=PROJECT_ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
    if not _write_fake_provider_process(instance, process.pid):
        with suppress(psutil.Error, OSError):
            _terminate_fake_provider_process(psutil.Process(process.pid))
        print("provider.... FAILED")
        print("  result: fake Provider process identity could not be persisted")
        return False

    if not _wait_for_fake_provider(instance, ready=True):
        stop_fake_provider(instance)
        print("provider.... FAILED")
        print(f"  result: fake Provider did not become ready at {instance.url}")
        print(f"  log: {instance.log_path}")
        return False

    print("provider.... yes")
    print(f"provider-url {instance.url}")
    print(f"provider-log {instance.log_path}")
    return True


def stop_fake_provider(instance: FakeProviderInstance | None) -> bool:
    """Stop an owned fake Provider without touching unrelated local processes."""

    if instance is None:
        return True

    identity = _read_fake_provider_process(instance)
    owned_process = _owned_fake_provider_process(identity) if identity is not None else None
    ready = _fake_provider_is_ready(instance)
    if owned_process is None:
        if ready:
            print("provider-stop FAILED")
            print(
                "  result: stored process identity does not match the fake Provider "
                f"running at {instance.url}; refusing to terminate PID "
                f"{identity.pid if identity is not None else 'unknown'}"
            )
            return False
        instance.pid_path.unlink(missing_ok=True)
        print("provider-stop confirmed")
        return True

    if not _terminate_fake_provider_process(owned_process):
        print("provider-stop FAILED")
        print("  result: owned fake Provider process did not terminate")
        return False

    if not _wait_for_fake_provider(instance, ready=False):
        print("provider-stop FAILED")
        print(f"  result: fake Provider did not stop at {instance.url}")
        return False

    instance.pid_path.unlink(missing_ok=True)
    print("provider-stop confirmed")
    return True


def _terminate_fake_provider_process(process: psutil.Process) -> bool:
    """Terminate one already-validated process with a bounded kill fallback."""
    try:
        process.terminate()
        process.wait(timeout=FAKE_PROVIDER_STARTUP_TIMEOUT_SECONDS)
        return True
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=FAKE_PROVIDER_STARTUP_TIMEOUT_SECONDS)
            return True
        except (psutil.Error, OSError):
            return False
    except (psutil.Error, OSError):
        return False


def build_frontend() -> int:
    """Build the Svelte frontend for live testing. Returns 0 on success."""
    print("frontend.... building", end="", flush=True)
    npm = "npm.cmd" if sys.platform == "win32" else "npm"

    try:
        if not WEBUI_NODE_MODULES.exists():
            install_result = _run([npm, "install"], cwd=WEBUI_DIR)
            if install_result.returncode != 0:
                print(" FAILED")
                # npm writes build errors to both streams — forward everything.
                print((install_result.stdout + install_result.stderr).strip())
                return 1

        build_result = _run([npm, "run", "build"], cwd=WEBUI_DIR)
        if build_result.returncode != 0:
            print(" FAILED")
            print((build_result.stdout + build_result.stderr).strip())
            return 1
    except KeyboardInterrupt:
        print(" INTERRUPTED")
        print("  result: interrupted during frontend build")
        return 130
    except OSError as exc:
        print(" FAILED")
        print(f"  result: {exc.__class__.__name__}: {exc}")
        return 1

    print(" DONE")
    return 0


def _describe_exception(exc: BaseException) -> str:
    """Return a concise lifecycle error for automation-friendly output."""

    if isinstance(exc, KeyboardInterrupt):
        return "interrupted while waiting for local server readiness"

    message = str(exc).strip()
    if message:
        return f"{exc.__class__.__name__}: {message}"
    return exc.__class__.__name__


def _result_log_path(result: CommandResult) -> Path:
    """Return the log path for a lifecycle result."""

    return result.log_path or result.instance.log_path


def _print_failure(
    label: str,
    *,
    result_text: str,
    running_text: str,
    url: str,
    webui_text: str,
    log_path: Path,
) -> None:
    """Print a structured command failure summary."""

    print(f"{label}..... FAILED")
    print(f"  result: {result_text}")
    print(f"  running: {running_text}")
    print(f"  url: {url}")
    print(f"  webui: {webui_text}")
    print(f"  log: {log_path}")


def _running_text(result: CommandResult) -> str:
    if result.health and result.health.is_vbot:
        return "yes"
    if result.message in {"already running", "running", "started"}:
        return "yes"
    return "no"


def _webui_text(result: CommandResult) -> str:
    if result.webui is None:
        return "unknown"
    if result.webui.available:
        return "available"
    return "unavailable"


def start_server(host: str, port: int | None, data_dir: str | None) -> int:
    """Start the vBot server and wait for health check. Returns 0 on success."""
    instance = resolve_instance(host=host, port=port, data_dir=data_dir)
    print(f"target..... {instance.url}")

    try:
        result = start_server_command(instance, startup_timeout_seconds=STARTUP_TIMEOUT_SECONDS)
    except KeyboardInterrupt as exc:
        _print_failure(
            "server",
            result_text=_describe_exception(exc),
            running_text="no",
            url=instance.url,
            webui_text="unknown",
            log_path=instance.log_path,
        )
        return 130
    except Exception as exc:
        _print_failure(
            "server",
            result_text=_describe_exception(exc),
            running_text="no",
            url=instance.url,
            webui_text="unknown",
            log_path=instance.log_path,
        )
        return 1

    if not result.ok:
        _print_failure(
            "server",
            result_text=result.message,
            running_text=_running_text(result),
            url=result.instance.url,
            webui_text=_webui_text(result),
            log_path=_result_log_path(result),
        )
        return 1

    print(f"server..... {_running_text(result)}")
    print(f"url........ {result.instance.url}")
    print(f"webui...... {_webui_text(result)}")
    print(f"log........ {_result_log_path(result)}")

    return 0


def stop_server(host: str, port: int | None, data_dir: str | None) -> int:
    """Stop the vBot server. Returns 0 on success."""
    instance = resolve_instance(host=host, port=port, data_dir=data_dir)
    try:
        result = stop_server_command(instance)
    except KeyboardInterrupt as exc:
        _print_failure(
            "stop",
            result_text=_describe_exception(exc),
            running_text="unknown",
            url=instance.url,
            webui_text="unknown",
            log_path=instance.log_path,
        )
        return 130
    except Exception as exc:
        _print_failure(
            "stop",
            result_text=_describe_exception(exc),
            running_text="unknown",
            url=instance.url,
            webui_text="unknown",
            log_path=instance.log_path,
        )
        return 1

    if not result.ok:
        _print_failure(
            "stop",
            result_text=result.message,
            running_text=_running_text(result),
            url=result.instance.url,
            webui_text=_webui_text(result),
            log_path=_result_log_path(result),
        )
        return 1

    print(f"stop....... {_running_text(result) or 'confirmed'}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse test-env arguments."""
    parser = argparse.ArgumentParser(description="Manage vBot test environment")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start_parser = subparsers.add_parser("start", help="Build frontend and start server")
    start_parser.add_argument("--host", default="127.0.0.1")
    start_parser.add_argument("--port", type=int)
    start_parser.add_argument("--data-dir")

    stop_parser = subparsers.add_parser("stop", help="Stop the running server")
    stop_parser.add_argument("--host", default="127.0.0.1")
    stop_parser.add_argument("--port", type=int)
    stop_parser.add_argument("--data-dir")

    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()

    if args.command == "start":
        print("Test Environment")
        print("=================")

        frontend_rc = build_frontend()
        if frontend_rc != 0:
            return frontend_rc

        instance = resolve_instance(host=args.host, port=args.port, data_dir=args.data_dir)
        fake_provider = _resolve_fake_provider(instance)
        if not start_fake_provider(fake_provider):
            return 1

        server_rc = start_server(args.host, args.port, args.data_dir)
        if server_rc != 0:
            stop_fake_provider(fake_provider)
            return server_rc

        print()
        print("Ready for live testing.")
        return 0

    if args.command == "stop":
        server_rc = stop_server(args.host, args.port, args.data_dir)
        instance = resolve_instance(host=args.host, port=args.port, data_dir=args.data_dir)
        provider_ok = stop_fake_provider(_resolve_fake_provider(instance))
        return server_rc if server_rc != 0 else int(not provider_ok)

    return 1


if __name__ == "__main__":
    sys.exit(main())
