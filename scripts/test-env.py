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
import subprocess
import sys
from pathlib import Path

from cli.server_management import CommandResult, resolve_instance
from cli.server_management import start_server as start_server_command
from cli.server_management import stop_server as stop_server_command

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBUI_DIR = PROJECT_ROOT / "webui"
WEBUI_DIST = WEBUI_DIR / "dist" / "index.html"
WEBUI_NODE_MODULES = WEBUI_DIR / "node_modules"
STARTUP_TIMEOUT_SECONDS = 15


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

        server_rc = start_server(args.host, args.port, args.data_dir)
        if server_rc != 0:
            return server_rc

        print()
        print("Ready for live testing.")
        return 0

    if args.command == "stop":
        return stop_server(args.host, args.port, args.data_dir)

    return 1


if __name__ == "__main__":
    sys.exit(main())
