"""Fakes for the Live Tool tests: the app behind the RPC seam, its owning window and a clock."""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import UTC, datetime
from typing import Any

from server.live._context import LiveUiError
from server.live._memory import LiveMemory
from server.live._terminals import TerminalTimings
from server.live._tools import LiveToolExecutor
from server.rpc.errors import RpcError

JsonObject = dict[str, Any]

CALL_START = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
BEFORE = "2026-09-25T09:00:00+00:00"
AFTER = "2026-09-25T10:05:00+00:00"

# The vBot Project folder in the host's path grammar, and how Live Tools show it to the Model.
PROJECT_FOLDER, PROJECT_FOLDER_SHOWN = (
    ("C:\\work\\vbot", "C:/work/vbot") if sys.platform == "win32" else ("/work/vbot", "/work/vbot")
)

SHELL = "PowerShell 7.6.6\nPS C:\\work\\vbot>"
CODEX_HEADER = (
    "╭──────────────────────────────╮\n"
    "│ >_ OpenAI Codex (v0.153.2)   │\n"
    "│ model:     {model}           │\n"
    "╰──────────────────────────────╯\n"
)
CODEX_LOADING = (
    SHELL + " codex\n" + CODEX_HEADER.format(model="loading") + "\n› Ask Codex to do anything\n"
)
CODEX_READY = (
    SHELL + " codex\n" + CODEX_HEADER.format(model="gpt-5") + "\n› Ask Codex to do anything\n"
)
CODEX_UPDATE = (
    "PowerShell 7.6.6\n  ✨ Update available! 0.153.2 -> 0.157.0\n\n"
    "› 1. Update now\n  2. Skip\n  3. Skip until next version\n\n  Press enter to continue"
)
CLAUDE_READY = (
    SHELL + " claude\n Claude Code v2.1.280\n────\n❯\xa0Try something\n────\n  ⏵⏵ auto mode on"
)
CLAUDE_TRUST = (
    SHELL + " claude\n Quick safety check\n ❯ No, exit\n   Yes, I trust this folder\n"
    " Enter to confirm · Esc to cancel"
)
STALE = RpcError(
    "invalid_request", "Terminal screen changed; inspect status before sending this input"
)


def session_row(session_id: str, agent: str, **fields: Any) -> JsonObject:
    return {
        "id": session_id,
        "agent_address": agent,
        "created_at": BEFORE,
        "last_active_at": BEFORE,
        "has_active_run": False,
        **fields,
    }


class FakeApp:
    """In-process RPC double holding a small app: Agents, Sessions, Terminals."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, JsonObject]] = []
        self.agents = [
            {"id": "main", "name": "Main"},
            {"id": "coder", "name": "Coder"},
            {"id": "writer", "name": "Writer"},
        ]
        self.projects = [{"project_id": "vbot", "display_name": "vBot", "cwd": PROJECT_FOLDER}]
        self.teams = {"vbot": [{"agent_id": "reviewer", "display_name": "Reviewer"}]}
        self.sessions: list[JsonObject] = []
        self.histories: dict[str, JsonObject] = {}
        self.results: dict[str, str] = {}
        self.terminals: list[JsonObject] = []
        self.groups: list[JsonObject] = [
            {"group_id": "grp_mine", "name": "Mine", "kind": "user"},
            {"group_id": "grp_done", "name": "Finished", "kind": "finished"},
        ]
        self.screens: dict[str, list[str]] = {}
        self.inputs: list[tuple[str, str, int | None]] = []
        self.failures: dict[str, list[Exception | None]] = {}
        self.queued = False
        self.created = 0

    def fail(self, method: str, *errors: Exception | None) -> None:
        """Answer the next calls of *method* with these errors; ``None`` lets one pass."""
        self.failures[method] = list(errors)

    def count(self, method: str) -> int:
        return sum(1 for name, _params in self.calls if name == method)

    def params(self, method: str) -> list[JsonObject]:
        return [params for name, params in self.calls if name == method]

    def effects(self) -> list[str]:
        reads = {
            "agent.list",
            "project.list",
            "project.show",
            "session.list",
            "chat.history",
            "chat.run_result",
            "terminal.list",
            "terminal.read",
        }
        return [name for name, _params in self.calls if name not in reads]

    def add_terminal(self, terminal_id: str, command: str = "codex", **fields: Any) -> JsonObject:
        terminal = {
            "terminal_id": terminal_id,
            "group_id": "grp_mine",
            "name": None,
            "state": "ready",
            "command": "pwsh.exe",
            "launch_command": command,
            "workdir": PROJECT_FOLDER,
            "screen_revision": 5,
            **fields,
        }
        self.terminals.append(terminal)
        return terminal

    async def __call__(self, method: str, params: JsonObject) -> JsonObject:
        self.calls.append((method, dict(params)))
        pending = self.failures.get(method)
        if pending:
            error = pending.pop(0)
            if error is not None:
                raise error
        handler = getattr(self, "_" + method.replace(".", "_"))
        return handler(params)  # type: ignore[no-any-return]

    def _agent_list(self, _params: JsonObject) -> JsonObject:
        return {"agents": self.agents}

    def _project_list(self, _params: JsonObject) -> JsonObject:
        return {"projects": self.projects}

    def _project_show(self, params: JsonObject) -> JsonObject:
        return {"project": {}, "scan": {"team": self.teams.get(params["project_id"], [])}}

    def _session_list(self, params: JsonObject) -> JsonObject:
        addresses = params.get("agent_ids") or [params["agent_id"]]
        rows = [
            row
            for row in self.sessions
            if row["agent_address"] in addresses
            and (params.get("include_cron", True) or "cron" not in row.get("run_kinds", ()))
            and (params.get("include_channels", True) or not row.get("platform_conv_id"))
        ]
        rows.sort(key=lambda row: row["last_active_at"], reverse=True)
        return {"sessions": rows[: params["limit"]]}

    def _session_create(self, params: JsonObject) -> JsonObject:
        self.created += 1
        session_id = f"ses_new{self.created}"
        self.sessions.append(session_row(session_id, params["agent_id"], last_active_at=AFTER))
        return {"agent_id": params["agent_id"].split("@")[0], "session_id": session_id}

    def _chat_stream(self, params: JsonObject) -> JsonObject:
        for row in self.sessions:
            if row["id"] == params["session_id"]:
                row["has_active_run"] = True
        if self.queued:
            return {"queued": True, "item": {"item_id": "q1"}}
        return {"run_id": "run_1", "status": "running", "events": [], "sse_url": "/x"}

    def _chat_history(self, params: JsonObject) -> JsonObject:
        return self.histories.get(params["session_id"], {"messages": []})

    def _chat_run_result(self, params: JsonObject) -> JsonObject:
        return {"content": self.results.get(params["run_id"], ""), "truncated": False}

    def _chat_cancel(self, params: JsonObject) -> JsonObject:
        return {"run_id": params["run_id"], "status": "cancelled"}

    def _terminal_list(self, _params: JsonObject) -> JsonObject:
        return {"terminals": self.terminals, "groups": self.groups, "launch_history": []}

    def _terminal_start(self, params: JsonObject) -> JsonObject:
        terminal_id = f"term_start{len(self.terminals) + 1}"
        terminal = self.add_terminal(
            terminal_id,
            params["command"],
            group_id=params["group_id"],
            workdir=params["workdir"],
            name=params.get("name"),
            state="starting",
        )
        ready = CODEX_READY if params["command"] == "codex" else CLAUDE_READY
        self.screens.setdefault(terminal_id, [SHELL, ready])
        return {"terminal": terminal, "launch_history": []}

    def _terminal_read(self, params: JsonObject) -> JsonObject:
        terminal = self._terminal(params["terminal_id"])
        script = self.screens.setdefault(params["terminal_id"], [CODEX_READY])
        screen = script.pop(0) if len(script) > 1 else script[0]
        return {"terminal": terminal, "screen": screen, "bracketed_paste": True}

    def _terminal_input(self, params: JsonObject) -> JsonObject:
        terminal = self._terminal(params["terminal_id"])
        expected = params.get("expected_screen_revision")
        self.inputs.append((params["terminal_id"], params["data"], expected))
        terminal["screen_revision"] += 1
        data = params["data"]
        if data.startswith("\x1b[200~"):
            # The program echoes pasted text on its input line.
            text = data.removeprefix("\x1b[200~").removesuffix("\x1b[201~")
            script = self.screens.setdefault(params["terminal_id"], [CODEX_READY])
            script[:] = [re.sub(r"^([›❯])[ \xa0].*$", rf"\1 {text}", script[-1], flags=re.M)]
        return {"terminal": terminal}

    def _terminal_kill(self, params: JsonObject) -> JsonObject:
        terminal = self._terminal(params["terminal_id"])
        terminal["state"] = "exited"
        return {"terminal": terminal}

    def _terminal_forget(self, params: JsonObject) -> JsonObject:
        terminal = self._terminal(params["terminal_id"])
        self.terminals.remove(terminal)
        return {"terminal": terminal}

    def _terminal_group_create(self, params: JsonObject) -> JsonObject:
        group = {"group_id": f"grp_new{len(self.groups)}", "name": params["name"], "kind": "user"}
        self.groups.append(group)
        return {"group": group}

    def _terminal_group_rename(self, params: JsonObject) -> JsonObject:
        return {"group": {"group_id": params["group_id"], "name": params["name"]}}

    def _terminal_group_delete(self, params: JsonObject) -> JsonObject:
        return {"group_id": params["group_id"], "terminals_killed": 2}

    def _terminal_group_order(self, params: JsonObject) -> JsonObject:
        return {"group_id": params["group_id"], "order": params["order"]}

    def _terminal(self, terminal_id: str) -> JsonObject:
        for terminal in self.terminals:
            if terminal["terminal_id"] == terminal_id:
                return terminal
        raise RpcError("invalid_request", f"Terminal Session not found: {terminal_id}")


class FakeUi:
    """The owning app window: its navigation and Terminal layout."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, JsonObject]] = []
        self.error: LiveUiError | None = None
        self.applied = True

    async def __call__(self, action: str, args: JsonObject) -> JsonObject:
        self.requests.append((action, args))
        if self.error is not None:
            raise self.error
        if action == "open":
            return {"applied": self.applied}
        return {}

    def of(self, action: str) -> list[JsonObject]:
        return [args for name, args in self.requests if name == action]


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        # Let other Tool executions run, as a real wait would.
        await asyncio.sleep(0)


class Fixture:
    def __init__(self, memory: LiveMemory | None = None) -> None:
        self.app = FakeApp()
        self.ui = FakeUi()
        self.clock = FakeClock()
        self.active = True
        self.ended = 0
        # What the app window last reported it shows; ``None`` before any report.
        self.context: JsonObject | None = {
            "view": "chat",
            "selected_agent_id": "main",
            "selected_project_id": "vbot",
            "chat_session": None,
        }
        self.executor = LiveToolExecutor(
            rpc=self.app,
            ui=self.ui,
            app_context=lambda: self.context,
            is_active=lambda: self.active,
            started_at=CALL_START,
            end_call=self._end_call,
            memory=memory,
            timings=TerminalTimings(),
            sleep=self.clock.sleep,
            clock=self.clock,
        )

    def _end_call(self) -> None:
        self.ended += 1

    async def call(self, tool: str, **arguments: Any) -> JsonObject:
        return await self.executor.execute(tool, arguments)

    async def ok(self, tool: str, **arguments: Any) -> str:
        result = await self.call(tool, **arguments)
        assert result["ok"] is True, result
        return str(result["data"]["content"])

    async def failed(self, tool: str, **arguments: Any) -> tuple[str, str]:
        result = await self.call(tool, **arguments)
        assert result["ok"] is False, result
        return str(result["error"]["code"]), str(result["error"]["message"])

    async def partial(self, tool: str, **arguments: Any) -> str:
        """A call that did part of its work: the failure message says which part."""
        code, message = await self.failed(tool, **arguments)
        assert code == "partial", message
        return message
