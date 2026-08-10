"""Log and prompt RPC handlers."""

from __future__ import annotations

import inspect
from typing import Any, cast

from core.projects import (
    resolve_prompt_project,
    resolve_skill_scope,
    resolve_working_project_id,
    runtime_agent_body,
)
from core.prompts import ProjectPromptContext, PromptError, SystemPromptManager
from core.utils.log_viewer import LogViewer
from core.utils.logging import get_logger
from core.utils.tokens import estimate_json_tokens, estimate_tokens
from core.utils.workers import BoundedWorkerPool
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_DOMAIN, RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import (
    _reject_unsupported,
    _required_agent_address,
    _required_block_slug,
    _required_string,
)

JsonObject = dict[str, Any]
_LOGGER = get_logger("server.rpc.prompts")
_PROMPT_RPC_WORKERS = BoundedWorkerPool(name="prompt-rpc", max_workers=4)


async def _run_prompt_method(
    manager: Any,
    async_name: str,
    sync_name: str,
    *arguments: Any,
    **keyword_arguments: Any,
) -> Any:
    """Prefer Prompt's async facade while keeping lightweight sync substitutes valid."""
    async_method = getattr(manager, async_name, None)
    if callable(async_method) and inspect.iscoroutinefunction(async_method):
        return await async_method(*arguments, **keyword_arguments)
    return await _PROMPT_RPC_WORKERS.run(
        getattr(manager, sync_name),
        *arguments,
        **keyword_arguments,
    )


def _list_logs(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "log.list does not accept params")
    return _log_viewer(state).list_files()


def _read_log(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"file"}, "log read")

    file_name = _required_string(params, "file")
    try:
        return _log_viewer(state).read_file(file_name)
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except FileNotFoundError as exc:
        raise RpcError(RPC_ERROR_DOMAIN, str(exc)) from exc


def _list_prompts(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"scope"}, "prompt.list")
    try:
        manager = _prompt_manager(state)
        blocks = manager.list_blocks(params.get("scope"))
        scopes = manager.list_scopes()
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"blocks": blocks, "scopes": scopes}


def _update_prompt(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id", "content", "scope"}, "prompt.update")
    block_id = _required_string(params, "id")
    content = params.get("content")
    if not isinstance(content, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.content must be a string")
    try:
        manager = _prompt_manager(state)
        before = _find_prompt_block(manager, block_id, params.get("scope"))
        result = manager.update_block(block_id, content, params.get("scope"))
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if before is None or before.get("text") != result.get("text"):
        _log_prompt_mutation("updated", block_id, params.get("scope"))
    return result


def _reset_prompt(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id", "scope"}, "prompt.reset")
    block_id = _required_string(params, "id")
    try:
        manager = _prompt_manager(state)
        before = _find_prompt_block(manager, block_id, params.get("scope"))
        result = manager.reset_block(block_id, params.get("scope"))
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if before is not None and before.get("is_modified"):
        _log_prompt_mutation("reset", block_id, params.get("scope"))
    return result


def _set_prompt_layout(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"layout", "scope"}, "prompt.set_layout")
    layout = params.get("layout")
    if not isinstance(layout, list):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.layout must be a list")
    try:
        manager = _prompt_manager(state)
        before = _prompt_layout_signature(manager, params.get("scope"))
        result = manager.set_layout(layout, params.get("scope"))
        after = _prompt_layout_signature(manager, params.get("scope"))
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if before != after:
        _log_prompt_mutation("layout_updated", None, params.get("scope"))
    return result


def _create_prompt_block(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"slug", "content", "scope", "position"}, "prompt.create_block")
    slug = _required_block_slug(params, "slug")
    content = params.get("content")
    if content is not None and not isinstance(content, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.content must be a string")
    position = _optional_layout_position(params)
    try:
        result = _prompt_manager(state).create_block(
            slug, content, params.get("scope"), position=position
        )
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    _log_prompt_mutation("created", result.get("id"), params.get("scope"))
    return result


def _remove_prompt_block(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id", "scope"}, "prompt.remove_block")
    block_id = _required_string(params, "id")
    try:
        manager = _prompt_manager(state)
        existed = _find_prompt_block(manager, block_id, params.get("scope")) is not None
        result = manager.remove_block(block_id, params.get("scope"))
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if existed:
        _log_prompt_mutation("removed", block_id, params.get("scope"))
    return result


def _reset_prompt_layout(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"scope"}, "prompt.reset_layout")
    try:
        manager = _prompt_manager(state)
        before = _prompt_layout_signature(manager, params.get("scope"))
        result = manager.reset_layout(params.get("scope"))
        after = _prompt_layout_signature(manager, params.get("scope"))
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if before != after:
        _log_prompt_mutation("layout_reset", None, params.get("scope"))
    return result


def _find_prompt_block(
    manager: SystemPromptManager, block_id: str, scope: Any
) -> JsonObject | None:
    return next(
        (block for block in manager.list_blocks(scope) if block.get("id") == block_id), None
    )


def _prompt_layout_signature(
    manager: SystemPromptManager, scope: Any
) -> tuple[tuple[str, bool], ...]:
    return tuple(
        (str(block.get("id", "")), bool(block.get("enabled", False)))
        for block in manager.list_blocks(scope)
    )


def _log_prompt_mutation(operation: str, block_id: object, scope: Any) -> None:
    scope_name = "default"
    if isinstance(scope, dict) and scope.get("type") == "agent":
        scope_name = f"agent:{scope.get('agent_id', '')}"
    fields = [f"operation={operation}", f"scope={scope_name}"]
    if isinstance(block_id, str) and block_id:
        fields.append(f"block={block_id}")
    _LOGGER.info("Prompt mutated (%s)", " ".join(fields))


def _optional_layout_position(params: JsonObject) -> int | None:
    """Read an optional 0-based layout insertion index (``None`` = append).

    A custom block can be created at a position in the layout; the index is
    non-negative (0 inserts at the front) and the manager clamps it to the list
    length. Distinct from :func:`_optional_positive_integer`, which forbids 0.
    """
    value = params.get("position")
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.position must be a non-negative integer")
    return value


async def _preview_prompt(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id", "scope"}, "prompt.preview")
    scope = params.get("scope")
    try:
        prompt_scope = _prompt_manager(state).validate_scope(scope) if scope is not None else None
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    # An Agent prompt scope is identity-only: its ``agent_id`` names a store agent
    # with custom prompts enabled, never a project/config agent (those have no
    # Agent scope), so that path forces ``project_id`` to ``None``. Otherwise the
    # ``agent_id`` param is an ``agent@projekt`` address — a bare value stays
    # identity (unchanged), a qualified one previews that project's config agent
    # so the Working Project block and imported body render like a real run.
    if prompt_scope is not None and prompt_scope.type == "agent":
        agent_id = cast(str, prompt_scope.agent_id)
        project_id: str | None = None
    else:
        agent_id, project_id = _required_agent_address(params, "agent_id")

    def resolve_preview_agent() -> tuple[Any, Any | None]:
        resolved_agent = state.runtime.agent_resolver.resolve_agent(project_id, agent_id)
        working_project_id = resolve_working_project_id(project_id, resolved_agent)
        return resolved_agent, resolve_prompt_project(
            state.runtime.projects,
            working_project_id,
        )

    try:
        agent, prompt_project = await _PROMPT_RPC_WORKERS.run(resolve_preview_agent)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    project_context = (
        ProjectPromptContext.from_project(
            prompt_project.project_id,
            prompt_project.display_name,
            prompt_project.cwd,
            prompt_project.auto_load,
        )
        if prompt_project is not None
        else None
    )
    # Skill scope mirrors the chat loop through the shared policy: a rooted
    # identity preview sees its home project's skills like the run would, and the
    # private-skill layer applies to identity previews only (a project-qualified
    # preview renders a config agent, whose slug must not resolve a same-named
    # identity agent's private home).
    skill_project_id, identity_agent_id = resolve_skill_scope(project_id, prompt_project, agent_id)

    try:
        prompt_manager = state.runtime.system_prompts
        working_project_context = (
            await _run_prompt_method(
                prompt_manager,
                "render_working_project_context_async",
                "render_working_project_context",
                project_context,
            )
            if project_id is None and prompt_project is not None and project_context is not None
            else None
        )
        skill_registry = await _PROMPT_RPC_WORKERS.run(
            state.runtime.skills_for,
            skill_project_id,
            identity_agent_id,
        )
        text = await _run_prompt_method(
            prompt_manager,
            "build_system_prompt_async",
            "build_system_prompt",
            agent,
            scope=prompt_scope,
            agent_body=runtime_agent_body(agent),
            project_context=project_context,
            working_project_context=working_project_context,
            agent_project_id=project_id,
            skill_registry=skill_registry,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    # The provider tool-definition array occupies model context alongside the
    # prompt text but is not part of it — report it separately so the preview
    # reflects the request's real prompt-side footprint.
    tool_definitions = await _run_prompt_method(
        prompt_manager,
        "provider_tool_definitions_async",
        "provider_tool_definitions",
        agent,
    )

    def estimate_preview() -> tuple[int, bool, int]:
        token_count, estimated = estimate_tokens(text)
        tool_tokens = estimate_json_tokens(tool_definitions)[0] if tool_definitions else 0
        return token_count, estimated, tool_tokens

    token_count, estimated, tool_tokens = await _PROMPT_RPC_WORKERS.run(estimate_preview)
    return {
        "text": text,
        "tokens": token_count,
        "tool_tokens": tool_tokens,
        "tool_count": len(tool_definitions),
        "estimated": estimated,
    }


def _log_viewer(state: Any) -> LogViewer:
    log_viewer = getattr(state, "log_viewer", None)
    if log_viewer is not None:
        return cast(LogViewer, log_viewer)
    log_viewer = LogViewer(state.runtime.storage.data_dir)
    state.log_viewer = log_viewer
    return log_viewer


def _prompt_manager(state: Any) -> SystemPromptManager:
    """Return the runtime's live block-edit/assembly facade.

    The single prompt-edit facade is the ``SystemPromptManager`` on the runtime —
    the same instance that assembles prompts, so block listing/editing and the
    preview share one definition collection, block store, and default layout.
    """
    return cast(SystemPromptManager, state.runtime.system_prompts)


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return log and prompt RPC handlers."""

    return {
        "log.list": _list_logs,
        "log.read": _read_log,
        "prompt.list": _list_prompts,
        "prompt.update": _update_prompt,
        "prompt.reset": _reset_prompt,
        "prompt.set_layout": _set_prompt_layout,
        "prompt.create_block": _create_prompt_block,
        "prompt.remove_block": _remove_prompt_block,
        "prompt.reset_layout": _reset_prompt_layout,
        "prompt.preview": _preview_prompt,
    }
