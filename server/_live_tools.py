"""The Live Tools a voice call runs, acting as the app's user.

Each Tool call arrives as one canonical, validated call (see
``core/model_tasks/_live_arguments.py``) and returns a Tool Result envelope
whose content is short plain text. Live starts ordinary top-level Sessions and
Terminals, never subagents, and reads or changes the app only through vBot's
canonical RPCs and the owner's UI requests. A failure says what was wrong and
names the next valid call; an effect is never replayed, and a failure after a
possible effect says so.

UI requests to the owning accessor (``{"type": "ui_request", "action", "args"}``):

* ``context``: the app selection (``view``, ``selected_agent_id``,
  ``selected_project_id``), ``agents`` (``agent_id``, ``name``), ``projects``
  (``project_id``, ``name``, ``cwd``), and ``selected_project_team``
  (``agent_id`` as an ``agent_id@project_id`` address, ``name``);
* ``open``: ``{"view": "chat" | "terminals" | "agents" | "projects"}``, with
  ``agent_id`` and ``session_id`` for a chat Session, ``agent_id`` for an Agent
  page, or ``project_id`` for a Project page; answers ``{"applied": bool}``;
* ``terminal_view``: ``{"op": "show" | "maximize" | "show_group" | "restore" |
  "refresh", "terminal_id"?, "group_id"?}``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from core.model_tasks.live import (
    CODING_PROGRAMS,
    LIVE_READ_ONLY_TOOLS,
    TOOL_OPEN,
    TOOL_OVERVIEW,
    TOOL_READ,
    TOOL_SEND_MESSAGE,
    TOOL_START_AGENT_SESSION,
    TOOL_START_CODING_TERMINAL,
    TOOL_STOP,
    TOOL_TERMINAL,
    live_failure,
    live_success,
)
from core.tools._call_vocabulary import spelling
from server._live_context import (
    NAVIGATION_NOT_APPLIED,
    OPERATION_FAILED,
    UI_ACTION_OPEN,
    UNCERTAIN_DELIVERY,
    JsonObject,
    LiveContext,
    LiveToolError,
    LiveUiError,
    RpcInvoker,
    UiRequester,
    join_words,
    text_field,
)
from server._live_targets import (
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
    Target,
    agent_spellings,
    resolve_target,
    session_key,
)
from server._live_terminals import LiveTerminals, TerminalTimings, terminal_line
from server.rpc.errors import RpcError
from server.rpc.validation import CHAT_INPUT_ORIGIN_SPEECH_TRANSCRIPTION

_LOGGER = logging.getLogger("vbot.server.live")

_LIST_CAP = 12
_MAX_CUT_WORD_CHARS = 40
_EXCERPT_CHARS = 160
_CHAT_READ_LIMIT = 20
_MAX_CHAT_CONTEXT_CHARS = 8_000
# A final message ending in a question mark (before closing quotes or
# brackets) is taken as a question to the user; a heuristic, not a Run state.
_QUESTION_END = re.compile(r"\?[\s\"')\]*_]*$")
_FAILED_COMPLETIONS = {"failed": "failed", "interrupted": "interrupted"}

Handler = Callable[[JsonObject, LiveCatalog], Awaitable[JsonObject]]


class LiveToolExecutor:
    """Run the Live Tools for one call and keep the call's ref table.

    The owner serializes executions per call. ``is_active`` turns false once
    the call stops or is replaced; multi-step operations check it before each
    further effect. ``started_at`` bounds the recently finished Sessions
    ``overview`` shows.
    """

    def __init__(
        self,
        *,
        rpc: RpcInvoker,
        ui: UiRequester,
        is_active: Callable[[], bool],
        started_at: datetime,
        timings: TerminalTimings | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ctx = LiveContext(rpc=rpc, ui=ui, is_active=is_active)
        self._refs = LiveRefs()
        self._started_at = started_at
        self._terminals = LiveTerminals(
            self._ctx, self._refs, timings=timings or TerminalTimings(), sleep=sleep, clock=clock
        )
        self._handlers: dict[str, Handler] = {
            TOOL_OVERVIEW: self._overview,
            TOOL_START_AGENT_SESSION: self._start_agent_session,
            TOOL_START_CODING_TERMINAL: self._start_coding_terminal,
            TOOL_SEND_MESSAGE: self._send_message,
            TOOL_READ: self._read,
            TOOL_STOP: self._stop,
            TOOL_OPEN: self._open,
            TOOL_TERMINAL: self._terminal,
        }

    def session_ref(self, address: str, session_id: str) -> str:
        """The call's ref for a Session, assigned on first mention."""
        return self._refs.session(SessionKey(address=address, session_id=session_id))

    async def execute(self, name: str, arguments: JsonObject) -> JsonObject:
        """Run one canonical Live Tool call and return its Tool Result envelope."""
        handler = self._handlers.get(name)
        if handler is None:
            return live_failure(
                "unknown_tool",
                f'There is no Tool called "{name}". Call one of: {", ".join(self._handlers)}.',
            )
        try:
            self._ctx.ensure_active()
            return await handler(dict(arguments), LiveCatalog(self._ctx, self._refs))
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
            return live_success(await self._agent_overview(target.agent, catalog))
        selection = await catalog.selection()
        agents = await catalog.agents()
        projects = await catalog.projects()
        selected_project = await catalog.selected_project()
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
                f"{project.name} ({project.folder})" if project.folder else project.name
                for project in projects
            ]
            lines.append(f"Projects: {_capped(listed)}.")
        lines.append(await self._sessions_block(catalog))
        lines.append(await self._terminals_block(catalog))
        return live_success("\n".join(lines))

    async def _selection_line(
        self, selection: JsonObject, project: LiveProject | None, catalog: LiveCatalog
    ) -> str:
        parts = [f"App: {selection.get('view') or 'unknown'} view"]
        agent_id = selection.get("selected_agent_id")
        if isinstance(agent_id, str) and agent_id:
            parts.append(f"selected Agent: {await catalog.agent_name(agent_id)}")
        if project is not None:
            parts.append(f"selected Project: {project.name}")
        return "; ".join(parts) + "."

    async def _sessions_block(self, catalog: LiveCatalog) -> str:
        sessions = await catalog.sessions()
        touched = set(self._refs.touched())
        running = [item for item in sessions if item.get("has_active_run") is True]
        finished = [
            item
            for item in sessions
            if item.get("has_active_run") is not True
            and (session_key(item) in touched or self._recent(item))
        ]
        shown = (running + finished)[:_LIST_CAP]
        if not shown:
            return "Sessions: none running or finished since the call started."
        lines = ["Sessions (running, then recently finished):"]
        lines += [await self._session_line(item, catalog) for item in shown]
        hidden = len(running) + len(finished) - len(shown)
        if hidden:
            lines.append(f"- and {hidden} more")
        return "\n".join(lines)

    async def _agent_overview(self, agent: LiveAgent, catalog: LiveCatalog) -> str:
        sessions = [
            item for item in await catalog.sessions() if item["agent_address"] == agent.address
        ]
        if not sessions:
            return f"{agent.label} has no Sessions."
        shown = sessions[:_LIST_CAP]
        lines = [f"Sessions of {agent.label}, most recent first:"]
        lines += [await self._session_line(item, catalog) for item in shown]
        if len(sessions) > len(shown):
            lines.append(f"- and {len(sessions) - len(shown)} older")
        return "\n".join(lines)

    async def _session_line(self, item: JsonObject, catalog: LiveCatalog) -> str:
        key = session_key(item)
        ref = self._refs.session(key)
        name = await catalog.agent_name(key.address)
        title = str(item.get("title") or item.get("auto_title") or "").strip()
        head = f'- {ref} {name} "{title}"' if title else f"- {ref} {name}"
        if item.get("has_active_run") is True:
            return f"{head}: working"
        status = _FAILED_COMPLETIONS.get(str(item.get("unread_run_status") or ""))
        if status is not None:
            return f"{head}: {status}"
        excerpt = await self._final_message(key, item)
        if not excerpt:
            return f"{head}: finished"
        state = "waiting for an answer" if _QUESTION_END.search(excerpt) else "finished"
        return f'{head}: {state}: "{_excerpt(excerpt)}"'

    async def _final_message(self, key: SessionKey, item: JsonObject) -> str:
        run_id = item.get("latest_completion_run_id")
        if not isinstance(run_id, str) or not run_id:
            return ""
        try:
            result = await self._ctx.call(
                "chat.run_result",
                {"agent_id": key.address, "session_id": key.session_id, "run_id": run_id},
            )
        except RpcError:
            return ""
        return str(result.get("content") or "").strip()

    async def _terminals_block(self, catalog: LiveCatalog) -> str:
        terminals = await catalog.terminals()
        if not terminals:
            return "Terminals: none."
        live = [item for item in terminals if item.get("state") not in {"exited", "error"}]
        ordered = live + [item for item in terminals if item not in live]
        shown = ordered[:_LIST_CAP]
        lines = ["Terminals:"]
        lines += [
            f"- {terminal_line(self._refs.terminal(str(item['terminal_id'])), item)}"
            for item in shown
        ]
        if len(ordered) > len(shown):
            lines.append(f"- and {len(ordered) - len(shown)} more")
        return "\n".join(lines)

    def _recent(self, item: JsonObject) -> bool:
        active = _timestamp(item.get("last_active_at"))
        return active is not None and active >= self._started_at

    # -- start_agent_session ------------------------------------------------

    async def _start_agent_session(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        agent = await self._start_agent(
            text_field(arguments, "agent"), text_field(arguments, "project"), catalog
        )
        task = text_field(arguments, "task")
        count = arguments.get("count", 1)
        count = count if isinstance(count, int) and count >= 1 else 1
        started: list[str] = []
        queued: list[str] = []
        for _ in range(count):
            try:
                self._ctx.ensure_active()
                created = await self._ctx.call("session.create", {"agent_id": agent.address})
            except Exception as exc:
                return self._start_failure(agent, count, started, exc, created_ref=None)
            key = SessionKey(address=agent.address, session_id=str(created["session_id"]))
            ref = self._refs.touch(key)
            try:
                self._ctx.ensure_active()
                result = await self._send(key, task)
            except Exception as exc:
                return self._start_failure(agent, count, started, exc, created_ref=ref)
            started.append(ref)
            if result.get("queued") is True:
                queued.append(ref)
        if count == 1:
            text = (
                f"Started a Session at {agent.label} with the task: {started[0]}. It works in the "
                "background; an update follows when it finishes."
            )
        else:
            text = (
                f"Started {count} Sessions at {agent.label} with the task: "
                f"{', '.join(started)}. They work in the background; an update follows when "
                "each finishes."
            )
        if queued:
            verbs = ("waits", "starts") if len(queued) == 1 else ("wait", "start")
            text += (
                f" {join_words(queued)} {verbs[0]} in the Queue and {verbs[1]} when a slot is free."
            )
        return live_success(text)

    def _start_failure(
        self,
        agent: LiveAgent,
        count: int,
        started: list[str],
        exc: Exception,
        *,
        created_ref: str | None,
    ) -> JsonObject:
        uncertain = not isinstance(exc, LiveToolError | RpcError)
        if uncertain:
            _LOGGER.exception("Live Session start failed unexpectedly")
        reason = exc.message if isinstance(exc, LiveToolError | RpcError) else "It failed."
        parts = []
        if started:
            parts.append(
                f"Started {len(started)} of {count} Sessions at {agent.label}: "
                f"{join_words(started)}."
            )
        if created_ref is not None:
            parts.append(
                f"{created_ref} was created, but the task was not delivered to it: {reason}"
            )
        else:
            parts.append(f"Starting Session {len(started) + 1} of {count} failed: {reason}")
        parts.append("Nothing was retried.")
        if uncertain:
            parts.append(UNCERTAIN_DELIVERY)
        return live_failure("partial" if started else "start_failed", " ".join(parts))

    async def _start_agent(
        self, agent_text: str, project_text: str, catalog: LiveCatalog
    ) -> LiveAgent:
        if not project_text:
            target = await resolve_target(
                agent_text,
                {AGENT},
                tool=TOOL_START_AGENT_SESSION,
                field="agent",
                refs=self._refs,
                catalog=catalog,
            )
            assert target.agent is not None
            return target.agent
        project_target = await resolve_target(
            project_text,
            {PROJECT},
            tool=TOOL_START_AGENT_SESSION,
            field="project",
            refs=self._refs,
            catalog=catalog,
        )
        project = project_target.project
        assert project is not None
        team = await catalog.team(project)
        key = spelling(agent_text)
        matches = [agent for agent in team if key in agent_spellings(agent)]
        if len(matches) == 1:
            return matches[0]
        names = ", ".join(agent.name for agent in team) or "no Agents"
        own = ", ".join(agent.name for agent in await catalog.agents() if not agent.project)
        raise LiveToolError(
            "agent_not_found",
            f'The team of Project {project.name} has no single Agent called "{agent_text}". '
            f"Team: {names}. Call start_agent_session again with one of these as agent, or "
            f"without project for the user's own Agents: {own or 'none'}.",
        )

    # -- send_message, read, stop --------------------------------------------

    async def _send_message(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        text = text_field(arguments, "text")
        target = await self._conversation_target(arguments, TOOL_SEND_MESSAGE, catalog)
        if target.terminal is not None:
            return await self._terminals.send(target.terminal, text)
        key = await self._session_of(target, TOOL_SEND_MESSAGE, catalog)
        ref = self._refs.touch(key)
        name = await catalog.agent_name(key.address)
        try:
            self._ctx.ensure_active()
            result = await self._send(key, text)
        except LiveToolError:
            raise
        except RpcError as exc:
            return live_failure(exc.code, f"Nothing was sent to {ref} ({name}): {exc.message}")
        except Exception:
            _LOGGER.exception("Live message failed unexpectedly")
            return live_failure(
                "send_failed",
                f"Sending to {ref} ({name}) failed unexpectedly. {UNCERTAIN_DELIVERY}",
            )
        if result.get("queued") is True:
            return live_success(
                f"Sent to {ref} ({name}). It is still working, so the message waits in its Queue "
                "and runs next."
            )
        return live_success(f"Sent to {ref} ({name}). An update follows when it finishes.")

    async def _read(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        target = await self._conversation_target(arguments, TOOL_READ, catalog)
        if target.terminal is not None:
            return await self._terminals.read(target.terminal)
        key = await self._session_of(target, TOOL_READ, catalog)
        ref = self._refs.session(key)
        name = await catalog.agent_name(key.address)
        history = await self._ctx.call(
            "chat.history",
            {"agent_id": key.address, "session_id": key.session_id, "limit": _CHAT_READ_LIMIT},
        )
        state = "working" if history.get("active_run") else "not working"
        messages = _recent_messages(history.get("messages"))
        if not messages:
            return live_success(f"{ref} at {name} ({state}) has no messages yet.")
        blocks = []
        for message in messages:
            speaker = {"user": "User", "error": "Error"}.get(message["role"], name)
            cut = " (earlier part cut)" if message["truncated"] else ""
            quoted = "\n".join(
                f"> {line}" if line else ">" for line in message["content"].splitlines()
            )
            blocks.append(f"{speaker}{cut}:\n{quoted or '>'}")
        body = "\n".join(blocks)
        return live_success(f"{ref} at {name}, {state}. Latest messages, quoted:\n{body}")

    async def _stop(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        target = await self._conversation_target(arguments, TOOL_STOP, catalog)
        if target.terminal is not None:
            return await self._terminals.interrupt(target.terminal)
        key = await self._session_of(target, TOOL_STOP, catalog)
        ref = self._refs.session(key)
        name = await catalog.agent_name(key.address)
        history = await self._ctx.call(
            "chat.history", {"agent_id": key.address, "session_id": key.session_id, "limit": 1}
        )
        active = history.get("active_run")
        run_id = active.get("run_id") if isinstance(active, dict) else None
        if not isinstance(run_id, str) or not run_id:
            return live_success(f"{ref} ({name}) is not working on anything; nothing changed.")
        self._ctx.ensure_active()
        await self._ctx.call("chat.cancel", {"run_id": run_id})
        return live_success(
            f"Stopped the current work of {ref} ({name}). It stays open for new messages."
        )

    async def _conversation_target(
        self, arguments: JsonObject, tool: str, catalog: LiveCatalog
    ) -> Target:
        return await resolve_target(
            text_field(arguments, "target"),
            {SESSION, TERMINAL, AGENT},
            tool=tool,
            field="target",
            refs=self._refs,
            catalog=catalog,
        )

    async def _session_of(self, target: Target, tool: str, catalog: LiveCatalog) -> SessionKey:
        """The Session a target names; an Agent name must leave one clear Session."""
        if target.session is not None:
            return target.session
        agent = target.agent
        assert agent is not None
        sessions = [
            item for item in await catalog.sessions() if item["agent_address"] == agent.address
        ]
        candidates = [
            item
            for item in sessions
            if item.get("has_active_run") is True
            or (
                tool != TOOL_STOP
                and (self._refs.is_touched(session_key(item)) or self._recent(item))
            )
        ]
        if len(candidates) == 1:
            return session_key(candidates[0])
        if not candidates and tool == TOOL_READ and sessions:
            return session_key(sessions[0])
        if not candidates:
            what = "working" if tool == TOOL_STOP else "running or recent"
            raise LiveToolError(
                "no_session",
                f"{agent.label} has no {what} Session. Call overview with "
                f'{{"agent": "{agent.name}"}} to see its Sessions, then call {tool} again with a '
                "ref as target.",
            )
        listed = ", ".join(
            [
                (await self._session_line(item, catalog)).removeprefix("- ")
                for item in candidates[:8]
            ]
        )
        raise LiveToolError(
            "ambiguous_target",
            f"{agent.label} has several Sessions: {listed}. Ask the user which one they mean, "
            f"then call {tool} again with its ref as target.",
        )

    async def _send(self, key: SessionKey, text: str) -> JsonObject:
        return await self._ctx.call(
            "chat.stream",
            {
                "agent_id": key.address,
                "session_id": key.session_id,
                "content": text,
                "input_origin": CHAT_INPUT_ORIGIN_SPEECH_TRANSCRIPTION,
            },
        )

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
                    '"s2"} or {"view": "terminals"}; views are chat, terminals, agents, projects.',
                )
            await self._navigate({"view": view})
            return live_success(f"Opened the {view} view.")
        target = await resolve_target(
            target_text,
            {SESSION, TERMINAL, GROUP, AGENT, PROJECT},
            tool=TOOL_OPEN,
            field="target",
            refs=self._refs,
            catalog=catalog,
        )
        if target.session is not None:
            return await self._open_session(target.session, catalog)
        if target.terminal is not None:
            ref = self._refs.terminal(str(target.terminal["terminal_id"]))
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
        return live_success(f"Showing {self._refs.session(key)} ({name}) in the chat.")

    async def _open_project(self, project: LiveProject) -> JsonObject:
        await self._navigate({"view": "projects", "project_id": project.project_id})
        return live_success(f"Showing the page of Project {project.name}.")

    async def _open_agent(self, agent: LiveAgent, view: str, catalog: LiveCatalog) -> JsonObject:
        project_id = agent.address.partition("@")[2]
        if view != "agents":
            sessions = [
                item for item in await catalog.sessions() if item["agent_address"] == agent.address
            ]
            if sessions:
                key = session_key(sessions[0])
                await self._navigate(
                    {"view": "chat", "agent_id": key.address, "session_id": key.session_id}
                )
                return live_success(
                    f"Showing the latest Session of {agent.label}, {self._refs.session(key)}, in "
                    "the chat."
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


def _recent_messages(messages: Any) -> list[JsonObject]:
    """The newest user, assistant, and error texts within one shared character budget."""
    remaining = _MAX_CHAT_CONTEXT_CHARS
    recent: list[JsonObject] = []
    for message in reversed(messages if isinstance(messages, list) else []):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if message.get("role") not in {"user", "assistant", "error"} or not isinstance(
            content, str
        ):
            continue
        if not content.strip():
            continue
        kept = content[-remaining:]
        remaining -= len(kept)
        truncated = len(kept) < len(content)
        if truncated:
            # Start the kept part at a word, not inside one.
            words = kept.split(None, 1)
            kept = words[1] if len(words) == 2 and len(words[0]) < _MAX_CUT_WORD_CHARS else kept
        recent.insert(0, {"role": message["role"], "content": kept, "truncated": truncated})
        if not remaining:
            break
    return recent


def _excerpt(text: str) -> str:
    """The end of a final message, where its question or result is, as one line."""
    flat = " ".join(text.split())
    if len(flat) <= _EXCERPT_CHARS:
        return flat
    return "..." + flat[-(_EXCERPT_CHARS - 3) :]


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _capped(items: list[str]) -> str:
    shown = ", ".join(items[:_LIST_CAP])
    hidden = len(items) - _LIST_CAP
    return f"{shown}, and {hidden} more" if hidden > 0 else shown
