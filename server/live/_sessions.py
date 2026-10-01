"""The Session side of the Live Tools: starting, messaging, reading and stopping.

Live starts ordinary top-level Sessions (never subagents) and talks to them
through ``chat.stream`` like the user would, marked as passed on by Live voice.
A target naming an Agent selects its one clear Session; a Terminal target goes
to the Terminal side (``_terminals.py``). Session lines for ``overview`` are
built here too.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

from core.model_tasks.live import live_failure, live_success
from core.tools.call_syntax import spelling
from server.live._brief import TOOL_READ, TOOL_SEND_MESSAGE, TOOL_START_AGENT_SESSION, TOOL_STOP
from server.live._context import (
    UNCERTAIN_DELIVERY,
    JsonObject,
    LiveContext,
    LiveToolError,
    join_words,
    text_field,
)
from server.live._targets import (
    AGENT,
    PROJECT,
    SESSION,
    TERMINAL,
    LiveAgent,
    LiveCatalog,
    LiveRefs,
    SessionKey,
    Target,
    agent_spellings,
    resolve_target,
    session_key,
    session_title,
)
from server.live._terminals import LiveTerminals
from server.rpc.errors import RpcError
from server.rpc.validation import CHAT_INPUT_ORIGIN_LIVE_VOICE

_LOGGER = logging.getLogger("vbot.server.live")

# The most items one list in a result shows.
LIST_CAP = 12
_MAX_CUT_WORD_CHARS = 40
_EXCERPT_CHARS = 160
_CHAT_READ_LIMIT = 20
_MAX_CHAT_CONTEXT_CHARS = 8_000
# A final message ending in a question mark (before closing quotes or
# brackets) is taken as a question to the user; a heuristic, not a Run state.
_QUESTION_END = re.compile(r"\?[\s\"')\]*_]*$")
_FAILED_COMPLETIONS = {"failed": "failed", "interrupted": "interrupted"}
# How a Session list marks a Session a Cron job or Channel started.
_BACKGROUND_SESSION = "Cron or Channel Session"


class LiveSessions:
    """Run the Session Tools of one call on its context and ref table.

    ``started_at`` bounds the recently finished Sessions lists show.
    """

    def __init__(
        self,
        ctx: LiveContext,
        refs: LiveRefs,
        terminals: LiveTerminals,
        *,
        started_at: datetime,
    ) -> None:
        self._ctx = ctx
        self._refs = refs
        self._terminals = terminals
        self._started_at = started_at

    async def block(self, catalog: LiveCatalog) -> str:
        sessions = await catalog.sessions()
        touched = set(self._refs.touched())
        running = [item for item in sessions if item.get("has_active_run") is True]
        finished = [
            item
            for item in sessions
            if item.get("has_active_run") is not True
            and (session_key(item) in touched or self._recent(item))
        ]
        shown = (running + finished)[:LIST_CAP]
        if not shown:
            return (
                "Sessions: none running or finished since the call started. Older Sessions are "
                "not listed: read or open an Agent by name for its latest Session."
            )
        lines = ["Sessions (running, then recently finished; older ones are not listed):"]
        lines += await asyncio.gather(*(self._session_line(item, catalog) for item in shown))
        hidden = len(running) + len(finished) - len(shown)
        if hidden:
            lines.append(f"- and {hidden} more")
        return "\n".join(lines)

    async def agent_overview(self, agent: LiveAgent, catalog: LiveCatalog) -> str:
        sessions = [
            item for item in await catalog.sessions() if item["agent_address"] == agent.address
        ]
        if not sessions:
            return f"{agent.label} has no Sessions."
        background = {session_key(item) for item in (await self.agent_sessions(agent, catalog))[1]}
        shown = sessions[:LIST_CAP]
        lines = [f"Sessions of {agent.label}, most recent first:"]
        lines += await asyncio.gather(
            *(
                self._session_line(
                    item,
                    catalog,
                    origin=_BACKGROUND_SESSION if session_key(item) in background else "",
                )
                for item in shown
            )
        )
        if len(sessions) > len(shown):
            lines.append(f"- and {len(sessions) - len(shown)} older")
        return "\n".join(lines)

    async def _session_line(
        self, item: JsonObject, catalog: LiveCatalog, *, origin: str = ""
    ) -> str:
        key = session_key(item)
        # Assigned before the first wait: refs follow the list order even
        # though the lines load concurrently.
        self._refs.session(key)
        name = await catalog.agent_name(key.address)
        title = str(item.get("title") or item.get("auto_title") or "").strip()
        ref = self._refs.session(key, session_title(name, title))
        head = f'- {ref} {name} "{title}"' if title else f"- {ref} {name}"
        if origin:
            head += f" ({origin})"
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

    def _recent(self, item: JsonObject) -> bool:
        active = _timestamp(item.get("last_active_at"))
        return active is not None and active >= self._started_at

    async def start(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
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
            ref = self._refs.touch(key, session_title(agent.label))
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

    async def send_message(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        text = text_field(arguments, "text")
        target = await self._conversation_target(arguments, TOOL_SEND_MESSAGE, catalog)
        if target.terminal is not None:
            return await self._terminals.send(target.terminal, text)
        key = await self._session_of(target, TOOL_SEND_MESSAGE, catalog)
        name = await catalog.agent_name(key.address)
        ref = self._refs.touch(key, session_title(name))
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

    async def read(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        target = await self._conversation_target(arguments, TOOL_READ, catalog)
        if target.terminal is not None:
            return await self._terminals.read(target.terminal)
        key = await self._session_of(target, TOOL_READ, catalog)
        name = await catalog.agent_name(key.address)
        ref = self._refs.session(key, session_title(name))
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

    async def stop(self, arguments: JsonObject, catalog: LiveCatalog) -> JsonObject:
        target = await self._conversation_target(arguments, TOOL_STOP, catalog)
        if target.terminal is not None:
            return await self._terminals.interrupt(target.terminal)
        key = await self._session_of(target, TOOL_STOP, catalog)
        name = await catalog.agent_name(key.address)
        ref = self._refs.session(key, session_title(name))
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
            kind_hint=(
                'For an Agent, call overview with {"agent": "<its id>"} to see its Session refs.'
            ),
        )

    async def _session_of(self, target: Target, tool: str, catalog: LiveCatalog) -> SessionKey:
        """The Session a target names; an Agent name must leave one clear Session."""
        if target.session is not None:
            return target.session
        agent = target.agent
        assert agent is not None
        sessions, background = await self.agent_sessions(agent, catalog)

        def clear(item: JsonObject) -> bool:
            return item.get("has_active_run") is True or (
                tool != TOOL_STOP
                and (self._refs.is_touched(session_key(item)) or self._recent(item))
            )

        candidates = [item for item in sessions if clear(item)]
        if len(candidates) == 1:
            return session_key(candidates[0])
        if not candidates and tool == TOOL_READ and sessions:
            return session_key(sessions[0])
        if not candidates:
            what = "working" if tool == TOOL_STOP else "running or recent"
            skipped = bool(background) if tool == TOOL_READ else any(map(clear, background))
            note = (
                f" Its {_BACKGROUND_SESSION}s (started by a schedule or another chat app) are "
                "not chosen by its name."
                if skipped
                else ""
            )
            raise LiveToolError(
                "no_session",
                f"{agent.label} has no {what} Session.{note} Call overview with "
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

    async def agent_sessions(
        self, agent: LiveAgent, catalog: LiveCatalog
    ) -> tuple[list[JsonObject], list[JsonObject]]:
        """The Agent's Sessions its name can select, then its Cron and Channel Sessions.

        A Cron job's or Channel's Session is not the user's conversation with the
        Agent, so its name selects one only when the Agent has no other Session.
        """
        sessions = [
            item for item in await catalog.sessions() if item["agent_address"] == agent.address
        ]
        if not sessions:
            return [], []
        own = await catalog.own_sessions(agent.address)
        if not own:
            return sessions, []
        return (
            [item for item in sessions if session_key(item) in own],
            [item for item in sessions if session_key(item) not in own],
        )

    async def _send(self, key: SessionKey, text: str) -> JsonObject:
        return await self._ctx.call(
            "chat.stream",
            {
                "agent_id": key.address,
                "session_id": key.session_id,
                "content": text,
                "input_origin": CHAT_INPUT_ORIGIN_LIVE_VOICE,
            },
        )


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
