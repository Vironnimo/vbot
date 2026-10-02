"""What one MCP connection retains about itself: events, redacted errors, diagnosis.

Events are bounded and numbered, so a reader that polls with a cursor learns how
many it missed. Every retained string passes credential redaction first: the
credentials the connection references and its stored OAuth tokens never enter
an event, an error message or the captured stderr of a local server.

A failed connection also keeps a diagnosis for the people setting it up: the
last lines a local server wrote to stderr and, when the failure has a known
cause (a missing program, directory or credential, an unreachable or refusing
server), that cause with what to do about it.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import re
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import asdict, is_dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx2
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, METHOD_NOT_FOUND

from core.extensions.operations import ExtensionHost

from ._oauth import oauth_secrets

EVENT_HISTORY_LIMIT = 256
STDERR_CHUNK_SIZE = 4096
# The stderr tail a failed connection reports: its last lines, each bounded.
STDERR_TAIL_LINES = 20
STDERR_LINE_CHARACTERS = 500
REDACTED = "[redacted]"
# Terminal colour and title sequences, which some servers write to stderr.
_TERMINAL_SEQUENCE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
# What to install for the launchers that MCP setup instructions use most.
_PROGRAM_SOURCES = {
    "npx": "Node.js",
    "npm": "Node.js",
    "node": "Node.js",
    "pnpm": "pnpm",
    "pnpx": "pnpm",
    "yarn": "Yarn",
    "bun": "Bun",
    "bunx": "Bun",
    "deno": "Deno",
    "uv": "uv",
    "uvx": "uv",
    "python": "Python",
    "python3": "Python",
    "py": "Python",
    "pipx": "pipx",
    "docker": "Docker",
    "podman": "Podman",
}
_WINDOWS_PROGRAM_SUFFIXES = (".exe", ".cmd", ".bat", ".ps1")


class MissingCredentialError(ValueError):
    """A credential the connection references has no value."""

    def __init__(self, key: str) -> None:
        super().__init__(f"Missing MCP credential: {key}")
        self.key = key


def dump(value: Any) -> dict[str, Any]:
    """An SDK model, dataclass or object as the JSON object the protocol carries."""
    if hasattr(value, "model_dump"):
        return dict(value.model_dump(mode="json", by_alias=True, exclude_none=True))
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, dict):
        return value
    raise TypeError("MCP payload must be an object")


class ConnectionEvents:
    """One connection's event history and the redaction of its credentials.

    *config* returns the connection's current configuration, whose credential
    references name what to redact. *on_stderr* runs after the stderr tail
    changed.
    """

    def __init__(
        self,
        host: ExtensionHost,
        config: Callable[[], dict[str, Any]],
        *,
        on_stderr: Callable[[], None] | None = None,
    ) -> None:
        self._host = host
        self._config = config
        self._events: deque[dict[str, Any]] = deque(maxlen=EVENT_HISTORY_LIMIT)
        self._sequence = 0
        self._on_stderr = on_stderr
        # The current local server process's last complete stderr lines and
        # the line it is still writing.
        self._stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._stderr_line = ""
        self._problem: dict[str, Any] | None = None
        # The status of the latest HTTP response, while it is an error.
        self._http_status: int | None = None

    def record(self, kind: str, payload: Any) -> None:
        """Keep one event; its strings and keys are redacted after JSON decoding."""
        secrets = sorted(self._secrets(), key=len, reverse=True)

        def redact(value: Any) -> Any:
            if isinstance(value, str):
                for secret in secrets:
                    value = value.replace(secret, REDACTED)
                return value
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {redact(key): redact(item) for key, item in value.items()}
            return value

        # Redact decoded strings: replacing inside serialized JSON misses escaped
        # credentials and can corrupt escape sequences for short credentials.
        safe_payload = redact(json.loads(json.dumps(payload, ensure_ascii=False, default=dump)))
        self._sequence += 1
        self._events.append({"sequence": self._sequence, "kind": kind, "payload": safe_payload})

    def read(self, after: int = 0) -> dict[str, Any]:
        """The events after cursor *after*, the current cursor, and how many were dropped."""
        first = self._events[0]["sequence"] if self._events else self._sequence + 1
        return {
            "events": [event for event in self._events if event["sequence"] > after],
            "cursor": self._sequence,
            "missed_events": max(0, first - after - 1),
        }

    def redact(self, message: str) -> str:
        for secret in sorted(self._secrets(), key=len, reverse=True):
            message = message.replace(secret, REDACTED)
        return message

    def safe_error(self, error: BaseException) -> str:
        """*error* as one redacted line naming its type; a group joins its members."""
        if isinstance(error, BaseExceptionGroup):
            message = "; ".join(self.safe_error(item) for item in error.exceptions)
        else:
            message = f"{type(error).__name__}: {error}"
        return self.redact(message)

    def drain_stderr(self, descriptor: int, loop: asyncio.AbstractEventLoop) -> None:
        """Record a local server's stderr as redacted ``stderr`` events; runs on its own thread.

        Draining continuously keeps a verbose server from blocking its protocol
        pipe. Text that could still be the start of a credential waits for the
        next chunk, so a credential split across reads is redacted too. Each
        drain belongs to a new process, so it starts a new stderr tail.
        """
        if not loop.is_closed():
            loop.call_soon_threadsafe(self._restart_stderr)
        with os.fdopen(descriptor, "r", encoding="utf-8", errors="replace") as stream:
            pending = ""
            while chunk := stream.read(STDERR_CHUNK_SIZE):
                pending += chunk
                tail = max((len(secret) for secret in self._secrets()), default=0)
                if len(pending) <= tail:
                    continue
                boundary = len(pending) - tail
                for secret in self._secrets():
                    start = pending.rfind(secret, 0, boundary + len(secret))
                    if start >= 0 and start < boundary < start + len(secret):
                        boundary = start
                output, pending = pending[:boundary], pending[boundary:]
                if output and not loop.is_closed():
                    loop.call_soon_threadsafe(self._stderr, self.redact(output))
            if pending and not loop.is_closed():
                loop.call_soon_threadsafe(self._stderr, self.redact(pending))

    def _restart_stderr(self) -> None:
        self._stderr_tail.clear()
        self._stderr_line = ""

    def _stderr(self, text: str) -> None:
        """Keep one redacted stderr chunk as an event and in the stderr tail."""
        self.record("stderr", {"text": text})
        *lines, line = (self._stderr_line + text).split("\n")
        self._stderr_line = line[-STDERR_CHUNK_SIZE:]
        for complete in lines:
            if complete := _TERMINAL_SEQUENCE.sub("", complete).rstrip():
                self._stderr_tail.append(_bounded(complete))
        if self._on_stderr is not None:
            self._on_stderr()

    def stderr_tail(self) -> list[str]:
        """The last stderr lines of the current local server process, oldest first."""
        tail = list(self._stderr_tail)
        if line := _TERMINAL_SEQUENCE.sub("", self._stderr_line).rstrip():
            tail = [*tail, _bounded(line)][-STDERR_TAIL_LINES:]
        return tail

    def observe_status(self, status: int) -> None:
        """Remember an HTTP error status, which the SDK reports without it."""
        self._http_status = status if status >= 400 else None

    def diagnose(self, error: BaseException | None) -> None:
        """Keep the known cause of the connection failure *error*; ``None`` forgets it."""
        self._problem = (
            None if error is None else _diagnosis(error, self._config(), self._http_status)
        )

    @property
    def problem(self) -> dict[str, Any] | None:
        """The known cause of the latest connection failure, with what to do about it."""
        return None if self._problem is None else dict(self._problem)

    def _secrets(self) -> list[str]:
        config = self._config()
        keys = set(config.get("credential_environment", {}).values())
        keys.update(config.get("credential_headers", {}).values())
        if config.get("oauth_client_secret"):
            keys.add(config["oauth_client_secret"])
        secrets = [self._host.resolve_credential(key) for key in keys]
        secrets.extend(oauth_secrets(self._host, config["id"]))
        return [value for value in secrets if value]


def _bounded(line: str) -> str:
    if len(line) <= STDERR_LINE_CHARACTERS:
        return line
    return line[: STDERR_LINE_CHARACTERS - 1] + "…"


def _leaves(error: BaseException) -> Iterator[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        for item in error.exceptions:
            yield from _leaves(item)
    else:
        yield error


def _program_name(command: str) -> str:
    name = re.split(r"[\\/]", command)[-1].lower()
    for suffix in _WINDOWS_PROGRAM_SUFFIXES:
        name = name.removesuffix(suffix)
    return name


def _diagnosis(
    error: BaseException, config: dict[str, Any], http_status: int | None
) -> dict[str, Any] | None:
    """The known cause of a connection failure: a code, its details and English advice.

    The code and details let the WebUI phrase the advice in the user's
    language; ``message`` is the same advice for the CLI.
    """
    leaves = list(_leaves(error))
    for leaf in leaves:
        if isinstance(leaf, MissingCredentialError):
            return {
                "code": "credential_missing",
                "credential": leaf.key,
                "message": f"Credential {leaf.key} has no value. Set it for this connection.",
            }
    if config["transport"] == "stdio":
        return _process_diagnosis(leaves, config)
    return _server_diagnosis(leaves, config, http_status)


def _process_diagnosis(
    leaves: list[BaseException], config: dict[str, Any]
) -> dict[str, Any] | None:
    command = config["command"]
    for leaf in leaves:
        if not isinstance(leaf, OSError):
            continue
        directory = config.get("cwd")
        if directory and not os.path.isdir(directory):
            return {
                "code": "directory_not_found",
                "directory": directory,
                "message": (
                    f"Working directory {directory} does not exist on the vBot host. "
                    "Create it or choose another one."
                ),
            }
        if isinstance(leaf, PermissionError):
            return {
                "code": "command_not_executable",
                "command": command,
                "message": (
                    f"Program {command} could not be started: permission denied. "
                    "Choose a program file you may run."
                ),
            }
        if isinstance(leaf, FileNotFoundError | NotADirectoryError) or leaf.errno in {
            errno.ENOENT,
            errno.ENOTDIR,
        }:
            requirement = _PROGRAM_SOURCES.get(_program_name(command))
            install = f"Install {requirement}" if requirement else "Install it"
            return {
                "code": "command_not_found",
                "command": command,
                "requirement": requirement,
                "message": (
                    f"Program {command} was not found on the vBot host. {install}, then "
                    "restart vBot so it finds the program, or enter the program's full path."
                ),
            }
    if any(isinstance(leaf, MCPError) and leaf.code == CONNECTION_CLOSED for leaf in leaves):
        return {
            "code": "process_exited",
            "message": (
                "The local server ended or closed its connection. "
                "Its last error output usually names the cause."
            ),
        }
    return _timeout_diagnosis(leaves, config)


def _server_diagnosis(
    leaves: list[BaseException], config: dict[str, Any], http_status: int | None
) -> dict[str, Any] | None:
    host = urlsplit(config["url"]).hostname or config["url"]
    for leaf in leaves:
        if isinstance(leaf, httpx2.HTTPStatusError):
            http_status = leaf.response.status_code
        elif isinstance(leaf, httpx2.ConnectError | httpx2.ConnectTimeout):
            return {
                "code": "server_unreachable",
                "host": host,
                "message": (
                    f"Could not reach {host}. Check that the server is running and the "
                    "URL is correct."
                ),
            }
        elif (
            http_status is None
            and isinstance(leaf, MCPError)
            and leaf.code == METHOD_NOT_FOUND
            and leaf.error.message == "Not Found"
        ):
            # The SDK's spelling of an HTTP 404 before a session exists.
            http_status = 404
    if http_status in {401, 403}:
        return {
            "code": "unauthorized",
            "status": http_status,
            "message": (
                f"The server refused access (HTTP {http_status}). Set the credential it "
                "expects, or turn on OAuth sign-in if it offers one."
            ),
        }
    if http_status == 404:
        return {
            "code": "endpoint_not_found",
            "status": http_status,
            "message": (
                "The server has no MCP endpoint at this URL (HTTP 404). "
                "Check the full URL, including a path such as /mcp."
            ),
        }
    if http_status == 405 and config["transport"] == "http":
        return {
            "code": "method_not_allowed",
            "status": http_status,
            "message": (
                "The server does not accept Streamable HTTP at this URL (HTTP 405). "
                "Check the URL; an older server may need the legacy SSE connection type."
            ),
        }
    if http_status is not None and http_status >= 500:
        return {
            "code": "server_error",
            "status": http_status,
            "message": f"The server failed with HTTP {http_status}. Try again later.",
        }
    return _timeout_diagnosis(leaves, config)


def _timeout_diagnosis(
    leaves: list[BaseException], config: dict[str, Any]
) -> dict[str, Any] | None:
    if any(isinstance(leaf, TimeoutError | httpx2.TimeoutException) for leaf in leaves):
        seconds = config["timeout"]
        return {
            "code": "timed_out",
            "seconds": seconds,
            "message": f"The server did not answer within {seconds:g} seconds.",
        }
    return None
