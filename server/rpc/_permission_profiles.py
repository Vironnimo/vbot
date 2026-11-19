"""Operator management of the named profiles shared by Agent and Project assignments."""

from __future__ import annotations

from typing import Any

from core.permissions import (
    BUILTIN_PERMISSION_PROFILES,
    PermissionRulesError,
    complete_effective_permission_rules,
    custom_permission_profile_name,
    normalize_permission_profile_name,
    permission_profile_catalog,
    resolve_permission_profile,
)
from core.utils.logging import get_logger
from server.access import ACCESS_DENIED, current_principal
from server.events import RESOURCE_KIND_AGENTS, RESOURCE_KIND_PROJECTS, RESOURCE_KIND_SETTINGS
from server.rpc._mutations import (
    MutationHandler,
    require_mutation_available,
    serialized_mutation,
    serialized_read,
    state_read_lock,
)
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RPC_ERROR_SETTINGS_CONFLICT, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import _optional_bool, _reject_unsupported

type JsonObject = dict[str, Any]

_LOGGER = get_logger("server.rpc.permissions")
_SETTINGS_LOCK = "_settings_mutation_lock"


def _require_operator() -> str:
    # Operator identity belongs to the ingress task, before mutation admission.
    principal = current_principal()
    if principal is None:
        raise RpcError(ACCESS_DENIED, "Only the user can change permission profiles.")
    return principal.actor


def guard_profile_assignment(handler: MutationHandler) -> MutationHandler:
    async def guarded(state: Any, params: JsonObject) -> JsonObject:
        if "expected_profile_rules" in params:
            _require_operator()
            if not ({"permission_profile", "permission_rules"} & params.keys()) and params.get(
                "field"
            ) not in ("permission_profile", "permission_rules"):
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST, "expected_profile_rules needs a permission change."
                )
        if params.get("field") == "permission_profile":
            _require_operator()
            async with _agent_reference_lock(state):
                return await handler(state, params)
        if "permission_profile" in params:
            _require_operator()
        return await handler(state, params)

    return guarded


def guard_reviewed_rules(handler: MutationHandler) -> MutationHandler:
    """Keep operator Settings writes out between a reviewed comparison and its Agent write."""

    async def guarded(state: Any, params: JsonObject) -> JsonObject:
        if "expected_profile_rules" in params:
            async with state_read_lock(state, _SETTINGS_LOCK):
                return await handler(state, params)
        return await handler(state, params)

    return guarded


def check_reviewed_rules(params: JsonObject, effective: JsonObject) -> None:
    """Compare the exact effective policy after this change to the approved outcome."""
    if "expected_profile_rules" not in params:
        return
    try:
        expected = complete_effective_permission_rules(params["expected_profile_rules"])
    except PermissionRulesError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"Invalid expected_profile_rules: {exc}") from exc
    if (
        effective.get("error")
        or complete_effective_permission_rules(effective["value"]) != expected
    ):
        raise RpcError(
            RPC_ERROR_SETTINGS_CONFLICT,
            "The resulting permission rules have changed or are unavailable. "
            "Review the current rules before applying this change.",
        )


def inline_profile_precondition(params: JsonObject, *, inline_rules: bool) -> bool:
    """A conditional inline write cannot silently remove a concurrent assignment."""
    if "require_no_permission_profile" in params and not inline_rules:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "require_no_permission_profile only applies to an inline permission_rules update.",
        )
    return _optional_bool(params, "require_no_permission_profile", default=False)


def validate_profile_selection(state: Any, value: Any) -> str:
    """Check against the live catalog while holding the assignment reference lock."""
    try:
        name = normalize_permission_profile_name(value)
        resolve_permission_profile(
            name, state.runtime.storage.load_settings().get("permission_profiles", {})
        )
    except PermissionRulesError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    return name


def _profile_response(profiles: JsonObject) -> JsonObject:
    return {
        "profiles": [
            {"name": name, "builtin": name in BUILTIN_PERMISSION_PROFILES, "rules": rules}
            for name, rules in permission_profile_catalog(profiles).items()
        ]
    }


async def _list_profiles(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "permissions.profiles")

    def read() -> JsonObject:
        return _profile_response(
            state.runtime.storage.load_settings().get("permission_profiles", {})
        )

    result: JsonObject = await state.runtime.chat_sessions.run_async(read)
    return result


def _refuse_assigned(state: Any, name: str) -> None:
    from core.archive import permission_profile_references

    references, unavailable = permission_profile_references(
        state.runtime.storage.data_dir, state.runtime.chat_sessions.archive_ledger, name
    )
    if references:
        raise RpcError(
            "permission_profile_in_use",
            f"Permission profile '{name}' is assigned to {', '.join(references)}.",
            data={"agents": references},
        )
    if unavailable:
        raise RpcError(
            "permission_profile_references_unavailable",
            "Could not verify permission profile assignments in "
            + ", ".join(unavailable)
            + ". Check the listed configuration or Archive Entry before deleting the profile.",
            data={"locations": unavailable},
        )


async def _change_profile(
    state: Any, params: JsonObject, *, delete: bool, actor: str
) -> JsonObject:
    method = "permissions.delete_profile" if delete else "permissions.save_profile"
    _reject_unsupported(
        params, {"name", "base"} if delete else {"name", "permission_rules", "base"}, method
    )
    try:
        name = custom_permission_profile_name(params.get("name"))
    except PermissionRulesError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    rules = params.get("permission_rules")
    if not delete and not isinstance(rules, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.permission_rules must be an object.")
    base = {name: params["base"]} if "base" in params else None

    def save() -> JsonObject:
        if delete:
            _refuse_assigned(state, name)
        saved = state.runtime.storage.update_permission_profiles({name: rules}, base=base)
        return _profile_response(saved)

    try:
        result: JsonObject = await state.runtime.chat_sessions.run_async(save)
    except PermissionRulesError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    _LOGGER.info(
        "Permission profile %s (profile=%s actor=%s)", "deleted" if delete else "saved", name, actor
    )
    for kind in (RESOURCE_KIND_SETTINGS, RESOURCE_KIND_AGENTS, RESOURCE_KIND_PROJECTS):
        publish_resource_changed(state, kind)
    return result


def _operator_mutation(*, delete: bool) -> MutationHandler:
    async def guarded(state: Any, params: JsonObject) -> JsonObject:
        actor = _require_operator()

        async def change(state: Any, params: JsonObject) -> JsonObject:
            return await _change_profile(state, params, delete=delete, actor=actor)

        serialized = serialized_mutation(change, lock_attribute=_SETTINGS_LOCK)
        if delete:
            require_mutation_available(state, _SETTINGS_LOCK)
            async with _agent_reference_lock(state):
                return await serialized(state, params)
        return await serialized(state, params)

    return guarded


def method_handlers() -> dict[str, MutationHandler]:
    return {
        "permissions.profiles": serialized_read(_list_profiles, lock_attribute=_SETTINGS_LOCK),
        "permissions.save_profile": _operator_mutation(delete=False),
        "permissions.delete_profile": _operator_mutation(delete=True),
    }
