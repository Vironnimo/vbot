"""Agent and session RPC handlers."""

from __future__ import annotations

import inspect
from contextlib import AsyncExitStack
from typing import Any, cast

from core.channels import ChannelConfigError
from core.compaction import COMPACTION_POLICY_META_KEY
from core.memory import MEMORY_PROMPT_MODES
from core.projects import (
    InvalidAgentAddressError,
    format_agent_address,
    parse_agent_address,
)
from core.prompts import load_bundled_default_layout
from core.runs import RunAdmissionBlockedError
from core.sessions import (
    FORK_SOURCE_META_KEY,
    SESSION_FORK_ALWAYS_STRIP_META_KEYS,
    SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS,
)
from core.settings import (
    ALLOWED_THINKING_EFFORTS,
    MAX_TEMPERATURE,
    MIN_TEMPERATURE,
    SettingsValidationError,
    validate_temperature,
    validate_thinking_effort,
)
from core.settings.normalizers import normalize_compaction_settings
from core.tools.availability import (
    BASH_ALLOWED_ENV_KEY,
    BASH_TOOL_SETTINGS_KEY,
    normalize_env_keys,
)
from core.tools.terminal_manager import TerminalOwner
from core.utils.errors import StorageError
from core.utils.logging import get_logger
from server.events import (
    RESOURCE_KIND_AGENTS,
    RESOURCE_KIND_CHANNELS,
    RESOURCE_KIND_CRON,
    RESOURCE_KIND_SESSIONS,
)
from server.rpc.agent_refs import (
    _agent_reference_ids,
    _agent_reference_lock,
    _rename_agent_and_retarget_references,
    _subagents_reference_identity_agent,
)
from server.rpc.channel_methods import _channel_config_by_id
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_AGENT_BUSY,
    RPC_ERROR_AGENT_IN_USE,
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_LAST_AGENT,
    RPC_ERROR_SESSION_BUSY,
    RpcError,
)
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.payloads import _agent_response
from server.rpc.runtime_access import _state_chat_runs
from server.rpc.validation import (
    _ensure_model_connection_supported,
    _optional_bool,
    _optional_string,
    _reject_unsupported,
    _required_agent_address,
    _required_string,
)

JsonObject = dict[str, Any]
_LOGGER = get_logger("server.rpc.agents")

__all__ = ["ALLOWED_THINKING_EFFORTS", "MAX_TEMPERATURE", "MIN_TEMPERATURE"]


def _list_agents(state: Any) -> JsonObject:
    try:
        listing = state.runtime.agents.list_with_order()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _agent_list_response(state, listing)


async def _reorder_agents(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_ids", "expected_revision"}, "agent.reorder")
    agent_ids = _validate_string_list("agent_ids", params.get("agent_ids"))
    expected_revision = params.get("expected_revision")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.expected_revision must be a non-negative integer",
        )

    try:
        async with _agent_reference_lock(state):
            listing = state.runtime.agents.reorder(
                agent_ids,
                expected_revision=expected_revision,
            )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if listing.order_changed:
        publish_resource_changed(state, RESOURCE_KIND_AGENTS)
        _LOGGER.info("Agent order updated (agents=%s)", ",".join(agent_ids))
    return _agent_list_response(state, listing)


def _agent_list_response(state: Any, listing: Any) -> JsonObject:
    return {
        "agents": [_agent_response(state, agent) for agent in listing.agents],
        "order_revision": listing.order_revision,
    }


def _get_agent(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id"}, "agent.get")

    agent_id = _required_string(params, "id")
    try:
        agent = state.runtime.agents.get(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _agent_response(state, agent)


def _create_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        changes = _agent_changes(params, blocked={"id"}, for_create=True)
        name = changes.pop("name", None)
        _ensure_agent_model_connections(state, changes)
        state.runtime.agents.create(agent_id, name, **changes)
        if changes.get("custom_system_prompt_enabled") is True:
            _seed_agent_custom_prompt(state, agent_id)
        agent = state.runtime.agents.get(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response = _agent_response(state, agent)
    # Agent CRUD rides the generic reload-on-change channel ("one app system"):
    # the signal carries no agent data, open windows re-fetch agent.list.
    publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    _LOGGER.info("Agent created (agent=%s)", agent_id)
    return response


def _update_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        changes = _agent_changes(params, blocked={"id"}, for_create=False)
        copy_workspace_identity_files = changes.pop("copy_workspace_identity_files", False)
        root_project_id = changes.get("root_project_id")
        if root_project_id is not None and not state.runtime.projects.exists(root_project_id):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"unknown Project: {root_project_id}",
            )
        _ensure_agent_model_connections(state, changes)
        previous_agent = state.runtime.agents.get(agent_id)
        if (
            changes.get("custom_system_prompt_enabled") is True
            and not previous_agent.custom_system_prompt_enabled
        ):
            _seed_agent_custom_prompt(state, agent_id)
        update_result = state.runtime.agents.update_with_metadata(
            agent_id,
            copy_workspace_identity_files=copy_workspace_identity_files,
            **changes,
        )
        agent = update_result.agent
        changed_fields = sorted(
            field
            for field in changes
            if getattr(previous_agent, field, None) != getattr(agent, field, None)
        )
        if update_result.copied_files or update_result.backed_up_files:
            changed_fields.append("workspace_identity_files")
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response = _agent_response(state, agent)
    response["workspace_relocation"] = {
        "copied_files": list(update_result.copied_files),
        "backed_up_files": list(update_result.backed_up_files),
        "backup_created": update_result.backup_dir is not None,
    }
    publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    if changed_fields:
        _LOGGER.info(
            "Agent updated (agent=%s fields=%s)",
            agent_id,
            ",".join(changed_fields),
        )
    return response


async def _rename_agent(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id", "new_id"}, "agent.rename")
    agent_id = _required_string(params, "id")
    new_agent_id = _required_string(params, "new_id")
    if agent_id == new_agent_id:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.new_id must differ from params.id",
        )

    try:
        async with _agent_reference_lock(state):
            try:
                async with AsyncExitStack() as guards:
                    for guarded_agent_id in sorted((agent_id, new_agent_id)):
                        await guards.enter_async_context(
                            _state_chat_runs(state).agent_admission_guard(
                                guarded_agent_id,
                                project_id=None,
                            )
                        )
                    busy_subagent_ids = [
                        guarded_agent_id
                        for guarded_agent_id in sorted((agent_id, new_agent_id))
                        if _subagents_reference_identity_agent(state, guarded_agent_id)
                    ]
                    if busy_subagent_ids:
                        raise RpcError(
                            RPC_ERROR_AGENT_BUSY,
                            (
                                "cannot rename agent while the old or new id has open "
                                f"Sub-Agent activity: {', '.join(busy_subagent_ids)}"
                            ),
                        )
                    result = _rename_agent_and_retarget_references(
                        state,
                        agent_id,
                        new_agent_id,
                    )
                    invalidate_agent_skills = getattr(
                        state.runtime,
                        "invalidate_agent_skills",
                        None,
                    )
                    if callable(invalidate_agent_skills):
                        invalidate_agent_skills(agent_id)
                        invalidate_agent_skills(new_agent_id)
            except RunAdmissionBlockedError as exc:
                raise RpcError(
                    RPC_ERROR_AGENT_BUSY,
                    (
                        "cannot rename agent while the old or new id has active "
                        f"or queued runs: {agent_id} -> {new_agent_id}"
                    ),
                ) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    await _remove_renamed_sessions_from_recall(state, agent_id, result.session_ids)

    response = _agent_response(state, result.agent)
    response["rename"] = {
        "old_id": agent_id,
        "new_id": new_agent_id,
        "channels_updated": list(result.channel_ids),
        "cron_jobs_updated": list(result.cron_job_ids),
        "bootstrap_jobs_updated": list(result.bootstrap_job_ids),
        "agent_policies_updated": list(result.policy_agent_ids),
        "session_links_updated": result.session_reference_count,
    }
    rename_scope = {"old_agent_id": agent_id, "new_agent_id": new_agent_id}
    publish_resource_changed(state, RESOURCE_KIND_AGENTS, scope=rename_scope)
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope=rename_scope)
    if result.channel_ids:
        publish_resource_changed(state, RESOURCE_KIND_CHANNELS)
    if result.cron_job_ids:
        publish_resource_changed(state, RESOURCE_KIND_CRON)
    _LOGGER.info(
        "Agent renamed (agent=%s new_agent=%s channels=%s cron=%s "
        "bootstrap=%s policies=%s session_links=%s)",
        agent_id,
        new_agent_id,
        len(result.channel_ids),
        len(result.cron_job_ids),
        len(result.bootstrap_job_ids),
        len(result.policy_agent_ids),
        result.session_reference_count,
    )
    return response


async def _remove_renamed_sessions_from_recall(
    state: Any,
    old_agent_id: str,
    session_ids: tuple[str, ...],
) -> None:
    """Best-effort cleanup of disposable old-address recall index rows."""
    remove_session = getattr(state.runtime, "remove_session_from_recall", None)
    if not callable(remove_session):
        return
    for session_id in session_ids:
        try:
            cleanup = remove_session(old_agent_id, session_id, None)
            if inspect.isawaitable(cleanup):
                await cleanup
        except Exception as error:
            _LOGGER.warning(
                "Recall cleanup failed after Agent rename (agent=%s session=%s): %s",
                old_agent_id,
                session_id,
                error,
            )


def _seed_agent_custom_prompt(state: Any, agent_id: str) -> None:
    """Seed an agent's prompt scope when its custom System Prompt is just enabled.

    Both halves of the System Prompt move into the agent's scope together (D4):
    the editable text fragments and the block layout, each seeded from the current
    effective default scope and independent afterwards. The effective default
    layout is the saved default-scope layout, falling back to the bundled default
    when the default scope owns none. Both seeds preserve an existing agent file,
    so re-enabling never clobbers an already-customized agent scope; text overrides
    are intentionally not copied — the agent inherits block text until it overrides.
    """

    storage = state.runtime.storage
    storage.copy_agent_prompt_fragments(agent_id)
    default_layout = storage.read_block_layout(None) or load_bundled_default_layout()
    storage.seed_agent_block_layout(agent_id, default_layout)


async def _delete_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        async with _agent_reference_lock(state):
            remaining_agents = [
                agent for agent in state.runtime.agents.list() if agent.id != agent_id
            ]
            if not remaining_agents:
                raise RpcError(RPC_ERROR_LAST_AGENT, "cannot delete the last agent")
            try:
                # Identity scope only: a same-named Project Team agent remains
                # independent. The guard makes the idle check and the following
                # archive one atomic boundary against every Run ingress path.
                async with _state_chat_runs(state).agent_admission_guard(agent_id, project_id=None):
                    references = _agent_reference_ids(state, agent_id)
                    if references:
                        raise RpcError(
                            RPC_ERROR_AGENT_IN_USE,
                            (
                                "cannot delete agent referenced by "
                                f"{', '.join(references)}: {agent_id}"
                            ),
                        )
                    await state.runtime.terminal_manager.close_agent_scope(agent_id, None)
                    state.runtime.agents.delete(agent_id)
            except RunAdmissionBlockedError as exc:
                raise RpcError(
                    RPC_ERROR_AGENT_BUSY,
                    f"cannot delete agent with active or queued runs: {agent_id}",
                ) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    result = {
        "agent_id": agent_id,
        "remaining_agents": [_agent_response(state, agent) for agent in remaining_agents],
    }
    publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    _LOGGER.info("Agent archived (agent=%s)", agent_id)
    return result


def _create_session(state: Any, params: JsonObject) -> JsonObject:
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    make_current = _optional_bool(params, "make_current", default=False)
    try:
        # One resolver seam validates both sources: identity agents through the
        # store, project agents through the team scan. The session is then created
        # under the matching anchor (identity dir vs. project anchor).
        state.runtime.agent_resolver.resolve_agent(project_id, agent_id)
        session = state.runtime.chat_sessions.create(
            agent_id, session_id=session_id, project_id=project_id
        )
        # ``current_session_id`` lives on the identity ``agent.json``; a project
        # config agent has no such pointer (the anchor owns project-session
        # selection), so the make-current update is identity-only.
        if make_current and project_id is None:
            state.runtime.agents.update(agent_id, current_session_id=session.id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # Session creation is the single emit point for the sessions channel: it also
    # covers /new and /handoff, which create their session through here. Other
    # windows refresh their session list (and the make-current marking) for this
    # agent; they do NOT switch to the new session. Scoped to the agent so windows
    # on a different agent ignore it.
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope={"agent_id": agent_id})
    return {"agent_id": agent_id, "session_id": session.id}


async def _delete_session(state: Any, params: JsonObject) -> JsonObject:
    """Archive one session and report where the viewing accessor should land.

    Decisions baked in: the session is archived, not hard-deleted (#1,
    recoverable); deletion is refused while a run is active or queued on it (#4);
    the response carries ``next_session_id`` for #2 navigation; and the removed
    session is dropped from the active recall index immediately (#6). Channel-
    bound and sub-agent sessions need no special handling — a channel session
    simply resumes empty on the next inbound message, and an active sub-agent
    child is already covered by the per-session busy guard.
    """
    supported_fields = {"agent_id", "session_id"}
    _reject_unsupported(params, supported_fields, "session.delete")

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    deleting_current = False
    try:
        # One resolver seam validates both agent sources, exactly like
        # session.create, so an unknown agent fails before any file work.
        state.runtime.agent_resolver.resolve_agent(project_id, agent_id)
        chat_sessions = state.runtime.chat_sessions
        try:
            async with (
                _agent_reference_lock(state),
                _state_chat_runs(state).session_admission_guard((project_id, agent_id, session_id)),
            ):
                _ensure_no_bootstrap_session_reference(state, agent_id, project_id, session_id)
                # Existence check under the guard: concurrent deletes cannot both
                # cross the storage boundary, and a missing Session still maps to
                # the ordinary domain error.
                chat_sessions.get(agent_id, session_id, project_id)
                # An identity agent tracks a current-session pointer; note when we
                # are deleting it so the re-aim is broadcast below.
                if project_id is None:
                    deleting_current = (
                        state.runtime.agents.get(agent_id).current_session_id == session_id
                    )
                await state.runtime.terminal_manager.close_scope(
                    TerminalOwner(project_id, agent_id, session_id)
                )
                await chat_sessions.archive(agent_id, session_id, project_id)
                next_session_id = _resolve_post_delete_landing(
                    state, agent_id, session_id, project_id
                )
                await state.runtime.remove_session_from_recall(agent_id, session_id, project_id)
        except RunAdmissionBlockedError as exc:
            raise RpcError(
                RPC_ERROR_SESSION_BUSY,
                f"cannot delete session with an active or queued run: {session_id}",
            ) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # Same emit point as session.create/rename: other windows on this agent
    # refresh their list (and the current marking), scoped to the agent.
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope={"agent_id": agent_id})
    # Re-aiming the identity current pointer is an agent-config change, so refresh
    # agent state in other windows (the current marking + return-to-current path).
    if deleting_current:
        publish_resource_changed(state, RESOURCE_KIND_AGENTS)
    _LOGGER.info(
        "Session archived (agent=%s session=%s)",
        format_agent_address(agent_id, project_id),
        session_id,
    )
    return {"agent_id": agent_id, "session_id": session_id, "next_session_id": next_session_id}


def _ensure_no_bootstrap_session_reference(
    state: Any,
    agent_id: str,
    project_id: str | None,
    session_id: str,
) -> None:
    service = getattr(state.runtime, "bootstrap_service", None)
    if service is None:
        return
    references = sorted(
        f"bootstrap:{job.id}"
        for job in service.list_jobs()
        if (
            job.agent_id == agent_id
            and job.project_id == project_id
            and job.session_id == session_id
            and getattr(job, "status", "active") != "completed"
        )
    )
    if references:
        raise RpcError(
            RPC_ERROR_SESSION_BUSY,
            f"cannot delete Session referenced by {', '.join(references)}",
        )


def _resolve_post_delete_landing(
    state: Any, agent_id: str, session_id: str, project_id: str | None
) -> str:
    """Return the session a viewing accessor should switch to after a delete (#2).

    The most-recently-active remaining session, or a fresh empty one when none
    remain. For an identity agent this goes through the shared
    ``reset_current_after_session_removed`` seam, which re-aims the current
    pointer when the deleted session was the current one and creates the fresh
    session when none remain — so the landing is that agent's resulting current
    and no session is ever created twice. A project config agent has no
    server-side current pointer, so the landing is derived directly from the
    remaining sessions (creating a fresh one when none remain).
    """
    chat_sessions = state.runtime.chat_sessions
    if project_id is None:
        agent = state.runtime.agents.reset_current_after_session_removed(agent_id, session_id)
        return str(agent.current_session_id)
    remaining = chat_sessions.list_with_metadata(agent_id, project_id)
    if remaining:
        newest = max(remaining, key=lambda session: session["last_active_at"])
        return str(newest["id"])
    return str(chat_sessions.create(agent_id, project_id=project_id).id)


def _list_sessions(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id"}, "session.list")

    agent_id, project_id = _required_agent_address(params, "agent_id")
    try:
        sessions = state.runtime.chat_sessions.list_with_metadata(agent_id, project_id)
        resolver = getattr(state.runtime, "agent_resolver", None)
        agents = getattr(state.runtime, "agents", None)
        if resolver is None and agents is None:
            return {"sessions": sessions}
        if resolver is not None:
            agent = resolver.resolve_agent(project_id, agent_id)
        else:
            assert agents is not None
            agent = agents.get(agent_id)
        own_policy = getattr(agent, "compaction_policy", None)
        inherited_policy = (
            dict(own_policy)
            if isinstance(own_policy, dict)
            else (
                state.runtime.storage.load_compaction_settings()
                if getattr(state.runtime, "storage", None) is not None
                else normalize_compaction_settings(None)
            )
        )
        for session in sessions:
            override = session.get(COMPACTION_POLICY_META_KEY)
            session["compaction_policy_override"] = (
                dict(override) if isinstance(override, dict) else None
            )
            session["compaction_policy_effective"] = (
                dict(override) if isinstance(override, dict) else dict(inherited_policy)
            )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"sessions": sessions}


def _mark_session_read(state: Any, params: JsonObject) -> JsonObject:
    """Acknowledge the exact terminal Run rendered in one Session."""
    _reject_unsupported(params, {"agent_id", "session_id", "run_id"}, "session.mark_read")
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    run_id = _required_string(params, "run_id")
    try:
        state.runtime.agent_resolver.resolve_agent(project_id, agent_id)
        activity = state.runtime.chat_sessions.mark_terminal_run_read(
            agent_id,
            session_id,
            run_id,
            project_id,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "agent_id": format_agent_address(agent_id, project_id),
        "session_id": session_id,
        **activity,
    }


async def _fork_session(state: Any, params: JsonObject) -> JsonObject:
    """Copy a session 1:1 into a fresh id, optionally re-homed to another agent.

    A general capability: the fork is a normal, visible session that records its
    provenance (``fork_source``). Channel- and sub-agent bindings are always
    stripped so the copy is unbound; a cross-agent fork additionally drops the
    pinned skill catalog so the target re-pins its own. The strip policy lives in
    the server (``SESSION_FORK_*`` constants) so the sessions domain imports no
    chat/channel constant.
    """
    supported_fields = {"agent_id", "session_id", "target_agent_id"}
    _reject_unsupported(params, supported_fields, "session.fork")

    source_agent_id, source_project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    target_agent_id, target_project_id = _optional_fork_target(
        params, source_agent_id, source_project_id
    )

    strip_meta_keys = SESSION_FORK_ALWAYS_STRIP_META_KEYS
    re_homed = (target_agent_id, target_project_id) != (source_agent_id, source_project_id)
    if re_homed:
        strip_meta_keys = strip_meta_keys | SESSION_FORK_CROSS_AGENT_STRIP_META_KEYS

    try:
        # Resolve both endpoints through the one seam so an unknown source or
        # target agent fails before any file work (mirrors session.create/delete).
        state.runtime.agent_resolver.resolve_agent(source_project_id, source_agent_id)
        if re_homed:
            state.runtime.agent_resolver.resolve_agent(target_project_id, target_agent_id)
        fork = await state.runtime.chat_sessions.fork(
            source_agent_id,
            session_id,
            target_agent_id=target_agent_id,
            source_project_id=source_project_id,
            target_project_id=target_project_id,
            strip_meta_keys=strip_meta_keys,
        )
        fork_source = state.runtime.chat_sessions.get_metadata(
            target_agent_id, fork.id, target_project_id
        ).get(FORK_SOURCE_META_KEY)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    # Same emit point as session.create: other windows on the *target* agent
    # refresh their session list so the fork shows immediately.
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope={"agent_id": target_agent_id})
    _LOGGER.info(
        "Session forked (source_agent=%s source_session=%s target_agent=%s target_session=%s)",
        format_agent_address(source_agent_id, source_project_id),
        session_id,
        format_agent_address(target_agent_id, target_project_id),
        fork.id,
    )
    return {
        "session": {
            "id": fork.id,
            "agent_id": format_agent_address(target_agent_id, target_project_id),
            "fork_source": fork_source,
        }
    }


def _optional_fork_target(
    params: JsonObject, source_agent_id: str, source_project_id: str | None
) -> tuple[str, str | None]:
    """Parse the optional ``target_agent_id`` fork destination, defaulting to source.

    Absent → fork within the source's own (agent, project). A malformed address is
    a client error surfaced as ``invalid_request`` (mirrors ``_required_agent_address``).
    """
    raw = _optional_string(params, "target_agent_id")
    if raw is None:
        return source_agent_id, source_project_id
    try:
        return parse_agent_address(raw)
    except InvalidAgentAddressError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc


async def _link_session_to_channel(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {"agent_id", "session_id", "channel_id", "platform_conv_id"}
    _reject_unsupported(params, supported_fields, "session.link_channel")

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    channel_id = _required_string(params, "channel_id")
    platform_conv_id = _required_string(params, "platform_conv_id")

    try:
        channel_service = state.runtime.channel_service
        channel_config = _channel_config_by_id(channel_service, channel_id)
        if channel_config.agent_id != agent_id:
            raise ChannelConfigError(
                f"Channel {channel_id} belongs to agent {channel_config.agent_id}, not {agent_id}"
            )
        state.runtime.chat_sessions.get(agent_id, session_id)
        metadata = dict(state.runtime.chat_sessions.get_metadata(agent_id, session_id))
        previous_link = (
            metadata.get("source_channel_id"),
            metadata.get("platform"),
            metadata.get("platform_conv_id"),
        )
        metadata.update(
            {
                "source_channel_id": channel_id,
                "platform": channel_config.platform,
                "platform_conv_id": platform_conv_id,
                "last_reply_target": {
                    "channel_id": channel_id,
                    "platform_target": platform_conv_id,
                },
            }
        )
        state.runtime.chat_sessions.set_metadata(agent_id, session_id, metadata)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if previous_link != (channel_id, channel_config.platform, platform_conv_id):
        _LOGGER.info(
            "Session linked to Channel (agent=%s session=%s channel=%s)",
            agent_id,
            session_id,
            channel_id,
        )
    return {"ok": True}


def _rename_session(state: Any, params: JsonObject) -> JsonObject:
    """Set or clear a session's display title (the WebUI rename and ``/rename``).

    Thin over the single titling seam ``chat_sessions.set_title``: an empty (or
    absent) title clears it, so the session reverts to its automatic display.
    The response carries the stored title (``None`` when cleared) so the caller
    can confirm what was applied after normalization.
    """
    supported_fields = {"agent_id", "session_id", "title"}
    _reject_unsupported(params, supported_fields, "session.rename")

    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    title = _session_title_param(params)
    try:
        stored_title = state.runtime.chat_sessions.set_title(
            agent_id, session_id, title, project_id
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # A rename changes the session's list display, so other windows on this agent
    # refresh their session list — scoped to the agent like session.create.
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope={"agent_id": agent_id})
    return {"agent_id": agent_id, "session_id": session_id, "title": stored_title}


def _set_session_compaction_policy(state: Any, params: JsonObject) -> JsonObject:
    """Set a full Session Policy override, or clear it back to live inheritance."""
    _reject_unsupported(
        params, {"agent_id", "session_id", "policy"}, "session.set_compaction_policy"
    )
    agent_id, project_id = _required_agent_address(params, "agent_id")
    session_id = _required_string(params, "session_id")
    policy = params.get("policy")
    try:
        from core.settings.normalizers import normalize_compaction_policy

        normalized = normalize_compaction_policy(policy) if policy is not None else None
        state.runtime.chat_sessions.get(agent_id, session_id, project_id)
        metadata = state.runtime.chat_sessions.get_metadata(agent_id, session_id, project_id)
        previous_override = metadata.get(COMPACTION_POLICY_META_KEY)
        if normalized is None:
            metadata.pop(COMPACTION_POLICY_META_KEY, None)
        else:
            metadata[COMPACTION_POLICY_META_KEY] = normalized
        state.runtime.chat_sessions.set_metadata(agent_id, session_id, metadata, project_id)
        agent = state.runtime.agent_resolver.resolve_agent(project_id, agent_id)
        own_policy = getattr(agent, "compaction_policy", None)
        inherited = (
            dict(own_policy)
            if isinstance(own_policy, dict)
            else state.runtime.storage.load_compaction_settings()
        )
        effective = normalized or inherited
    except StorageError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    publish_resource_changed(state, RESOURCE_KIND_SESSIONS, scope={"agent_id": agent_id})
    if previous_override != normalized:
        _LOGGER.info(
            "Session compaction policy %s (agent=%s session=%s)",
            "set" if normalized is not None else "cleared",
            format_agent_address(agent_id, project_id),
            session_id,
        )
    return {
        "agent_id": format_agent_address(agent_id, project_id),
        "session_id": session_id,
        "override": normalized,
        "effective": effective,
        "source": "session" if normalized is not None else "agent_or_global",
    }


def _session_title_param(params: JsonObject) -> str:
    """Read the rename title: any string, empty allowed (an empty title clears).

    Unlike ``_required_string``/``_optional_string`` this accepts the empty
    string, which is the explicit "clear the title" signal; an absent field is
    treated the same way.
    """
    value = params.get("title", "")
    if not isinstance(value, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.title must be a string")
    return value


def _agent_changes(params: JsonObject, *, blocked: set[str], for_create: bool) -> JsonObject:
    public_fields = {
        "name",
        "model",
        "fallback_model",
        "memory_prompt_mode",
        "temperature",
        "thinking_effort",
        "allowed_tools",
        "allowed_skills",
        "tools",
        "custom_system_prompt_enabled",
        "compaction_policy",
    }
    if not for_create:
        public_fields.add("current_session_id")
        public_fields.add("workspace")
        public_fields.add("root_project_id")
        public_fields.add("copy_workspace_identity_files")

    rejected_fields = sorted(set(params) - public_fields - blocked)
    if rejected_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported agent fields: {', '.join(rejected_fields)}",
        )

    changes: JsonObject = {}
    for key, value in params.items():
        if key in blocked:
            continue
        changes[key] = _validate_agent_field(key, value)
    return changes


def _ensure_agent_model_connections(state: Any, changes: JsonObject) -> None:
    """Reject agent model / fallback_model pinned to a connection they forbid."""
    models = state.runtime.models
    for field in ("model", "fallback_model"):
        if field in changes:
            _ensure_model_connection_supported(models, field, changes[field])


def _validate_agent_field(key: str, value: Any) -> Any:
    if key == "name":
        if value is not None and not isinstance(value, str):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.name must be a string or null",
            )
        return value
    if key == "workspace":
        if value is not None and not isinstance(value, str):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.workspace must be a string or null",
            )
        return value
    if key == "current_session_id":
        if not isinstance(value, str) or not value:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a non-empty string")
        return value
    if key == "root_project_id":
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.root_project_id must be null or a non-empty string",
            )
        return value
    if key == "copy_workspace_identity_files":
        if not isinstance(value, bool):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.copy_workspace_identity_files must be a boolean",
            )
        return value
    if key in {"model", "fallback_model"}:
        if not isinstance(value, str):
            raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a string")
        return value
    if key == "temperature":
        return _validate_temperature(value, allow_none=True)
    if key == "thinking_effort":
        return _validate_thinking_effort(value, allow_none=True)
    if key == "memory_prompt_mode":
        return _validate_memory_prompt_mode(value)
    if key in {"allowed_tools", "allowed_skills"}:
        return _validate_string_list(key, value)
    if key == "tools":
        if not isinstance(value, dict):
            raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.tools must be an object")
        bash = value.get(BASH_TOOL_SETTINGS_KEY)
        if bash is not None and not isinstance(bash, dict):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.tools.{BASH_TOOL_SETTINGS_KEY} must be an object",
            )
        if isinstance(bash, dict):
            unsupported_bash = sorted(set(bash) - {BASH_ALLOWED_ENV_KEY})
            if unsupported_bash:
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    f"unsupported tools.{BASH_TOOL_SETTINGS_KEY} fields: "
                    + ", ".join(unsupported_bash),
                )
            if BASH_ALLOWED_ENV_KEY in bash:
                try:
                    bash[BASH_ALLOWED_ENV_KEY] = normalize_env_keys(
                        bash[BASH_ALLOWED_ENV_KEY],
                        field_name=(f"tools.{BASH_TOOL_SETTINGS_KEY}.{BASH_ALLOWED_ENV_KEY}"),
                    )
                except ValueError as error:
                    raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error
        subagent = value.get("subagent")
        if subagent is not None and not isinstance(subagent, dict):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.tools.subagent must be an object",
            )
        if isinstance(subagent, dict) and "allowed_agents" in subagent:
            _validate_string_list(
                "tools.subagent.allowed_agents",
                subagent["allowed_agents"],
            )
        return dict(value)
    if key == "custom_system_prompt_enabled":
        if not isinstance(value, bool):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.custom_system_prompt_enabled must be a boolean",
            )
        return value
    if key == "compaction_policy":
        if value is None:
            return None
        try:
            from core.settings.normalizers import normalize_compaction_policy

            return normalize_compaction_policy(value)
        except Exception as exc:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    raise RpcError(RPC_ERROR_INVALID_REQUEST, f"unsupported agent field: {key}")


def _validate_memory_prompt_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in MEMORY_PROMPT_MODES:
        allowed = ", ".join(repr(item) for item in MEMORY_PROMPT_MODES)
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.memory_prompt_mode must be one of: {allowed}",
        )
    return value


def _validate_temperature(
    value: Any,
    *,
    label: str = "params.temperature",
    allow_none: bool = False,
) -> float | None:
    try:
        return validate_temperature(value, label=label, allow_none=allow_none)
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc


def _validate_thinking_effort(
    value: Any,
    *,
    label: str = "params.thinking_effort",
    allow_none: bool = False,
) -> str | None:
    try:
        return validate_thinking_effort(value, label=label, allow_none=allow_none)
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc


def _validate_string_list(key: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a list of strings")
    return list(value)


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return agent and session RPC handlers."""

    def list_agents(state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _list_agents(state))

    return {
        "agent.list": list_agents,
        "agent.reorder": _reorder_agents,
        "agent.get": _get_agent,
        "agent.create": _create_agent,
        "agent.update": _update_agent,
        "agent.rename": _rename_agent,
        "agent.delete": _delete_agent,
        "session.create": _create_session,
        "session.list": _list_sessions,
        "session.mark_read": _mark_session_read,
        "session.fork": _fork_session,
        "session.delete": _delete_session,
        "session.rename": _rename_session,
        "session.set_compaction_policy": _set_session_compaction_policy,
        "session.link_channel": _link_session_to_channel,
    }
