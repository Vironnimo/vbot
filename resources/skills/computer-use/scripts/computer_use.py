#!/usr/bin/env python3
"""Stable JSON wrapper for window-scoped cua-driver desktop sessions."""

from __future__ import annotations

import argparse
import base64
import binascii
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

SESSION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_SCREENSHOT_BYTES = 32 * 1024 * 1024
MAX_TREE_CHARS = 20_000
MAX_ELEMENTS = 200
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
BLOCKED_SHORTCUTS = {
    frozenset(("alt", "f4")),
    frozenset(("cmd", "q")),
    frozenset(("command", "q")),
    frozenset(("cmd", "option", "escape")),
    frozenset(("command", "option", "escape")),
    frozenset(("ctrl", "alt", "delete")),
    frozenset(("control", "alt", "delete")),
    frozenset(("win", "l")),
    frozenset(("windows", "l")),
    frozenset(("super", "l")),
    frozenset(("cmd", "ctrl", "q")),
    frozenset(("command", "control", "q")),
}


class ComputerUseError(Exception):
    """Expected script or backend failure."""


class JsonArgumentParser(argparse.ArgumentParser):
    """Convert argparse failures into the script's JSON error contract."""

    def error(self, message: str) -> NoReturn:
        raise ComputerUseError(message)


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
    raise ComputerUseError(f"cua-driver returned non-JSON output: {_diagnostic(stripped)}")


class CuaDriverCli:
    """Invoke cua-driver's one-shot CLI without a shell or credentials."""

    def __init__(self, executable: str, timeout: int) -> None:
        self.executable = executable
        self.timeout = timeout

    def version(self) -> str:
        completed = self._invoke(["--version"])
        return completed.stdout.strip() or completed.stderr.strip()

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        payload = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
        completed = self._invoke(["call", tool, payload])
        result = _parse_json_output(completed.stdout)
        if isinstance(result, dict) and (
            result.get("isError") is True or result.get("is_error") is True
        ):
            detail = result.get("error") or result.get("message") or result
            raise ComputerUseError(f"cua-driver reported an error: {_diagnostic(str(detail))}")
        return result

    def _invoke(self, arguments: list[str]) -> subprocess.CompletedProcess[str]:
        environment = _safe_environment()
        environment["CUA_DRIVER_RS_TELEMETRY_ENABLED"] = "0"
        try:
            completed = subprocess.run(
                [self.executable, *arguments],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ComputerUseError(f"cua-driver timed out after {self.timeout} seconds") from exc
        except OSError as exc:
            raise ComputerUseError(f"could not execute cua-driver: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr or completed.stdout or "no diagnostic output"
            raise ComputerUseError(
                f"cua-driver exited with {completed.returncode}: {_diagnostic(detail)}"
            )
        return completed


def _validate_session(value: str) -> str:
    if not SESSION_PATTERN.fullmatch(value):
        raise ComputerUseError(
            "session must be 1-64 lowercase letters, digits, hyphens, or underscores"
        )
    return value


def _output_directory(cwd: Path, session: str) -> Path:
    path = cwd / "tmp" / "computer-use" / session
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _artifact_path(directory: Path, kind: str, suffix: str) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    token = uuid.uuid4().hex[:8]
    return directory / f"{kind}-{timestamp}-{token}{suffix}"


def _unwrap_payload(value: Any) -> Any:
    current = value
    for _ in range(4):
        if not isinstance(current, dict):
            break
        nested = None
        for key in ("structuredContent", "data"):
            candidate = current.get(key)
            if isinstance(candidate, (dict, list)):
                nested = candidate
                break
        if nested is None:
            result = current.get("result")
            if isinstance(result, (dict, list)):
                nested = result
        if nested is None:
            break
        current = nested
    return current


def _collection(payload: Any, key: str) -> list[Any]:
    value = _unwrap_payload(payload)
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, list):
            return candidate
    return []


def _target_arguments(args: argparse.Namespace) -> dict[str, int]:
    if args.pid <= 0 or args.window_id <= 0:
        raise ComputerUseError("pid and window-id must both be positive integers")
    return {"pid": args.pid, "window_id": args.window_id}


def _delivery_arguments(args: argparse.Namespace) -> dict[str, str]:
    if args.foreground:
        return {"delivery_mode": "foreground"}
    return {}


def _dry_run(
    action: str,
    session: str,
    target: dict[str, int],
    details: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ok": True,
        "action": action,
        "session": session,
        "applied": False,
        "dry_run": {"target": target, **details},
    }


def _applied(action: str, session: str) -> dict[str, Any]:
    return {"ok": True, "action": action, "session": session, "applied": True}


def _write_screenshot(payload: dict[str, Any], path: Path) -> bool:
    encoded = payload.pop("screenshot_png_b64", None)
    if isinstance(encoded, str):
        try:
            screenshot = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ComputerUseError("cua-driver returned invalid screenshot data") from exc
        if len(screenshot) > MAX_SCREENSHOT_BYTES:
            raise ComputerUseError("cua-driver screenshot exceeds the 32 MiB safety limit")
        path.write_bytes(screenshot)
    return path.is_file()


def _capture_result(
    raw_payload: Any,
    mode: str,
    directory: Path,
    screenshot_path: Path,
) -> dict[str, Any]:
    unwrapped = _unwrap_payload(raw_payload)
    if not isinstance(unwrapped, dict):
        raise ComputerUseError("cua-driver returned an unexpected capture response")
    payload = dict(unwrapped)
    has_screenshot = _write_screenshot(payload, screenshot_path)
    payload.pop("screenshot_file_path", None)

    result: dict[str, Any] = {}
    artifacts: list[dict[str, str]] = []
    if mode in {"som", "vision"}:
        if not has_screenshot:
            raise ComputerUseError("cua-driver reported success but did not create a screenshot")
        result["screenshot"] = str(screenshot_path)
        artifacts.append({"type": "image", "path": str(screenshot_path)})

    if mode in {"som", "ax"}:
        tree = payload.get("tree_markdown")
        if isinstance(tree, str):
            if len(tree) > MAX_TREE_CHARS:
                tree_path = _artifact_path(directory, "accessibility-tree", ".txt")
                tree_path.write_text(tree, encoding="utf-8")
                result["tree"] = f"{tree[:MAX_TREE_CHARS]}..."
                result["tree_truncated"] = True
                result["tree_file"] = str(tree_path)
                artifacts.append({"type": "text", "path": str(tree_path)})
            else:
                result["tree"] = tree
        elements = payload.get("elements")
        if isinstance(elements, list):
            if len(elements) > MAX_ELEMENTS:
                elements_path = _artifact_path(directory, "elements", ".json")
                elements_path.write_text(
                    json.dumps(elements, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                result["elements"] = elements[:MAX_ELEMENTS]
                result["elements_truncated"] = True
                result["elements_file"] = str(elements_path)
                artifacts.append({"type": "json", "path": str(elements_path)})
            else:
                result["elements"] = elements

    metadata = {
        key: value
        for key, value in payload.items()
        if key not in {"tree_markdown", "elements", "screenshot_png_b64"}
    }
    if metadata:
        result["backend"] = metadata
    if artifacts:
        result["artifacts"] = artifacts
    return result


def _add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--window-id", type=int, required=True)


def _add_mutation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--foreground", action="store_true")


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
    subparsers.add_parser("start")
    subparsers.add_parser("apps")
    subparsers.add_parser("windows")

    capture = subparsers.add_parser("capture")
    _add_target(capture)
    capture.add_argument("--mode", choices=("som", "vision", "ax"), default="som")

    click = subparsers.add_parser("click")
    _add_target(click)
    click.add_argument("--element", type=int)
    click.add_argument("--x", type=int)
    click.add_argument("--y", type=int)
    click.add_argument("--button", choices=("left", "right", "middle"), default="left")
    click.add_argument("--count", type=int, choices=(1, 2), default=1)
    _add_mutation_options(click)

    type_parser = subparsers.add_parser("type")
    _add_target(type_parser)
    type_parser.add_argument("text")
    _add_mutation_options(type_parser)

    key = subparsers.add_parser("key")
    _add_target(key)
    key.add_argument("shortcut")
    _add_mutation_options(key)

    scroll = subparsers.add_parser("scroll")
    _add_target(scroll)
    scroll.add_argument("direction", choices=("up", "down", "left", "right"))
    scroll.add_argument("--amount", type=int, default=3, choices=range(1, 101))
    scroll.add_argument("--element", type=int)
    _add_mutation_options(scroll)

    subparsers.add_parser("close")
    return parser


def _execute(
    argv: list[str],
    *,
    client: CuaDriverCli | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    args = _build_parser().parse_args(argv)
    session = _validate_session(args.session)
    directory = _output_directory(cwd or Path.cwd(), session)

    if client is None:
        executable = shutil.which("cua-driver")
        if executable is None:
            raise ComputerUseError("cua-driver is not installed or is not available on PATH")
        client = CuaDriverCli(executable, args.timeout)

    if args.action == "doctor":
        return {
            "ok": True,
            "action": "doctor",
            "session": session,
            "backend": "cua-driver",
            "version": client.version(),
        }

    if args.action == "start":
        return {
            "ok": True,
            "action": "start",
            "session": session,
            "backend": client.call("start_session", {"session": session}),
        }

    if args.action == "apps":
        raw = client.call("list_apps", {"session": session})
        return {
            "ok": True,
            "action": "apps",
            "session": session,
            "apps": _collection(raw, "apps"),
        }

    if args.action == "windows":
        raw = client.call("list_windows", {"session": session, "on_screen_only": True})
        return {
            "ok": True,
            "action": "windows",
            "session": session,
            "windows": _collection(raw, "windows"),
        }

    if args.action == "capture":
        target = _target_arguments(args)
        screenshot_path = _artifact_path(directory, "capture", ".png")
        raw = client.call(
            "get_window_state",
            {
                "session": session,
                **target,
                "screenshot_out_file": str(screenshot_path),
            },
        )
        return {
            "ok": True,
            "action": "capture",
            "session": session,
            "target": target,
            "mode": args.mode,
            **_capture_result(raw, args.mode, directory, screenshot_path),
        }

    target = _target_arguments(args) if args.action in {"click", "type", "key", "scroll"} else {}

    if args.action == "click":
        has_element = args.element is not None
        has_coordinates = args.x is not None or args.y is not None
        if has_element == has_coordinates:
            raise ComputerUseError("click requires either --element or both --x and --y")
        if has_element and args.element < 0:
            raise ComputerUseError("element must be zero or greater")
        if has_coordinates and (args.x is None or args.y is None):
            raise ComputerUseError("coordinate clicks require both --x and --y")
        click_target: dict[str, Any]
        if has_element:
            click_target = {"element_index": args.element}
        else:
            click_target = {"x": args.x, "y": args.y}
        details = {
            **click_target,
            "button": args.button,
            "count": args.count,
            "delivery_mode": "foreground" if args.foreground else "background",
        }
        if not args.apply:
            return _dry_run("click", session, target, details)
        tool = "double_click" if args.count == 2 else "click"
        payload = {
            "session": session,
            **target,
            **click_target,
            "button": args.button,
            **_delivery_arguments(args),
        }
        client.call(tool, payload)
        return _applied("click", session)

    if args.action == "type":
        details = {
            "character_count": len(args.text),
            "delivery_mode": "foreground" if args.foreground else "background",
        }
        if not args.apply:
            return _dry_run("type", session, target, details)
        client.call(
            "type_text",
            {
                "session": session,
                **target,
                "text": args.text,
                **_delivery_arguments(args),
            },
        )
        return _applied("type", session)

    if args.action == "key":
        keys = [key.strip().lower() for key in args.shortcut.split("+") if key.strip()]
        if not keys:
            raise ComputerUseError("shortcut must contain at least one key")
        if frozenset(keys) in BLOCKED_SHORTCUTS:
            raise ComputerUseError("that lock, quit, or system shortcut is blocked")
        details = {
            "keys": keys,
            "delivery_mode": "foreground" if args.foreground else "background",
        }
        if not args.apply:
            return _dry_run("key", session, target, details)
        if len(keys) == 1:
            tool = "press_key"
            key_arguments: dict[str, Any] = {"key": keys[0]}
        else:
            tool = "hotkey"
            key_arguments = {"keys": keys}
        client.call(
            tool,
            {
                "session": session,
                **target,
                **key_arguments,
                **_delivery_arguments(args),
            },
        )
        return _applied("key", session)

    if args.action == "scroll":
        if args.element is not None and args.element < 0:
            raise ComputerUseError("element must be zero or greater")
        scroll_details: dict[str, Any] = {
            "direction": args.direction,
            "amount": args.amount,
            "delivery_mode": "foreground" if args.foreground else "background",
        }
        if args.element is not None:
            scroll_details["element_index"] = args.element
        if not args.apply:
            return _dry_run("scroll", session, target, scroll_details)
        scroll_payload: dict[str, Any] = {
            "session": session,
            **target,
            "direction": args.direction,
            "amount": args.amount,
            **_delivery_arguments(args),
        }
        if args.element is not None:
            scroll_payload["element_index"] = args.element
        client.call("scroll", scroll_payload)
        return _applied("scroll", session)

    if args.action == "close":
        return {
            "ok": True,
            "action": "close",
            "session": session,
            "backend": client.call("end_session", {"session": session}),
        }

    raise ComputerUseError(f"unsupported action: {args.action}")


def main(argv: list[str] | None = None) -> int:
    try:
        result = _execute(sys.argv[1:] if argv is None else argv)
    except ComputerUseError as exc:
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
