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
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, cast

from core.chat.messages import SubAgentMessage
from core.projects import (
    InvalidAgentAddressError,
    WorkingProjectMissingError,
    format_agent_address,
)
from core.runs import (
    ActiveRunError,
    ChatRunManager,
    Run,
    RunNotFoundError,
)
from core.secrets.agent_secrets import SecretUseError
from core.sessions import (
    SessionAddress,
    SessionNotFoundError,
    TemporarySessionBinding,
)
from core.settings import SettingsValidationError
from core.subagents._constants import (
    MESSAGE_PARENT_DELIVERY_UNAVAILABLE_MESSAGE,
    MESSAGE_PARENT_NOT_SUBAGENT_MESSAGE,
    MESSAGE_PARENT_PARENT_GONE_MESSAGE,
    MESSAGE_PARENT_QUEUED_NOTE,
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
    SUBAGENT_SECRETS_NOTE_TEMPLATE,
    SUBAGENT_SEND_APP_LIMIT_MESSAGE_TEMPLATE,
    SUBAGENT_SEND_LIMIT_MESSAGE_TEMPLATE,
    SUBAGENT_SEND_QUEUED_NOTE,
    SUBAGENT_SEND_STARTED_NOTE,
    SUBAGENT_SEND_STEERED_NOTE,
    SUBAGENT_SEND_WITHOUT_CONTENT_MESSAGE_TEMPLATE,
    SUBAGENT_SEND_WITHOUT_ID_MESSAGE_TEMPLATE,
    SUBAGENT_SESSION_NOT_SUBAGENT_MESSAGE_TEMPLATE,
    SUBAGENT_SESSION_PROJECT_MISSING_MESSAGE_TEMPLATE,
    SUBAGENT_SESSION_STARTED_EVENT,
    SUBAGENT_STAND_IN_SESSION_NOTE_TEMPLATE,
    SUBAGENT_START_FIRST_TEXT,
    SUBAGENT_STARTED_NOTE,
    SUBAGENT_TARGET_NOT_ALLOWED_MESSAGE_TEMPLATE,
)
from core.subagents._delegation import (
    _admission,
    _caller,
    _child_working_project,
    _counts_as_working,
    _emit,
    _event_data,
    _load_subagent_settings,
    _narrow_session_execution,
    _nesting_limit_reached,
    _open_subagent_session,
    _parse_session_overrides,
    _public_identity,
    _remove_unstarted_session,
    _resolve_target_address,
    _session_title,
    _target_binding,
    _target_is_allowed,
    _validate_continued_session,
    _validate_target_agent,
    _working_subagent_count,
)
from core.subagents._interpretation import (
    SUBAGENT_ID_PATTERN,
    call_text,
    has_text,
    is_generic_target,
    optional_text,
    quoted,
    reads_as_new_session,
    target_choices,
    yours_text,
)
from core.subagents._tree import SubAgentTreeControl
from core.subagents._types import SubAgentState, SubAgentStopOutcome
from core.subagents._types import SubAgentStatus as SubAgentStatus
from core.subagents._types import SubAgentTree as SubAgentTree
from core.subagents.catalog import SubAgentPromptTarget, build_subagent_prompt_targets
from core.subagents.forwarding import (
    SubAgentActivities,
    SubAgentForwarding,
    is_working,
)
from core.subagents.links import (
    SubAgentLink,
    ancestors,
    children,
    find_link,
    read_link,
)
from core.tools.background_tasks import BACKGROUND_TASKS_TOOL_NAME
from core.tools.contracts import ToolContractError
from core.tools.secrets import secret_line
from core.tools.tools import (
    JsonObject,
    ToolContext,
    tool_failure,
    tool_success,
)
from core.utils.ids import new_id
from core.utils.logging import get_logger
from core.utils.workers import finish_despite_cancel

if TYPE_CHECKING:
    from core.runtime.interfaces import RuntimeServices
    from core.secrets.agent_secrets import AgentSecrets
    from core.sessions.session import ChatSession


_LOGGER = get_logger("subagents")


def _passed_secret_names(value: Any) -> list[str] | JsonObject:
    """The Secret names a ``run`` passes: a list, or one comma-separated string."""
    if value is None or value == "" or value == []:
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return tool_failure(
            "invalid_arguments",
            'Nothing was started. secrets takes Secret names, such as ["GITHUB"].',
        )
    return [item.strip() for item in value if item.strip()]


def _subagent_count(count: int) -> str:
    return f"{count} Sub-Agent" if count == 1 else f"{count} Sub-Agents"


class SubAgentCoordinator:
    """Run the Sub-Agent Tools and forward Sub-Agent answers to their Parents."""

    def __init__(
        self,
        runtime: RuntimeServices,
        trigger_service: Any,
        *,
        secrets: AgentSecrets | None = None,
    ) -> None:
        self._runtime = runtime
        self._secrets = secrets
        self._trigger_service = trigger_service
        self._activities = SubAgentActivities(runtime)
        self._forwarding = SubAgentForwarding(runtime, trigger_service, self._activities)
        # One Sub-Agent start at a time: the limit checks count working Sub-Agents,
        # a count that is stale once another start has finished meanwhile.
        self._admission_lock = asyncio.Lock()
        self._tree = SubAgentTreeControl(
            self._runtime, self._admission_lock, self._activities, self._forwarding
        )

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

    def subagent_tool_usable(self, address: SessionAddress) -> bool:
        """Return whether the Session at *address* has a use for ``subagent``. Blocking.

        Chat asks this for each Run of a Sub-Agent Session and leaves the Tool
        out of its Tool list when not. Below the nesting limit the Session can
        start Sub-Agents. At the limit, ``run`` is always refused, and only
        Sub-Agents of its own, started before the limit was lowered, give the
        Tool a use (``send``).
        """
        runtime = self._runtime
        if _nesting_limit_reached(runtime, address) is None:
            return True
        return bool(children(runtime.chat_sessions, address))

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
        activity = await self._activities.ensure(_caller(context), parent=parent)
        delivery_id = new_id("subagent-message")
        content = cast(str, arguments["content"]).strip()
        caller = _caller(context)
        delivery = self._trigger_service.submit_completion(
            parent,
            notice_id=delivery_id,
            origin_run_id=context.run_id,
            body=PARENT_MESSAGE_SECTION_TEMPLATE.format(
                id=link.id,
                title=link.title or link.id,
                delivery_id=delivery_id,
                content=content,
            ),
            execution_owner=context.execution_owner,
            subagent_message=SubAgentMessage(
                subagent_id=link.id,
                title=link.title or link.id,
                content=content,
                delivery_id=delivery_id,
                agent_id=caller.agent_id,
                session_id=caller.session_id,
                project_id=caller.project_id,
            ),
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
        self, agent_id: str, session_id: str, subagent_id: str, *, project_id: str | None = None
    ) -> JsonObject | None:
        return await self._tree.inspect(agent_id, session_id, subagent_id, project_id=project_id)

    async def stop_tree(self, address: SessionAddress) -> int:
        return await self._tree.stop_tree(address)

    async def work_snapshot(
        self, address: SessionAddress, *, include_entries: bool = False
    ) -> JsonObject:
        return await self._tree.work_snapshot(address, include_entries=include_entries)

    async def subagents(self, caller: SessionAddress) -> list[SubAgentState]:
        return await self._tree.subagents(caller)

    async def running_work(self, subagent: SubAgentState) -> list[str]:
        return await self._tree.running_work(subagent)

    async def stop_subagent(
        self, context: ToolContext, subagent: SubAgentState
    ) -> SubAgentStopOutcome:
        return await self._tree.stop_subagent(context, subagent)

    async def drain_activity(self) -> None:
        """Wait until the activity files' text so far is on disk."""
        await self._activities.drain()

    def forwarding_pending(self, address: SessionAddress) -> bool:
        """Whether a Run of the Session at *address* has not yet handed its answer
        to delivery; a Run its Parent stopped itself forwards nothing."""
        return self._forwarding.pending(address)

    def add_forwarded_callback(
        self, callback: Callable[[SessionAddress], None]
    ) -> Callable[[], None]:
        """Call *callback* with a Session's address once a Run of it is done
        forwarding, delivered or not. Returns the unsubscribe function."""
        return self._forwarding.add_forwarded_callback(callback)

    async def parent_link(self, address: SessionAddress) -> SubAgentLink | None:
        """Read the Session's Parent link off the Event Loop.

        None for a Session without one or one that no longer exists; raises when
        its metadata cannot be read.
        """
        sessions = self._runtime.chat_sessions
        return await sessions.run_async(read_link, sessions, address, strict=True)

    async def child_sessions(self, address: SessionAddress) -> list[SessionAddress]:
        """Read the indexed direct children, before resolving their current links."""
        sessions = self._runtime.chat_sessions
        return await sessions.run_async(sessions.subagent_children, address)

    async def parent_answer_pending(self, address: SessionAddress) -> bool:
        """Whether followed work already owes Parent an answer despite a later redirect."""
        return await self._forwarding.parent_answer_pending(address)

    def references_identity_agent(self, agent_id: str) -> bool:
        """Return whether a followed Run in an Identity Agent's Session is still forwarding."""
        return any(
            run.project_id is None and run.agent_id == agent_id
            for run in self._forwarding.followed_runs()
        )

    # run

    async def _run(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
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

        if not has_text(arguments, "content"):
            message = SUBAGENT_MISSING_TASK_MESSAGE
            if context.can_call(BACKGROUND_TASKS_TOOL_NAME):
                message += SUBAGENT_MISSING_TASK_LIST_TEXT
            return tool_failure("invalid_arguments", message)
        description = optional_text(arguments, "description")
        if description is None:
            return tool_failure("invalid_arguments", SUBAGENT_MISSING_DESCRIPTION_MESSAGE)

        passed = _passed_secret_names(arguments.get("secrets"))
        if isinstance(passed, dict):
            return passed
        if passed:
            if self._secrets is None:
                return tool_failure(
                    "secret_unavailable", "Nothing was started. Secrets are not available here."
                )
            try:
                self._secrets.passable(
                    SessionAddress(context.project_id, context.agent_id, context.session_id),
                    passed,
                    passed_only=context.subagent_session,
                )
            except SecretUseError as unavailable:
                return tool_failure("secret_unavailable", f"Nothing was started. {unavailable}")
        if arguments.get("background") is False:
            notes.append(SUBAGENT_BACKGROUND_IGNORED_NOTE)
        agent_address = optional_text(arguments, "agent_id")
        if agent_address is not None and await sessions.run_async(
            is_generic_target, runtime, context, agent_address
        ):
            notes.append(SUBAGENT_GENERIC_TARGET_NOTE_TEMPLATE.format(name=quoted(agent_address)))
            agent_address = None
        target_agent_id, target_project_id = _resolve_target_address(
            agent_address or context.agent_id, context.project_id
        )
        target_address = agent_address or format_agent_address(target_agent_id, target_project_id)
        overrides = _parse_session_overrides(arguments)
        if not _target_is_allowed(context, target_agent_id, target_project_id):
            choices = await sessions.run_async(target_choices, runtime, context)
            return tool_failure(
                "agent_not_allowed",
                SUBAGENT_TARGET_NOT_ALLOWED_MESSAGE_TEMPLATE.format(
                    target=target_address, choices=choices
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
                choices = await sessions.run_async(target_choices, runtime, context)
                return tool_failure(
                    error["code"],
                    f"Nothing was started. {reason}. {choices}",
                )
            return failure

        # Chat leaves the Tool out of the Tool list of a Session at the nesting
        # limit (``subagent_tool_usable``); this refusal stays the safety net.
        depth_limit = await sessions.run_async(_nesting_limit_reached, runtime, caller)
        if depth_limit is not None:
            return tool_failure(
                "subagent_depth_exceeded",
                SUBAGENT_DEPTH_LIMIT_MESSAGE_TEMPLATE.format(limit=depth_limit),
            )
        settings = _load_subagent_settings(runtime)
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
                    secrets=passed,
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
        overrides: dict[str, Any],
        temporary_parent: TemporarySessionBinding | None,
        notes: list[str],
        secrets: list[str] | None = None,
    ) -> JsonObject:
        """Create the Sub-Agent Session, start its Run and return the ``run`` result.

        A Run that cannot start removes the Session again, so no Sub-Agent
        exists without its first Run.
        """
        runtime = self._runtime
        sessions = runtime.chat_sessions
        new_subagent_id = new_id("sub")
        async with AsyncExitStack() as lifecycle:
            binding = _target_binding(context, target_agent_id, target_project_id)
            if binding is not None:
                # Agent create/rename/archive/restore hold this same lock. Keep
                # the identity check and Run admission together; afterwards an
                # active Run itself prevents replacing its Agent.
                await lifecycle.enter_async_context(runtime.automation_references.lock)
                if not await sessions.run_async(
                    runtime.agents.delegation_target_allowed, target_agent_id, binding
                ):
                    choices = await sessions.run_async(target_choices, runtime, context)
                    return tool_failure(
                        "agent_not_allowed",
                        SUBAGENT_TARGET_NOT_ALLOWED_MESSAGE_TEMPLATE.format(
                            target=target_agent_id, choices=choices
                        ),
                    )
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
                if secrets and self._secrets is not None:
                    await self._pass_secrets(context, session, secrets)
                run = await runtime.chat_run_manager.start(
                    address,
                    executor,
                    admission=_admission(
                        context,
                        new_subagent_id,
                        _child_working_project(context, target_project_id),
                        expected_session_generation_id=session.generation_id,
                    ),
                )
                await run.wait_admitted()
            except Exception:
                assert session.generation_id is not None
                await _remove_unstarted_session(runtime, address, session.generation_id)
                raise
        activity = await self._activities.ensure(address, parent=_caller(context))
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

    async def _pass_secrets(
        self, context: ToolContext, session: ChatSession, names: list[str]
    ) -> None:
        """Grant the Sub-Agent's Session the named Secrets and tell it which they are."""
        assert self._secrets is not None
        granted = await asyncio.to_thread(
            self._secrets.pass_to_session,
            SessionAddress(context.project_id, context.agent_id, context.session_id),
            names,
            session.address,
            passed_only=context.subagent_session,
        )
        lines = "\n".join(secret_line(secret) for secret in granted)
        await session.add_note_async(SUBAGENT_SECRETS_NOTE_TEMPLATE.format(secrets=lines))

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
                # The Parent's boundary now narrows the stored one: a Parent whose
                # rules were tightened since the spawn passes that on.
                ceiling = context.permissions.ceiling(source="parent")
                if ceiling is not None:
                    await sessions.run_async(
                        runtime.agent_resolver.join_session_ceiling, address, ceiling
                    )
                await sessions.run_async(
                    _narrow_session_execution, runtime, address, context.execution
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
        await self._activities.ensure(address, parent=_caller(context))
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


def _log_message_delivery(delivery: asyncio.Future[None]) -> None:
    if delivery.cancelled() or delivery.exception() is None:
        return
    _LOGGER.warning("Sub-Agent message to its Parent failed: %s", delivery.exception())


__all__ = ["SubAgentCoordinator", "SubAgentState", "SubAgentStatus", "SubAgentStopOutcome"]
