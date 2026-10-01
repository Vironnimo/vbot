"""The Live Tools a voice call runs, acting as the app's user.

:meth:`LiveToolExecutor.run` takes a Tool call as a Model made it, prepares it
into one canonical, validated call (``_arguments.py``), runs it once, and
returns a Tool Result envelope whose content is short plain text. Live starts
ordinary top-level Sessions and Terminals, never subagents, and reads or
changes the app only through vBot's canonical RPCs and the owner's UI requests.
A failure says what was wrong and names the next valid call; an effect is
never replayed, and a failure after a possible effect says so.

UI requests to the owning accessor (``{"type": "ui_request", "action", "args"}``):

* ``open``: ``{"view": <one of LIVE_VIEWS>}``, with ``agent_id`` and
  ``session_id`` for a chat Session, ``agent_id`` for an Agent page, or
  ``project_id`` for a Project page; answers ``{"applied": bool}``;
* ``terminal_view``: ``{"op": "show" | "maximize" | "show_group" | "restore" |
  "refresh", "terminal_id"?, "group_id"?}``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from core.model_tasks.live import LiveToolRun, live_failure, live_result_text, live_success
from core.utils.paths import model_path
from server.live._arguments import run_live_call
from server.live._brief import (
    LIVE_READ_ONLY_TOOLS,
    TARGET_VIEWS,
    TOOL_END_CALL,
    TOOL_OPEN,
    TOOL_OVERVIEW,
    TOOL_READ,
    TOOL_SEND_MESSAGE,
    TOOL_START_AGENT_SESSION,
    TOOL_START_CODING_TERMINAL,
    TOOL_STOP,
    TOOL_TERMINAL,
)
from server.live._context import (
    NAVIGATION_NOT_APPLIED,
    OPERATION_FAILED,
    UI_ACTION_OPEN,
    UNCERTAIN_DELIVERY,
    AppContext,
    JsonObject,
    LiveContext,
    LiveToolError,
    LiveUiError,
    RpcInvoker,
    UiRequester,
    text_field,
)
from server.live._memory import LiveMemory
from server.live._programs import CODING_PROGRAMS
from server.live._sessions import LIST_CAP, LiveSessions
from server.live._targets import (
    AGENT,
    GROUP,
    PROJECT,
    SESSION,
    TERMINAL,
    LiveAgent,
    LiveCatalog,
    LiveProject,
    LiveRefs,
    SessionKey,
    TeamCache,
    resolve_target,
    session_key,
    session_title,
    terminal_title,
)
from server.live._terminals import LiveTerminals, TerminalTimings, terminal_line
from server.rpc.errors import RpcError

_LOGGER = logging.getLogger("vbot.server.live")

# Refs in a result text; only known ones become links.
_NAMED_REF = re.compile(r"\b[st][1-9][0-9]{0,5}\b")
_ACTION_ARGUMENT_CHARS = 200
_ACTION_RESULT_CHARS = 2_000
_ACTION_LINKS = 12
# With a target, open's view names the kind of thing to show.
_OPEN_VIEW_KINDS = {
    "chat": frozenset({SESSION, AGENT}),
    "terminals": frozenset({TERMINAL, GROUP}),
    "agents": frozenset({AGENT}),
    "projects": frozenset({PROJECT}),
}

Handler = Callable[[JsonObject, LiveCatalog], Awaitable[JsonObject]]
Recorder = Callable[[JsonObject], None]


class LiveToolExecutor:
    """Run the Live Tools for one call and keep the call's ref table.

    Executions of one call run one at a time; only a coding Terminal start
    lets the others run while it waits for the program and types the task.
    ``app_context`` returns what the app window last reported it shows.
    ``is_active`` turns false once the call stops or is replaced; multi-step
    operations check it before each further effect. ``started_at`` bounds the
    recently finished Sessions ``overview`` shows (``_sessions.py``). ``end_call``
    ends the voice call after a short goodbye. ``memory`` holds the refs and assignments
    shared with earlier and later calls (a fresh one by default). ``report``,
    when given, receives one ``action`` update per executed Tool call for the
    app (see :func:`action_update`). ``record``, when given, receives one
    record per Tool call, labeled with ``mode``.
    """

    def __init__(
        self,
        *,
        rpc: RpcInvoker,
        ui: UiRequester,
        app_context: AppContext,
        is_active: Callable[[], bool],
        started_at: datetime,
        end_call: Callable[[], None],
        memory: LiveMemory | None = None,
        report: Callable[[JsonObject], None] | None = None,
        record: Recorder | None = None,
        timings: TerminalTimings | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ctx = LiveContext(rpc=rpc, ui=ui, app_context=app_context, is_active=is_active)
        self._memory = memory or LiveMemory(clock=clock)
        self._refs = self._memory.refs
        self._teams = TeamCache(clock=clock)
        self._end_call = end_call
        self._report = report
        self._record = record
        self._clock = clock
        self.mode = "delegated"
        self._terminals = LiveTerminals(
            self._ctx, self._refs, timings=timings or TerminalTimings(), sleep=sleep, clock=clock
        )
        self._sessions = LiveSessions(self._ctx, self._refs, self._terminals, started_at=started_at)
        self._handlers: dict[str, Handler] = {
            TOOL_OVERVIEW: self._overview,
            TOOL_START_AGENT_SESSION: self._sessions.start,
            TOOL_START_CODING_TERMINAL: self._start_coding_terminal,
            TOOL_SEND_MESSAGE: self._sessions.send_message,
            TOOL_READ: self._sessions.read,
            TOOL_STOP: self._sessions.stop,
            TOOL_OPEN: self._open,
            TOOL_TERMINAL: self._terminal,
            TOOL_END_CALL: self._end,
        }

    def session_ref(self, address: str, session_id: str) -> str:
        """The Session's ref, assigned on first mention and kept across calls."""
        return self._refs.session(SessionKey(address=address, session_id=session_id))

    def known_refs(self) -> str:
        """The refs named so far, one labeled line each, most recent last."""
        return self._refs.legend()

    async def run(
        self, name: Any, arguments: Any, *, rejection: JsonObject | None = None
    ) -> LiveToolRun:
        """Prepare and run one Tool call as a Model made it; never raises for failures."""
        return await run_live_call(
            name,
            arguments,
            execute=self.execute,
            rejection=rejection,
            record=self._record,
            mode=self.mode,
            clock=self._clock,
        )

    async def current_state(self) -> str:
        """The full overview as text; it is not reported or noted as an assignment."""
        result = await self._execute(TOOL_OVERVIEW, {})
        data = result.get("data")
        if result.get("ok") is not True or not isinstance(data, dict):
            return ""
        return str(data.get("content") or "")

    async def execute(self, name: str, arguments: JsonObject) -> JsonObject:
        """Run one canonical Live Tool call and return its Tool Result envelope."""
        result = await self._execute(name, arguments)
        if name in self._handlers:
            self._memory.note(name, arguments, result)
            if self._report is not None:
                self._report(action_update(name, arguments, result, self._refs))
        return result

    async def _execute(self, name: str, arguments: JsonObject) -> JsonObject:
        handler = self._handlers.get(name)
        if handler is None:
            return live_failure(
                "unknown_tool",
                f'There is no Tool called "{name}". Call one of: {", ".join(self._handlers)}.',
            )
        catalog = LiveCatalog(self._ctx, self._refs, self._teams)
        try:
            async with self._ctx.exclusive():
                self._ctx.ensure_active()
                return await handler(dict(arguments), catalog)
        except LiveToolError as exc:
            return live_failure(exc.code, exc.message)
        except LiveUiError as exc:
            return live_failure(exc.code, exc.message)
        except RpcError as exc:
            return live_failure(exc.code, f"vBot could not do this: {exc.message}")
        except Exception:
            _LOGGER.exception("Live Tool failed unexpectedly (tool=%s)", name)
            if name in LIVE_READ_ONLY_TOOLS:
                return live_failure(
                    OPERATION_FAILED,
                    f"{name} failed unexpectedly; nothing was changed. Call it again once, and "
                    "tell the user if it fails again.",
                )
            return live_failure(
                OPERATION_FAILED,
                f"The call failed unexpectedly. {UNCERTAIN_DELIVERY} Call overview to see what "
                "happened.",
            )
        finally:
            catalog.discard()

    # -- end_call -------------------------------------------------------------

    async def _end(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        self._end_call()
        return live_success(
            "The call ends in a few seconds. Say a short goodbye now; running work goes on."
        )

    # -- overview -------------------------------------------------------------

    async def _overview(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        agent_text = text_field(arguments, "agent")
        if agent_text:
            target = await resolve_target(
                agent_text,
                {AGENT},
                tool=TOOL_OVERVIEW,
                field="agent",
                refs=self._refs,
                catalog=catalog,
            )
            assert target.agent is not None
            return live_success(await self._sessions.agent_overview(target.agent, catalog))
        selection = catalog.selection()
        agents, projects, selected_project, sessions, terminals = await asyncio.gather(
            catalog.agents(),
            catalog.projects(),
            catalog.selected_project(),
            self._sessions.block(catalog),
            self._terminals_block(catalog),
        )
        lines: list[str] = []
        if selection is not None:
            lines.append(await self._selection_line(selection, selected_project, catalog))
        identity = [agent.name for agent in agents if not agent.project]
        team = [agent.name for agent in agents if agent.project]
        lines.append(f"Agents: {_capped(identity)}." if identity else "Agents: none.")
        if team and selected_project is not None:
            lines.append(f"Team of Project {selected_project.name}: {_capped(team)}.")
        if projects:
            listed = [
                f"{project.name} ({model_path(project.folder)})" if project.folder else project.name
                for project in projects
            ]
            lines.append(f"Projects: {_capped(listed)}.")
        lines += [sessions, terminals]
        return live_success("\n".join(lines))

    async def _selection_line(
        self, selection: JsonObject, project: LiveProject | None, catalog: LiveCatalog
    ) -> str:
        parts = [f"App: {selection.get('view') or 'unknown'} view"]
        shown = selection.get("chat_session")
        if (
            selection.get("view") == "chat"
            and isinstance(shown, dict)
            and isinstance(shown.get("agent_id"), str)
            and isinstance(shown.get("session_id"), str)
        ):
            name = await catalog.agent_name(shown["agent_id"])
            key = SessionKey(address=shown["agent_id"], session_id=shown["session_id"])
            parts.append(f"showing {self._refs.session(key, session_title(name))} ({name})")
        agent_id = selection.get("selected_agent_id")
        if isinstance(agent_id, str) and agent_id:
            parts.append(f"selected Agent: {await catalog.agent_name(agent_id)}")
        if project is not None:
            parts.append(f"selected Project: {project.name}")
        return "; ".join(parts) + "."

    async def _terminals_block(self, catalog: LiveCatalog) -> str:
        terminals = await catalog.terminals()
        if not terminals:
            return "Terminals: none."
        live = [item for item in terminals if item.get("state") not in {"exited", "error"}]
        ordered = live + [item for item in terminals if item not in live]
        shown = ordered[:LIST_CAP]
        running = await self._running_programs()
        lines = ["Terminals:"]
        for item in shown:
            terminal_id = str(item["terminal_id"])
            ref = self._refs.terminal(terminal_id, terminal_title(item))
            line = terminal_line(ref, item, program_running=running.get(terminal_id))
            lines.append(f"- {line}")
        if len(ordered) > len(shown):
            lines.append(f"- and {len(ordered) - len(shown)} more")
        return "\n".join(lines)

    async def _running_programs(self) -> dict[str, bool]:
        """Which Terminals still run the program they were started with; empty when unknown."""
        try:
            listed = await self._ctx.call("terminal.programs", {})
        except RpcError as exc:
            _LOGGER.warning("Live overview could not check Terminal programs (%s)", exc.code)
            return {}
        running = listed.get("running")
        if not isinstance(running, dict):
            return {}
        return {str(key): value for key, value in running.items() if isinstance(value, bool)}

    # -- start_coding_terminal, terminal -------------------------------------

    async def _start_coding_terminal(
        self, arguments: JsonObject, catalog: LiveCatalog
    ) -> JsonObject:
        program = CODING_PROGRAMS.get(text_field(arguments, "program"))
        if program is None:
            raise LiveToolError(
                "invalid_program",
                f"program must be one of {', '.join(CODING_PROGRAMS)}. Call "
                'start_coding_terminal again with {"program": "codex"} or {"program": "claude"}.',
            )
        count = arguments.get("count", 1)
        return await self._terminals.start(
            program=program,
            count=count if isinstance(count, int) and count >= 1 else 1,
            folder=text_field(arguments, "folder"),
            name=text_field(arguments, "name"),
            task=text_field(arguments, "task"),
            catalog=catalog,
        )

    async def _terminal(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        return await self._terminals.run_action(arguments, catalog)

    # -- open ----------------------------------------------------------------

    async def _open(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        target_text = text_field(arguments, "target")
        view = text_field(arguments, "view")
        if not target_text:
            if not view:
                raise LiveToolError(
                    "missing_target",
                    'open needs a target or a view. Call open again, for example with {"target": '
                    '"s2"} or {"view": "terminals"}.',
                )
            await self._navigate({"view": view})
            return live_success(f"Opened the {view} view.")
        if view and view not in TARGET_VIEWS:
            raise LiveToolError(
                "invalid_view",
                f"The {view} view shows no single thing. Call open again with only "
                f'{{"view": "{view}"}}, or with the target and one of: {", ".join(TARGET_VIEWS)}.',
            )
        target = await resolve_target(
            target_text,
            _OPEN_VIEW_KINDS.get(view, {SESSION, TERMINAL, GROUP, AGENT, PROJECT}),
            tool=TOOL_OPEN,
            field="target",
            refs=self._refs,
            catalog=catalog,
            kind_hint=(
                "Or call open again with this target and the view of the kind meant: chat (a "
                "Session), terminals (a Terminal or group), agents (an Agent), or projects (a "
                "Project)."
            ),
        )
        if target.session is not None:
            return await self._open_session(target.session, catalog)
        if target.terminal is not None:
            ref = self._refs.terminal(
                str(target.terminal["terminal_id"]), terminal_title(target.terminal)
            )
            await self._ctx.view("show", terminal_id=target.terminal["terminal_id"])
            return live_success(f"Showing {ref} in the Terminals view.")
        if target.group is not None:
            label = target.group.get("name") or target.group["group_id"]
            await self._ctx.view("show_group", group_id=target.group["group_id"])
            return live_success(f'Showing the group "{label}" in the Terminals view.')
        if target.project is not None:
            return await self._open_project(target.project)
        assert target.agent is not None
        return await self._open_agent(target.agent, view, catalog)

    async def _open_session(self, key: SessionKey, catalog: LiveCatalog) -> JsonObject:
        await self._navigate(
            {"view": "chat", "agent_id": key.address, "session_id": key.session_id}
        )
        name = await catalog.agent_name(key.address)
        ref = self._refs.session(key, session_title(name))
        return live_success(f"Showing {ref} ({name}) in the chat.")

    async def _open_project(self, project: LiveProject) -> JsonObject:
        await self._navigate({"view": "projects", "project_id": project.project_id})
        return live_success(f"Showing the page of Project {project.name}.")

    async def _open_agent(self, agent: LiveAgent, view: str, catalog: LiveCatalog) -> JsonObject:
        project_id = agent.address.partition("@")[2]
        if view != "agents":
            sessions = (await self._sessions.agent_sessions(agent, catalog))[0]
            if not sessions and view == "chat":
                raise LiveToolError(
                    "no_session",
                    f"{agent.label} has no Session to show in the chat. Call overview with "
                    f'{{"agent": "{agent.name}"}} to see its Sessions, or call open with '
                    f'{{"target": "{agent.name}", "view": "agents"}} for its page.',
                )
            if sessions:
                key = session_key(sessions[0])
                await self._navigate(
                    {"view": "chat", "agent_id": key.address, "session_id": key.session_id}
                )
                ref = self._refs.session(key, session_title(agent.label))
                return live_success(
                    f"Showing the latest Session of {agent.label}, {ref}, in the chat."
                )
        if project_id:
            await self._navigate({"view": "projects", "project_id": project_id})
            return live_success(
                f"{agent.name} belongs to the team of Project {agent.project or project_id}, "
                "which has no Agent page; showing that Project's page."
            )
        await self._navigate({"view": "agents", "agent_id": agent.address})
        return live_success(f"Showing the page of Agent {agent.name}.")

    async def _navigate(self, args: JsonObject) -> None:
        self._ctx.ensure_active()
        outcome = await self._ctx.ui(UI_ACTION_OPEN, args)
        if outcome.get("applied") is not True:
            raise LiveUiError(NAVIGATION_NOT_APPLIED)


def action_update(
    tool: str, arguments: JsonObject, result: JsonObject, refs: LiveRefs
) -> JsonObject:
    """The app's view of one executed Tool call.

    ``{"type": "action", "tool", "arguments" (string values shortened), "ok",
    "result" (the text the Model read, shortened), "links"}``; ``links`` hold
    what each known ref in the result names (see :meth:`LiveRefs.link`), in
    order of first mention.
    """
    text = live_result_text(result)
    named = dict.fromkeys(match.group(0) for match in _NAMED_REF.finditer(text))
    links = [link for ref in named if (link := refs.link(ref)) is not None]
    return {
        "type": "action",
        "tool": tool,
        "arguments": {
            key: _shortened(value, _ACTION_ARGUMENT_CHARS) for key, value in arguments.items()
        },
        "ok": result.get("ok") is True,
        "result": _shortened(text, _ACTION_RESULT_CHARS),
        "links": links[:_ACTION_LINKS],
    }


def _shortened(value: Any, limit: int) -> Any:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _capped(items: list[str]) -> str:
    shown = ", ".join(items[:LIST_CAP])
    hidden = len(items) - LIST_CAP
    return f"{shown}, and {hidden} more" if hidden > 0 else shown
