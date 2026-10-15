"""The ``subagent`` and ``message_parent`` Tools over the durable Sub-Agent tree.

A Sub-Agent is a Session linked to the Parent Session that started it, with one
public id for its whole life. ``run`` starts a new Sub-Agent in the background
and ``send`` gives one of the caller's Sub-Agents another message. The
``background_tasks`` Tool lists, checks and stops the caller's whole tree
through :meth:`SubAgentCoordinator.subagents`, ``running_work`` and
``stop_subagent``. Answers reach the Parent through
:mod:`core.subagents.forwarding`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from core.agents import is_builtin_agent
from core.projects import (
    AgentResolutionError,
    InvalidAgentAddressError,
    ModelConfigurationError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
    WorkingProjectMissingError,
    format_agent_address,
    parse_agent_address,
)
from core.runs import (
    ActiveRunError,
    ChatRunManager,
    Run,
    RunAdmission,
    RunKind,
    RunNotFoundError,
)
from core.sessions import (
    AGENT_DEFAULT_PROJECT,
    SessionAddress,
    SessionNotFoundError,
    TemporarySessionBinding,
)
from core.settings import SettingsValidationError, validate_thinking_effort
from core.subagents._constants import (
    DEFAULT_MAX_ACTIVE_SUBAGENTS,
    DEFAULT_MAX_ACTIVE_SUBAGENTS_TOTAL,
    DEFAULT_MAX_SUBAGENT_DEPTH,
    MESSAGE_PARENT_DELIVERY_UNAVAILABLE_MESSAGE,
    MESSAGE_PARENT_NOT_SUBAGENT_MESSAGE,
    MESSAGE_PARENT_PARENT_GONE_MESSAGE,
    MESSAGE_PARENT_QUEUED_NOTE,
    PARENT_AGENT_CANCEL_REASON,
    PARENT_MESSAGE_SECTION_TEMPLATE,
    SUBAGENT_ACTIVE_LIMIT_MESSAGE_TEMPLATE,
    SUBAGENT_ACTIVITY_NOTE_TEMPLATE,
    SUBAGENT_APP_LIMIT_MESSAGE_TEMPLATE,
    SUBAGENT_BACKGROUND_IGNORED_NOTE,
    SUBAGENT_CHOOSE_RECIPIENT_TEXT,
    SUBAGENT_DEPTH_LIMIT_MESSAGE_TEMPLATE,
    SUBAGENT_GENERIC_TARGET_NOTE_TEMPLATE,
    SUBAGENT_ID_WITHOUT_ACTION_MESSAGE_TEMPLATE,
    SUBAGENT_ID_WITHOUT_ACTION_STOP_TEMPLATE,
    SUBAGENT_IGNORED_LABEL_NOTE_TEMPLATE,
    SUBAGENT_LIMIT_STOP_TEXT,
    SUBAGENT_MISSING_DESCRIPTION_MESSAGE,
    SUBAGENT_MISSING_TASK_LIST_TEXT,
    SUBAGENT_MISSING_TASK_MESSAGE,
    SUBAGENT_NOT_DIRECT_CHILD_MESSAGE_TEMPLATE,
    SUBAGENT_NOT_DIRECT_CHILD_STOP_TEMPLATE,
    SUBAGENT_NOT_FOUND_MESSAGE_TEMPLATE,
    SUBAGENT_SEND_APP_LIMIT_MESSAGE_TEMPLATE,
    SUBAGENT_SEND_LIMIT_MESSAGE_TEMPLATE,
    SUBAGENT_SEND_QUEUED_NOTE,
    SUBAGENT_SEND_STARTED_NOTE,
    SUBAGENT_SEND_STEERED_NOTE,
    SUBAGENT_SEND_WITHOUT_CONTENT_MESSAGE_TEMPLATE,
    SUBAGENT_SEND_WITHOUT_ID_MESSAGE_TEMPLATE,
    SUBAGENT_SESSION_MODEL_UNUSABLE_MESSAGE_TEMPLATE,
    SUBAGENT_SESSION_NOT_SUBAGENT_MESSAGE_TEMPLATE,
    SUBAGENT_SESSION_PROJECT_MISSING_MESSAGE_TEMPLATE,
    SUBAGENT_SESSION_SETTINGS_UNREADABLE_MESSAGE_TEMPLATE,
    SUBAGENT_SESSION_STARTED_EVENT,
    SUBAGENT_SESSION_TITLE_MAX_CHARACTERS,
    SUBAGENT_STAND_IN_SESSION_NOTE_TEMPLATE,
    SUBAGENT_START_FIRST_TEXT,
    SUBAGENT_STARTED_NOTE,
    SUBAGENT_STATUS_CHANGED_EVENT,
    SUBAGENT_TARGET_NOT_ALLOWED_MESSAGE_TEMPLATE,
    SUBAGENT_TARGET_UNAVAILABLE_MESSAGE_TEMPLATE,
    USER_CANCEL_REASON,
)
from core.subagents._interpretation import (
    SUBAGENT_ID_PATTERN,
    call_text,
    has_text,
    is_generic_target,
    optional_text,
    quoted,
    reads_as_new_session,
    subagent_address,
    target_choices,
    yours_text,
)
from core.subagents.catalog import SubAgentPromptTarget, build_subagent_prompt_targets
from core.subagents.forwarding import (
    SubAgentActivities,
    SubAgentForwarding,
    is_working,
    running_entries,
)
from core.subagents.links import (
    SubAgentLink,
    ancestors,
    children,
    descendants,
    find_link,
    link_session,
    read_link,
)
from core.tools.availability import subagent_allowed_agents
from core.tools.background_tasks import BACKGROUND_TASKS_TOOL_NAME
from core.tools.contracts import ToolContractError
from core.tools.terminal_manager import TerminalOwner
from core.tools.tools import (
    JsonObject,
    ToolContext,
    tool_failure,
    tool_success,
)
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.workers import finish_despite_cancel, settle_before_cancelling

if TYPE_CHECKING:
    from core.runtime.interfaces import RuntimeServices
    from core.sessions.session import ChatSession

_LOGGER = get_logger("subagents")


def _subagent_count(count: int) -> str:
    return f"{count} Sub-Agent" if count == 1 else f"{count} Sub-Agents"


# The WebUI's names for the kinds of running work `inspect` reports.
_INSPECTED_WORK_KINDS = {
    "Sub-Agent": "subagent",
    "background command": "command",
    "terminal": "terminal",
}

# ``working``: its turn runs; ``queued``: a message waits to start its next turn;
SubAgentStatus = Literal["working", "queued", "idle"]

# What a stop of a Sub-Agent did. ``stopped``: something ran and was stopped;
# ``idle`` means that nothing needed stopping.
SubAgentStopOutcome = Literal["stopped", "idle"]


@dataclass(frozen=True)
class SubAgentState:
    """One Sub-Agent below a caller, as it is now.

    ``agent_id`` is the address ``subagent`` accepts for its Agent. ``parent_id``
    names the caller's Sub-Agent it works for, ``None`` for a direct Sub-Agent of
    the caller. ``last_tool`` is the Tool its working turn called last.
    ``reply_target`` is independent of membership, status and control.
    """

    id: str
    title: str
    agent_id: str
    session: SessionAddress
    parent_id: str | None
    status: SubAgentStatus
    last_tool: str | None
    activity_file: str | None
    reply_target: str = "parent"


class SubAgentTree(Protocol):
    """The Sub-Agents below a caller, what they run, and how to stop one.

    ``background_tasks`` lists, checks and stops them and the background commands
    of their Sessions; ``terminal`` names the Sub-Agent whose Session holds a
    terminal. The complete tree stays visible and controllable.
    """

    async def subagents(self, caller: SessionAddress) -> Sequence[SubAgentState]:
        """*caller*'s Sub-Agents at every depth, breadth first, as they are now."""
        ...

    async def running_work(self, subagent: SubAgentState) -> Sequence[str]:
        """What *subagent* still runs besides its own turn, each as ``kind id (label)``."""
        ...

    async def stop_subagent(
        self, context: ToolContext, subagent: SubAgentState
    ) -> SubAgentStopOutcome:
        """Stop *subagent* with its queue, background work and own Sub-Agents.

        The complete subtree is stopped regardless of where its answers go.
        """
        ...


class SubAgentCoordinator:
    """Run the Sub-Agent Tools and forward Sub-Agent answers to their Parents."""

    def __init__(self, runtime: RuntimeServices, trigger_service: Any) -> None:
        self._runtime = runtime
        self._trigger_service = trigger_service
        self._activities = SubAgentActivities(runtime)
        self._forwarding = SubAgentForwarding(runtime, trigger_service, self._activities)
        # One Sub-Agent start at a time: the limit checks count working Sub-Agents,
        # a count that is stale once another start has finished meanwhile.
        self._admission_lock = asyncio.Lock()

    async def _limit_refusal(
        self,
        parent: SessionAddress,
        settings: dict[str, int],
        idle_id: str | None = None,
        *,
        can_stop: bool,
    ) -> JsonObject | None:
        """Refuse a start when *parent* or the whole app has its limit of working Sub-Agents.

        *idle_id* names the idle Sub-Agent a ``send`` would start; omitted for ``run``.
        *can_stop*: the caller can call ``background_tasks``, so the refusal names its stop.
        All working Sub-Agents count. Call it under the admission lock.
        """
        runtime, sessions = self._runtime, self._runtime.chat_sessions
        own = await sessions.run_async(children, sessions, parent)
        limit = settings["max_active_subagents"]
        if sum(1 for link in own if _counts_as_working(runtime, link)) >= limit:
            template = (
                SUBAGENT_ACTIVE_LIMIT_MESSAGE_TEMPLATE
                if idle_id is None
                else SUBAGENT_SEND_LIMIT_MESSAGE_TEMPLATE
            )
            return tool_failure(
                "subagent_limit_exceeded",
                template.format(
                    count=_subagent_count(limit),
                    id=idle_id,
                    or_stop=SUBAGENT_LIMIT_STOP_TEXT if can_stop else "",
                ),
            )
        manager = runtime.chat_run_manager
        busy = {
            SessionAddress(
                project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
            )
            for run in manager.active_runs()
        }
        busy.update(address for address, _item in manager.all_queued())
        limit = settings["max_active_subagents_total"]
        if await sessions.run_async(_working_subagent_count, runtime, busy) >= limit:
            template = (
                SUBAGENT_APP_LIMIT_MESSAGE_TEMPLATE
                if idle_id is None
                else SUBAGENT_SEND_APP_LIMIT_MESSAGE_TEMPLATE
            )
            return tool_failure(
                "subagent_app_limit_exceeded",
                template.format(count=_subagent_count(limit), id=idle_id),
            )
        return None

    def install(self, run_manager: ChatRunManager) -> None:
        """Start following the Runs *run_manager* starts in Sub-Agent Sessions.

        Called while the runtime is being built, before its services count as
        started, so the Run manager is passed in.
        """
        self._forwarding.install(run_manager)

    def prompt_targets(self, agent: Any, project_id: str | None) -> list[SubAgentPromptTarget]:
        """Return additional targets for the Tool-owned System Prompt block."""
        return build_subagent_prompt_targets(self._runtime, agent, project_id)

    def subagent_reply_to_user(self, address: SessionAddress, message_id: str) -> None:
        """Handle an input that switches a Sub-Agent to user-directed answers."""
        self._forwarding.reply_to_user(address, message_id)

    async def spawn(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Handle one ``subagent`` call."""
        try:
            action = arguments.get("action")
            if action is None:
                implied = _implied_action(
                    arguments, can_stop=context.can_call(BACKGROUND_TASKS_TOOL_NAME)
                )
                if not isinstance(implied, str):
                    return implied
                action = implied
            if action == "run":
                return await self._run(context, arguments)
            if action == "send":
                return await self._send_call(context, arguments)
        except (ToolContractError, InvalidAgentAddressError, SettingsValidationError) as error:
            return tool_failure("invalid_arguments", str(error))
        return tool_failure(
            "invalid_arguments", "subagent was not run: action must be one of: run, send."
        )

    async def message_parent(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        """Handle one ``message_parent`` call from a Sub-Agent."""
        if not has_text(arguments, "content"):
            return tool_failure(
                "invalid_arguments", '"content" must carry the message for your Parent Agent.'
            )
        sessions = self._runtime.chat_sessions
        link = await sessions.run_async(read_link, sessions, _caller(context))
        if link is None:
            return tool_failure("not_a_subagent", MESSAGE_PARENT_NOT_SUBAGENT_MESSAGE)
        if not await sessions.run_async(sessions.exists, link.parent):
            return tool_failure("parent_not_found", MESSAGE_PARENT_PARENT_GONE_MESSAGE)
        parent = link.parent
        activity = await self._activities.ensure(_caller(context))
        delivery_id = new_id("subagent-message")
        delivery = self._trigger_service.submit_completion(
            parent.agent_id,
            parent.session_id,
            notice_id=delivery_id,
            origin_run_id=context.run_id,
            body=PARENT_MESSAGE_SECTION_TEMPLATE.format(
                id=link.id,
                title=link.title or link.id,
                delivery_id=delivery_id,
                content=cast(str, arguments["content"]).strip(),
            ),
            project_id=parent.project_id,
            execution_owner=context.execution_owner,
        )
        delivery.add_done_callback(_log_message_delivery)
        if activity is not None:
            activity.track_delivery(
                delivery, delivery_id=delivery_id, run_id=context.run_id, parent=parent
            )
        if delivery.cancelled():
            return tool_failure("delivery_unavailable", MESSAGE_PARENT_DELIVERY_UNAVAILABLE_MESSAGE)
        # Once accepted, cancellation must not leave the message sent but its answer
        # still addressed to the user. The Session lock orders this with steering.
        await finish_despite_cancel(self._resume_parent_replies(_caller(context)))
        return tool_success(
            {"status": "queued", "delivery_id": delivery_id, "note": MESSAGE_PARENT_QUEUED_NOTE}
        )

    async def _resume_parent_replies(self, address: SessionAddress) -> None:
        sessions = self._runtime.chat_sessions
        async with sessions.write_lock(address):
            session = await sessions.get_async(address)
            await session.append_many_async([], subagent_reply_target="parent")

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

    async def work_snapshot(self, address: SessionAddress) -> dict[str, bool]:
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
            return {"run_active": run_active, "background_work_running": background}

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

    async def drain_activity(self) -> None:
        """Wait until the activity files' text so far is on disk."""
        await self._activities.drain()

    def references_identity_agent(self, agent_id: str) -> bool:
        """Return whether a followed Run in an Identity Agent's Session is still forwarding."""
        return any(
            run.project_id is None and run.agent_id == agent_id
            for run in self._forwarding.followed_runs()
        )

    # run

    async def _run(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        if not has_text(arguments, "content"):
            message = SUBAGENT_MISSING_TASK_MESSAGE
            if context.can_call(BACKGROUND_TASKS_TOOL_NAME):
                message += SUBAGENT_MISSING_TASK_LIST_TEXT
            return tool_failure("invalid_arguments", message)
        runtime, caller = self._runtime, _caller(context)
        sessions = runtime.chat_sessions
        notes: list[str] = []

        subagent_id = optional_text(arguments, "id")
        if subagent_id is not None:
            link = await sessions.run_async(find_link, sessions, subagent_id)
            if link is not None or SUBAGENT_ID_PATTERN.fullmatch(subagent_id):
                return await self._send(context, arguments, subagent_id, link)
            notes.append(SUBAGENT_IGNORED_LABEL_NOTE_TEMPLATE.format(label=quoted(subagent_id)))

        session_id = optional_text(arguments, "session_id")
        if session_id is not None:
            own = await sessions.run_async(children, sessions, caller)
            continued = next((link for link in own if link.session.session_id == session_id), None)
            if continued is not None:
                return await self._send(context, arguments, continued.id, continued)
            if not reads_as_new_session(session_id):
                return tool_failure(
                    "invalid_arguments",
                    SUBAGENT_SESSION_NOT_SUBAGENT_MESSAGE_TEMPLATE.format(
                        session_id=quoted(session_id), yours=yours_text(own)
                    ),
                )
            notes.append(
                SUBAGENT_STAND_IN_SESSION_NOTE_TEMPLATE.format(session_id=quoted(session_id))
            )

        description = optional_text(arguments, "description")
        if description is None:
            return tool_failure("invalid_arguments", SUBAGENT_MISSING_DESCRIPTION_MESSAGE)

        if arguments.get("background") is False:
            notes.append(SUBAGENT_BACKGROUND_IGNORED_NOTE)
        agent_address = optional_text(arguments, "agent_id")
        if agent_address is not None and is_generic_target(runtime, context, agent_address):
            notes.append(SUBAGENT_GENERIC_TARGET_NOTE_TEMPLATE.format(name=quoted(agent_address)))
            agent_address = None
        target_agent_id, target_project_id = _resolve_target_address(
            agent_address or context.agent_id, context.project_id
        )
        target_address = agent_address or format_agent_address(target_agent_id, target_project_id)
        overrides = _parse_session_overrides(arguments)
        if not _target_is_allowed(context, target_agent_id, target_project_id):
            return tool_failure(
                "agent_not_allowed",
                SUBAGENT_TARGET_NOT_ALLOWED_MESSAGE_TEMPLATE.format(
                    target=target_address, choices=target_choices(runtime, context)
                ),
            )
        temporary_parent = await self._temporary_parent(context, target_agent_id, target_project_id)
        failure = await _validate_target_agent(
            runtime,
            target_agent_id,
            target_project_id,
            model=overrides.get("model"),
            temporary_parent_binding=temporary_parent,
            caller=context,
            overrides=overrides,
        )
        if failure is not None:
            error = failure["error"]
            if error["code"] in {"agent_not_found", "project_not_found"}:
                reason = str(error["message"]).rstrip(".")
                return tool_failure(
                    error["code"],
                    f"Nothing was started. {reason}. {target_choices(runtime, context)}",
                )
            return failure

        settings = _load_subagent_settings(runtime)
        chain = await sessions.run_async(ancestors, sessions, caller)
        if len(chain) >= settings["max_subagent_depth"]:
            return tool_failure(
                "subagent_depth_exceeded",
                SUBAGENT_DEPTH_LIMIT_MESSAGE_TEMPLATE.format(limit=settings["max_subagent_depth"]),
            )
        async with self._admission_lock:
            refusal = await self._limit_refusal(
                caller, settings, can_stop=context.can_call(BACKGROUND_TASKS_TOOL_NAME)
            )
            if refusal is not None:
                return refusal
            if context.is_cancelled():
                return tool_failure(
                    "run_cancelled", "Your Run was cancelled before the Sub-Agent started."
                )
            # Past this check the start completes even when the calling Run is
            # cancelled meanwhile: once the child Run exists, the Parent must
            # receive this result, or it takes the call as unfinished and may
            # start the same work a second time.
            return await finish_despite_cancel(
                self._start_subagent(
                    context,
                    cast(str, arguments["content"]),
                    target_agent_id,
                    target_project_id,
                    description=description,
                    overrides=overrides,
                    temporary_parent=temporary_parent,
                    notes=notes,
                )
            )

    async def _start_subagent(
        self,
        context: ToolContext,
        content: str,
        target_agent_id: str,
        target_project_id: str | None,
        *,
        description: str,
        overrides: dict[str, str],
        temporary_parent: TemporarySessionBinding | None,
        notes: list[str],
    ) -> JsonObject:
        """Create the Sub-Agent Session, start its Run and return the ``run`` result.

        A Run that cannot start removes the Session again, so no Sub-Agent
        exists without its first Run.
        """
        runtime = self._runtime
        sessions = runtime.chat_sessions
        new_subagent_id = new_id("sub")
        session = await sessions.run_async(
            _open_subagent_session,
            runtime,
            target_agent_id,
            target_project_id,
            _session_title(description),
            new_subagent_id,
            context,
            overrides,
        )
        address = session.address
        executor = runtime.chat_loop.run_executor(
            content, parent_agent_input=True, temporary_parent_binding=temporary_parent
        )
        try:
            run = await runtime.chat_run_manager.start(
                address,
                executor,
                admission=_admission(
                    context, new_subagent_id, _child_working_project(context, target_project_id)
                ),
            )
        except Exception:
            await _remove_unstarted_session(runtime, address)
            raise
        activity = await self._activities.ensure(address)
        activity_file = self._activities.path(address)
        _LOGGER.info(
            "Sub-agent started (subagent=%s parent_run=%s parent_session=%s child_session=%s "
            "agent=%s run=%s)",
            new_subagent_id,
            context.run_id,
            context.session_id,
            address.session_id,
            target_agent_id,
            run.id,
        )
        await _emit(
            context,
            SUBAGENT_SESSION_STARTED_EVENT,
            _event_data(
                new_subagent_id,
                address,
                status="running",
                run_id=run.id,
                activity_file=activity_file,
            ),
        )
        result = _public_identity(new_subagent_id, address)
        result["status"] = "running"
        result["note"] = " ".join([*notes, SUBAGENT_STARTED_NOTE])
        if activity is not None and activity_file is not None:
            result["activity_note"] = SUBAGENT_ACTIVITY_NOTE_TEMPLATE.format(path=activity_file)
        return tool_success(result)

    # send

    async def _send_call(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        sessions = self._runtime.chat_sessions
        subagent_id = optional_text(arguments, "id")
        if subagent_id is None:
            session_id = optional_text(arguments, "session_id")
            own = await sessions.run_async(children, sessions, _caller(context))
            continued = next(
                (link for link in own if session_id and link.session.session_id == session_id),
                None,
            )
            if continued is None:
                return tool_failure(
                    "invalid_arguments",
                    SUBAGENT_SEND_WITHOUT_ID_MESSAGE_TEMPLATE.format(
                        yours=yours_text(own),
                        next_step=(
                            SUBAGENT_CHOOSE_RECIPIENT_TEXT if own else SUBAGENT_START_FIRST_TEXT
                        ),
                    ),
                )
            return await self._send(context, arguments, continued.id, continued)
        link = await sessions.run_async(find_link, sessions, subagent_id)
        return await self._send(context, arguments, subagent_id, link)

    async def _send(
        self,
        context: ToolContext,
        arguments: JsonObject,
        subagent_id: str,
        link: SubAgentLink | None,
    ) -> JsonObject:
        runtime, caller = self._runtime, _caller(context)
        sessions = runtime.chat_sessions
        chain = (
            await sessions.run_async(ancestors, sessions, link.session) if link is not None else []
        )
        if link is None or caller not in chain:
            own = await sessions.run_async(children, sessions, caller)
            return _not_found(subagent_id, own)
        if link.parent != caller:
            parent_link = await sessions.run_async(read_link, sessions, link.parent)
            or_stop = (
                SUBAGENT_NOT_DIRECT_CHILD_STOP_TEMPLATE.format(id=link.id)
                if context.can_call(BACKGROUND_TASKS_TOOL_NAME)
                else ""
            )
            return tool_failure(
                "subagent_not_direct",
                SUBAGENT_NOT_DIRECT_CHILD_MESSAGE_TEMPLATE.format(
                    id=link.id,
                    parent_id=parent_link.id if parent_link is not None else "?",
                    or_stop=or_stop,
                ),
            )
        if not has_text(arguments, "content"):
            return tool_failure(
                "invalid_arguments",
                SUBAGENT_SEND_WITHOUT_CONTENT_MESSAGE_TEMPLATE.format(id=link.id),
            )
        address = link.session
        try:
            session = await sessions.get_async(address)
        except SessionNotFoundError:
            return _not_found(subagent_id, await sessions.run_async(children, sessions, caller))
        generation_id = session.generation_id
        assert generation_id is not None  # get_async returns a generation-bound handle.
        overrides = _parse_session_overrides(arguments)
        temporary_parent = await self._temporary_parent(
            context, address.agent_id, address.project_id
        )
        failure = await _validate_continued_session(runtime, link, model=overrides.get("model"))
        if failure is not None:
            return failure
        try:
            working_project_id = await runtime.agent_resolver.session_working_project_async(address)
        except WorkingProjectMissingError as error:
            return tool_failure(
                "project_not_found",
                SUBAGENT_SESSION_PROJECT_MISSING_MESSAGE_TEMPLATE.format(
                    id=link.id, project_id=error.project_id
                ),
                retryable=False,
            )
        steerable = context.execution_owner is None and temporary_parent is None
        # A message to an idle Sub-Agent starts its next Run, which counts against the limits.
        async with self._admission_lock:
            if not is_working(runtime, address):
                refusal = await self._limit_refusal(
                    caller,
                    _load_subagent_settings(runtime),
                    idle_id=link.id,
                    can_stop=context.can_call(BACKGROUND_TASKS_TOOL_NAME),
                )
                if refusal is not None:
                    return refusal
            if context.is_cancelled():
                return tool_failure(
                    "run_cancelled", "Your Run was cancelled before the message was sent."
                )
            # Past this check the message is sent even when the calling Run is
            # cancelled meanwhile, and the Parent receives the result, so it
            # never sends the same message a second time.
            return await finish_despite_cancel(
                self._deliver_send(
                    context,
                    link.id,
                    address,
                    cast(str, arguments["content"]),
                    overrides=overrides,
                    temporary_parent=temporary_parent,
                    working_project_id=working_project_id,
                    steerable=steerable,
                    generation_id=generation_id,
                )
            )

    async def _deliver_send(
        self,
        context: ToolContext,
        subagent_id: str,
        address: SessionAddress,
        content: str,
        *,
        overrides: dict[str, str],
        temporary_parent: TemporarySessionBinding | None,
        working_project_id: str | None,
        steerable: bool,
        generation_id: str,
    ) -> JsonObject:
        """Admit *content* before storing overrides or letting Chat consume it."""
        runtime = self._runtime
        sessions = runtime.chat_sessions
        manager = runtime.chat_run_manager
        # Lock order is Session -> manager admission. Never hold the manager lock
        # over metadata I/O: the admitted Run/Queue item already claims this
        # Session and, when busy, its waiting-work capacity.
        async with sessions.write_lock(address):
            session: ChatSession | None
            try:
                session = await sessions.get_async(address)
            except SessionNotFoundError:
                session = None
            current_link = await sessions.run_async(read_link, sessions, address, strict=True)
            if (
                session is None
                or session.generation_id != generation_id
                or current_link is None
                or current_link.id != subagent_id
                or current_link.parent != _caller(context)
            ):
                return _not_found(
                    subagent_id, await sessions.run_async(children, sessions, _caller(context))
                )
            executor = runtime.chat_loop.run_executor(
                content, parent_agent_input=True, temporary_parent_binding=temporary_parent
            )
            prepared = asyncio.Event()
            preparation_error: BaseException | None = None

            async def execute_prepared(run: Run) -> Any:
                await prepared.wait()
                if preparation_error is not None:
                    raise preparation_error
                return await executor(run)

            item = await manager.enqueue(
                address,
                execute_prepared,
                display_content=content,
                admission=_admission(
                    context,
                    subagent_id,
                    working_project_id,
                    expected_session_generation_id=generation_id,
                ),
            )
            try:
                if overrides:
                    await sessions.run_async(
                        runtime.agent_resolver.update_session_overrides, address, overrides
                    )
                # Queued input cannot be steered before its settings commit;
                # an idle or drained Run waits at the executor gate instead.
                if not item.future.cancelled() and not item.withdrawn and not item.steering:
                    # Steering needs Chat's captured request, not the admission
                    # gate. Already-started Runs retain the gate closure.
                    item.executor = executor
                    item.steerable = steerable
            except BaseException as error:
                preparation_error = error
                manager.remove_queued(
                    address.agent_id,
                    address.session_id,
                    item.item_id,
                    project_id=address.project_id,
                )
                raise
            finally:
                prepared.set()
        status, note = "queued", SUBAGENT_SEND_QUEUED_NOTE
        if item.future.done() and not item.future.cancelled():
            status, note = "started", SUBAGENT_SEND_STARTED_NOTE
        elif steerable:
            try:
                manager.steer_queued(
                    address.agent_id,
                    address.session_id,
                    item.item_id,
                    project_id=address.project_id,
                )
            except ActiveRunError, RunNotFoundError:
                pass
            else:
                status, note = "steered", SUBAGENT_SEND_STEERED_NOTE
        await self._activities.ensure(address)
        await _emit(
            context,
            SUBAGENT_SESSION_STARTED_EVENT,
            _event_data(
                subagent_id,
                address,
                status="running" if status != "queued" else "queued",
                activity_file=self._activities.path(address),
            ),
        )
        result = _public_identity(subagent_id, address)
        result["status"] = status
        result["note"] = note
        return tool_success(result)

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

    async def _temporary_parent(
        self, context: ToolContext, target_agent_id: str, target_project_id: str | None
    ) -> TemporarySessionBinding | None:
        """Return the temporary Agent binding a copy of a temporary caller runs under."""
        owner = context.execution_owner
        if owner is None or (target_agent_id, target_project_id) != (
            context.agent_id,
            context.project_id,
        ):
            return None
        sessions = self._runtime.chat_sessions
        binding = await sessions.run_async(
            sessions.temporary_binding_by_participant,
            owner_name=owner.extension,
            group_id=owner.group_id,
            participant_id=owner.participant_id,
        )
        if binding is None or (
            binding.generation_id != owner.generation_id
            or binding.address.agent_id != target_agent_id
            or binding.address.project_id != target_project_id
        ):
            return None
        return cast(TemporarySessionBinding, binding)


def _implied_action(arguments: JsonObject, *, can_stop: bool) -> str | JsonObject:
    """Return the action a call without ``action`` clearly means, or its refusal.

    *can_stop*: the caller can call ``background_tasks``, so the refusal names its stop.
    """
    subagent_id = arguments.get("id")
    if isinstance(subagent_id, str) and subagent_id.strip():
        if has_text(arguments, "content"):
            return "send"
        stop = (
            SUBAGENT_ID_WITHOUT_ACTION_STOP_TEMPLATE.format(
                stop_call=call_text({"action": "stop", "id": subagent_id})
            )
            if can_stop
            else ""
        )
        return tool_failure(
            "invalid_arguments",
            SUBAGENT_ID_WITHOUT_ACTION_MESSAGE_TEMPLATE.format(
                id=subagent_id,
                stop=stop,
            ),
        )
    return "run"


def _not_found(subagent_id: str, own: list[SubAgentLink]) -> JsonObject:
    message = SUBAGENT_NOT_FOUND_MESSAGE_TEMPLATE.format(id=subagent_id, yours=yours_text(own))
    return tool_failure("subagent_not_found", message.rstrip())


def _caller(context: ToolContext) -> SessionAddress:
    return SessionAddress(
        project_id=context.project_id, agent_id=context.agent_id, session_id=context.session_id
    )


def _admission(
    context: ToolContext,
    subagent_id: str,
    working_project_id: str | None,
    *,
    expected_session_generation_id: str | None = None,
) -> RunAdmission:
    return RunAdmission(
        working_project_id=working_project_id,
        run_kind=RunKind.SUBAGENT,
        work_id=subagent_id,
        owner=context.execution_owner,
        contributes_to_agent_activity=context.execution_owner is None,
        expected_session_generation_id=expected_session_generation_id,
    )


def _public_identity(subagent_id: str, address: SessionAddress) -> JsonObject:
    """Identify a Sub-Agent for the calling Agent; ``agent_id`` is what the Tool accepts."""
    data: JsonObject = {
        "id": subagent_id,
        "agent_id": format_agent_address(address.agent_id, address.project_id),
        "session_id": address.session_id,
    }
    if address.project_id is not None:
        data["project_id"] = address.project_id
    return data


def _event_data(
    subagent_id: str,
    address: SessionAddress,
    *,
    status: str,
    run_id: str | None = None,
    activity_file: str | None = None,
) -> JsonObject:
    """Describe a Sub-Agent for WebUI events, with bare ids."""
    data: JsonObject = {
        "id": subagent_id,
        "agent_id": address.agent_id,
        "session_id": address.session_id,
        "status": status,
        "delivery": "automatic",
        "activity_file": activity_file,
    }
    if address.project_id is not None:
        data["project_id"] = address.project_id
    if run_id is not None:
        data["run_id"] = run_id
    return data


async def _emit(context: ToolContext, event: str, data: JsonObject) -> None:
    await context.emit(
        event,
        {
            "tool_call": {
                "id": context.tool_call_id,
                "index": context.tool_call_index,
                "name": context.tool_name,
            },
            "data": data,
        },
    )


async def _run_end(run: Run) -> None:
    try:
        await run.wait()
    except Exception:
        return


def _latest_tool_name(run: Run) -> str | None:
    for event in reversed(run.events):
        if event.type != "tool_call_started":
            continue
        tool_call = event.payload.get("tool_call")
        if isinstance(tool_call, dict):
            name = tool_call.get("name")
            if isinstance(name, str) and name:
                return name
    return None


def _log_message_delivery(delivery: asyncio.Future[None]) -> None:
    if delivery.cancelled() or delivery.exception() is None:
        return
    _LOGGER.warning("Sub-Agent message to its Parent failed: %s", delivery.exception())


def _session_title(description: str) -> str:
    return " ".join(description.split())[:SUBAGENT_SESSION_TITLE_MAX_CHARACTERS]


def _open_subagent_session(
    runtime: RuntimeServices,
    agent_id: str,
    project_id: str | None,
    title: str,
    subagent_id: str,
    context: ToolContext,
    overrides: dict[str, str],
) -> ChatSession:
    """Create the Sub-Agent Session, title it, link it to its Parent and store *overrides*.

    Blocking. One unit of Session work, so a cancelled Parent never leaves a
    created Sub-Agent Session without its Parent link. The Session works in
    :func:`_child_working_project`.
    """
    sessions = runtime.chat_sessions
    session = sessions.create(
        agent_id,
        project_id=project_id,
        working_project_id=(
            AGENT_DEFAULT_PROJECT
            if project_id is not None
            else _child_working_project(context, project_id)
        ),
    )
    address = session.address
    sessions.set_auto_title(address, title)
    link_session(
        sessions,
        address,
        subagent_id=subagent_id,
        parent=_caller(context),
        run_id=context.run_id,
        tool_call_id=context.tool_call_id,
        tool_call_index=context.tool_call_index,
    )
    if overrides:
        runtime.agent_resolver.update_session_overrides(address, overrides)
    return session


async def _remove_unstarted_session(runtime: RuntimeServices, address: SessionAddress) -> None:
    """Remove a Sub-Agent Session whose first Run could not start."""
    sessions = runtime.chat_sessions
    try:
        await sessions.run_async(sessions.delete, address)
    except Exception:
        _LOGGER.warning(
            "Could not remove a Sub-Agent Session whose Run did not start (session=%s)",
            address.session_id,
            exc_info=True,
        )


def _child_working_project(context: ToolContext, target_project_id: str | None) -> str | None:
    """Return the Project a new Sub-Agent Session works in.

    A Team target works in its Team's Project. An Identity target, including a
    copy of the Parent itself, works where the Parent's Run works, so delegated
    work stays in the Parent's working Project (or its Workspace).
    """
    return target_project_id if target_project_id is not None else context.working_project_id


def _load_subagent_settings(runtime: RuntimeServices) -> dict[str, int]:
    settings = runtime.storage.load_subagent_settings()
    return {
        "max_subagent_depth": _positive_int(
            settings.get("max_subagent_depth"), DEFAULT_MAX_SUBAGENT_DEPTH
        ),
        "max_active_subagents": _positive_int(
            settings.get("max_active_subagents"), DEFAULT_MAX_ACTIVE_SUBAGENTS
        ),
        "max_active_subagents_total": _positive_int(
            settings.get("max_active_subagents_total"), DEFAULT_MAX_ACTIVE_SUBAGENTS_TOTAL
        ),
    }


def _counts_as_working(runtime: RuntimeServices, link: SubAgentLink) -> bool:
    """Whether *link* takes a place under the limits: working, whatever its answer destination."""
    return is_working(runtime, link.session)


def _working_subagent_count(runtime: RuntimeServices, busy: Iterable[SessionAddress]) -> int:
    """Count the Sub-Agent Sessions among *busy*."""
    sessions = runtime.chat_sessions
    links = (read_link(sessions, address) for address in busy)
    return sum(1 for link in links if link is not None)


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, int) and value > 0:
        return value
    return default


def _parse_session_overrides(arguments: JsonObject) -> dict[str, str]:
    """Parse the Sub-Agent Session's Agent overrides the call sets.

    An omitted field keeps the Session's current value: the Agent's own for a
    new Sub-Agent, the stored override for an existing one. An empty
    ``thinking_effort`` string counts as omitted.
    """
    model = optional_text(arguments, "model")
    thinking_effort: str | None = None
    raw = arguments.get("thinking_effort")
    if isinstance(raw, str) and raw:
        thinking_effort = cast(
            str, validate_thinking_effort(raw, label="thinking_effort", allow_none=False)
        )
    overrides: dict[str, str] = {}
    if model is not None:
        overrides["model"] = model
    if thinking_effort is not None:
        overrides["thinking_effort"] = thinking_effort
    return overrides


async def _validate_target_agent(
    runtime: RuntimeServices,
    target_agent_id: str,
    project_id: str | None,
    *,
    model: str | None = None,
    temporary_parent_binding: TemporarySessionBinding | None = None,
    caller: ToolContext | None = None,
    overrides: dict[str, Any] | None = None,
) -> JsonObject | None:
    """Validate that the target resolves under its addressed Project and can run.

    Only a missing Agent (unknown, off-Team or the built-in Librarian) or
    Project reports ``agent_not_found`` / ``project_not_found``; a target that
    cannot run reports ``agent_unavailable`` with the resolver's reason, and a
    requested *model* that cannot run reports ``invalid_arguments``.
    """
    try:
        if temporary_parent_binding is not None:
            await runtime.agent_resolver.resolve_temporary_agent_async(
                temporary_parent_binding.address,
                generation_id=temporary_parent_binding.generation_id,
            )
        else:
            if (
                model is None
                and project_id is not None
                and caller is not None
                and overrides is not None
            ):
                parent_address = SessionAddress(
                    caller.project_id, caller.agent_id, caller.session_id
                )
                binding = await runtime.chat_sessions.run_async(
                    runtime.chat_sessions.temporary_binding, parent_address
                )
                if binding is not None:
                    parent = await runtime.agent_resolver.resolve_temporary_agent_async(
                        parent_address, generation_id=binding.generation_id, session=parent_address
                    )
                else:
                    parent = await runtime.agent_resolver.resolve_agent_async(
                        caller.project_id, caller.agent_id, session_id=caller.session_id
                    )
                target = await runtime.agent_resolver.resolve_delegated_agent_async(
                    project_id, target_agent_id, caller_model=parent.model
                )
                if getattr(target, "model_inherit", False):
                    overrides["model"] = parent.model
            else:
                target = await runtime.agent_resolver.resolve_agent_async(
                    project_id, target_agent_id
                )
            if is_builtin_agent(target):
                # A built-in Agent is no delegation target: it looks like no Agent at all.
                raise ResolutionAgentNotFoundError(f"Agent not found: {target_agent_id}")
        if model is not None:
            await runtime.agent_resolver.require_model_configured_async(model)
    except ResolutionProjectNotFoundError as error:
        return tool_failure("project_not_found", str(error))
    except ResolutionAgentNotFoundError as error:
        return tool_failure("agent_not_found", str(error))
    except AgentResolutionError as error:
        return tool_failure(
            "agent_unavailable",
            SUBAGENT_TARGET_UNAVAILABLE_MESSAGE_TEMPLATE.format(
                target=format_agent_address(target_agent_id, project_id), reason=error
            ),
            retryable=False,
        )
    except ModelConfigurationError as error:
        return tool_failure("invalid_arguments", str(error))
    return None


async def _validate_continued_session(
    runtime: RuntimeServices, link: SubAgentLink, *, model: str | None
) -> JsonObject | None:
    """Refuse a message to a Sub-Agent whose stored Agent settings stop its next Run."""
    resolver = runtime.agent_resolver
    try:
        if model is not None:
            await resolver.require_model_configured_async(model)
        stored = await resolver.session_overrides_async(link.session)
    except ModelConfigurationError as error:
        return tool_failure("invalid_arguments", str(error))
    except ValueError as error:
        return tool_failure(
            "invalid_arguments",
            SUBAGENT_SESSION_SETTINGS_UNREADABLE_MESSAGE_TEMPLATE.format(id=link.id, reason=error),
        )
    if model is not None or stored.model is None:
        return None
    try:
        await resolver.require_model_configured_async(stored.model)
    except ModelConfigurationError as error:
        return tool_failure(
            "invalid_arguments",
            SUBAGENT_SESSION_MODEL_UNUSABLE_MESSAGE_TEMPLATE.format(id=link.id, reason=error),
        )
    return None


def _resolve_target_address(address: str, caller_project_id: str | None) -> tuple[str, str | None]:
    """Resolve the Tool's address; a bare address inherits the caller's Project scope."""
    agent_id, addressed_project_id = parse_agent_address(address)
    return agent_id, addressed_project_id or caller_project_id


def _target_is_allowed(
    context: ToolContext, target_agent_id: str, target_project_id: str | None
) -> bool:
    """Check the caller's Tool settings and enforce the Project boundary independently."""
    if context.project_id is not None and target_project_id != context.project_id:
        return False
    if target_agent_id == context.agent_id and target_project_id == context.project_id:
        return True
    allowed = subagent_allowed_agents(context.tool_settings)
    if "*" in allowed:
        return True
    address = (
        target_agent_id
        if context.project_id is not None
        else format_agent_address(target_agent_id, target_project_id)
    )
    return address in allowed


__all__ = ["SubAgentCoordinator", "SubAgentState", "SubAgentStatus", "SubAgentStopOutcome"]
