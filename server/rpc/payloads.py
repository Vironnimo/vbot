"""Shared RPC response payload mappers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from core.chat import ChatMessage, parse_bare_model
from core.providers.providers import (
    model_is_local,
    resolve_effective_context_window,
)
from core.runs import QueuedRunItem, Run
from core.settings.normalizers import normalize_compaction_settings
from core.tools import tool_is_ready

JsonObject = dict[str, Any]


def _run_response(
    run: Run,
    *,
    final_message: ChatMessage | None = None,
    sse_url: str | None = None,
    file_delivery: Any | None = None,
) -> JsonObject:
    response: JsonObject = {
        "run_id": run.id,
        "agent_id": run.agent_id,
        "session_id": run.session_id,
        "run_kind": run.run_kind.value,
        "status": run.status.value,
        "started_at": run.created_at,
        "iteration_count": run.iteration_count,
        "events": [
            remove_opaque_provider_metadata(event.to_dict(), file_delivery=file_delivery)
            for event in run.events
        ],
    }
    if final_message is not None:
        response["message"] = _visible_message(final_message, file_delivery=file_delivery)
    if sse_url is not None:
        response["sse_url"] = sse_url
    return response


def _queued_response(item: QueuedRunItem) -> JsonObject:
    return {
        "queued": True,
        "item": item.to_dict(),
    }


def _visible_message(message: ChatMessage, *, file_delivery: Any | None = None) -> JsonObject:
    return cast(
        JsonObject,
        remove_opaque_provider_metadata(message.to_dict(), file_delivery=file_delivery),
    )


def _is_visible_history_message(message: ChatMessage) -> bool:
    return message.role not in {"note", "history_edit"}


def _resolve_context_window(state: Any, model: str) -> int | None:
    """Resolve a model string (provider/model-id) to the usable context window.

    This is the *active agent's* window for the WebUI token badge, so it
    resolves through the shared effective chain (user-set/capped for
    flagged-local models, else model window → provider-config default → global
    floor, see :func:`resolve_effective_context_window`): a model whose window
    is unknown still yields a usable number instead of ``None``/NaN. Returns
    ``None`` only when the model string is unusable or the model/provider
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
    return resolve_effective_context_window(
        model_entry.context_window,
        _provider_config(state, provider_id),
        model_metadata=model_entry.metadata,
        model_key=f"{provider_id}/{model_id}",
        local_context_windows=_local_context_windows(state),
    )


def _local_context_windows(state: Any) -> Any:
    """Return the live local-model window map from settings, or empty."""
    try:
        return state.runtime.storage.load_local_models_settings()["context_windows"]
    except (AttributeError, KeyError):
        return {}


def _provider_config(state: Any, provider_id: str) -> Any:
    """Return the ProviderConfig for the read-side window default, or None."""
    try:
        return state.runtime.providers.get(provider_id)
    except (KeyError, AttributeError):
        return None


def _agent_response(state: Any, agent: Any) -> JsonObject:
    agent_policy = getattr(agent, "compaction_policy", None)
    if not isinstance(agent_policy, dict):
        agent_policy = None
    return {
        "id": agent.id,
        "name": agent.name,
        "model": agent.model,
        "fallback_models": list(getattr(agent, "fallback_models", ()) or ()),
        "workspace": agent.workspace,
        "root_project_id": getattr(agent, "root_project_id", None),
        # The location a fresh agent's workspace is seeded to
        # (``agents/<id>/workspace/``), resolved to the same string form as
        # ``workspace`` above. The editor shows a "set to default" action when
        # ``workspace`` differs from this (a custom identity/Memory home).
        "default_workspace": state.runtime.agents.default_workspace(agent.id),
        "temperature": agent.temperature,
        "thinking_effort": agent.thinking_effort,
        "memory_prompt_mode": agent.memory_prompt_mode,
        "tool_access": agent.tool_access.to_dict(),
        "allowed_skills": list(agent.allowed_skills),
        "tools": dict(getattr(agent, "tools", {})),
        "custom_system_prompt_enabled": bool(agent.custom_system_prompt_enabled),
        "compaction_policy": dict(agent_policy) if agent_policy is not None else None,
        "effective_compaction_policy": (
            dict(agent_policy)
            if agent_policy is not None
            else (
                state.runtime.storage.load_compaction_settings()
                if getattr(state.runtime, "storage", None) is not None
                else normalize_compaction_settings(None)
            )
        ),
        "current_session_id": agent.current_session_id,
        "context_window": _resolve_context_window(state, agent.model),
        # Raw own values (pre-default-bake), so the editor can distinguish an
        # explicit per-agent value from an inherited global default. Top-level keys
        # above keep today's baked semantics unchanged.
        "config": _agent_raw_config(state, agent.id),
        # Per-field effective value + winning tier ("agent"/"global_default"/null),
        # the provenance seam shared with the resolver.
        "effective": _agent_effective(state, agent.id),
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


def _agent_raw_config(state: Any, agent_id: str) -> JsonObject:
    """Return an identity agent's raw own values (``""``/``None`` preserved)."""
    raw = state.runtime.agents.get_raw(agent_id)
    raw_policy = getattr(raw, "compaction_policy", None)
    if not isinstance(raw_policy, dict):
        raw_policy = None
    return {
        "model": raw.model,
        "fallback_models": list(getattr(raw, "fallback_models", ()) or ()),
        "temperature": raw.temperature,
        "thinking_effort": raw.thinking_effort,
        "compaction_policy": dict(raw_policy) if raw_policy is not None else None,
    }


def _agent_effective(state: Any, agent_id: str) -> JsonObject:
    """Return an identity agent's per-field effective value + source map."""
    return cast(JsonObject, state.runtime.agent_resolver.effective_config(None, agent_id))


def _model_response(
    provider_id: str,
    model: Any,
    *,
    provider_config: Any = None,
    local_context_windows: Any = None,
) -> JsonObject:
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
        # The window vBot actually budgets against: resolved (user-set/capped)
        # for flagged-local models, else the raw window (null when unknown) —
        # the picker suitability filter consumes this field.
        "effective_context_window": _effective_context_window_field(
            provider_id, model, provider_config, local_context_windows
        ),
        # Explicit locality flag (refresh-stamped metadata) so the WebUI can
        # list local models in the context editor without inferring from windows.
        "local": model_is_local(model.metadata),
        "max_output_tokens": model.max_output_tokens,
        "connections": list(model.connections),
    }


def _model_detail_response(
    provider_id: str,
    model: Any,
    *,
    provider_config: Any = None,
    local_context_windows: Any = None,
) -> JsonObject:
    """Return the complete public projection of one loaded Model."""

    response = _model_response(
        provider_id,
        model,
        provider_config=provider_config,
        local_context_windows=local_context_windows,
    )
    capabilities = response["capabilities"]
    reasoning = capabilities["reasoning"]
    reasoning["budget_max"] = model.capabilities.reasoning.budget_max
    capabilities["supported_voices"] = list(model.capabilities.supported_voices)
    capabilities["task_options"] = _json_compatible(model.capabilities.task_options)
    response["family"] = model.family
    response["recommended_temperature"] = model.recommended_temperature
    response["metadata"] = _json_compatible(model.metadata)
    return response


def _json_compatible(value: Any) -> Any:
    """Convert frozen Model mappings and sequences into JSON-native values."""

    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_compatible(item) for item in value]
    return value


def _effective_context_window_field(
    provider_id: str,
    model: Any,
    provider_config: Any,
    local_context_windows: Any,
) -> int | None:
    if not model_is_local(model.metadata):
        # Deliberately the RAW window for non-local models (null stays null —
        # the WebUI shows an honest "context unknown" badge, never a floor).
        return cast("int | None", model.context_window)
    return resolve_effective_context_window(
        model.context_window,
        provider_config,
        model_metadata=model.metadata,
        model_key=f"{provider_id}/{model.model_id}",
        local_context_windows=local_context_windows,
    )


def _tool_response(tool: Any) -> JsonObject:
    return {
        "name": tool.name,
        "description": tool.description,
        # Whether the tool can run right now (its readiness predicate, a raising
        # predicate counting as false); the picker/whitelist editor styles a
        # not-ready tool instead of hiding it.
        "ready": tool_is_ready(tool),
        # Optional English hint explaining the readiness precondition, or null.
        "readiness_hint": getattr(tool, "readiness_hint", None),
        "family": getattr(tool, "family", None),
        "family_label": getattr(tool, "family_label", None),
        "activation": getattr(tool, "activation", "configurable"),
        "activation_source": getattr(tool, "activation_source", None),
        "constraints": list(getattr(tool, "constraints", ())),
        "session_scoped": bool(getattr(tool, "session_scoped", False)),
        # The owning extension name, or null for a built-in tool.
        "extension": getattr(tool, "extension", None),
        # Stable contract diagnostics. The fingerprint changes whenever the
        # Tool's input, result, or scheduling contract changes.
        "schema_fingerprint": tool.contract.schema_fingerprint,
        "parallel_safe": tool.parallel_safe,
    }


def _skill_response(skill_registry: Any, skill: Any) -> JsonObject:
    warnings = skill_registry.warnings_for(skill.name)
    availability = _skill_availability(skill_registry, skill.name)
    return {
        "name": skill.name,
        "description": skill.description,
        "origin": getattr(skill, "origin", None),
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


def remove_opaque_provider_metadata(value: Any, *, file_delivery: Any | None = None) -> Any:
    """Project one internal value into the shared client-visible payload form.

    Opaque reasoning metadata and its Provider/Model/Connection/Account scope are
    adapter round-trip state, and Assistant output-file references contain raw
    server paths. Neither may reach a client as metadata. When the server's file
    delivery service is available it consumes those references to rewrite the
    corresponding content lines to signed URLs; without it, they are simply
    removed. Shared by RPC, SSE, and the event bridge so transports cannot drift.
    """
    if isinstance(value, dict):
        source = file_delivery.project_message(value) if file_delivery is not None else value
        return {
            key: remove_opaque_provider_metadata(item, file_delivery=file_delivery)
            for key, item in source.items()
            if key not in {"output_files", "reasoning_meta", "reasoning_scope"}
        }
    if isinstance(value, list):
        return [
            remove_opaque_provider_metadata(item, file_delivery=file_delivery) for item in value
        ]
    return value
