"""Shared seams of a Live call's Tool execution: RPCs, UI requests, and failures.

Data operations dispatch the registered RPC handlers in-process, so validation,
Queue admission and ``/ws`` event publication match every other accessor.
Display operations (app context, navigation, Terminal layout) become UI
requests to the call's owning accessor.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

JsonObject = dict[str, Any]
RpcInvoker = Callable[[str, JsonObject], Awaitable[JsonObject]]
UiRequester = Callable[[str, JsonObject], Awaitable[JsonObject]]

UI_ACTION_CONTEXT = "context"
UI_ACTION_OPEN = "open"
UI_ACTION_TERMINAL_VIEW = "terminal_view"

UI_UNAVAILABLE = "ui_unavailable"
UI_TIMEOUT = "ui_timeout"
VOICE_STOPPED = "voice_stopped"
NAVIGATION_NOT_APPLIED = "navigation_not_applied"
OPERATION_FAILED = "operation_failed"

# Agent-facing: these sentences reach the Model inside failure results.
_UI_MESSAGES = {
    UI_UNAVAILABLE: (
        "No vBot app window is attached to the voice call, so the app could not show or "
        "report this."
    ),
    UI_TIMEOUT: "The app window did not answer in time; it may or may not show the change.",
    NAVIGATION_NOT_APPLIED: "The app did not switch to it.",
}
_UI_ERROR_MESSAGE_MAX_CHARS = 300

# Agent-facing: follows a failure whose effect may or may not have happened.
UNCERTAIN_DELIVERY = "It may or may not have been delivered; do not send it again."


def join_words(items: list[str]) -> str:
    """``a``, ``a and b``, ``a, b and c``."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def text_field(arguments: JsonObject, name: str) -> str:
    """A text argument without surrounding space; empty when absent or not text."""
    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""


class LiveUiError(Exception):
    """A UI request the owning accessor could not answer or did not apply."""

    def __init__(self, code: str, message: str | None = None, *, uncertain: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.message = (
            message
            or _UI_MESSAGES.get(code)
            or f"The app could not do this ({code[:_UI_ERROR_MESSAGE_MAX_CHARS]})."
        )
        self.uncertain = uncertain


class LiveToolError(Exception):
    """A failure the Model reads: what was wrong and the next valid call."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


class LiveContext:
    """RPC and UI access for one Live call's Tool executions.

    ``is_active`` turns false once the call stops or is replaced; multi-step
    operations check it before each further effect.
    """

    def __init__(self, *, rpc: RpcInvoker, ui: UiRequester, is_active: Callable[[], bool]) -> None:
        self._rpc = rpc
        self._ui = ui
        self._is_active = is_active

    async def call(self, method: str, params: JsonObject) -> JsonObject:
        """Dispatch one registered RPC method in-process."""
        return await self._rpc(method, params)

    async def ui(self, action: str, args: JsonObject) -> JsonObject:
        """Send one UI request to the owning accessor."""
        return await self._ui(action, args)

    async def view(self, op: str, **args: Any) -> JsonObject:
        """Send one Terminals view operation to the owning accessor."""
        return await self._ui(UI_ACTION_TERMINAL_VIEW, {"op": op, **args})

    def ensure_active(self) -> None:
        """Stop before a further effect once the voice call ended."""
        if not self._is_active():
            raise LiveToolError(
                VOICE_STOPPED,
                "The voice call ended before this could continue; nothing further was done.",
            )
