"""Session-owned browser automation with explicit Agent grants."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from core.extensions import ExtensionAPI
from core.extensions.operations import ExtensionHost
from core.tools import ToolContext, ToolDisplay, tool_failure, tool_success
from core.tools.availability import resolve_tool_access
from core.utils.ids import write_id_file

from .runtime import BrowserRuntime, SetupError

BROWSER_DESCRIPTION = (
    "Use the configured browser to navigate websites, read pages, fill forms, "
    "and inspect visual content. Start with open or tabs. Use element refs from "
    "the latest snapshot; refresh after page changes. Fill accepts several "
    "fields in order and stops on the first failure. Screenshots are returned "
    "as images. Close disconnects a user-owned browser and closes a browser "
    "started for this Session."
)
BROWSER_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "status",
                "open",
                "back",
                "forward",
                "reload",
                "snapshot",
                "read",
                "click",
                "fill",
                "press",
                "select",
                "hover",
                "scroll",
                "wait",
                "screenshot",
                "tabs",
                "new_tab",
                "switch_tab",
                "close_tab",
                "dialog",
                "upload",
                "downloads",
                "close",
            ],
            "description": "Browser operation.",
        },
        "url": {
            "type": "string",
            "description": "Web address for open or new_tab. Omit for a blank new tab.",
        },
        "target": {
            "type": "string",
            "description": (
                "Element ref from the latest snapshot. Required for click, select, "
                "hover, and upload. Omit for whole-page read or scrolling."
            ),
        },
        "fields": {
            "type": "array",
            "description": (
                "Form fields for fill, in execution order. Earlier completed fields "
                "remain filled if a later field fails."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Element ref from the latest snapshot.",
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "Text to fill or option value to select. "
                            "An empty string clears a text field."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["fill", "select"],
                        "description": (
                            "Form control operation. Omit for text input; "
                            "use select for an option value."
                        ),
                    },
                },
                "required": ["target", "text"],
            },
        },
        "text": {
            "type": "string",
            "description": (
                "Key combination for press, option value for select, visible text for wait, "
                "or prompt response for dialog. Omit when accepting a dialog without a response."
            ),
        },
        "direction": {
            "type": "string",
            "enum": ["up", "down", "left", "right"],
            "description": "Scroll direction. Omit to scroll down.",
        },
        "amount": {
            "type": "integer",
            "description": "Scroll distance in pixels. Omit for 600 pixels.",
        },
        "full": {
            "type": "boolean",
            "description": (
                "Include noninteractive content in snapshot, or the whole page in screenshot. "
                "Omit for interactive elements or the visible viewport."
            ),
        },
        "selector": {
            "type": "string",
            "description": (
                "CSS selector limiting snapshot to a page section. Omit for the whole page."
            ),
        },
        "observe": {
            "type": "boolean",
            "description": (
                "Return a fresh snapshot after the action. "
                "Omit to observe after navigation and tab switching only."
            ),
        },
        "tab": {"type": "string", "description": "Tab id from tabs, for switch_tab or close_tab."},
        "accept": {
            "type": "boolean",
            "description": "Whether to accept the dialog. Omit to dismiss it.",
        },
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Absolute file paths on the browser's computer, for upload.",
        },
        "offset": {
            "type": "integer",
            "description": "Character offset for read. Omit to start at zero.",
        },
        "limit": {"type": "integer", "description": "Maximum characters for read. Omit for 12000."},
    },
    "required": ["action"],
}
MESSAGES = {
    "unavailable": (
        "Browser components could not be prepared automatically. "
        "Retry the browser operation once. If preparation fails again, "
        "report the setup stage and error."
    ),
    "config": (
        "Browser settings are invalid. Ask the user to check the Browser Use Extension settings."
    ),
    "denied": (
        "Browser Use is not permitted for this Agent. Ask the user to grant the browser Tool."
    ),
    "stopped": "Browser Use has stopped. Retry after Extensions have reloaded.",
    "session": "Browser Use requires an active Session.",
    "cancelled": "Browser Use was cancelled. Inspect the page before repeating any input.",
    "not_open": "No browser is connected for this Session. Use open or tabs first.",
    "stale": "The element ref is no longer current. Take a new snapshot before using the element.",
    "failed": (
        "The browser command failed and its effect may be uncertain. "
        "Inspect the page before repeating input. If the browser is disconnected, "
        "ask the user to check its connection and any Chrome permission dialog."
    ),
    "tab_gone": "The selected tab is closed. Use tabs and switch_tab, or create a new_tab.",
    "changed": "Browser configuration changed. Use open or tabs to connect with the new settings.",
    "shortened": (
        "Snapshot shortened. Use a selector to inspect a smaller page section, "
        "or read for page text."
    ),
    "media": (
        "The screenshot is missing, invalid, or too large. Try capturing only the visible viewport."
    ),
    "tab": "The tab id is not current. Use tabs before selecting or closing a tab.",
    "busy": (
        "The tab is in use by another vBot Session. Select a different tab or create a new_tab."
    ),
    "local_download": (
        "Download files can be listed only for a browser managed by vBot. "
        "In a connected browser, downloads stay on its computer."
    ),
    "partial": "Some fields may already be filled. Inspect the page before continuing.",
}
MAX_TEXT = 16000
MAX_MEDIA = 32 * 1024 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
MIN_VERSION = (0, 36, 0)
SAFE_ENV = {
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
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
}
# This is an action inventory, not a second model-facing schema.
FIELDS = {
    "status": (),
    "open": ("url", "observe"),
    "back": ("observe",),
    "forward": ("observe",),
    "reload": ("observe",),
    "snapshot": ("full", "selector"),
    "read": ("target", "offset", "limit"),
    "click": ("target", "observe"),
    "fill": ("fields", "observe"),
    "press": ("text", "observe"),
    "select": ("target", "text", "observe"),
    "hover": ("target", "observe"),
    "scroll": ("target", "direction", "amount", "observe"),
    "wait": ("text", "observe"),
    "screenshot": ("full",),
    "tabs": (),
    "new_tab": ("url", "observe"),
    "switch_tab": ("tab", "observe"),
    "close_tab": ("tab",),
    "dialog": ("accept", "text", "observe"),
    "upload": ("target", "files", "observe"),
    "downloads": (),
    "close": (),
}
REQUIRED = {
    "open": ("url",),
    "click": ("target",),
    "fill": ("fields",),
    "press": ("text",),
    "select": ("target", "text"),
    "hover": ("target",),
    "wait": ("text",),
    "switch_tab": ("tab",),
    "close_tab": ("tab",),
    "upload": ("target", "files"),
}
NAVIGATION = {"open", "back", "forward", "reload", "new_tab", "switch_tab"}
READ_ACTIONS = {"status", "snapshot", "read", "screenshot", "tabs", "wait", "downloads"}


class BrowserError(Exception):
    """Expected failure without raw credentials, page text, or CLI arguments."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message if message is not None else MESSAGES[code])


class BrowserArgumentError(BrowserError):
    """Identify an argument by its schema path without echoing input values."""

    def __init__(self, field: str, correction: str):
        self.field = field
        super().__init__("invalid", f"Invalid {field}. {correction}")


def _validate_text(value: Any, path: str, *, empty: bool = False) -> None:
    if not (
        isinstance(value, str)
        and (empty or bool(value))
        and len(value) <= 100000
        and "\x00" not in value
    ):
        qualifier = "" if empty else "non-empty "
        raise BrowserArgumentError(
            path,
            f"Provide a {qualifier}string with at most 100000 characters and no null characters.",
        )


def validate_arguments(arguments: Any) -> dict[str, Any]:
    """Validate all input before starting a browser or applying a form field."""
    if not isinstance(arguments, dict):
        raise BrowserArgumentError("arguments", "Provide an object.")
    action = arguments.get("action")
    if not isinstance(action, str) or action not in FIELDS:
        raise BrowserArgumentError("action", "Choose one of: " + ", ".join(FIELDS) + ".")
    if set(arguments) - {"action", *FIELDS[action]}:
        unexpected = next(key for key in arguments if key not in {"action", *FIELDS[action]})
        path = (
            unexpected
            if isinstance(unexpected, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", unexpected)
            else "arguments"
        )
        raise BrowserArgumentError(
            path,
            "Use only these fields: " + ", ".join(("action", *FIELDS[action])) + ".",
        )
    missing = [key for key in REQUIRED.get(action, ()) if key not in arguments]
    if missing:
        raise BrowserArgumentError(
            f"arguments for {action}", "Provide the required fields: " + ", ".join(missing) + "."
        )
    for key, value in arguments.items():
        if key in {"full", "observe", "accept"}:
            if type(value) is not bool:
                raise BrowserArgumentError(key, "Provide true or false.")
        elif key in {"amount", "offset", "limit"}:
            lower, upper = {"amount": (1, 5000), "offset": (0, 10000000), "limit": (1, MAX_TEXT)}[
                key
            ]
            if type(value) is not int or not lower <= value <= upper:
                raise BrowserArgumentError(key, f"Provide an integer from {lower} to {upper}.")
        elif key == "fields":
            if not isinstance(value, list) or not 1 <= len(value) <= 30:
                raise BrowserArgumentError(key, "Provide between 1 and 30 items.")
            for index, item in enumerate(value):
                path = f"fields[{index}]"
                if not isinstance(item, dict):
                    raise BrowserArgumentError(path, "Provide an object.")
                if set(item) - {"target", "text", "kind"}:
                    unexpected = next(key for key in item if key not in {"target", "text", "kind"})
                    if isinstance(unexpected, str) and re.fullmatch(
                        r"[A-Za-z_][A-Za-z0-9_]{0,63}", unexpected
                    ):
                        path += "." + unexpected
                    raise BrowserArgumentError(path, "Use only these fields: target, text, kind.")
                missing = [name for name in ("target", "text") if name not in item]
                if missing:
                    raise BrowserArgumentError(
                        path, "Provide the required fields: " + ", ".join(missing) + "."
                    )
                if item.get("kind", "fill") not in ("fill", "select"):
                    raise BrowserArgumentError(path + ".kind", "Choose one of: fill, select.")
                _validate_text(item["target"], path + ".target")
                _validate_text(item["text"], path + ".text", empty=True)
        elif key == "files":
            if not isinstance(value, list) or not 1 <= len(value) <= 20:
                raise BrowserArgumentError(key, "Provide between 1 and 20 items.")
            for index, path in enumerate(value):
                _validate_text(path, f"files[{index}]")
                if not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()):
                    raise BrowserArgumentError(
                        f"files[{index}]",
                        "Provide an absolute file path on the browser's computer.",
                    )
        else:
            _validate_text(value, key)
    if "direction" in arguments and arguments["direction"] not in {"up", "down", "left", "right"}:
        raise BrowserArgumentError("direction", "Choose one of: up, down, left, right.")
    if "text" in arguments and action == "dialog" and not arguments.get("accept", False):
        raise BrowserArgumentError(
            "text", "Set accept to true when supplying a prompt response, or omit text."
        )
    if "url" in arguments:
        validate_url(arguments["url"])
    return dict(arguments)


def validate_url(url: str) -> None:
    if url == "about:blank":
        return
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid or any(character.isspace() for character in url):
        raise BrowserArgumentError(
            "url", "Use an HTTP(S) URL without credentials or whitespace, or about:blank."
        )


@dataclass
class BrowserSession:
    key: tuple[str | None, str, str]
    name: str
    directory: Path
    config: tuple[str, str, bool]
    lock: Any = field(default_factory=threading.RLock)
    refs: dict[str, str] = field(default_factory=dict)
    tabs: dict[str, str] = field(default_factory=dict)
    active_target: str | None = None
    connected: bool = False
    last_used: float = field(default_factory=time.monotonic)
    last_run: str = ""
    client: Any = None
    browser_executable: str = ""


class BrowserClient:
    """Bounded native CLI transport; daemon handles never own our pipe EOF."""

    def __init__(self, executable: str, session: BrowserSession, namespace: str):
        self.executable = executable
        self.session = session
        self.namespace = namespace

    def version(self) -> str:
        return self._invoke(["--version"]).strip()

    def call(self, arguments: list[str]) -> dict[str, Any]:
        mode, endpoint, headed = self.session.config
        options = ["--pin-tab", "--no-auto-dialog", "--no-webmcp", "--idle-timeout", "15m"]
        if mode == "existing":
            options += ["--auto-connect"]
        elif mode == "remote":
            options += ["--cdp", endpoint]
        else:
            options += ["--download-path", str(self.session.directory / "downloads")]
            if self.session.browser_executable:
                options += ["--executable-path", self.session.browser_executable]
            if headed:
                options += ["--headed"]
        raw = self._invoke(
            [
                "--namespace",
                self.namespace,
                "--session",
                self.session.name,
                "--config",
                str(self.session.directory / "config.json"),
                *options,
                "batch",
                "--bail",
                "--json",
            ],
            json.dumps([arguments]).encode("utf-8"),
        )
        try:
            results = json.loads(raw)
            if not isinstance(results, list) or len(results) != 1:
                raise BrowserError("failed")
            payload = results[0]
        except (ValueError, TypeError) as error:
            raise BrowserError("failed") from error
        if not isinstance(payload, dict) or payload.get("success") is not True:
            code = payload.get("code") if isinstance(payload, dict) else None
            raise BrowserError("tab_gone" if code == "tab_gone" else "failed")
        data = payload.get("result")
        if not isinstance(data, dict):
            raise BrowserError("failed")
        return data

    def _invoke(self, arguments: list[str], input_data: bytes = b"") -> str:
        environment = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV}
        environment.update(NO_COLOR="1", AGENT_BROWSER_DEFAULT_TIMEOUT="20000")
        try:
            with (
                tempfile.TemporaryFile() as stdout,
                tempfile.TemporaryFile() as stderr,
                tempfile.TemporaryFile() as stdin,
            ):
                stdin.write(input_data)
                stdin.seek(0)
                result = subprocess.run(
                    [self.executable, *arguments],
                    stdout=stdout,
                    stderr=stderr,
                    stdin=stdin,
                    env=environment,
                    cwd=self.session.directory,
                    timeout=45,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                if result.returncode and not stdout.tell():
                    # Never forward command echoes, profile paths, endpoint tokens or form values.
                    raise BrowserError("failed")
                if stdout.tell() > MAX_OUTPUT:
                    raise BrowserError("failed")
                stdout.seek(0)
                return stdout.read(MAX_OUTPUT + 1).decode("utf-8")
        except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
            raise BrowserError("failed") from error


class BrowserService:
    """Own the complete capability, using existing Extension/Tool policy seams."""

    def __init__(self, api: ExtensionAPI):
        self.api = api
        self.host: ExtensionHost | None = None
        self.executable: str | None = None
        self.runtime: BrowserRuntime | None = None
        self._version: str | None = None
        self._guard = threading.RLock()
        self._external_lock = threading.RLock()
        self._sessions: dict[tuple[str | None, str, str], BrowserSession] = {}
        self._closed = False
        self._ref_next = 0
        self._ref_end = 0

    async def start(self, host: ExtensionHost) -> None:
        self.host = host
        self.runtime = BrowserRuntime(host.data_dir)

    def ready(self) -> bool:
        return not self._closed

    def _config(self) -> tuple[str, str, bool]:
        config = self.api.get_config()
        mode = config.get("mode", "managed")
        headed = config.get("headed", False)
        endpoint = self.api.resolve_credential("BROWSER_USE_CDP_URL") if mode == "remote" else ""
        if (
            not isinstance(mode, str)
            or mode not in {"managed", "existing", "remote"}
            or type(headed) is not bool
        ):
            raise BrowserError("config")
        if mode == "remote":
            try:
                url = urlsplit(endpoint)
                valid = (
                    url.scheme in {"http", "https", "ws", "wss"}
                    and url.hostname
                    and not url.username
                    and not url.password
                )
                _ = url.port
            except ValueError:
                valid = False
            if not valid or any(character.isspace() for character in endpoint):
                raise BrowserError("config")
        return mode, endpoint, headed

    def _check_access(self, context: ToolContext) -> None:
        if self._closed or self.host is None:
            raise BrowserError("stopped")
        if not context.session_id or not context.run_id:
            raise BrowserError("session")
        if context.is_cancelled() or context.was_cancelled_by_user():
            raise BrowserError("cancelled")
        if context.tool_restriction is not None and "browser" not in context.tool_restriction:
            raise BrowserError("denied")
        if context.tool_denial_resolver and context.tool_denial_resolver("browser"):
            raise BrowserError("denied")
        registry = self.api.operations.tool_registry
        if registry is None:
            raise BrowserError("stopped")
        agent = self.host.resolve_agent(context.project_id, context.agent_id)
        allowed = resolve_tool_access(
            agent.tool_access,
            registry.list_tools(),
            agent.memory_prompt_mode,
            workspace=str(agent.workspace or ""),
        ).allowed_tools
        if "browser" not in allowed:
            raise BrowserError("denied")

    def _call(
        self, context: ToolContext, session: BrowserSession, command: list[str]
    ) -> dict[str, Any]:
        self._check_access(context)
        if session.config != self._config():
            raise BrowserError("changed")
        result: dict[str, Any] = session.client.call(command)
        return result

    def _get_session(self, context: ToolContext) -> BrowserSession:
        key = (context.project_id, context.agent_id, context.session_id)
        with self._guard:
            existing = self._sessions.get(key)
            if existing is not None:
                return existing
        if self.runtime is None:
            raise BrowserError("stopped")
        config = self._config()
        executable, chrome = self.runtime.ensure(config[0], lambda: self._check_access(context))
        self._check_access(context)
        if self._config() != config:
            raise BrowserError("changed")
        with self._guard:
            session = self._sessions.get(key)
            if session is None:
                name = "vbot-" + uuid.uuid4().hex
                directory = context.data_root.resolve() / "tmp" / "browser-use" / name
                if not directory.resolve().is_relative_to(context.data_root.resolve()):
                    raise BrowserError("config")
                directory.mkdir(parents=True, exist_ok=True)
                if not directory.resolve().is_relative_to(context.data_root.resolve()):
                    raise BrowserError("config")
                (directory / "config.json").write_text("{}\n", encoding="utf-8")
                (directory / "downloads").mkdir(exist_ok=True)
                session = BrowserSession(key, name, directory, config)
                session.browser_executable = chrome
                if config[0] != "managed":
                    session.lock = self._external_lock
                namespace = (
                    "vbot-"
                    + hashlib.sha256(str(context.data_root.resolve()).encode()).hexdigest()[:16]
                )
                self.executable = executable
                session.client = BrowserClient(executable, session, namespace)
                self._sessions[key] = session
            return session

    def _connect(self, context: ToolContext, session: BrowserSession) -> None:
        if self._version is None:
            if not self.executable:
                raise BrowserError("unavailable")
            version = session.client.version()
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
            if not match or tuple(map(int, match.groups())) < MIN_VERSION:
                raise BrowserError("unavailable")
            self._version = version
        # Mark before dispatch so a lost response still retains cleanup ownership.
        session.connected = True
        self._tabs(context, session)

    def _tabs(self, context: ToolContext, session: BrowserSession) -> dict[str, Any]:
        payload = self._call(context, session, ["tab", "list"])
        tabs = payload.get("tabs")
        if not isinstance(tabs, list):
            raise BrowserError("failed")
        current = {}
        visible = []
        characters = 0
        truncated = False
        for tab in tabs[:200]:
            if not isinstance(tab, dict):
                raise BrowserError("failed")
            tab_id = tab.get("targetId")
            target = tab.get("targetId")
            if not isinstance(tab_id, str) or not isinstance(target, str):
                raise BrowserError("failed")
            row = {
                "id": tab_id,
                "url": str(tab.get("url", ""))[:2048],
                "title": str(tab.get("title", ""))[:256],
                "active": target == session.active_target
                if session.active_target
                else bool(tab.get("active")),
            }
            characters += len(json.dumps(row))
            if characters > MAX_TEXT:
                truncated = True
                break
            current[tab_id] = target
            visible.append(row)
            if session.active_target is None and tab.get("active"):
                session.active_target = target
        session.tabs = current
        return {"tabs": visible, "truncated": truncated or len(tabs) > 200}

    def _ensure_tab(
        self, context: ToolContext, session: BrowserSession, args: dict[str, Any]
    ) -> None:
        if (
            args["action"] in {"tabs", "new_tab", "switch_tab", "close_tab", "downloads"}
            or session.active_target is None
        ):
            return
        payload = self._call(context, session, ["tab", "list"])
        rows = payload.get("tabs", [])
        selected = next((row for row in rows if row.get("targetId") == session.active_target), None)
        if selected is None:
            session.refs.clear()
            raise BrowserError("tab_gone")
        if not selected.get("active"):
            self._call(context, session, ["tab", session.active_target])
            session.refs.clear()
            if "target" in args or "fields" in args:
                raise BrowserError("stale")

    def _ref(self, session: BrowserSession, target: str) -> str:
        ref = session.refs.get(target)
        if ref is None:
            raise BrowserError("stale")
        return ref

    def _new_refs(self, count: int) -> list[str]:
        """Lease durable numeric ranges, so refs never repeat after a reload.

        SQLite's transaction serializes processes sharing this data directory.
        A small range amortizes disk writes; unused numbers are never reclaimed.
        """
        if not count:
            return []
        with self._guard:
            if self.host is None:
                raise BrowserError("stopped")
            if self._ref_next + count > self._ref_end:
                directory = self.host.data_dir.resolve() / "artifacts" / "browser-use"
                if not directory.resolve().is_relative_to(self.host.data_dir.resolve()):
                    raise BrowserError("config")
                directory.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(directory / "refs.db", timeout=5)) as connection:
                    with connection:
                        connection.execute("BEGIN IMMEDIATE")
                        connection.execute(
                            "CREATE TABLE IF NOT EXISTS sequence "
                            "(id INTEGER PRIMARY KEY CHECK(id=1), "
                            "next INTEGER NOT NULL CHECK(next>0)) STRICT"
                        )
                        row = connection.execute("SELECT next FROM sequence WHERE id=1").fetchone()
                        start = row[0] if row else 1
                        end = start + max(count, 4096)
                        connection.execute("INSERT OR REPLACE INTO sequence VALUES (1, ?)", (end,))
                    self._ref_next, self._ref_end = start, end
            start = self._ref_next
            self._ref_next += count
            return [f"r{number}" for number in range(start, self._ref_next)]

    def _snapshot(
        self, context: ToolContext, session: BrowserSession, args: dict[str, Any]
    ) -> dict[str, Any]:
        session.refs.clear()
        command = ["snapshot", "-c"]
        if not args.get("full", False):
            command.append("-i")
        if "selector" in args:
            command += ["-s", args["selector"]]
        payload = self._call(context, session, command)
        tree, refs = payload.get("snapshot"), payload.get("refs")
        if not isinstance(tree, str) or not isinstance(refs, dict):
            raise BrowserError("failed")
        source: str = tree
        keys: list[str] = list(
            dict.fromkeys(key for key in re.findall(r"\bref=(e[1-9][0-9]*)\b", tree) if key in refs)
        )
        try:
            labels: dict[str, str] = dict(zip(keys, self._new_refs(len(keys)), strict=True))
        except (OSError, sqlite3.Error):
            raise BrowserError("failed") from None
        # Track generated labels so page text cannot publish a truncated ref.
        positions: list[tuple[int, str, str]] = []
        shift = 0

        def replace_ref(match: re.Match[str]) -> str:
            nonlocal shift
            key = match.group(1)
            label = labels.get(key)
            if label is None:
                return match[0]
            replacement = "ref=" + label
            shift += len(replacement) - len(match[0])
            if source[match.end() : match.end() + 1] == "]":
                positions.append((match.end() + shift + 1, label, key))
            return replacement

        tree = re.sub(r"\bref=(e[1-9][0-9]*)\b", replace_ref, source)
        truncated = len(tree) > MAX_TEXT
        if truncated:
            tree = tree[:MAX_TEXT].rsplit("\n", 1)[0]
        for end, label, key in positions:
            if end <= len(tree):
                session.refs[label] = "@" + key
        result: dict[str, Any] = {
            "snapshot": tree,
            "url": payload.get("origin", ""),
            "truncated": truncated,
        }
        if truncated:
            result["hint"] = MESSAGES["shortened"]
        return result

    def _close(self, session: BrowserSession) -> None:
        session.refs.clear()
        if session.connected:
            session.client.call(["close"])
        session.connected = False
        session.active_target = None
        with self._guard:
            if self._sessions.get(session.key) is session:
                del self._sessions[session.key]

    def _prune(self, current: BrowserSession) -> None:
        with self._guard:
            expired = [
                session
                for session in self._sessions.values()
                if session is not current and time.monotonic() - session.last_used > 900
            ]
        for session in expired:
            if session.lock.acquire(blocking=False):
                try:
                    if time.monotonic() - session.last_used > 900:
                        self._close(session)
                except BrowserError:
                    self.api.logger.warning("Browser Use idle cleanup failed")
                finally:
                    session.lock.release()

    def handle(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            args = validate_arguments(arguments)
            self._check_access(context)
            key = (context.project_id, context.agent_id, context.session_id)
            with self._guard:
                connected = key in self._sessions
            if not connected and args["action"] == "close":
                return tool_success({"action": "close", "closed": True})
            if not connected and args["action"] not in {"open", "tabs", "new_tab", "status"}:
                raise BrowserError("not_open")
            session = self._get_session(context)
            self._prune(session)
            with session.lock:
                self._check_access(context)
                with self._guard:
                    if self._sessions.get(session.key) is not session:
                        raise BrowserError("changed")
                session.last_used = time.monotonic()
                # Refs never cross Runs, even though the browser stays connected.
                if session.last_run != context.run_id:
                    session.refs.clear()
                    session.last_run = context.run_id
                if session.config != self._config():
                    self._close(session)
                    raise BrowserError("changed")
                action = args["action"]
                if action == "close":
                    self._close(session)
                    return tool_success({"action": action, "closed": True})
                if action == "status":
                    version = session.client.version()
                    return tool_success(
                        {
                            "action": action,
                            "version": version,
                            "mode": session.config[0],
                            "connected": session.connected,
                        }
                    )
                if not session.connected:
                    if action not in {"open", "tabs", "new_tab"}:
                        raise BrowserError("not_open")
                    self._connect(context, session)
                else:
                    self._ensure_tab(context, session, args)
                result = self._execute(context, session, args)
                return tool_success({"action": action, **result})
        except SetupError as error:
            self.api.logger.warning("Browser setup failed at stage %s", error.stage)
            return tool_failure(
                "browser_setup_" + error.stage, MESSAGES["unavailable"], retryable=False
            )
        except BrowserError as error:
            code = "invalid_arguments" if error.code == "invalid" else "browser_" + error.code
            return tool_failure(code, str(error), retryable=False)
        except Exception:
            self.api.logger.exception("Browser Use request failed")
            return tool_failure("browser_failed", MESSAGES["failed"], retryable=False)

    def _execute(
        self, context: ToolContext, session: BrowserSession, args: dict[str, Any]
    ) -> dict[str, Any]:
        action = args["action"]
        if action == "snapshot":
            return self._snapshot(context, session, args)
        if action == "tabs":
            return self._tabs(context, session)
        if action == "downloads":
            if session.config[0] != "managed":
                raise BrowserError("local_download")
            files = []
            for item in (session.directory / "downloads").iterdir():
                if item.is_file() and not item.is_symlink() and item.suffix != ".crdownload":
                    files.append(
                        {
                            "path": item.as_posix(),
                            "filename": item.name,
                            "bytes": item.stat().st_size,
                        }
                    )
                    if len(files) == 200:
                        break
            return {"files": files}
        if action == "screenshot":
            path = write_id_file(session.directory, "shot", ".png", b"")
            self._call(
                context,
                session,
                ["screenshot", str(path), *(["--full"] if args.get("full") else [])],
            )
            if not path.is_file() or path.stat().st_size > MAX_MEDIA or path.is_symlink():
                raise BrowserError("media")
            raw = path.read_bytes()
            if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                raise BrowserError("media")
            context.presentation_images.append({"path": path.as_posix(), "filename": path.name})
            context.result_media.append(
                {
                    "path": path.as_posix(),
                    "filename": path.name,
                    "media_type": "image/png",
                    "base64": base64.b64encode(raw).decode("ascii"),
                }
            )
            return {"screenshot": path.as_posix()}
        if action == "read":
            target = self._ref(session, args["target"]) if "target" in args else "body"
            payload = self._call(context, session, ["get", "text", target])
            text = payload.get("text")
            if not isinstance(text, str):
                raise BrowserError("failed")
            offset, limit = args.get("offset", 0), args.get("limit", 12000)
            end = min(len(text), offset + limit)
            return {
                "text": text[offset:end],
                "offset": offset,
                "total": len(text),
                "next_offset": end if end < len(text) else None,
            }
        # Resolve every field before the first side effect.
        commands: list[list[str]] = []
        if action == "fill":
            commands = [
                [item.get("kind", "fill"), self._ref(session, item["target"]), item["text"]]
                for item in args["fields"]
            ]
        elif action in {"switch_tab", "close_tab"}:
            tab = args["tab"]
            if tab not in session.tabs:
                raise BrowserError("tab")
            target = session.tabs[tab]
            with self._guard:
                if any(
                    other is not session
                    and other.config == session.config
                    and other.active_target == target
                    for other in self._sessions.values()
                ):
                    raise BrowserError("busy")
            commands = [["tab", *(["close", tab] if action == "close_tab" else [tab])]]
        elif action in {"open", "new_tab"}:
            commands = (
                [["open", args["url"]]]
                if action == "open"
                else [["tab", "new", args.get("url", "about:blank")]]
            )
        elif action in {"back", "forward", "reload"}:
            commands = [[action]]
        elif action in {"click", "hover", "select"}:
            commands = [
                [
                    action,
                    self._ref(session, args["target"]),
                    *([args["text"]] if action == "select" else []),
                ]
            ]
        elif action == "press":
            commands = [["press", args["text"]]]
        elif action == "scroll":
            commands = [
                [
                    "scroll",
                    args.get("direction", "down"),
                    str(args.get("amount", 600)),
                    *(
                        ["--selector", self._ref(session, args["target"])]
                        if "target" in args
                        else []
                    ),
                ]
            ]
        elif action == "wait":
            commands = [["wait", "--text", args["text"]]]
        elif action == "dialog":
            commands = [
                [
                    "dialog",
                    "accept" if args.get("accept", False) else "dismiss",
                    *([args["text"]] if "text" in args else []),
                ]
            ]
        elif action == "upload":
            if session.config[0] != "remote":
                for index, path in enumerate(args["files"]):
                    if not Path(path).is_file():
                        raise BrowserArgumentError(
                            f"files[{index}]",
                            "Provide a path to an existing file on the browser's computer.",
                        )
            commands = [["upload", self._ref(session, args["target"]), *args["files"]]]
        if action not in READ_ACTIONS:
            session.refs.clear()
        completed = 0
        try:
            for command in commands:
                response = self._call(context, session, command)
                target_id = response.get("targetId")
                if action in NAVIGATION and isinstance(target_id, str):
                    session.active_target = target_id
                    session.tabs[target_id] = target_id
                completed += 1
        except BrowserError as error:
            if action == "close_tab" and error.code in {"failed", "tab_gone"}:
                # A lost/failed reply can follow a committed close. Verify the
                # requested postcondition without ever replaying the input.
                self._tabs(context, session)
                if args["tab"] not in session.tabs:
                    return {"closed": True, "verified": True}
            if action == "fill" and completed:
                return {
                    "status": "partial",
                    "completed": completed,
                    "total": len(commands),
                    "error": {"code": "browser_" + error.code, "message": str(error)},
                    "hint": MESSAGES["partial"],
                }
            raise
        result: dict[str, Any] = {"completed": completed}
        if action == "close_tab":
            try:
                self._tabs(context, session)
            except BrowserError as error:
                result["observation_error"] = {
                    "code": "browser_" + error.code,
                    "message": str(error),
                }
        if args.get("observe", action in NAVIGATION):
            try:
                result.update(self._snapshot(context, session, {}))
            except BrowserError as error:
                # The action succeeded; a failed observation must not invite a duplicate action.
                result["observation_error"] = {
                    "code": "browser_" + error.code,
                    "message": str(error),
                }
        return result

    def run_end(self, context: Any, **kwargs: Any) -> None:
        with self._guard:
            sessions = [
                session for session in self._sessions.values() if session.last_run == context.run_id
            ]
        for session in sessions:
            with session.lock:
                session.refs.clear()
                session.last_used = time.monotonic()

    def close(self) -> None:
        self._closed = True
        with self._guard:
            sessions = list(self._sessions.values())
        for session in sessions:
            with session.lock:
                try:
                    self._close(session)
                except BrowserError:
                    self.api.logger.warning("Browser Use shutdown cleanup failed")


def register(api: ExtensionAPI) -> None:
    api.register_settings(
        [
            {
                "key": "mode",
                "type": "text",
                "label": "Connection mode",
                "default": "managed",
                "description": (
                    "managed: start a browser; existing: connect to local Chrome; "
                    "remote: use the CDP URL."
                ),
            },
            {
                "key": "headed",
                "type": "toggle",
                "label": "Show managed browser",
                "default": False,
                "description": (
                    "Show a browser window on the server computer. Applies only to managed mode."
                ),
            },
            {
                "key": "cdp_url",
                "type": "secret",
                "label": "Remote CDP URL",
                "env_key": "BROWSER_USE_CDP_URL",
                "description": (
                    "HTTP(S) or WebSocket endpoint, including its token if needed. "
                    "Used only in remote mode."
                ),
            },
        ]
    )
    service = BrowserService(api)
    api.operations.startup.append(service.start)
    api.on_shutdown(service.close)
    api.on("run_end", service.run_end)
    api.register_tool(
        "browser",
        BROWSER_DESCRIPTION,
        BROWSER_PARAMETERS,
        service.handle,
        requires_opt_in=True,
        parallel_safe=False,
        open_input_schema=True,
        ready=service.ready,
        readiness_hint=MESSAGES["unavailable"],
        result_schema={"type": "object", "required": ["action"]},
        display=ToolDisplay(summary_fields=("action",), hidden_argument_keys=("fields", "text")),
    )
