#!/usr/bin/env python3
"""Stable JSON wrapper for isolated playwright-cli browser sessions."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

SESSION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_SNAPSHOT_CHARS = 20_000
MAX_DIAGNOSTIC_CHARS = 4_000
DEFAULT_TIMEOUT_SECONDS = 45
MAX_TIMEOUT_SECONDS = 300
SAFE_ENVIRONMENT_KEYS = {
    "APPDATA",
    "COMSPEC",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
}


class BrowserUseError(Exception):
    """Expected script or backend failure."""


class JsonArgumentParser(argparse.ArgumentParser):
    """Convert argparse failures into the script's JSON error contract."""

    def error(self, message: str) -> NoReturn:
        raise BrowserUseError(message)


def _safe_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENVIRONMENT_KEYS}


def _diagnostic(value: str) -> str:
    value = value.strip()
    if len(value) <= MAX_DIAGNOSTIC_CHARS:
        return value
    return f"{value[:MAX_DIAGNOSTIC_CHARS]}..."


def _parse_json_output(output: str) -> Any:
    stripped = output.strip()
    if not stripped:
        return {}
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, character in enumerate(stripped):
            if character not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(stripped[index:])
            except json.JSONDecodeError:
                continue
            return value
    raise BrowserUseError(f"playwright-cli returned non-JSON output: {_diagnostic(stripped)}")


class PlaywrightCli:
    """Invoke playwright-cli without a shell or inherited credentials."""

    def __init__(self, executable: str, session: str, timeout: int) -> None:
        self.executable = executable
        self.session = session
        self.timeout = timeout

    def version(self) -> str:
        completed = self._invoke(["--version"])
        return completed.stdout.strip() or completed.stderr.strip()

    def call(self, arguments: list[str]) -> Any:
        completed = self._invoke([f"-s={self.session}", "--json", *arguments])
        payload = _parse_json_output(completed.stdout)
        if isinstance(payload, dict) and (
            payload.get("isError") is True or payload.get("is_error") is True
        ):
            detail = payload.get("error") or payload.get("message") or payload
            raise BrowserUseError(f"playwright-cli reported an error: {_diagnostic(str(detail))}")
        return payload

    def _invoke(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                [self.executable, *arguments],
                capture_output=True,
                check=False,
                env=_safe_environment(),
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BrowserUseError(f"playwright-cli timed out after {self.timeout} seconds") from exc
        except OSError as exc:
            raise BrowserUseError(f"could not execute playwright-cli: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr or completed.stdout or "no diagnostic output"
            raise BrowserUseError(
                f"playwright-cli exited with {completed.returncode}: {_diagnostic(detail)}"
            )
        return completed


def _validate_session(value: str) -> str:
    if not SESSION_PATTERN.fullmatch(value):
        raise BrowserUseError(
            "session must be 1-64 lowercase letters, digits, hyphens, or underscores"
        )
    return value


def _validate_url(value: str) -> str:
    if value == "about:blank":
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BrowserUseError("URL must use http or https, or be exactly about:blank")
    if parsed.username or parsed.password:
        raise BrowserUseError("URLs containing credentials are not allowed")
    return value


def _output_directory(cwd: Path, session: str) -> Path:
    path = cwd / "tmp" / "browser-use" / session
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _artifact_path(directory: Path, kind: str, suffix: str) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    token = uuid.uuid4().hex[:8]
    return directory / f"{kind}-{timestamp}-{token}{suffix}"


def _snapshot_text(payload: Any) -> str:
    if isinstance(payload, dict):
        snapshot = payload.get("snapshot")
        if isinstance(snapshot, str):
            return snapshot
        result = payload.get("result")
        if isinstance(result, str):
            return result
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, ensure_ascii=False)


def _snapshot_result(payload: Any, directory: Path) -> dict[str, Any]:
    snapshot = _snapshot_text(payload)
    if len(snapshot) <= MAX_SNAPSHOT_CHARS:
        return {"snapshot": snapshot}
    path = _artifact_path(directory, "snapshot", ".txt")
    path.write_text(snapshot, encoding="utf-8")
    return {
        "snapshot": f"{snapshot[:MAX_SNAPSHOT_CHARS]}...",
        "snapshot_truncated": True,
        "snapshot_file": str(path),
        "artifacts": [{"type": "text", "path": str(path)}],
    }


def _with_snapshot(
    client: PlaywrightCli,
    action: str,
    session: str,
    backend: Any,
    directory: Path,
) -> dict[str, Any]:
    snapshot = client.call(["snapshot"])
    return {
        "ok": True,
        "action": action,
        "session": session,
        "backend": backend,
        **_snapshot_result(snapshot, directory),
    }


def _build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        choices=range(1, MAX_TIMEOUT_SECONDS + 1),
        metavar="SECONDS",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    subparsers.add_parser("doctor")

    start = subparsers.add_parser("start")
    start.add_argument("url", nargs="?", default="about:blank")
    start.add_argument("--browser", choices=("chrome", "firefox", "webkit", "msedge"))
    start.add_argument("--headed", action="store_true")
    start.add_argument("--persistent", action="store_true")

    navigate = subparsers.add_parser("navigate")
    navigate.add_argument("url")

    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--target")
    snapshot.add_argument("--depth", type=int, choices=range(1, 21), metavar="DEPTH")
    snapshot.add_argument("--boxes", action="store_true")

    click = subparsers.add_parser("click")
    click.add_argument("target")
    click.add_argument("--button", choices=("left", "right", "middle"))

    fill = subparsers.add_parser("fill")
    fill.add_argument("target")
    fill.add_argument("text")
    fill.add_argument("--submit", action="store_true")

    type_parser = subparsers.add_parser("type")
    type_parser.add_argument("text")

    press = subparsers.add_parser("press")
    press.add_argument("key")

    scroll = subparsers.add_parser("scroll")
    scroll.add_argument("direction", choices=("up", "down"))
    scroll.add_argument("--amount", type=int, default=600, choices=range(1, 5001))

    subparsers.add_parser("back")

    screenshot = subparsers.add_parser("screenshot")
    screenshot.add_argument("--target")
    screenshot.add_argument("--full-page", action="store_true")

    subparsers.add_parser("close")
    return parser


def _execute(
    argv: list[str],
    *,
    client: PlaywrightCli | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    args = _build_parser().parse_args(argv)
    session = _validate_session(args.session)
    directory = _output_directory(cwd or Path.cwd(), session)

    if client is None:
        executable = shutil.which("playwright-cli")
        if executable is None:
            raise BrowserUseError("playwright-cli is not installed or is not available on PATH")
        client = PlaywrightCli(executable, session, args.timeout)

    if args.action == "doctor":
        return {
            "ok": True,
            "action": "doctor",
            "session": session,
            "backend": "playwright-cli",
            "version": client.version(),
        }

    if args.action == "start":
        command = ["open", _validate_url(args.url)]
        if args.browser:
            command.extend(["--browser", args.browser])
        if args.headed:
            command.append("--headed")
        if args.persistent:
            command.append("--persistent")
        backend = client.call(command)
        return _with_snapshot(client, "start", session, backend, directory)

    if args.action == "navigate":
        backend = client.call(["goto", _validate_url(args.url)])
        return _with_snapshot(client, "navigate", session, backend, directory)

    if args.action == "snapshot":
        command = ["snapshot"]
        if args.target:
            command.append(args.target)
        if args.depth:
            command.extend(["--depth", str(args.depth)])
        if args.boxes:
            command.append("--boxes")
        return {
            "ok": True,
            "action": "snapshot",
            "session": session,
            **_snapshot_result(client.call(command), directory),
        }

    if args.action == "click":
        command = ["click", args.target]
        if args.button:
            command.extend(["--button", args.button])
        backend = client.call(command)
        return _with_snapshot(client, "click", session, backend, directory)

    if args.action == "fill":
        command = ["fill", args.target, args.text]
        if args.submit:
            command.append("--submit")
        backend = client.call(command)
        return _with_snapshot(client, "fill", session, backend, directory)

    if args.action == "type":
        backend = client.call(["type", args.text])
        return _with_snapshot(client, "type", session, backend, directory)

    if args.action == "press":
        backend = client.call(["press", args.key])
        return _with_snapshot(client, "press", session, backend, directory)

    if args.action == "scroll":
        delta = args.amount if args.direction == "down" else -args.amount
        backend = client.call(["mousewheel", "0", str(delta)])
        return _with_snapshot(client, "scroll", session, backend, directory)

    if args.action == "back":
        backend = client.call(["go-back"])
        return _with_snapshot(client, "back", session, backend, directory)

    if args.action == "screenshot":
        path = _artifact_path(directory, "screenshot", ".png")
        command = ["screenshot"]
        if args.target:
            command.append(args.target)
        command.extend(["--filename", str(path)])
        if args.full_page:
            command.append("--full-page")
        backend = client.call(command)
        if not path.is_file():
            raise BrowserUseError(
                "playwright-cli reported success but did not create the screenshot"
            )
        return {
            "ok": True,
            "action": "screenshot",
            "session": session,
            "backend": backend,
            "screenshot": str(path),
            "artifacts": [{"type": "image", "path": str(path)}],
        }

    if args.action == "close":
        return {
            "ok": True,
            "action": "close",
            "session": session,
            "backend": client.call(["close"]),
        }

    raise BrowserUseError(f"unsupported action: {args.action}")


def main(argv: list[str] | None = None) -> int:
    try:
        result = _execute(sys.argv[1:] if argv is None else argv)
    except BrowserUseError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - final JSON contract guard
        print(
            json.dumps({"ok": False, "error": f"unexpected failure: {exc}"}),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
