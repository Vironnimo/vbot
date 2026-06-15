"""Shared RPC response payload mappers."""

from __future__ import annotations

from typing import Any, cast

from core.chat import ChatMessage, parse_bare_model
from core.providers.providers import resolve_context_window
from core.runs import QueuedRunItem, Run

JsonObject = dict[str, Any]


def _run_response(
    run: Run,
    *,
    final_message: ChatMessage | None = None,
    sse_url: str | None = None,
) -> JsonObject:
    response: JsonObject = {
        "run_id": run.id,
        "agent_id": run.agent_id,
        "session_id": run.session_id,
        "status": run.status.value,
        "events": [_remove_opaque_provider_metadata(event.to_dict()) for event in run.events],
    }
    if final_message is not None:
        response["message"] = _visible_message(final_message)
    if sse_url is not None:
        response["sse_url"] = sse_url
    return response


def _queued_response(item: QueuedRunItem) -> JsonObject:
    return {
        "queued": True,
        "item": item.to_dict(),
    }


def _visible_message(message: ChatMessage) -> JsonObject:
    return cast(JsonObject, _remove_opaque_provider_metadata(message.to_dict()))


def _is_visible_history_message(message: ChatMessage) -> bool:
    return message.role != "note"


def _resolve_context_window(state: Any, model: str) -> int | None:
    """Resolve a model string (provider/model-id) to the usable context window.

    This is the *active agent's* window for the WebUI token badge, so it
    resolves through the shared default chain (model window → provider-config
    default → global floor, see :func:`resolve_context_window`): a model whose
    window is unknown still yields a usable number instead of ``None``/NaN.
    Returns ``None`` only when the model string is unusable or the model/provider
    cannot be found in the registry.
    """
    bare_model = parse_bare_model(model)
    if "/" not in bare_model:
        return None
    provider_id, _, model_id = bare_model.partition("/")
    if not provider_id or not model_id:
        return None
    try:
        model_entry = state.runtime.models.get(provider_id, model_id)
    except (KeyError, AttributeError):
        return None
    return resolve_context_window(
        model_entry.context_window,
        _provider_config(state, provider_id),
    )


def _provider_config(state: Any, provider_id: str) -> Any:
    """Return the ProviderConfig for the read-side window default, or None."""
    try:
        return state.runtime.providers.get(provider_id)
    except (KeyError, AttributeError):
        return None


def _agent_response(state: Any, agent: Any) -> JsonObject:
    return {
        "id": agent.id,
        "name": agent.name,
        "model": agent.model,
        "fallback_model": agent.fallback_model,
        "workspace": agent.workspace,
        "temperature": agent.temperature,
        "thinking_effort": agent.thinking_effort,
        "memory_prompt_mode": agent.memory_prompt_mode,
        "allowed_tools": list(agent.allowed_tools),
        "allowed_skills": list(agent.allowed_skills),
        "custom_system_prompt_enabled": bool(agent.custom_system_prompt_enabled),
        "current_session_id": agent.current_session_id,
        "context_window": _resolve_context_window(state, agent.model),
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


def _model_response(provider_id: str, model: Any) -> JsonObject:
    return {
        "id": f"{provider_id}/{model.model_id}",
        "provider_id": provider_id,
        "model_id": model.model_id,
        "name": model.name,
        "capabilities": {
            "vision": model.capabilities.vision,
            "tools": model.capabilities.tools,
            "json_mode": model.capabilities.json_mode,
            "reasoning": {
                "supported": model.capabilities.reasoning.supported,
                "control": model.capabilities.reasoning.control,
                "levels": list(model.capabilities.reasoning.levels),
            },
            "input_modalities": list(model.capabilities.input_modalities),
            "output_modalities": list(model.capabilities.output_modalities),
            "supported_parameters": list(model.capabilities.supported_parameters),
            "task_types": list(model.capabilities.task_types),
        },
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
        "connections": list(model.connections),
    }


def _tool_response(tool: Any) -> JsonObject:
    return {
        "name": tool.name,
        "description": tool.description,
    }


def _skill_response(skill_registry: Any, skill: Any) -> JsonObject:
    warnings = skill_registry.warnings_for(skill.name)
    availability = _skill_availability(skill_registry, skill.name)
    return {
        "name": skill.name,
        "description": skill.description,
        "valid": len(warnings) == 0,
        "warnings": warnings,
        "state": availability["state"],
        "requirements": {
            "missing": availability["missing"],
            "optional_missing": availability["optional_missing"],
        },
    }


def _skill_availability(skill_registry: Any, skill_name: str) -> JsonObject:
    availability_for = getattr(skill_registry, "availability_for", None)
    if not callable(availability_for):
        return {"state": "available", "missing": [], "optional_missing": []}

    availability = availability_for(skill_name)
    return {
        "state": getattr(availability, "state", "available"),
        "missing": list(getattr(availability, "missing", ())),
        "optional_missing": list(getattr(availability, "optional_missing", ())),
    }


def _invalid_skill_response(diagnostic: Any) -> JsonObject:
    return {
        "name": diagnostic.name,
        "path": str(diagnostic.path),
        "valid": False,
        "warnings": list(diagnostic.warnings),
    }


def _remove_opaque_provider_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _remove_opaque_provider_metadata(item)
            for key, item in value.items()
            if key != "reasoning_meta"
        }
    if isinstance(value, list):
        return [_remove_opaque_provider_metadata(item) for item in value]
    return value
