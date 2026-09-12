"""Private Built-in Command Session actions."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import TYPE_CHECKING, Any

from core.chat.commands import (
    _COMMAND_WORKERS,
    _LOGGER,
    CommandExecutionContext,
    CommandFeedback,
    CommandNavigation,
    CommandOutcome,
    CommandResourceChange,
    CommandRun,
    _command_session_io,
    _extract_text,
    _notice,
    _require_dependency,
    parse_agent_argument,
    parse_handoff_argument,
)
from core.chat.errors import CompactionUnavailableError
from core.chat.messages import ChatMessage
from core.projects import (
    AgentResolutionError,
    InvalidAgentAddressError,
    format_agent_address,
    parse_agent_address,
)
from core.runs import ActiveRunError, ChatRunManager, RunAdmissionBlockedError
from core.sessions import SESSION_MOVE_STRIP_META_KEYS, SessionAddress
from core.tools.availability import memory_tool_enabled
from core.tools.terminal_manager import TerminalManager, TerminalOwner

if TYPE_CHECKING:
    from core.agents import AgentStore
    from core.projects import AgentResolver, ProjectStore
    from core.sessions import ChatSessionManager


HANDOFF_FRAGMENT_NAME = "handoff.md"

LEARN_FRAGMENT_NAME = "learn.md"

CHANNEL_SOURCE_META_KEY = "source_channel_id"

SUBAGENT_SESSION_METADATA_FLAG = "is_subagent_session"

SUBAGENT_PARENT_METADATA_KEY = "subagent_parent"

AGENT_TAKEOVER_NOTE = "This session was just moved to you from {source}."


def _build_handoff_prompt(base_instruction: str, instruction: str | None) -> str:
    base = base_instruction.strip()
    cleaned = (instruction or "").strip()
    if not cleaned:
        return base
    return (
        f"{base}\n\n"
        "The user added a specific instruction for this handoff. Follow it while "
        "writing, without dropping anything else that genuinely matters:\n"
        f"{cleaned}"
    )


def _build_learn_prompt(base_instruction: str, argument: str | None) -> str:
    base = base_instruction.strip()
    cleaned = (argument or "").strip()
    if not cleaned:
        return (
            f"{base}\n\n"
            "No request was given. If the recent conversation clearly establishes reusable "
            "learning, apply the instructions above to it. Otherwise, ask the user what "
            "they want captured."
        )
    return f"{base}\n\nThe request to learn from:\n{cleaned}"


async def _execute_compact(
    context: CommandExecutionContext, argument: str | None, *, trigger_service: Any | None
) -> CommandOutcome:
    trigger_service = _require_dependency(trigger_service, "TriggerService")
    try:
        run = await trigger_service.start_compaction_run(
            context.agent_id,
            context.session_id,
            argument,
            project_id=context.project_id,
        )
    except CompactionUnavailableError:
        return _notice("compact", "Compaction is not available.")
    except ActiveRunError:
        return _notice(
            "compact",
            "Cannot compact while a run is active for this session.",
        )
    return CommandOutcome(
        command="compact",
        runs=(CommandRun(role="primary", run=run),),
    )


async def _execute_handoff(
    context: CommandExecutionContext,
    argument: str | None,
    *,
    agent_resolver: AgentResolver | None,
    agents: AgentStore | None,
    chat_runs: ChatRunManager,
    sessions: ChatSessionManager | None,
    storage: Any | None,
    trigger_service: Any | None,
) -> CommandOutcome:
    resolver = _require_dependency(agent_resolver, "AgentResolver")
    sessions = _require_dependency(sessions, "ChatSessionManager")
    storage = _require_dependency(storage, "StorageManager")
    trigger_service = _require_dependency(trigger_service, "TriggerService")
    parsed = parse_handoff_argument(argument)
    try:
        if parsed.target_agent_id is None:
            target_agent_id, target_project_id = context.agent_id, context.project_id
        else:
            target_agent_id, target_project_id = parse_agent_address(parsed.target_agent_id)
    except InvalidAgentAddressError:
        return _notice(
            "handoff", f"Cannot handoff to invalid agent address: {parsed.target_agent_id}"
        )

    target_display = format_agent_address(target_agent_id, target_project_id)
    if (
        chat_runs.active_run(
            agent_id=context.agent_id,
            session_id=context.session_id,
            project_id=context.project_id,
        )
        is not None
    ):
        return _notice("handoff", "A handoff can be started after the current run finishes.")

    if (target_agent_id, target_project_id) != (context.agent_id, context.project_id):
        try:
            await _COMMAND_WORKERS.run(
                resolver.resolve_agent,
                target_project_id,
                target_agent_id,
            )
        except AgentResolutionError:
            return _notice("handoff", f"Cannot handoff to unknown agent: {target_display}")

    handoff_prompt = await _COMMAND_WORKERS.run(
        storage.read_prompt_fragment,
        HANDOFF_FRAGMENT_NAME,
    )
    handoff_run = await trigger_service.trigger_run(
        context.agent_id,
        _build_handoff_prompt(handoff_prompt, parsed.instruction),
        session_id=context.session_id,
        project_id=context.project_id,
        internal=True,
        reply_surface=context.reply_surface,
    )
    handoff_message = await handoff_run.wait()
    handoff_text = _extract_text(handoff_message.content)
    if not handoff_text:
        return _notice("handoff", "Handoff could not be generated.")

    target_session = await _command_session_io(
        sessions,
        "create_async",
        "create",
        target_agent_id,
        project_id=target_project_id,
    )
    if target_project_id is None:
        agents = _require_dependency(agents, "AgentStore")
        await _COMMAND_WORKERS.run(
            agents.update,
            target_agent_id,
            current_session_id=target_session.id,
        )
    change = CommandResourceChange(kind="sessions", scope={"agent_id": target_agent_id})
    context.report_change(change)
    target_run = await trigger_service.trigger_run(
        target_agent_id,
        handoff_text,
        session_id=target_session.id,
        project_id=target_project_id,
        internal=False,
        reply_surface=context.reply_surface,
    )
    _LOGGER.info(
        "Session handoff created "
        "(source_agent=%s source_session=%s target_agent=%s target_session=%s)",
        format_agent_address(context.agent_id, context.project_id),
        context.session_id,
        target_display,
        target_session.id,
    )
    return CommandOutcome(
        command="handoff",
        feedback=CommandFeedback(
            kind="notice",
            text=f"Handoff sent to {target_display}, session {target_session.id}.",
        ),
        facts={"session_id": target_session.id, "agent_id": target_display},
        navigation=CommandNavigation(
            kind="offer_session",
            agent_id=target_agent_id,
            session_id=target_session.id,
            project_id=target_project_id,
        ),
        runs=(CommandRun(role="follow_up", run=target_run),),
        resource_changes=(change,),
    )


async def _execute_learn(
    context: CommandExecutionContext,
    argument: str | None,
    *,
    agent_resolver: AgentResolver | None,
    chat_runs: ChatRunManager,
    storage: Any | None,
    trigger_service: Any | None,
) -> CommandOutcome:
    resolver = _require_dependency(agent_resolver, "AgentResolver")
    storage = _require_dependency(storage, "StorageManager")
    trigger_service = _require_dependency(trigger_service, "TriggerService")
    if (
        chat_runs.active_run(
            agent_id=context.agent_id,
            session_id=context.session_id,
            project_id=context.project_id,
        )
        is not None
    ):
        return _notice("learn", "A skill can be authored after the current run finishes.")
    agent = await _COMMAND_WORKERS.run(
        resolver.resolve_agent,
        context.project_id,
        context.agent_id,
    )
    if not getattr(agent, "workspace", ""):
        return _notice("learn", "Skill authoring needs an identity agent with its own skill home.")
    learn_prompt = await _COMMAND_WORKERS.run(
        storage.read_prompt_fragment,
        LEARN_FRAGMENT_NAME,
    )
    learn_run = await trigger_service.trigger_run(
        context.agent_id,
        _build_learn_prompt(learn_prompt, argument),
        session_id=context.session_id,
        project_id=context.project_id,
        internal=True,
        reply_surface=context.reply_surface,
    )
    learn_message = await learn_run.wait()
    summary = _extract_text(learn_message.content) or "Skill authoring run completed."
    return _notice("learn", summary)


async def _execute_reflect(
    context: CommandExecutionContext,
    argument: str | None,
    *,
    agent_resolver: AgentResolver | None,
    chat_runs: ChatRunManager,
    reflection_service: Any | None,
) -> CommandOutcome:
    resolver = _require_dependency(agent_resolver, "AgentResolver")
    reflection = _require_dependency(reflection_service, "ReflectionService")
    if (
        chat_runs.active_run(
            agent_id=context.agent_id,
            session_id=context.session_id,
            project_id=context.project_id,
        )
        is not None
    ):
        return _notice("reflect", "A reflection can run after the current run finishes.")
    agent = await _COMMAND_WORKERS.run(
        resolver.resolve_agent,
        context.project_id,
        context.agent_id,
    )
    if not getattr(agent, "workspace", ""):
        return _notice(
            "reflect", "Reflection needs an identity agent with its own memory and skill home."
        )
    if not memory_tool_enabled(agent.memory_prompt_mode):
        return _notice("reflect", "Reflection needs the memory Tool to be active for this Agent.")
    focus = (argument or "").strip()
    extra_instruction = (
        f"The user asked you to focus this reflection on:\n{focus}" if focus else None
    )
    change = CommandResourceChange(kind="sessions", scope={"agent_id": context.agent_id})
    result = await reflection.run_review(
        context.agent_id,
        context.session_id,
        project_id=context.project_id,
        extra_instruction=extra_instruction,
        on_fork_created=lambda _fork_id: context.report_change(change),
        reply_surface=context.reply_surface,
    )
    await _COMMAND_WORKERS.run(
        reflection.reset_counters,
        context.agent_id,
        context.session_id,
        context.project_id,
    )
    return CommandOutcome(
        command="reflect",
        feedback=CommandFeedback(kind="notice", text=result.summary or "Reflection completed."),
        facts={"session_id": result.session_id, "agent_id": context.agent_id},
        resource_changes=(change,),
    )


async def _execute_agent(
    context: CommandExecutionContext,
    argument: str | None,
    *,
    agent_resolver: AgentResolver | None,
    agents: AgentStore | None,
    chat_runs: ChatRunManager,
    projects: ProjectStore | None,
    sessions: ChatSessionManager | None,
    terminal_manager: TerminalManager | None,
    trigger_service: Any | None,
) -> CommandOutcome:
    if argument is None:
        return CommandOutcome(
            command="agent",
            feedback=CommandFeedback(
                kind="detail",
                text=await _COMMAND_WORKERS.run(
                    partial(
                        _build_agent_directory,
                        agent_resolver=agent_resolver,
                        agents=agents,
                        projects=projects,
                    )
                ),
            ),
        )

    resolver = _require_dependency(agent_resolver, "AgentResolver")
    sessions = _require_dependency(sessions, "ChatSessionManager")
    agents = _require_dependency(agents, "AgentStore")
    parsed = parse_agent_argument(argument)
    source_display = format_agent_address(context.agent_id, context.project_id)
    try:
        target_agent_id, target_project_id = parse_agent_address(parsed.address)
    except InvalidAgentAddressError:
        return _notice("agent", f"Cannot move to invalid agent address: {parsed.address}")
    target_display = format_agent_address(target_agent_id, target_project_id)
    if (target_agent_id, target_project_id) == (context.agent_id, context.project_id):
        return _notice("agent", f"This session already belongs to {target_display}.")
    if (
        chat_runs.active_run(
            agent_id=context.agent_id,
            session_id=context.session_id,
            project_id=context.project_id,
        )
        is not None
    ):
        return _notice("agent", "This session can be moved once its current run finishes.")
    if chat_runs.list_queued(context.agent_id, context.session_id, project_id=context.project_id):
        return _notice("agent", "This session can be moved once its queued run finishes.")
    try:
        await _COMMAND_WORKERS.run(
            resolver.resolve_agent,
            target_project_id,
            target_agent_id,
        )
    except AgentResolutionError:
        return _notice("agent", f"Cannot move to unknown agent: {target_display}")

    source_address = SessionAddress(
        project_id=context.project_id, agent_id=context.agent_id, session_id=context.session_id
    )
    target_address = SessionAddress(
        project_id=target_project_id, agent_id=target_agent_id, session_id=context.session_id
    )
    try:
        async with chat_runs.session_admission_guard(source_address, target_address):
            metadata = await _command_session_io(
                sessions,
                "get_metadata_async",
                "get_metadata",
                source_address,
            )
            refusal = _session_move_block_reason(metadata)
            if refusal is not None:
                return _notice("agent", refusal)

            await sessions.move(
                source_address,
                target_address,
                strip_meta_keys=SESSION_MOVE_STRIP_META_KEYS,
            )
            async with sessions.write_lock(target_address):
                destination = await _command_session_io(
                    sessions,
                    "get_async",
                    "get",
                    target_address,
                )
                await _command_session_io(
                    destination,
                    "append_async",
                    "append",
                    ChatMessage.agent_takeover(
                        from_address=source_display, to_address=target_display
                    ),
                )
                await _command_session_io(
                    destination,
                    "add_note_async",
                    "add_note",
                    AGENT_TAKEOVER_NOTE.format(source=source_display),
                )

            if terminal_manager is not None:
                terminal_manager.transfer_scope(
                    TerminalOwner(
                        context.project_id,
                        context.agent_id,
                        context.session_id,
                    ),
                    TerminalOwner(
                        target_project_id,
                        target_agent_id,
                        context.session_id,
                    ),
                )

            if context.project_id is None:
                await _COMMAND_WORKERS.run(
                    agents.reset_current_after_session_removed,
                    context.agent_id,
                    context.session_id,
                )
            if target_project_id is None:
                await _COMMAND_WORKERS.run(
                    agents.update,
                    target_agent_id,
                    current_session_id=context.session_id,
                )
    except RunAdmissionBlockedError:
        return _notice(
            "agent", "This session can be moved once its source and destination are idle."
        )

    changes = [
        CommandResourceChange(kind="sessions", scope={"agent_id": context.agent_id}),
        CommandResourceChange(kind="sessions", scope={"agent_id": target_agent_id}),
    ]
    if context.project_id is None or target_project_id is None:
        changes.append(CommandResourceChange(kind="agents"))
    for change in changes:
        context.report_change(change)

    _LOGGER.info(
        "Session moved between Agents (session=%s source_agent=%s target_agent=%s)",
        context.session_id,
        source_display,
        target_display,
    )

    runs: tuple[CommandRun, ...] = ()
    if parsed.task is not None:
        trigger_service = _require_dependency(trigger_service, "TriggerService")
        task_run = await trigger_service.trigger_run(
            target_agent_id,
            parsed.task,
            session_id=context.session_id,
            project_id=target_project_id,
            internal=False,
            reply_surface=context.reply_surface,
        )
        runs = (CommandRun(role="follow_up", run=task_run),)
    reply = (
        f"Session moved to {target_display}; it is now running your task."
        if runs
        else f"Session moved to {target_display}; it is waiting."
    )
    return CommandOutcome(
        command="agent",
        feedback=CommandFeedback(kind="notice", text=reply),
        facts={"session_id": context.session_id, "agent_id": target_display},
        navigation=CommandNavigation(
            kind="offer_session",
            agent_id=target_agent_id,
            session_id=context.session_id,
            project_id=target_project_id,
        ),
        runs=runs,
        resource_changes=tuple(changes),
    )


def _session_move_block_reason(metadata: Mapping[str, object]) -> str | None:
    if metadata.get(CHANNEL_SOURCE_META_KEY):
        return "A channel-bound session cannot be moved to another agent."
    if metadata.get(SUBAGENT_SESSION_METADATA_FLAG) or metadata.get(SUBAGENT_PARENT_METADATA_KEY):
        return "A sub-agent session cannot be moved to another agent."
    return None


async def _execute_new(
    context: CommandExecutionContext,
    argument: str | None,
    *,
    agent_resolver: AgentResolver | None,
    agents: AgentStore | None,
    chat_runs: ChatRunManager,
    sessions: ChatSessionManager | None,
) -> CommandOutcome:
    resolver = _require_dependency(agent_resolver, "AgentResolver")
    sessions = _require_dependency(sessions, "ChatSessionManager")
    if (
        chat_runs.active_run(
            agent_id=context.agent_id,
            session_id=context.session_id,
            project_id=context.project_id,
        )
        is not None
    ):
        return _notice("new", "A new session can be started after the current run finishes.")
    await _COMMAND_WORKERS.run(
        resolver.resolve_agent,
        context.project_id,
        context.agent_id,
    )
    session = await _command_session_io(
        sessions,
        "create_async",
        "create",
        context.agent_id,
        session_id=context.preferred_new_session_id,
        project_id=context.project_id,
    )
    if context.project_id is None:
        agents = _require_dependency(agents, "AgentStore")
        await _COMMAND_WORKERS.run(
            agents.update,
            context.agent_id,
            current_session_id=session.id,
        )
    return CommandOutcome(
        command="new",
        feedback=CommandFeedback(kind="notice", text=f"New session started: {session.id}"),
        facts={"session_id": session.id},
        navigation=CommandNavigation(
            kind="continue_in_session",
            agent_id=context.agent_id,
            session_id=session.id,
            project_id=context.project_id,
        ),
        resource_changes=(
            CommandResourceChange(kind="sessions", scope={"agent_id": context.agent_id}),
        ),
    )


async def _execute_rename(
    context: CommandExecutionContext, argument: str | None, *, sessions: ChatSessionManager | None
) -> CommandOutcome:
    sessions = _require_dependency(sessions, "ChatSessionManager")
    stored_title = await _command_session_io(
        sessions,
        "set_title_async",
        "set_title",
        SessionAddress(
            project_id=context.project_id,
            agent_id=context.agent_id,
            session_id=context.session_id,
        ),
        argument or "",
    )
    reply = f"Session renamed to {stored_title}." if stored_title else "Session name cleared."
    return CommandOutcome(
        command="rename",
        feedback=CommandFeedback(kind="notice", text=reply),
        facts={"session_id": context.session_id, "title": stored_title},
    )


def _build_agent_directory(
    *,
    agent_resolver: AgentResolver | None,
    agents: AgentStore | None,
    projects: ProjectStore | None,
) -> str:
    """List the move targets: personal agents plus every project's team.

    Bare ids are personal agents; team agents are shown project-qualified as
    ``name@projekt`` through the one address seam, so the card itself teaches
    the addressing the move expects. A project whose scan fails is skipped
    rather than failing the whole card.
    """
    lines = ["Move this session to another agent with /agent <id> [task].", ""]

    personal = sorted(agent.id for agent in agents.list()) if agents else []
    lines.append("Personal agents:")
    if personal:
        lines.extend(f"  {format_agent_address(agent_id, None)}" for agent_id in personal)
    else:
        lines.append("  (none)")

    if projects is not None and agent_resolver is not None:
        for project in projects.list():
            try:
                team = agent_resolver.scan_project_report(project).team
            except Exception:
                _LOGGER.warning(
                    "Failed to scan project %r while building the /agent directory",
                    project.project_id,
                    exc_info=True,
                )
                continue
            if not team:
                continue
            lines.append("")
            lines.append(f"Team — {project.display_name} ({project.project_id}):")
            lines.extend(
                f"  {format_agent_address(member.agent_id, project.project_id)}"
                for member in sorted(team, key=lambda member: member.agent_id)
            )
    return "\n".join(lines)
