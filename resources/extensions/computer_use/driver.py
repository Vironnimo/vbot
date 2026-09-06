"""Persistent, bounded MCP transport to the independently installed Cua Driver."""

from __future__ import annotations

import json
import os
import re
from contextlib import ExitStack, asynccontextmanager, suppress
from typing import Any

from anyio.from_thread import start_blocking_portal
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MINIMUM_VERSION = (0, 23, 2)
TIMEOUT = 45
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


class ComputerUseError(Exception):
    """A driver/observation failure; mutations must never retry automatically."""

    def __init__(self, message: str, code: str = "computer_use_failed") -> None:
        super().__init__(message)
        self.code = code


def unpack(result: dict[str, Any]) -> dict[str, Any]:
    """Retain structured data and image blocks, rejecting explicit refusals."""
    if result.get("isError") or result.get("is_error"):
        raise ComputerUseError(_error_text(result))
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        payload = None
        for block in result.get("content", []):
            if block.get("type") == "text":
                try:
                    candidate = json.loads(block.get("text", ""))
                except (ValueError, TypeError):
                    continue
                if isinstance(candidate, dict):
                    payload = candidate
                    break
        if payload is None:
            if "content" in result:
                raise ComputerUseError(_error_text(result))
            payload = result
    for key in ("result", "data"):
        if isinstance(payload.get(key), dict):
            payload = payload[key]
    if (
        payload.get("status") == "refused"
        or payload.get("effect") == "refused"
        or payload.get("isError")
    ):
        raise ComputerUseError(_error_text(payload))
    output = dict(payload)
    for block in result.get("content", []):
        if block.get("type") == "image" and block.get("mimeType") == "image/png":
            output.setdefault("screenshot_png_b64", block.get("data"))
            break
    return output


def _error_text(result: dict[str, Any]) -> str:
    refusal = result.get("refusal") or result.get("error") or result.get("message")
    if refusal:
        return str(refusal)[:4000]
    return (
        " ".join(
            block.get("text", "")
            for block in result.get("content", [])
            if block.get("type") == "text"
        )[:4000]
        or "The driver connection was lost. Capture the target again before sending input."
    )


class CuaDriver:
    """One SDK connection shared by serialized service calls, with no input replay."""

    def __init__(self, executable: str) -> None:
        self.executable = executable
        self._stack: ExitStack | None = None
        self._portal: Any = None
        self._session: ClientSession | None = None
        self.schemas: dict[str, dict[str, Any]] = {}
        self.version = ""
        self.broken = False

    @asynccontextmanager
    async def _connection(self):
        environment = {
            key: value for key, value in os.environ.items() if key.upper() in SAFE_ENVIRONMENT_KEYS
        }
        environment["CUA_DRIVER_RS_TELEMETRY_ENABLED"] = "0"
        parameters = StdioServerParameters(command=self.executable, args=["mcp"], env=environment)
        with open(os.devnull, "w", encoding="utf-8") as errors:
            async with stdio_client(parameters, errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=TIMEOUT) as session:
                    initialized = await session.initialize()
                    self.version = initialized.server_info.version
                    version = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", self.version)
                    if not version or tuple(map(int, version.groups())) < MINIMUM_VERSION:
                        raise ComputerUseError(
                            "This driver lacks a required capability. Update cua-driver and "
                            "reload Extensions."
                        )
                    catalog = await session.list_tools()
                    self.schemas = {tool.name: tool.input_schema for tool in catalog.tools}
                    config = await session.call_tool("get_config", {})
                    values = unpack(config.model_dump(by_alias=True, exclude_none=True))
                    if values.get("max_image_dimension") != 0:
                        raise ComputerUseError(
                            "Set cua-driver max_image_dimension to 0 and reload Extensions so "
                            "original screenshots remain available."
                        )
                    yield session

    def connect(self) -> None:
        if self._session is not None:
            return
        stack = ExitStack()
        try:
            portal = stack.enter_context(start_blocking_portal())
            session = stack.enter_context(portal.wrap_async_context_manager(self._connection()))
        except Exception as exc:
            stack.close()
            self.broken = True
            pending: list[BaseException] = [exc]
            while pending:
                error = pending.pop()
                if isinstance(error, ComputerUseError):
                    raise error from exc
                if isinstance(error, BaseExceptionGroup):
                    pending.extend(error.exceptions)
            raise ComputerUseError(
                "The driver connection was lost. Capture the target again before sending input."
            ) from exc
        self._stack, self._portal, self._session = stack, portal, session
        self.broken = False

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.connect()
        if name not in self.schemas:
            raise ComputerUseError(
                "This driver lacks a required capability. Update cua-driver and reload Extensions."
            )
        try:
            assert self._session is not None
            response = self._portal.call(self._session.call_tool, name, arguments)
        except Exception as exc:
            self.broken = True
            with suppress(Exception):  # Preserve the dispatch failure; never replay input.
                self.close()
            raise ComputerUseError(
                "The driver connection was lost. Capture the target again before sending input."
            ) from exc
        return unpack(response.model_dump(by_alias=True, exclude_none=True))

    def close(self) -> None:
        stack, self._stack = self._stack, None
        self._session = None
        self._portal = None
        if stack is not None:
            stack.close()
