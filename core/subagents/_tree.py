"""Live Sub-Agent tree observation and whole-subtree cancellation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from core.runs import (
    Run,
)
from core.sessions import (
    SessionAddress,
)
from core.subagents._constants import (
    PARENT_AGENT_CANCEL_REASON,
    SUBAGENT_STATUS_CHANGED_EVENT,
    USER_CANCEL_REASON,
)
from core.subagents._interpretation import (
    subagent_address,
)
from core.subagents.forwarding import (
    SubAgentActivities,
    SubAgentForwarding,
    is_working,
    running_entries,
)
from core.subagents.links import (
    descendants,
)
from core.tools.terminal_manager import TerminalOwner
from core.tools.tools import (
    JsonObject,
    ToolContext,
)
from core.utils.workers import settle_before_cancelling

if TYPE_CHECKING:
    from core.runtime.interfaces import RuntimeServices

from core.subagents._delegation import _emit, _event_data, _latest_tool_name, _run_end
from core.subagents._types import SubAgentState, SubAgentStatus, SubAgentStopOutcome

# The WebUI's names for the kinds of running work `inspect` reports.
_INSPECTED_WORK_KINDS = {
    "Sub-Agent": "subagent",
    "background command": "command",
    "terminal": "terminal",
}


class SubAgentTreeControl:
    """Read and stop the durable tree against one shared admission lock."""

    def __init__(
        self,
        runtime: RuntimeServices,
        admission_lock: asyncio.Lock,
        activities: SubAgentActivities,
        forwarding: SubAgentForwarding,
    ) -> None:
        self._runtime = runtime
        self._admission_lock = admission_lock
        self._activities = activities
        self._forwarding = forwarding

    async def inspect(
        self,
        agent_id: str,
        session_id: str,
        subagent_id: str,
        *,
        project_id: str | None = None,
    ) -> JsonObject | None:
        """Return the WebUI projection of one Sub-Agent Session's current state.

        ``running`` lists what the Sub-Agent still runs besides its own Run: its
        working Sub-Agents, background commands and terminals.
        """
        address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
        session = await self._runtime.chat_sessions.get_async(address)
        manager = self._runtime.chat_run_manager
        projection: JsonObject = {
            "id": subagent_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "running": [
                {"kind": _INSPECTED_WORK_KINDS[entry.kind], "id": entry.id, "label": entry.label}
                for entry in await running_entries(self._runtime, address)
            ],
        }
        if project_id is not None:
            projection["project_id"] = project_id
        active = manager.active_run(agent_id=agent_id, session_id=session_id, project_id=project_id)
        if active is not None:
            return {
                **projection,
                "run_id": active.id,
                "status": active.status.value,
                "result": None,
                "usage": None,
                "timing": None,
                "started_at": active.created_at,
                "tool_name": _latest_tool_name(active),
            }
        if manager.list_queued(agent_id, session_id, project_id=project_id):
            return {**projection, "run_id": None, "status": "queued", "result": None}
        result = await session.load_run_result_async()
        if result is None or result.summary.run_id is None or result.summary.status is None:
            return None
        assistant, summary = result.assistant, result.summary
        return {
            **projection,
            "run_id": summary.run_id,
            "status": summary.status,
            "result": assistant.content if assistant is not None else None,
            "usage": assistant.usage if assistant is not None else None,
            "timing": summary.timing,
            "started_at": None,
            "tool_name": result.latest_tool_name,
        }

    async def stop_tree(self, address: SessionAddress) -> int:
        """Stop a Session's current Run, its background work and every Sub-Agent below it.

        The user's "Stop all"; returns how many Runs, queued messages, commands and
        terminals were stopped. The Session keeps its own queued messages. Answers of
        the stopped Sub-Agents are not forwarded, so they do not wake the Sessions
        above them that are stopped too. The complete subtree is stopped regardless
        of where its answers go.
        """
        stopped = await self._stop_subtree(
            address,
            reason=USER_CANCEL_REASON,
            initiator="user_stop_all",
            silence_root=False,
            clear_root_queue=False,
        )
        # Without stopped_by the Session the user works in is always stopped.
        return stopped or 0

    async def work_snapshot(
        self, address: SessionAddress, *, include_entries: bool = False
    ) -> JsonObject:
        """Project Stop all's live scope, independently of loaded History.

        The root's own Queue is retained by Stop all and does not make background
        work stoppable. Descendant Queues, commands and Terminal attachments do.
        An unavailable tree read propagates instead of reporting an idle tree.
        """
        runtime = self._runtime
        sessions = runtime.chat_sessions
        async with self._admission_lock:
            links = await sessions.run_async(descendants, sessions, address, strict=True)
            below = {link.session for link in links}
            tree = {address, *below}
            run_active = (
                runtime.chat_run_manager.active_run(
                    agent_id=address.agent_id,
                    session_id=address.session_id,
                    project_id=address.project_id,
                )
                is not None
            )
            background = any(is_working(runtime, child) for child in below)
            commands = runtime.command_manager
            if commands is not None:
                background |= bool(commands.busy_addresses() & below) or any(
                    not report.finished for report in commands.commands([address])
                )
                if not run_active:
                    background |= address in commands.busy_addresses()
            terminals = runtime.terminal_manager
            if terminals is not None:
                background |= bool(terminals.busy_addresses() & tree) or any(
                    info.finished_at is None
                    and any(
                        owner is not None
                        and SessionAddress(owner.project_id, owner.agent_id, owner.session_id)
                        in tree
                        for owner in (info.lifecycle_owner, info.attachment)
                    )
                    for info in terminals.list_terminals()
                )
            snapshot: JsonObject = {
                "run_active": run_active,
                "background_work_running": background,
            }
            if not include_entries:
                return snapshot

            # The same strict tree read also supplies an Accessor's activity
            # list. Keep exact addresses with each entry so opening a child
            # never depends on a historical Tool row being loaded.
            ids = {link.session: link.id for link in links}

            def owner_fields(owner: SessionAddress) -> JsonObject:
                return {
                    "agent_id": owner.agent_id,
                    "session_id": owner.session_id,
                    "project_id": owner.project_id,
                    "subagent_id": ids.get(owner),
                }

            running: list[JsonObject] = []
            for link in links:
                child = link.session
                active = runtime.chat_run_manager.active_run(
                    agent_id=child.agent_id,
                    session_id=child.session_id,
                    project_id=child.project_id,
                )
                if active is None and not is_working(runtime, child):
                    continue
                running.append(
                    {
                        "kind": "subagent",
                        "id": link.id,
                        "label": link.title,
                        "status": "running" if active is not None else "queued",
                        **owner_fields(child),
                        "parent_id": ids.get(link.parent),
                        "run_id": active.id if active is not None else None,
                        "reply_target": link.reply_target,
                        "tool_name": _latest_tool_name(active) if active is not None else None,
                    }
                )
            if commands is not None:
                running.extend(
                    {
                        "kind": "command",
                        "id": report.command_id,
                        "label": report.label,
                        "status": report.status,
                        "still_running": bool(report.still_running),
                        **owner_fields(report.owner),
                    }
                    for report in commands.commands(tree)
                    if not report.finished
                )
            if terminals is not None:
                for info in terminals.list_terminals():
                    if info.finished_at is not None:
                        continue
                    owner = next(
                        (
                            scoped
                            for candidate in (info.attachment, info.lifecycle_owner)
                            if candidate is not None
                            and (
                                scoped := SessionAddress(
                                    candidate.project_id, candidate.agent_id, candidate.session_id
                                )
                            )
                            in tree
                        ),
                        None,
                    )
                    if owner is None:
                        continue
                    command = " ".join((info.command, *info.arguments)).strip()
                    running.append(
                        {
                            "kind": "terminal",
                            "id": info.terminal_id,
                            "label": f"{info.call_name}, {command}" if info.call_name else command,
                            "status": "running",
                            **owner_fields(owner),
                        }
                    )
            snapshot["running"] = running
            return snapshot

    async def subagents(self, caller: SessionAddress) -> list[SubAgentState]:
        """Return *caller*'s Sub-Agents at every depth, breadth first, as they are now.

        ``background_tasks`` lists, checks and stops them, and shows the caller
        the background commands of their Sessions.
        """
        runtime = self._runtime
        sessions = runtime.chat_sessions
        tree = await sessions.run_async(descendants, sessions, caller)
        ids = {link.session: link.id for link in tree}
        states: list[SubAgentState] = []
        for link in tree:
            address = link.session
            run = runtime.chat_run_manager.active_run(
                agent_id=address.agent_id,
                session_id=address.session_id,
                project_id=address.project_id,
            )
            last_tool: str | None = None
            status: SubAgentStatus
            if run is not None:
                status, last_tool = "working", _latest_tool_name(run)
            elif is_working(runtime, address):
                status = "queued"
            else:
                status = "idle"
            states.append(
                SubAgentState(
                    id=link.id,
                    title=link.title,
                    agent_id=subagent_address(link),
                    session=address,
                    parent_id=ids.get(link.parent) if link.parent != caller else None,
                    status=status,
                    last_tool=last_tool,
                    activity_file=self._activities.path(address),
                    reply_target=link.reply_target,
                )
            )
        return states

    async def running_work(self, subagent: SubAgentState) -> list[str]:
        """Name what *subagent* still runs besides its own turn, as ``kind id (label)``.

        Its background commands, its terminals and its own working Sub-Agents.
        """
        return [
            entry.describe() for entry in await running_entries(self._runtime, subagent.session)
        ]

    async def stop_subagent(
        self, context: ToolContext, subagent: SubAgentState
    ) -> SubAgentStopOutcome:
        """Stop *subagent*: its turn, its queue, its background work and its own Sub-Agents.

        The caller learns the outcome from its Tool result, so a direct
        Sub-Agent's stopped turn is not forwarded to it; a deeper one's is
        forwarded to its own Parent when its answer is addressed there.
        """
        stopped = await self._stop_subtree(
            subagent.session,
            reason=PARENT_AGENT_CANCEL_REASON,
            initiator=f"parent_run:{context.run_id}",
            silence_root=subagent.parent_id is None,
            clear_root_queue=True,
        )
        if not stopped:
            return "idle"
        await _emit(
            context,
            SUBAGENT_STATUS_CHANGED_EVENT,
            _event_data(subagent.id, subagent.session, status="cancelled"),
        )
        return "stopped"

    async def _stop_subtree(
        self,
        root: SessionAddress,
        *,
        reason: str,
        initiator: str,
        silence_root: bool,
        clear_root_queue: bool,
    ) -> int:
        """Cancel a complete admitted tree before awaiting any of its cleanup.

        Includes every descendant, whatever its current answer destination.
        """
        sessions = self._runtime.chat_sessions
        manager = self._runtime.chat_run_manager
        terminals = self._runtime.terminal_manager
        commands = self._runtime.command_manager
        stopped = 0
        runs: list[Run] = []
        # A child start already in flight must finish before the tree is read.
        # Cancel every captured Run while admissions are still excluded, so a
        # descendant cannot create or resume work during another Run's cleanup.
        async with self._admission_lock:
            below = await sessions.run_async(descendants, sessions, root, strict=True)
            addresses = [root, *(link.session for link in below)]
            for address in addresses:
                if address != root or clear_root_queue:
                    stopped += manager.clear_queued(
                        address.agent_id, address.session_id, project_id=address.project_id
                    )
                run = manager.active_run(
                    agent_id=address.agent_id,
                    session_id=address.session_id,
                    project_id=address.project_id,
                )
                if run is not None:
                    if address != root or silence_root:
                        self._forwarding.silence(run.id)
                    run.request_cancel(reason=reason, initiator=initiator)
                    runs.append(run)
                    stopped += 1

        async def cleanup() -> int:
            # Cleanup can wait on other work; never hold the admission lock here.
            # One failed resource must not leave the rest of the captured tree running.
            closed = 0
            failures: list[Exception] = []
            if terminals is not None:
                owners = [
                    TerminalOwner(address.project_id, address.agent_id, address.session_id)
                    for address in addresses
                ]
                try:
                    closed += await terminals.close_scopes(owners)
                except Exception as error:
                    failures.append(error)
            for address in addresses:
                if commands is not None:
                    # Commands still inside their call end with the cancelled Run;
                    # only background ones count as work of their own.
                    closed += sum(
                        1 for report in commands.commands([address]) if not report.finished
                    )
                    try:
                        await commands.close_scope(address)
                    except Exception as error:
                        failures.append(error)
            if runs:
                await asyncio.gather(*(_run_end(run) for run in runs))
            if failures:
                raise failures[0]
            return closed

        return stopped + await settle_before_cancelling(cleanup())
