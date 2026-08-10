#!/usr/bin/env python3
"""Stable JSON wrapper for isolated agent-browser sessions."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import urlparse

SESSION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
REF_PATTERN = re.compile(r"^@e[1-9][0-9]*$")
MAX_SNAPSHOT_CHARS = 20_000
MAX_DIAGNOSTIC_CHARS = 4_000
DEFAULT_TIMEOUT_SECONDS = 45
MAX_TIMEOUT_SECONDS = 300
VBOT_NAMESPACE = "vbot"
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
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in SAFE_ENVIRONMENT_KEYS
    }
    environment["NO_COLOR"] = "1"
    return environment


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
    raise BrowserUseError(f"agent-browser returned non-JSON output: {_diagnostic(stripped)}")


class AgentBrowserCli:
    """Invoke agent-browser without a shell or inherited credentials."""

    def __init__(
        self,
        executable: str,
        session: str,
        timeout: int,
        config_path: Path,
    ) -> None:
        self.executable = executable
        self.session = session
        self.timeout = timeout
        self.config_path = config_path

    def version(self) -> str:
        completed = self._invoke(["--version"])
        return completed.stdout.strip() or completed.stderr.strip()

    def call(
        self,
        arguments: list[str],
        *,
        launch_options: list[str] | None = None,
    ) -> Any:
        completed = self._invoke(
            [
                "--namespace",
                VBOT_NAMESPACE,
                "--session",
                self.session,
                "--config",
                str(self.config_path),
                "--content-boundaries",
                "--no-auto-dialog",
                *(launch_options or []),
                *arguments,
                "--json",
            ]
        )
        payload = _parse_json_output(completed.stdout)
        if isinstance(payload, dict) and (
            payload.get("success") is False
            or payload.get("isError") is True
            or payload.get("is_error") is True
        ):
            detail = payload.get("error") or payload.get("message") or payload
            raise BrowserUseError(f"agent-browser reported an error: {_diagnostic(str(detail))}")
        return payload

    def _invoke(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            # agent-browser starts a long-lived daemon. On Windows that daemon can
            # inherit anonymous pipe handles and keep capture_output=True waiting
            # for EOF after the short-lived CLI process has already exited.
            # Seekable temporary files preserve stdout/stderr without coupling the
            # wrapper's completion to the daemon's lifetime.
            with (
                tempfile.TemporaryFile(
                    mode="w+", encoding="utf-8", errors="replace"
                ) as stdout_file,
                tempfile.TemporaryFile(
                    mode="w+", encoding="utf-8", errors="replace"
                ) as stderr_file,
            ):
                command = [self.executable, *arguments]
                completed = subprocess.run(
                    command,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    check=False,
                    env=_safe_environment(),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                )
                stdout_file.seek(0)
                stderr_file.seek(0)
                completed = subprocess.CompletedProcess(
                    command,
                    completed.returncode,
                    stdout=stdout_file.read(),
                    stderr=stderr_file.read(),
                )
        except subprocess.TimeoutExpired as exc:
            raise BrowserUseError(f"agent-browser timed out after {self.timeout} seconds") from exc
        except OSError as exc:
            raise BrowserUseError(f"could not execute agent-browser: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr or completed.stdout or "no diagnostic output"
            raise BrowserUseError(
                f"agent-browser exited with {completed.returncode}: {_diagnostic(detail)}"
            )
        return completed


def _validate_session(value: str) -> str:
    if not SESSION_PATTERN.fullmatch(value):
        raise BrowserUseError(
            "session must be 1-64 lowercase letters, digits, hyphens, or underscores"
        )
    return value


def _validate_ref(value: str) -> str:
    if not REF_PATTERN.fullmatch(value):
        raise BrowserUseError("target must be an agent-browser ref such as @e12")
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
    root = cwd.resolve()
    path = root / "tmp" / "browser-use" / session
    path.mkdir(parents=True, exist_ok=True)
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise BrowserUseError("browser-use output directory resolves outside the cwd")
    return resolved


def _controlled_config(directory: Path) -> Path:
    path = directory / "agent-browser.json"
    path.write_text("{}\n", encoding="utf-8")
    return path


def _artifact_path(directory: Path, kind: str, suffix: str) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    token = uuid.uuid4().hex[:8]
    return directory / f"{kind}-{timestamp}-{token}{suffix}"


def _payload_data(payload: Any) -> Any:
    if isinstance(payload, dict) and isinstance(payload.get("data"), (dict, list, str)):
        return payload["data"]
    return payload


def _snapshot_text(payload: Any) -> str:
    data = _payload_data(payload)
    if isinstance(data, dict):
        snapshot = data.get("snapshot")
        if isinstance(snapshot, str):
            return snapshot
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False)


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


def _interactive_snapshot(client: AgentBrowserCli) -> Any:
    return client.call(["snapshot", "-i"])


def _with_snapshot(
    client: AgentBrowserCli,
    action: str,
    session: str,
    backend: Any,
    directory: Path,
) -> dict[str, Any]:
    snapshot = _interactive_snapshot(client)
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
    start.add_argument("--engine", choices=("chrome", "lightpanda"))
    start.add_argument("--headed", action="store_true")

    navigate = subparsers.add_parser("navigate")
    navigate.add_argument("url")

    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--target")
    snapshot.add_argument("--depth", type=int, choices=range(1, 21), metavar="DEPTH")
    snapshot.add_argument("--full", action="store_true")
    snapshot.add_argument("--boxes", action="store_true")

    click = subparsers.add_parser("click")
    click.add_argument("target")

    fill = subparsers.add_parser("fill")
    fill.add_argument("target")
    fill.add_argument("text")
    fill.add_argument("--submit", action="store_true")

    type_parser = subparsers.add_parser("type")
    type_parser.add_argument("text")

    press = subparsers.add_parser("press")
    press.add_argument("key")

    scroll = subparsers.add_parser("scroll")
    scroll.add_argument("direction", choices=("up", "down", "left", "right"))
    scroll.add_argument("--amount", type=int, default=600, choices=range(1, 5001))

    subparsers.add_parser("back")

    screenshot = subparsers.add_parser("screenshot")
    screenshot.add_argument("--full-page", action="store_true")

    subparsers.add_parser("close")
    return parser


def _execute(
    argv: list[str],
    *,
    client: AgentBrowserCli | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    args = _build_parser().parse_args(argv)
    session = _validate_session(args.session)
    directory = _output_directory(cwd or Path.cwd(), session)

    if client is None:
        executable = shutil.which("agent-browser")
        if executable is None:
            raise BrowserUseError("agent-browser is not installed or is not available on PATH")
        client = AgentBrowserCli(
            executable,
            session,
            args.timeout,
            _controlled_config(directory),
        )

    if args.action == "doctor":
        return {
            "ok": True,
            "action": "doctor",
            "session": session,
            "backend": "agent-browser",
            "version": client.version(),
        }

    if args.action == "start":
        launch_options: list[str] = []
        if args.engine:
            launch_options.extend(["--engine", args.engine])
        if args.headed:
            launch_options.append("--headed")
        backend = client.call(["open", _validate_url(args.url)], launch_options=launch_options)
        return _with_snapshot(client, "start", session, backend, directory)

    if args.action == "navigate":
        backend = client.call(["open", _validate_url(args.url)])
        return _with_snapshot(client, "navigate", session, backend, directory)

    if args.action == "snapshot":
        command = ["snapshot"]
        if not args.full:
            command.append("-i")
        if args.target:
            command.extend(["-s", args.target])
        if args.depth:
            command.extend(["-d", str(args.depth)])
        result = {
            "ok": True,
            "action": "snapshot",
            "session": session,
            **_snapshot_result(client.call(command), directory),
        }
        if args.boxes:
            path = _artifact_path(directory, "annotated-screenshot", ".png")
            result["backend_screenshot"] = client.call(["screenshot", "--annotate", str(path)])
            if not path.is_file():
                raise BrowserUseError(
                    "agent-browser reported success but did not create the annotated screenshot"
                )
            result["screenshot"] = str(path)
            result.setdefault("artifacts", []).append({"type": "image", "path": str(path)})
        return result

    if args.action == "click":
        backend = client.call(["click", _validate_ref(args.target)])
        return _with_snapshot(client, "click", session, backend, directory)

    if args.action == "fill":
        fill_backend: Any = client.call(["fill", _validate_ref(args.target), args.text])
        if args.submit:
            fill_backend = {
                "fill": fill_backend,
                "submit": client.call(["press", "Enter"]),
            }
        return _with_snapshot(client, "fill", session, fill_backend, directory)

    if args.action == "type":
        backend = client.call(["keyboard", "type", args.text])
        return _with_snapshot(client, "type", session, backend, directory)

    if args.action == "press":
        backend = client.call(["press", args.key])
        return _with_snapshot(client, "press", session, backend, directory)

    if args.action == "scroll":
        backend = client.call(["scroll", args.direction, str(args.amount)])
        return _with_snapshot(client, "scroll", session, backend, directory)

    if args.action == "back":
        backend = client.call(["back"])
        return _with_snapshot(client, "back", session, backend, directory)

    if args.action == "screenshot":
        path = _artifact_path(directory, "screenshot", ".png")
        command = ["screenshot", str(path)]
        if args.full_page:
            command.append("--full")
        backend = client.call(command)
        if not path.is_file():
            raise BrowserUseError(
                "agent-browser reported success but did not create the screenshot"
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
    # JSON escapes keep the stdout contract portable across Windows consoles
    # whose legacy code pages cannot represent arbitrary page text.
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
