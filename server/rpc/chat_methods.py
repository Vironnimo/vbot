"""Chat RPC handlers."""

from __future__ import annotations

from typing import Any, cast

from core.chat import (
    ChatMessage,
    CommandAction,
    CommandHandled,
    parse_agent_argument,
    parse_handoff_argument,
)
from core.chat.chat import VISITED_PROJECTS_META_KEY
from core.chat.content_blocks import ContentBlock, TextBlock
from core.projects import (
    AgentResolutionError,
    InvalidAgentAddressError,
    format_agent_address,
    parse_agent_address,
)
from core.runs import ActiveRunError, ChatRunManager, QueuedRunItem
from core.subagents.subagents import (
    SUBAGENT_PARENT_METADATA_KEY,
    SUBAGENT_SESSION_METADATA_FLAG,
)
from server.events import RESOURCE_KIND_QUEUE
from server.rpc.agent_methods import _create_session, _rename_session
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_QUEUE_ITEM_NOT_FOUND,
    RPC_ERROR_RUN_NOT_FOUND,
    RpcError,
)
from server.rpc.event_bridge import (
    _bridge_queued_item_to_event_bus,
    _bridge_run_to_event_bus,
    publish_resource_changed,
)
from server.rpc.payloads import (
    _is_visible_history_message,
    _queued_response,
    _run_response,
    _visible_message,
)
from server.rpc.runtime_access import (
    _build_streaming_queue_update,
    _state_chat_runs,
    _state_command_dispatcher,
    _streaming_chat_loop,
)
from server.rpc.validation import (
    _ensure_model_connection_supported,
    _optional_chat_input_origin,
    _optional_positive_integer,
    _optional_string,
    _parse_chat_content,
    _required_agent_address,
    _required_string,
)

JsonObject = dict[str, Any]
MAX_CHAT_HISTORY_LIMIT = 500

# Instruction sent to the current agent to write the handoff. Plain text, not
# i18n: it is delivered as an internal note to the model and never shown to the
# user. The wording is deliberate — do not paraphrase.
HANDOFF_INSTRUCTION = (
    "You are handing off this conversation to another agent who will continue it in a "
    "fresh session with none of its context. Write a handoff so they can carry on "
    "seamlessly, as if they had been here the whole time.\n"
    "\n"
    "Capture whatever actually matters in this conversation so far — what it has been "
    "about, what has been said, established, or decided, and where things currently "
    "stand. What that includes depends entirely on the conversation: it might be a "
    "task in progress, a discussion, a decision being worked through, or anything "
    "else. Include only what is genuinely relevant here and leave out the rest; do not "
    "force it into a fixed structure or invent things that are not there.\n"
    "\n"
    "Write it entirely from this conversation — do not use tools or go check anything. "
    "Write it as a briefing to the next agent, in the language of this conversation, "
    "and output only the handoff itself, with no preamble and no sign-off, because "
    "your reply becomes their first message."
)

# The /learn authoring brief seeded into the internal run. Kept as a constant
# (like HANDOFF_INSTRUCTION) rather than a resource file so the command needs no
# RPC-time file I/O. It embeds the skill authoring standards and instructs the
# agent to author exactly one skill into its own home via the skill_manage tool.
LEARN_INSTRUCTION = (
    "Author a reusable skill for yourself from the source described below. A skill is a "
    "SKILL.md playbook that teaches you how to handle a specific task or domain.\n"
    "\n"
    'Use the `skill_manage` tool with operation "create" to write exactly one well-formed '
    "skill into your own skill home. Give it a short, descriptive, hyphenated name; the "
    "SKILL.md needs YAML front matter with `name` (matching the skill's directory) and a "
    "`description` of at most 60 characters that says when to use the skill. Structure the "
    "body with clear sections in a fixed order: Overview (what it is for and when to use "
    "it), Steps (the procedure), then Notes (edge cases and gotchas). Keep it concise and "
    "actionable.\n"
    "\n"
    "Frame any tool usage in terms of vBot's actual tools — `read`, `write`, `edit`, "
    "`glob`, `grep`, `bash`, `web_fetch`, `web_search`, `process`, `status` — and do not "
    "invent tools, commands, or facts that are not in the source. If the source is a folder "
    "or URL, read it first with your file/web tools; if it is the recent conversation or "
    "pasted text, work from that. Capture only what is genuinely there.\n"
    "\n"
    "After creating the skill, tell the user in one or two sentences what skill you created "
    "and when it will help. Do not paste the full SKILL.md back."
)

# Sidecar marker on a channel-bound session; such sessions are excluded from
# ``/agent`` moves so the channel pointer is never left dangling.
CHANNEL_SOURCE_META_KEY = "source_channel_id"

# Silent takeover note delivered to the receiving agent on its next request as a
# <system-reminder>. Plain text, not i18n — an internal note to the model, never
# shown to the user. The wording is deliberate; do not paraphrase.
AGENT_TAKEOVER_NOTE = "This session was just moved to you from {source}."


def _publish_queue_changed(state: Any, agent_id: str, session_id: str) -> None:
    """Signal that one session's queue changed so other windows reload it live.

    Scoped to the affected session (bare agent id, as the queue is keyed) so
    windows on a different session ignore it. Only the browser/RPC send surface
    emits this — core enqueues (automation, channels, sub-agents) deliberately
    do not, keeping the chat core untouched; those windows still catch up on the
    next terminal event.
    """
    publish_resource_changed(
        state,
        RESOURCE_KIND_QUEUE,
        scope={"agent_id": agent_id, "session_id": session_id},
    )


def _chat_history(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {"agent_id", "session_id", "limit", "before"}
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.history fields: {', '.join(unsupported_fields)}",
        )

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    limit = _optional_positive_integer(params, "limit", max_value=MAX_CHAT_HISTORY_LIMIT)
    before = _optional_string(params, "before")
    try:
        active_session_id = _resolve_history_session_id(state, agent_id, session_id, project_id)
        session = state.runtime.chat_sessions.get(agent_id, active_session_id, project_id)
        visible_messages = [
            _visible_message(message)
            for message in session.load()
            if _is_visible_history_message(message)
        ]
        messages, has_more = _history_page(visible_messages, limit=limit, before=before)
        active_run = _active_run_response(state, agent_id, active_session_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response: JsonObject = {
        "agent_id": agent_id,
        "session_id": active_session_id,
        "messages": messages,
        "has_more": has_more,
    }
    if active_run is not None:
        response["active_run"] = active_run
    return response


def _resolve_history_session_id(
    state: Any, agent_id: str, session_id: str | None, project_id: str | None
) -> str:
    """Pick the session to read history from for an identity or project address.

    Identity (``project_id is None``) keeps today's behavior exactly: an explicit
    ``session_id`` wins, otherwise the identity agent's ``current_session_id``. A
    project session has no anchor-level current pointer (the config agent carries
    none), so an explicit ``session_id`` is required and a missing one is a clean
    client error.
    """
    if session_id is not None:
        return session_id
    if project_id is None:
        return cast(str, state.runtime.agents.get(agent_id).current_session_id)
    raise RpcError(
        RPC_ERROR_INVALID_REQUEST,
        "params.session_id is required for a project agent address",
    )


def _history_page(
    messages: list[JsonObject], *, limit: int | None, before: str | None
) -> tuple[list[JsonObject], bool]:
    page_source = messages
    if before is not None:
        before_index = next(
            (index for index, message in enumerate(messages) if message.get("id") == before),
            None,
        )
        if before_index is None:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.before must reference a message id")
        page_source = messages[:before_index]

    if limit is None:
        return list(page_source), False

    page = page_source[-limit:]
    return page, len(page_source) > len(page)


def _extract_command_text(content: str | list[ContentBlock]) -> str | None:
    if isinstance(content, str):
        return content

    if len(content) != 1:
        return None

    block = content[0]
    if isinstance(block, TextBlock):
        return block.text
    return None


def _command_handled_response(
    result: CommandHandled | str | None,
    *,
    output: str | None = None,
) -> JsonObject:
    if isinstance(result, CommandHandled):
        reply = result.reply
        data = result.data
        channel = output or result.output
    else:
        reply = result
        data = None
        channel = output

    response: JsonObject = {
        "command_handled": True,
        "reply": reply or "",
        # The output channel travels with the handled command so the frontend
        # presents it (toast / transient card / action) without a second lookup
        # by command name. Defaults to "toast" for replies that carry no channel.
        "output": channel or "toast",
    }
    if data:
        response["data"] = dict(data)
    return response


async def _dispatch_chat_command(
    state: Any,
    agent_id: str,
    session_id: str,
    command_text: str,
    *,
    streaming: bool,
    project_id: str | None = None,
) -> JsonObject | None:
    try:
        command_result = _state_command_dispatcher(state).dispatch(
            agent_id,
            session_id,
            command_text,
            project_id,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if isinstance(command_result, CommandHandled):
        return _command_handled_response(command_result)
    if isinstance(command_result, CommandAction):
        return await _handle_command_action(
            state,
            agent_id,
            session_id,
            command_result,
            streaming=streaming,
            project_id=project_id,
        )
    return None


async def _handle_command_action(
    state: Any,
    agent_id: str,
    session_id: str,
    command_action: CommandAction,
    *,
    streaming: bool,
    project_id: str | None = None,
) -> JsonObject:
    match command_action.name:
        case "compact":
            return await _handle_compact_command(
                state, agent_id, session_id, command_action.argument, project_id=project_id
            )
        case "handoff":
            return await _handle_handoff_command(
                state, agent_id, session_id, command_action.argument, project_id=project_id
            )
        case "learn":
            return await _handle_learn_command(
                state, agent_id, session_id, command_action.argument, project_id=project_id
            )
        case "move_session":
            return await _handle_move_session_command(
                state, agent_id, session_id, command_action.argument, project_id=project_id
            )
        case "new_session":
            return _handle_new_session_command(state, agent_id, session_id, project_id=project_id)
        case "rename_session":
            return _handle_rename_session_command(
                state, agent_id, session_id, command_action.argument, project_id=project_id
            )
        case "set_model":
            return _handle_set_model_command(
                state, agent_id, session_id, command_action.argument, project_id=project_id
            )
        case "retry_last_turn":
            return await _retry_chat_for_ids(
                state, agent_id, session_id, streaming=streaming, project_id=project_id
            )
    raise AssertionError(f"unsupported command action: {command_action.name}")


def _format_session_agent(agent_id: str, project_id: str | None) -> str:
    """Build the ``session.create`` ``agent_id`` value for an (agent, project) pair.

    ``session.create`` re-parses the address at its own entry (the single seam),
    so chat hands it back the outside spelling: a bare id for an identity
    session, ``agent@projekt`` for a project session.
    """
    return format_agent_address(agent_id, project_id)


def _handle_new_session_command(
    state: Any, agent_id: str, session_id: str, *, project_id: str | None = None
) -> JsonObject:
    try:
        active_run = _state_chat_runs(state).active_run(agent_id=agent_id, session_id=session_id)
        if active_run is not None:
            return _command_handled_response(
                "A new session can be started after the current run finishes.",
            )

        response = _create_session(
            state,
            {"agent_id": _format_session_agent(agent_id, project_id), "make_current": True},
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    new_session_id = _required_string(response, "session_id")
    return _command_handled_response(
        CommandHandled(
            reply=f"New session started: {new_session_id}",
            data={"command": "new", "session_id": new_session_id},
        ),
        output="action",
    )


def _handle_rename_session_command(
    state: Any,
    agent_id: str,
    session_id: str,
    argument: str | None,
    *,
    project_id: str | None = None,
) -> JsonObject:
    """Set or clear this session's name from ``/rename [name]``.

    A thin trigger over the same ``session.rename`` handler the WebUI calls, so
    the title write and the session-list refresh event stay in one place. No
    argument clears the name (the session reverts to its automatic display); the
    confirmation reflects the stored title after normalization.
    """
    try:
        response = _rename_session(
            state,
            {
                "agent_id": _format_session_agent(agent_id, project_id),
                "session_id": session_id,
                "title": argument or "",
            },
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    stored_title = response.get("title")
    reply = f"Session renamed to {stored_title}." if stored_title else "Session name cleared."
    return _command_handled_response(
        CommandHandled(
            reply=reply,
            data={"command": "rename", "session_id": session_id, "title": stored_title},
        ),
        output="toast",
    )


# Token that resets a /model selection back to the default chain, case-insensitive.
_MODEL_RESET_TOKEN = "reset"


def _handle_set_model_command(
    state: Any,
    agent_id: str,
    session_id: str,
    argument: str | None,
    *,
    project_id: str | None = None,
) -> JsonObject:
    """Persistently set or reset the session agent's model from ``/model <value>``.

    A case-insensitive ``reset`` clears the selection; any other text is a model
    value validated against actually-usable models. Routing follows the session
    kind: an identity session writes the agent's own ``model`` field (empty on
    reset → the global default), a project session writes/clears a per-agent
    override in ``project.json`` (the top model-chain tier). The change takes effect
    on the next run, so there is no busy-guard.
    """
    raw = (argument or "").strip()
    is_reset = raw.lower() == _MODEL_RESET_TOKEN
    model = "" if is_reset else raw
    if not is_reset:
        _ensure_model_usable(state, model)

    try:
        if project_id is None:
            state.runtime.agents.update(agent_id, model=model)
        elif is_reset:
            state.runtime.projects.clear_model_override(project_id, agent_id)
        else:
            state.runtime.projects.set_model_override(project_id, agent_id, model)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    reply = "Model reset." if is_reset else f"Model set to {model}."
    return _command_handled_response(
        CommandHandled(
            reply=reply,
            data={"command": "model", "agent_id": agent_id, "model": model},
        ),
        output="toast",
    )


def _ensure_model_usable(state: Any, model: str) -> None:
    """Reject a ``/model`` value that is not actually usable in this instance.

    Two gates, both surfaced as ``invalid_request``: the model must be configured
    here (provider registered, in catalog, usable credential — the resolver's
    public ``is_model_configured`` seam, the same rule behind the scan's BAD_MODEL
    finding), and a pinned ``::connection`` suffix must be allowed by the model's
    connection allowlist (the same save-time guard the ``agent.*`` RPC uses).
    """
    if not state.runtime.agent_resolver.is_model_configured(model):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"model {model!r} is not usable in this instance "
            "(unknown provider/model or no usable credential)",
        )
    _ensure_model_connection_supported(state.runtime.models, "model", model)


def _build_handoff_prompt(instruction: str | None) -> str:
    """Weave an optional user instruction into the base handoff prompt.

    Mirrors the `/compact <instruction>` pattern: the bare handoff prompt is
    unchanged when no instruction is given, so the no-argument path is identical
    to before.
    """
    cleaned = (instruction or "").strip()
    if not cleaned:
        return HANDOFF_INSTRUCTION
    return (
        f"{HANDOFF_INSTRUCTION}\n"
        "\n"
        "The user added a specific instruction for this handoff. Follow it while "
        "writing, without dropping anything else that genuinely matters:\n"
        f"{cleaned}"
    )


def _build_learn_prompt(source: str | None) -> str:
    """Weave the optional ``/learn`` source into the base authoring brief.

    With a source (a folder, URL, description, or pasted text) the agent authors from
    it; with no argument it asks the user what to learn or, when the recent
    conversation clearly shows a reusable procedure, authors a skill from that.
    """
    cleaned = (source or "").strip()
    if not cleaned:
        return (
            f"{LEARN_INSTRUCTION}\n"
            "\n"
            "No source was given. Ask the user what they want captured into a skill, or, "
            "if the recent conversation clearly demonstrates a reusable procedure, author "
            "a skill from that."
        )
    return f"{LEARN_INSTRUCTION}\n\nThe source to learn from:\n{cleaned}"


async def _start_command_run(
    state: Any,
    agent_id: str,
    message: str,
    *,
    session_id: str,
    project_id: str | None,
    internal: bool,
) -> Any:
    """Start a command-driven run, identity via the trigger service, project on the loop.

    Shared by ``/handoff``, agent takeover, and ``/learn``. An identity run
    (``project_id is None``) goes through ``trigger_service.trigger_run`` (with its
    queue-on-busy fallback); a project run has no project-aware trigger service yet
    (automation is a separate task), so it goes straight through the chat loop, which
    already threads ``project_id`` into the session anchor and run.
    """
    if project_id is None:
        return await state.runtime.trigger_service.trigger_run(
            agent_id,
            message,
            session_id=session_id,
            internal=internal,
        )
    return await state.chat_loop.start_run(
        agent_id,
        message,
        session_id=session_id,
        internal=internal,
        project_id=project_id,
    )


async def _handle_learn_command(
    state: Any,
    agent_id: str,
    session_id: str,
    argument: str | None,
    *,
    project_id: str | None = None,
) -> JsonObject:
    """Author a skill via an internal run seeded with the ``/learn`` brief.

    Mirrors ``/handoff``: an internal run (the brief rides in as a note, the agent
    acts on it with the always-available ``skill_manage`` tool) authors into the
    agent's own home, then we report the agent's summary. Refused while another run
    is active, like a handoff.
    """
    active_run = _state_chat_runs(state).active_run(agent_id=agent_id, session_id=session_id)
    if active_run is not None:
        return _command_handled_response("A skill can be authored after the current run finishes.")
    try:
        learn_run = await _start_command_run(
            state,
            agent_id,
            _build_learn_prompt(argument),
            session_id=session_id,
            project_id=project_id,
            internal=True,
        )
        learn_message = await learn_run.wait()
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    summary = _extract_handoff_text(learn_message.content)
    return _command_handled_response(summary or "Skill authoring run completed.")


async def _handle_handoff_command(
    state: Any,
    agent_id: str,
    session_id: str,
    argument: str | None,
    *,
    project_id: str | None = None,
) -> JsonObject:
    parsed = parse_handoff_argument(argument)

    # The handoff target may itself be project-qualified (``agent:orchestrator@vbot``).
    # Parse it through the single address seam; with no explicit target the handoff
    # stays in the source's (agent, project) scope.
    try:
        if parsed.target_agent_id is not None:
            target_agent_id, target_project_id = parse_agent_address(parsed.target_agent_id)
        else:
            target_agent_id, target_project_id = agent_id, project_id
    except InvalidAgentAddressError:
        return _command_handled_response(
            f"Cannot handoff to invalid agent address: {parsed.target_agent_id}",
        )

    target_display = format_agent_address(target_agent_id, target_project_id)
    try:
        active_run = _state_chat_runs(state).active_run(agent_id=agent_id, session_id=session_id)
        if active_run is not None:
            return _command_handled_response(
                "A handoff can be started after the current run finishes.",
            )

        if (target_agent_id, target_project_id) != (agent_id, project_id):
            try:
                state.runtime.agent_resolver.resolve_agent(target_project_id, target_agent_id)
            except AgentResolutionError:
                return _command_handled_response(
                    f"Cannot handoff to unknown agent: {target_display}",
                )

        handoff_run = await _start_command_run(
            state,
            agent_id,
            _build_handoff_prompt(parsed.instruction),
            session_id=session_id,
            project_id=project_id,
            internal=True,
        )
        handoff_message = await handoff_run.wait()
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    handoff_text = _extract_handoff_text(handoff_message.content)
    if not handoff_text:
        return _command_handled_response("Handoff could not be generated.")

    try:
        response = _create_session(
            state,
            {
                "agent_id": _format_session_agent(target_agent_id, target_project_id),
                "make_current": True,
            },
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    new_session_id = _required_string(response, "session_id")

    try:
        run = await _start_command_run(
            state,
            target_agent_id,
            handoff_text,
            session_id=new_session_id,
            project_id=target_project_id,
            internal=False,
        )
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return _command_handled_response(
        CommandHandled(
            reply=f"Handoff sent to {target_display}, session {new_session_id}.",
            data={
                "command": "handoff",
                "session_id": new_session_id,
                "agent_id": target_display,
            },
        ),
        output="action",
    )


def _extract_handoff_text(content: str | list[ContentBlock] | None) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(block.text for block in content if isinstance(block, TextBlock)).strip()
    return ""


def _session_move_block_reason(metadata: JsonObject) -> str | None:
    """Return why a session may not be moved, or ``None`` when it may.

    Channel- and sub-agent-bound sessions are excluded in v1: moving a
    channel-bound session would orphan its channel pointer, and moving a
    sub-agent session would break its parent linkage.
    """
    if metadata.get(CHANNEL_SOURCE_META_KEY):
        return "A channel-bound session cannot be moved to another agent."
    if metadata.get(SUBAGENT_SESSION_METADATA_FLAG) or metadata.get(SUBAGENT_PARENT_METADATA_KEY):
        return "A sub-agent session cannot be moved to another agent."
    return None


async def _handle_move_session_command(
    state: Any,
    agent_id: str,
    session_id: str,
    argument: str | None,
    *,
    project_id: str | None = None,
) -> JsonObject:
    """Relocate the current session to another agent (the ``/agent`` move).

    Unlike ``/handoff`` (a summary into a fresh session), this moves the **same**
    session with its full verbatim history. Every guard refuses cleanly *before*
    any relocation, so a refused move never leaves partial state. After the move a
    visible takeover divider and a silent takeover note are persisted at the
    destination, the "current" pointers follow the session on each identity side,
    and an optional task auto-runs the receiving agent.
    """
    parsed = parse_agent_argument(argument or "")
    source_display = format_agent_address(agent_id, project_id)
    try:
        target_agent_id, target_project_id = parse_agent_address(parsed.address)
    except InvalidAgentAddressError:
        return _command_handled_response(f"Cannot move to invalid agent address: {parsed.address}")

    target_display = format_agent_address(target_agent_id, target_project_id)
    if (target_agent_id, target_project_id) == (agent_id, project_id):
        return _command_handled_response(f"This session already belongs to {target_display}.")

    chat_runs = _state_chat_runs(state)
    if chat_runs.active_run(agent_id=agent_id, session_id=session_id) is not None:
        return _command_handled_response("This session can be moved once its current run finishes.")
    if chat_runs.list_queued(agent_id, session_id):
        return _command_handled_response("This session can be moved once its queued run finishes.")

    chat_sessions = state.runtime.chat_sessions
    try:
        state.runtime.agent_resolver.resolve_agent(target_project_id, target_agent_id)
    except AgentResolutionError:
        return _command_handled_response(f"Cannot move to unknown agent: {target_display}")
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    try:
        metadata = chat_sessions.get_metadata(agent_id, session_id, project_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    refusal = _session_move_block_reason(metadata)
    if refusal is not None:
        return _command_handled_response(refusal)

    try:
        await chat_sessions.move(
            agent_id,
            session_id,
            target_agent_id,
            source_project_id=project_id,
            target_project_id=target_project_id,
            strip_meta_keys=frozenset({VISITED_PROJECTS_META_KEY}),
        )
        async with chat_sessions.write_lock(target_agent_id, session_id, target_project_id):
            destination = chat_sessions.get(target_agent_id, session_id, target_project_id)
            destination.append(
                ChatMessage.agent_takeover(from_address=source_display, to_address=target_display)
            )
            destination.add_note(AGENT_TAKEOVER_NOTE.format(source=source_display))

        # "Current" pointers follow the session on each identity side; a project
        # config agent carries no server-side current (the accessor picks locally).
        if project_id is None:
            state.runtime.agents.reset_current_after_session_removed(agent_id, session_id)
        if target_project_id is None:
            state.runtime.agents.update(target_agent_id, current_session_id=session_id)

        run = None
        if parsed.task is not None:
            run = await _start_command_run(
                state,
                target_agent_id,
                parsed.task,
                session_id=session_id,
                project_id=target_project_id,
                internal=False,
            )
            _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    reply = (
        f"Session moved to {target_display}; it is now running your task."
        if run is not None
        else f"Session moved to {target_display}; it is waiting."
    )
    return _command_handled_response(
        CommandHandled(
            reply=reply,
            data={"command": "agent", "session_id": session_id, "agent_id": target_display},
        ),
        output="action",
    )


async def _send_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)

    command_text = _extract_command_text(content)
    if command_text is not None:
        command_response = await _dispatch_chat_command(
            state,
            agent_id,
            session_id,
            command_text,
            streaming=False,
            project_id=project_id,
        )
        if command_response is not None:
            return command_response

    try:
        if input_origin is None:
            run = await state.chat_loop.start_run(
                agent_id, content, session_id=session_id, project_id=project_id
            )
        else:
            run = await state.chat_loop.start_run(
                agent_id,
                content,
                session_id=session_id,
                input_origin=input_origin,
                project_id=project_id,
            )
    except ActiveRunError:
        try:
            if input_origin is None:
                queued_item = await state.chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                    project_id=project_id,
                )
            else:
                queued_item = await state.chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                    input_origin=input_origin,
                    project_id=project_id,
                )
            _bridge_queued_item_to_event_bus(state, queued_item)
            _publish_queue_changed(state, agent_id, session_id)
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        return _queued_response(queued_item)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    try:
        _bridge_run_to_event_bus(state, run)
        assistant_message = await run.wait()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, final_message=assistant_message)


async def _stream_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)

    command_text = _extract_command_text(content)
    if command_text is not None:
        command_response = await _dispatch_chat_command(
            state,
            agent_id,
            session_id,
            command_text,
            streaming=True,
            project_id=project_id,
        )
        if command_response is not None:
            return command_response

    streaming_chat_loop = _streaming_chat_loop(state)
    try:
        if input_origin is None:
            run = await streaming_chat_loop.start_run(
                agent_id, content, session_id=session_id, project_id=project_id
            )
        else:
            run = await streaming_chat_loop.start_run(
                agent_id,
                content,
                session_id=session_id,
                input_origin=input_origin,
                project_id=project_id,
            )
    except ActiveRunError:
        try:
            if input_origin is None:
                queued_item = await streaming_chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                    project_id=project_id,
                )
            else:
                queued_item = await streaming_chat_loop.queue_run(
                    agent_id,
                    content,
                    session_id=session_id,
                    input_origin=input_origin,
                    project_id=project_id,
                )
            _bridge_queued_item_to_event_bus(state, queued_item)
            _publish_queue_changed(state, agent_id, session_id)
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        return _queued_response(queued_item)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    try:
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, sse_url=f"/api/runs/{run.id}/events")


async def _handle_compact_command(
    state: Any,
    agent_id: str,
    session_id: str,
    instruction: str | None = None,
    *,
    project_id: str | None = None,
) -> JsonObject:
    try:
        reply = await state.runtime.trigger_service.compact_session(
            agent_id, session_id, instruction, project_id=project_id
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _command_handled_response(reply, output="toast")


async def _retry_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    return await _retry_chat_for_ids(
        state, agent_id, session_id, streaming=True, project_id=project_id
    )


async def _retry_chat_for_ids(
    state: Any,
    agent_id: str,
    session_id: str,
    *,
    streaming: bool,
    project_id: str | None = None,
) -> JsonObject:
    try:
        chat_loop = _streaming_chat_loop(state) if streaming else state.chat_loop
        run = await chat_loop.retry_run(agent_id, session_id, project_id=project_id)
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if streaming:
        return _run_response(run, sse_url=f"/api/runs/{run.id}/events")

    try:
        assistant_message = await run.wait()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, final_message=assistant_message)


async def _cancel_chat(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"run_id", "reason"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.cancel fields: {', '.join(unsupported_fields)}",
        )

    run_id = _required_string(params, "run_id")
    reason = _optional_string(params, "reason")
    try:
        run = await state.chat_runs.cancel(run_id, reason=reason)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run)


async def _cancel_tool_call_chat(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id", "run_id", "tool_call_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.cancel_tool_call fields: {', '.join(unsupported_fields)}",
        )

    run_id = _required_string(params, "run_id")
    tool_call_id = _required_string(params, "tool_call_id")
    try:
        run = state.chat_runs.get(run_id)
        cancelled = run.cancel_tool_call(tool_call_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if not cancelled:
        raise RpcError(
            RPC_ERROR_RUN_NOT_FOUND,
            f"tool call not found: {tool_call_id}",
        )
    return {"ok": True}


def _chat_queue_list(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id", "session_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.queue_list fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    try:
        items = [
            item
            for item in _state_chat_runs(state).list_queued(agent_id, session_id)
            if not item.internal
        ]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"items": [item.to_dict() for item in items]}


def _chat_queue_remove(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id", "session_id", "item_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.queue_remove fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    item_id = _required_string(params, "item_id")
    try:
        chat_runs = _state_chat_runs(state)
        if not _queue_item_is_public(chat_runs, agent_id, session_id, item_id):
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
        removed = chat_runs.remove_queued(agent_id, session_id, item_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if not removed:
        raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
    _publish_queue_changed(state, agent_id, session_id)
    return {"ok": True}


def _chat_queue_update(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(
        set(params) - {"agent_id", "session_id", "item_id", "content", "input_origin"}
    )
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.queue_update fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    item_id = _required_string(params, "item_id")
    content = _parse_chat_content(params, "content")
    input_origin = _optional_chat_input_origin(params)

    try:
        chat_runs = _state_chat_runs(state)
        queued_item = _public_queue_item(chat_runs, agent_id, session_id, item_id)
        if queued_item is None:
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")

        # The queue is keyed on the bare agent id, so the edit's params carry no project.
        # Rebuild against the anchor the item was queued under (its own project_id — the same
        # source the drain path uses); otherwise a project session is looked up in the
        # identity anchor and the rebuild fails with session-not-found.
        (
            resolved_session_id,
            updated_executor,
            updated_display_content,
        ) = _build_streaming_queue_update(
            state,
            agent_id,
            session_id,
            content,
            input_origin=input_origin,
            project_id=queued_item.project_id,
        )
        updated = chat_runs.update_queued(
            agent_id,
            resolved_session_id,
            item_id,
            updated_executor,
            updated_display_content,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if not updated:
        raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
    # Scope on the resolved session id — content can move an item to a different
    # session, and that resolved id (not the raw input) is what was mutated.
    _publish_queue_changed(state, agent_id, resolved_session_id)
    return {"ok": True}


def _active_run_response(state: Any, agent_id: str, session_id: str) -> JsonObject | None:
    run = _state_chat_runs(state).active_run(agent_id=agent_id, session_id=session_id)
    if run is None:
        return None
    return _run_response(run, sse_url=f"/api/runs/{run.id}/events")


def _public_queue_item(
    chat_runs: ChatRunManager,
    agent_id: str,
    session_id: str,
    item_id: str,
) -> QueuedRunItem | None:
    """Return the queued item if it exists and is public (not internal), else ``None``.

    Internal items (e.g. subagent-driven) stay hidden from the queue RPCs, so they are
    treated as absent here just like a missing id.
    """
    for item in chat_runs.list_queued(agent_id, session_id):
        if item.item_id == item_id:
            return item if not item.internal else None
    return None


def _queue_item_is_public(
    chat_runs: ChatRunManager,
    agent_id: str,
    session_id: str,
    item_id: str,
) -> bool:
    return _public_queue_item(chat_runs, agent_id, session_id, item_id) is not None


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return chat RPC handlers."""

    return {
        "chat.history": _chat_history,
        "chat.send": _send_chat,
        "chat.stream": _stream_chat,
        "chat.retry_last_turn": _retry_chat,
        "chat.cancel": _cancel_chat,
        "chat.cancel_tool_call": _cancel_tool_call_chat,
        "chat.queue_list": _chat_queue_list,
        "chat.queue_remove": _chat_queue_remove,
        "chat.queue_update": _chat_queue_update,
    }
