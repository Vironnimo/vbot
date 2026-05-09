#!/usr/bin/env python
"""Test environment manager — starts and stops a vBot server for live testing.

Usage:
    python scripts/test-env.py start [--host HOST] [--port PORT] [--data-dir DIR]
    python scripts/test-env.py stop [--host HOST] [--port PORT] [--data-dir DIR]

``start`` builds the frontend (if needed), starts the server in the background,
and waits until the health check passes. Prints the resolved URL and exits.

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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEBUI_DIR = PROJECT_ROOT / "webui"
WEBUI_DIST = WEBUI_DIR / "dist" / "index.html"
HEALTH_TIMEOUT_SECONDS = 15


def _run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command and return the result."""
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


def build_frontend() -> int:
    """Build the Svelte frontend if dist is missing. Returns 0 on success."""
    if WEBUI_DIST.exists():
        print("frontend.... already built (skipping)")
        return 0

    print("frontend.... building", end="", flush=True)
    npm = "npm"
    install_result = _run([npm, "install"], cwd=WEBUI_DIR)
    if install_result.returncode != 0:
        print(" FAILED")
        print(install_result.stderr)
        return 1

    build_result = _run([npm, "run", "build"], cwd=WEBUI_DIR)
    if build_result.returncode != 0:
        print(" FAILED")
        print(build_result.stderr)
        return 1

    print(" DONE")
    return 0


def start_server(host: str, port: int | None, data_dir: str | None) -> int:
    """Start the vBot server and wait for health check. Returns 0 on success."""
    cmd = [sys.executable, str(PROJECT_ROOT / "cli" / "main.py"), "server", "start"]
    if port is not None:
        cmd.extend(["--port", str(port)])
    if data_dir is not None:
        cmd.extend(["--data-dir", data_dir])

    result = _run(cmd)

    # Print key lines from the CLI output
    output_lines = result.stdout.strip().splitlines()
    url_line = ""
    running_line = ""
    webui_line = ""
    for line in output_lines:
        if line.startswith("url:"):
            url_line = line
        elif line.startswith("running:"):
            running_line = line
        elif line.startswith("webui:"):
            webui_line = line

    if result.returncode != 0:
        print("server..... FAILED")
        for line in output_lines:
            print(f"  {line}")
        if result.stderr.strip():
            print(f"  {result.stderr.strip()}")
        return 1

    print(f"server..... {running_line.removeprefix('running:').strip()}")
    print(f"url........ {url_line.removeprefix('url:').strip()}")
    print(f"webui...... {webui_line.removeprefix('webui:').strip()}")

    return 0


def stop_server(host: str, port: int | None, data_dir: str | None) -> int:
    """Stop the vBot server. Returns 0 on success."""
    cmd = [sys.executable, str(PROJECT_ROOT / "cli" / "main.py"), "server", "stop"]
    if port is not None:
        cmd.extend(["--port", str(port)])
    if data_dir is not None:
        cmd.extend(["--data-dir", data_dir])

    result = _run(cmd)

    output_lines = result.stdout.strip().splitlines()
    running_line = ""
    for line in output_lines:
        if line.startswith("running:"):
            running_line = line

    if result.returncode != 0:
        print("stop....... FAILED")
        for line in output_lines:
            print(f"  {line}")
        if result.stderr.strip():
            print(f"  {result.stderr.strip()}")
        return 1

    print(f"stop....... {running_line.removeprefix('running:').strip() or 'confirmed'}")
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
