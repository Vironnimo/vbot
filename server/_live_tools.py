"""App operations a Live call's delegation model runs through vBot's canonical RPCs.

Data operations dispatch the registered RPC handlers in-process, so validation,
Queue admission and ``/ws`` event publication match every other accessor. Display
operations (app context, navigation, Terminal layout) become UI requests to the
call's owning accessor. Operation failures return structured results; a
mutation is never replayed.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from core.model_tasks.live import LIVE_TOOL_APP, LIVE_TOOL_TERMINAL
from server.rpc.errors import RpcError
from server.rpc.validation import CHAT_INPUT_ORIGIN_SPEECH_TRANSCRIPTION

JsonObject = dict[str, Any]
RpcInvoker = Callable[[str, JsonObject], Awaitable[JsonObject]]
UiRequester = Callable[[str, JsonObject], Awaitable[JsonObject]]

_LOGGER = logging.getLogger("vbot.server.live")

UI_ACTION_CONTEXT = "context"
UI_ACTION_OPEN = "open"
UI_ACTION_TERMINAL_VIEW = "terminal_view"

_APP_FIELDS: dict[str, frozenset[str]] = {
    "context": frozenset(),
    "sessions": frozenset({"agent_id"}),
    "read": frozenset({"agent_id", "session_id"}),
    "open": frozenset({"view", "agent_id", "session_id"}),
    "send": frozenset({"agent_id", "session_id", "text"}),
}
_TERMINAL_FIELDS: dict[str, frozenset[str]] = {
    "list": frozenset(),
    "start": frozenset({"program", "count", "workdir", "name", "group_id"}),
    "read": frozenset({"terminal_id"}),
    "input": frozenset({"terminal_id", "text", "submit", "key"}),
    "show": frozenset({"terminal_id"}),
    "maximize": frozenset({"terminal_id"}),
    "restore": frozenset(),
    "reorder": frozenset({"group_id", "order"}),
    "create_group": frozenset({"name"}),
    "show_group": frozenset({"group_id"}),
    "rename_group": frozenset({"group_id", "name"}),
    "delete_group": frozenset({"group_id"}),
    "close": frozenset({"terminal_id"}),
}
_TOOL_FIELDS = {LIVE_TOOL_APP: _APP_FIELDS, LIVE_TOOL_TERMINAL: _TERMINAL_FIELDS}
_KEYS = {
    "enter": "\r",
    "escape": "\x1b",
    "tab": "\t",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "left": "\x1b[D",
    "right": "\x1b[C",
    "ctrl-c": "\x03",
}
_EDITABLE_GROUP_KINDS = frozenset({"user", "agent"})
_FINISHED_TERMINAL_STATES = frozenset({"exited", "error"})
_CODING_PROGRAMS = {"codex": "Codex", "claude": "Claude Code"}
_CODING_TERMINAL = re.compile(r"(codex|claude)(\.(exe|cmd|bat))?", re.IGNORECASE)
_TERMINAL_SUMMARY_FIELDS = (
    "terminal_id",
    "group_id",
    "name",
    "state",
    "command",
    "launch_command",
    "workdir",
    "screen_revision",
    "exit_code",
)
_SEND_RESULT_OMITTED_FIELDS = frozenset({"events", "controls", "controls_sequence", "sse_url"})
_MAX_TEXT_CHARS = 16_000
_MAX_NAME_CHARS = 80
_MAX_SUMMARY_STRING_CHARS = 1_000
_MAX_SCREEN_CHARS = 8_000
_MAX_CHAT_CONTEXT_CHARS = 8_000
_MAX_SAFE_INTEGER = 2**53 - 1
_SESSION_LIST_LIMIT = 30
_CHAT_READ_LIMIT = 20
_VALIDATED_SESSION_LIMIT = 256
_UI_ERROR_MESSAGE_MAX_CHARS = 300

REFRESH_FAILED = "refresh_failed"
NAVIGATION_NOT_APPLIED = "navigation_not_applied"
OPERATION_FAILED = "operation_failed"
UI_UNAVAILABLE = "ui_unavailable"
UI_TIMEOUT = "ui_timeout"
VOICE_STOPPED = "voice_stopped"

# Agent-facing: the delegation model reads these to correct its next call.
_FAILURE_MESSAGES = {
    "invalid_arguments": "Unknown action or a field this action does not accept.",
    "missing_target_or_text": "A required id, name, directory, or text is missing or empty.",
    "invalid_view": "view must be chat or terminals.",
    NAVIGATION_NOT_APPLIED: "The app did not apply the navigation.",
    "text_too_long": "text must be at most 16000 characters.",
    "invalid_name": "name must be text of at most 80 characters.",
    "group_not_found": "No group has this group_id; use list for exact ids.",
    "group_not_editable": "Only user and Agent groups can be changed.",
    "invalid_order": "order must name every Terminal of a user or Agent group exactly once.",
    "unsupported_program": "program must be codex or claude.",
    "invalid_count": "count must be a positive whole number.",
    "terminal_not_found": "No Terminal has this terminal_id; use list for exact ids.",
    "not_a_coding_terminal": "This Terminal does not run Codex or Claude Code.",
    "invalid_input": (
        "Send either text, optionally with submit, or one named key. Text must not contain "
        "control characters other than tab and line breaks."
    ),
    VOICE_STOPPED: "The voice call ended before the operation could continue.",
    UI_UNAVAILABLE: "No app window is attached to the voice call.",
    UI_TIMEOUT: "The app window did not answer in time; the display may or may not have changed.",
    OPERATION_FAILED: "The operation failed unexpectedly.",
}


class LiveUiError(Exception):
    """A UI request the owning accessor could not answer or did not apply."""

    def __init__(self, code: str, message: str | None = None, *, uncertain: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.message = message or _FAILURE_MESSAGES.get(code, "The app could not apply this.")
        self.uncertain = uncertain


class _OperationError(Exception):
    """Validation or guard failure raised before the next effect."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = _FAILURE_MESSAGES[code]


def _failure(code: str, message: str, *, uncertain: bool) -> JsonObject:
    return {
        "ok": False,
        "error": {"code": code, "message": message, "delivery_uncertain": uncertain},
    }


def _error_fields(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, (_OperationError, LiveUiError, RpcError)):
        return exc.code, exc.message
    return OPERATION_FAILED, _FAILURE_MESSAGES[OPERATION_FAILED]


def _required(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value
    raise _OperationError("missing_target_or_text")


def _launch_count(value: Any) -> int:
    if isinstance(value, bool):
        raise _OperationError("invalid_count")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or not 1 <= value <= _MAX_SAFE_INTEGER:
        raise _OperationError("invalid_count")
    return value


def _terminal_summary(item: JsonObject) -> JsonObject:
    return {
        key: item[key][:_MAX_SUMMARY_STRING_CHARS] if isinstance(item[key], str) else item[key]
        for key in _TERMINAL_SUMMARY_FIELDS
        if key in item
    }


def _is_coding_terminal(item: JsonObject) -> bool:
    command = str(item.get("launch_command") or item.get("command") or "")
    return _CODING_TERMINAL.fullmatch(re.split(r"[\\/]", command)[-1]) is not None


def _recent_messages(messages: list[Any]) -> list[JsonObject]:
    """Keep the newest user/assistant/error text within one shared character budget."""
    remaining = _MAX_CHAT_CONTEXT_CHARS
    recent: list[JsonObject] = []
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if message.get("role") not in {"user", "assistant", "error"} or not isinstance(
            content, str
        ):
            continue
        kept = content[-remaining:]
        recent.insert(
            0,
            {
                "id": message.get("id"),
                "role": message["role"],
                "content": kept,
                "truncated": len(kept) < len(content),
            },
        )
        remaining -= len(kept)
        if not remaining:
            break
    return recent


def _validated_action(name: str, arguments: Any) -> str:
    if not isinstance(arguments, dict):
        raise _OperationError("invalid_arguments")
    action = arguments.get("action")
    fields = _TOOL_FIELDS.get(name, {}).get(action) if isinstance(action, str) else None
    if fields is None or any(key != "action" and key not in fields for key in arguments):
        raise _OperationError("invalid_arguments")
    return str(action)


def _find(items: list[JsonObject], key: str, value: Any) -> JsonObject | None:
    if not isinstance(value, str):
        return None
    for item in items:
        if item.get(key) == value:
            return item
    return None


class LiveToolExecutor:
    """Run ``vbot_app`` and ``vbot_terminal`` operations for one Live call.

    The owner serializes executions per call. ``is_active`` turns false once
    the call stops or is replaced; multi-step operations check it before each
    further effect and report confirmed partial results.
    """

    def __init__(
        self,
        *,
        rpc: RpcInvoker,
        ui: UiRequester,
        is_active: Callable[[], bool],
    ) -> None:
        self._rpc = rpc
        self._ui = ui
        self._is_active = is_active
        self._validated_sessions: OrderedDict[tuple[str, str], None] = OrderedDict()

    async def execute(self, name: str, arguments: Any) -> JsonObject:
        """Return the operation result, or ``{"ok": false, "error": ...}`` on failure."""
        try:
            self._ensure_active()
            action = _validated_action(name, arguments)
            if name == LIVE_TOOL_APP:
                return await self._app(action, arguments)
            return await self._terminal(action, arguments)
        except _OperationError as failure:
            return _failure(failure.code, failure.message, uncertain=False)
        except LiveUiError as exc:
            return _failure(exc.code, exc.message, uncertain=exc.uncertain)
        except RpcError as exc:
            return _failure(exc.code, exc.message, uncertain=False)
        except Exception:
            _LOGGER.exception("Live Tool operation failed unexpectedly (tool=%s)", name)
            return _failure(OPERATION_FAILED, _FAILURE_MESSAGES[OPERATION_FAILED], uncertain=True)

    def _ensure_active(self) -> None:
        if not self._is_active():
            raise _OperationError(VOICE_STOPPED)

    async def _view(self, op: str, **arguments: Any) -> JsonObject:
        return await self._ui(UI_ACTION_TERMINAL_VIEW, {"op": op, **arguments})

    async def _refresh_after(
        self, result: JsonObject, op: str = "refresh", **args: Any
    ) -> JsonObject:
        """Report a completed mutation even when the following layout update fails."""
        try:
            self._ensure_active()
            await self._view(op, **args)
        except (_OperationError, LiveUiError):
            return {**result, "layout_error": REFRESH_FAILED}
        except Exception:
            _LOGGER.exception("Live Terminal layout refresh failed unexpectedly")
            return {**result, "layout_error": REFRESH_FAILED}
        return result

    # -- vbot_app ---------------------------------------------------------

    async def _app(self, action: str, args: JsonObject) -> JsonObject:
        if action == "context":
            return await self._ui(UI_ACTION_CONTEXT, {})
        if action == "sessions":
            agent_id = _required(args.get("agent_id"))
            return await self._rpc(
                "session.list", {"agent_id": agent_id, "limit": _SESSION_LIST_LIMIT}
            )
        if action == "open":
            return await self._open(args)
        target = await self._chat_target(args)
        if action == "read":
            history = await self._rpc("chat.history", {**target, "limit": _CHAT_READ_LIMIT})
            return {**target, "messages": _recent_messages(history.get("messages") or [])}
        text = _required(args.get("text"))
        if len(text) > _MAX_TEXT_CHARS:
            raise _OperationError("text_too_long")
        self._ensure_active()
        result = await self._rpc(
            "chat.stream",
            {**target, "content": text, "input_origin": CHAT_INPUT_ORIGIN_SPEECH_TRANSCRIPTION},
        )
        return {
            **target,
            **{
                key: value
                for key, value in result.items()
                if key not in _SEND_RESULT_OMITTED_FIELDS
            },
        }

    async def _open(self, args: JsonObject) -> JsonObject:
        view = args.get("view")
        if view not in {"chat", "terminals"}:
            raise _OperationError("invalid_view")
        target: JsonObject = {}
        if args.get("agent_id") or args.get("session_id"):
            if view != "chat":
                raise _OperationError("invalid_arguments")
            target = await self._chat_target(args)
        self._ensure_active()
        outcome = await self._ui(UI_ACTION_OPEN, {"view": view, **target})
        if outcome.get("applied") is not True:
            raise _OperationError(NAVIGATION_NOT_APPLIED)
        return {"view": view, **target}

    async def _chat_target(self, args: JsonObject) -> JsonObject:
        agent_id = _required(args.get("agent_id"))
        session_id = _required(args.get("session_id"))
        key = (agent_id, session_id)
        if key in self._validated_sessions:
            self._validated_sessions.move_to_end(key)
        else:
            # The canonical history read proves the exact address exists.
            await self._rpc(
                "chat.history", {"agent_id": agent_id, "session_id": session_id, "limit": 1}
            )
            self._validated_sessions[key] = None
            while len(self._validated_sessions) > _VALIDATED_SESSION_LIMIT:
                self._validated_sessions.popitem(last=False)
        self._ensure_active()
        return {"agent_id": agent_id, "session_id": session_id}

    # -- vbot_terminal ----------------------------------------------------

    async def _terminal(self, action: str, args: JsonObject) -> JsonObject:
        if action == "create_group":
            name = _required(args.get("name"))
            if len(name) > _MAX_NAME_CHARS:
                raise _OperationError("invalid_name")
            created = await self._rpc("terminal.group.create", {"name": name})
            return await self._refresh_after(
                created, "show_group", group_id=created["group"]["group_id"]
            )
        catalog = await self._rpc("terminal.list", {})
        self._ensure_active()
        terminals = [item for item in catalog.get("terminals") or [] if isinstance(item, dict)]
        groups = [item for item in catalog.get("groups") or [] if isinstance(item, dict)]
        if action == "list":
            return await self._list(terminals, groups)
        if action == "restore":
            return await self._view("restore")
        if action in {"show_group", "rename_group", "delete_group"}:
            return await self._group_action(action, args, groups)
        if action == "reorder":
            return await self._reorder(args, terminals, groups)
        if action == "start":
            return await self._start(args, groups)
        terminal = _find(terminals, "terminal_id", args.get("terminal_id"))
        if terminal is None:
            raise _OperationError("terminal_not_found")
        if action in {"show", "maximize"}:
            return await self._view(action, terminal_id=terminal["terminal_id"])
        if action == "close":
            return await self._close(terminal)
        if not _is_coding_terminal(terminal):
            raise _OperationError("not_a_coding_terminal")
        if action == "read":
            snapshot = await self._rpc("terminal.read", {"terminal_id": terminal["terminal_id"]})
            screen = str(snapshot.get("screen") or "")
            return {
                "terminal": _terminal_summary(snapshot.get("terminal") or {}),
                "screen": screen[-_MAX_SCREEN_CHARS:],
                "truncated": len(screen) > _MAX_SCREEN_CHARS,
            }
        return await self._input(terminal, args)

    async def _list(self, terminals: list[JsonObject], groups: list[JsonObject]) -> JsonObject:
        result: JsonObject = {
            "terminals": [_terminal_summary(item) for item in terminals],
            "groups": groups,
        }
        try:
            result["layout"] = await self._view("context")
        except LiveUiError as exc:
            # The catalog stays useful when no window can describe its layout.
            result["layout_error"] = exc.code
        return result

    async def _group_action(
        self, action: str, args: JsonObject, groups: list[JsonObject]
    ) -> JsonObject:
        group_id = _required(args.get("group_id"))
        group = _find(groups, "group_id", group_id)
        if group is None:
            raise _OperationError("group_not_found")
        if action == "show_group":
            return await self._view("show_group", group_id=group_id)
        if group.get("kind") not in _EDITABLE_GROUP_KINDS:
            raise _OperationError("group_not_editable")
        if action == "rename_group":
            name = _required(args.get("name"))
            if len(name) > _MAX_NAME_CHARS:
                raise _OperationError("invalid_name")
            result = await self._rpc("terminal.group.rename", {"group_id": group_id, "name": name})
        else:
            result = await self._rpc("terminal.group.delete", {"group_id": group_id})
        return await self._refresh_after(result)

    async def _reorder(
        self, args: JsonObject, terminals: list[JsonObject], groups: list[JsonObject]
    ) -> JsonObject:
        group_id = args.get("group_id")
        group = _find(groups, "group_id", group_id)
        members = [
            item.get("terminal_id") for item in terminals if item.get("group_id") == group_id
        ]
        order = args.get("order")
        if (
            group is None
            or group.get("kind") not in _EDITABLE_GROUP_KINDS
            or not isinstance(order, list)
            or len(order) != len(members)
            or any(not isinstance(item, str) for item in order)
            or len(set(order)) != len(members)
            or any(item not in members for item in order)
        ):
            raise _OperationError("invalid_order")
        result = await self._rpc(
            "terminal.group.order", {"group_id": group["group_id"], "order": order}
        )
        return await self._refresh_after(result)

    async def _start(self, args: JsonObject, groups: list[JsonObject]) -> JsonObject:
        program = args.get("program")
        if program not in _CODING_PROGRAMS:
            raise _OperationError("unsupported_program")
        count = _launch_count(args.get("count", 1))
        workdir = _required(args.get("workdir"))
        name = args.get("name")
        if "name" in args and (not isinstance(name, str) or len(name) > _MAX_NAME_CHARS):
            raise _OperationError("invalid_name")
        group_id = args.get("group_id")
        if group_id and not any(
            group.get("group_id") == group_id and group.get("kind") in _EDITABLE_GROUP_KINDS
            for group in groups
        ):
            raise _OperationError("group_not_found")
        completed: list[JsonObject] = []
        try:
            if not group_id:
                group_id = await self._program_group(_CODING_PROGRAMS[program], groups)
            for _ in range(count):
                self._ensure_active()
                params: JsonObject = {"command": program, "workdir": workdir, "group_id": group_id}
                if name:
                    params["name"] = name
                started = await self._rpc("terminal.start", params)
                completed.append(_terminal_summary(started["terminal"]))
        except Exception as exc:
            if not isinstance(exc, (_OperationError, RpcError)):
                _LOGGER.exception("Live Terminal launch failed unexpectedly")
            code, message = _error_fields(exc)
            # Confirmed launches stay reported; the batch is never replayed.
            return {
                "ok": False,
                "requested_count": count,
                "completed": completed,
                "group_id": group_id,
                "error": {"code": code, "message": message, "delivery_uncertain": True},
            }
        try:
            self._ensure_active()
            layout = await self._view("show", terminal_id=completed[0]["terminal_id"])
        except Exception as exc:
            if not isinstance(exc, (_OperationError, LiveUiError)):
                _LOGGER.exception("Live Terminal navigation failed unexpectedly")
            return {"completed": completed, "layout_error": NAVIGATION_NOT_APPLIED}
        return {"completed": completed, "layout": layout}

    async def _program_group(self, label: str, groups: list[JsonObject]) -> str:
        for group in groups:
            if (
                str(group.get("name") or "").lower() == label.lower()
                and group.get("kind") in _EDITABLE_GROUP_KINDS
            ):
                return str(group["group_id"])
        created = await self._rpc("terminal.group.create", {"name": label})
        return str(created["group"]["group_id"])

    async def _close(self, terminal: JsonObject) -> JsonObject:
        """Stop the exact Terminal, then forget it, like the app's close button."""
        terminal_id = terminal["terminal_id"]
        stopped = terminal.get("state") in _FINISHED_TERMINAL_STATES
        removed = False
        try:
            if not stopped:
                await self._rpc("terminal.kill", {"terminal_id": terminal_id})
                stopped = True
            self._ensure_active()
            await self._rpc("terminal.forget", {"terminal_id": terminal_id})
            removed = True
        except Exception as exc:
            if not isinstance(exc, (_OperationError, RpcError)):
                _LOGGER.exception("Live Terminal close failed unexpectedly")
            code, message = _error_fields(exc)
            return {
                "terminal_id": terminal_id,
                # None marks an outcome that may or may not have happened.
                "stopped": stopped or None,
                "removed": None if stopped and code != VOICE_STOPPED else False,
                "ok": False,
                "error": {"code": code, "message": message, "delivery_uncertain": True},
            }
        return await self._refresh_after(
            {"terminal_id": terminal_id, "stopped": stopped, "removed": removed}
        )

    async def _input(self, terminal: JsonObject, args: JsonObject) -> JsonObject:
        key = args.get("key")
        if "key" in args and ("text" in args or "submit" in args or key not in _KEYS):
            raise _OperationError("invalid_input")
        if "submit" in args and not isinstance(args["submit"], bool):
            raise _OperationError("invalid_input")
        terminal_id = terminal["terminal_id"]
        snapshot = await self._rpc("terminal.read", {"terminal_id": terminal_id})
        self._ensure_active()
        if "key" in args:
            data = _KEYS[str(key)]
        else:
            text = _required(args.get("text"))
            if len(text) > _MAX_TEXT_CHARS or any(
                ord(char) < 32 and char not in "\t\r\n" for char in text
            ):
                raise _OperationError("invalid_input")
            data = (
                f"\x1b[200~{text}\x1b[201~"
                if snapshot.get("bracketed_paste") and re.search(r"[\r\n]", text)
                else text
            )
            if args.get("submit") is not False:
                data += "\r"
        params: JsonObject = {"terminal_id": terminal_id, "data": data}
        revision = (snapshot.get("terminal") or {}).get("screen_revision")
        if isinstance(revision, int) and not isinstance(revision, bool):
            # The manager rejects the input if the screen changed since this read.
            params["expected_screen_revision"] = revision
        result = await self._rpc("terminal.input", params)
        return {"terminal": _terminal_summary(result.get("terminal") or {})}
