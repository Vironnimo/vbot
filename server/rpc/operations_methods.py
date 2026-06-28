"""Log and prompt RPC handlers."""

from __future__ import annotations

from typing import Any, cast

from core.projects import resolve_prompt_project, runtime_agent_body
from core.prompts import ProjectPromptContext, PromptError, SystemPromptManager
from core.utils.log_viewer import LogViewer
from core.utils.tokens import estimate_tokens
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_DOMAIN, RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import (
    _required_agent_address,
    _required_block_slug,
    _required_string,
)

JsonObject = dict[str, Any]


def _list_logs(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "log.list does not accept params")
    return _log_viewer(state).list_files()


def _read_log(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"file"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported log read fields: {', '.join(unsupported_fields)}",
        )

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
        return _prompt_manager(state).update_block(block_id, content, params.get("scope"))
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _reset_prompt(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id", "scope"}, "prompt.reset")
    block_id = _required_string(params, "id")
    try:
        return _prompt_manager(state).reset_block(block_id, params.get("scope"))
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _set_prompt_layout(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"layout", "scope"}, "prompt.set_layout")
    layout = params.get("layout")
    if not isinstance(layout, list):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.layout must be a list")
    try:
        return _prompt_manager(state).set_layout(layout, params.get("scope"))
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _create_prompt_block(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"slug", "content", "scope", "position"}, "prompt.create_block")
    slug = _required_block_slug(params, "slug")
    content = params.get("content")
    if content is not None and not isinstance(content, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.content must be a string")
    position = _optional_layout_position(params)
    try:
        return _prompt_manager(state).create_block(
            slug, content, params.get("scope"), position=position
        )
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _remove_prompt_block(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"id", "scope"}, "prompt.remove_block")
    block_id = _required_string(params, "id")
    try:
        return _prompt_manager(state).remove_block(block_id, params.get("scope"))
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _reset_prompt_layout(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"scope"}, "prompt.reset_layout")
    try:
        return _prompt_manager(state).reset_layout(params.get("scope"))
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


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


def _reject_unsupported(params: JsonObject, allowed: set[str], method: str) -> None:
    """Raise ``invalid_request`` when *params* carries a field outside *allowed*."""
    unsupported_fields = sorted(set(params) - allowed)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported {method} fields: {', '.join(unsupported_fields)}",
        )


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
    # so ``{project_files}`` and the imported body render like a real run.
    if prompt_scope is not None and prompt_scope.type == "agent":
        agent_id = cast(str, prompt_scope.agent_id)
        project_id: str | None = None
    else:
        agent_id, project_id = _required_agent_address(params, "agent_id")

    try:
        agent = state.runtime.agent_resolver.resolve_agent(project_id, agent_id)
        project_context = _preview_project_context(state, project_id, agent)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    try:
        prompt_manager = state.runtime.system_prompts
        text = prompt_manager.build_system_prompt(
            agent,
            scope=prompt_scope,
            agent_body=runtime_agent_body(agent),
            project_context=project_context,
            skill_registry=state.runtime.skills_for(project_id),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    token_count, estimated = estimate_tokens(text)
    return {"text": text, "tokens": token_count, "estimated": estimated}


def _preview_project_context(
    state: Any, project_id: str | None, agent: Any
) -> ProjectPromptContext | None:
    """Build the prompt-time project context for a preview, mirroring the chat loop.

    Resolves through the same shared rooting policy a run uses
    (:func:`core.projects.resolve_prompt_project`), so the preview matches what is
    actually sent: a project-qualified preview carries that project's cwd and
    auto-load list; a bare identity preview carries the files of the project the
    agent is *rooted* in (workspace == a registered repo), or nothing — in which
    case ``{project_files}`` collapses and the prompt is unchanged.
    """
    project = resolve_prompt_project(state.runtime.projects, project_id, agent)
    if project is None:
        return None
    return ProjectPromptContext.from_project(project.cwd, project.auto_load)


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
