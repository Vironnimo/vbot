"""Skill mutation RPC handlers.

Write data-dir skill scopes for the UI/accessors: ``global`` (the user-curated
``<data_dir>/skills``) or ``agent:<agent_id>`` (a chosen agent's private home).
Never the project/repo scope — those are repo files authored with the ordinary
file tools. All writes go through the one validated authoring service as a person
(``HUMAN_WRITER``: ``metadata.vbot.author: human`` and a ``human`` history revision),
then scoped invalidation so the change is live without a restart; package imports
preserve the source document. ``skill.delete`` moves the package into the home's
archive.
Validation failures surface authoring diagnostics as an ``invalid_request`` error.

The same scopes expose the Skill history and archive: ``skill.history`` lists
revisions, ``skill.revert`` undoes some (a conflict is a ``domain_error`` whose
``data`` names the later revision), ``skill.archived`` / ``skill.restore`` /
``skill.purge`` manage archived packages and ``skill.set_pinned`` pins a Skill
against background changes.

The manager surface (``skill.inventory`` / ``skill.set_disabled`` / ``skill.share``)
reads every source without exclusions and mutates the Skills domain's policy file;
both mutations invalidate live and publish the generic resource-changed event with
the ``skills`` kind. The inventory adds each package's Skill use from Statistics.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from core.settings import is_valid_agent_id
from core.skills import (
    HUMAN_WRITER,
    SkillAuthoringError,
    SkillPolicyError,
    SkillRegistry,
    SkillRevertConflictError,
    SkillWriteResult,
)
from core.statistics import SkillUse
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool
from server.events import RESOURCE_KIND_SKILLS
from server.rpc._mutations import MutationHandler, serialized_mutation
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import (
    RPC_ERROR_AGENT_NOT_FOUND,
    RPC_ERROR_DOMAIN,
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_SKILL_NOT_FOUND,
    RpcError,
)
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.statistics_methods import statistics_service
from server.rpc.validation import _optional_string, _required_string

JsonObject = dict[str, Any]

_GLOBAL_SCOPE = "global"
_AGENT_SCOPE_PREFIX = "agent:"
_LOGGER = get_logger("server.rpc.skills")
_SKILL_READ_WORKERS = BoundedWorkerPool(name="skill-manager", max_workers=1)
_DEFAULT_HISTORY_LIMIT = 50
_MAX_HISTORY_LIMIT = 500


def _validated_scope(state: Any, params: JsonObject) -> str:
    """Return the request's ``scope``, rejecting anything but global / agent:<id>.

    The ``agent:<id>`` id becomes a filesystem path segment, so it is validated with
    the canonical (traversal-safe) agent-id rule before any path is built, and it
    must name an **existing identity agent**: private skill homes are identity-only,
    so writing to an unknown id (e.g. a project-team slug) would create a stray
    ``agents/<id>/skills`` directory that no agent owns. A project or repo scope is
    rejected here — v1 write surfaces never target the repo.
    """
    scope = _required_string(params, "scope")
    if scope == _GLOBAL_SCOPE:
        return scope
    if scope.startswith(_AGENT_SCOPE_PREFIX):
        agent_id = scope[len(_AGENT_SCOPE_PREFIX) :]
        if not is_valid_agent_id(agent_id):
            raise RpcError(RPC_ERROR_INVALID_REQUEST, f"invalid agent scope id: {agent_id!r}")
        if not state.runtime.agents.exists(agent_id):
            raise RpcError(
                RPC_ERROR_AGENT_NOT_FOUND,
                f"unknown agent for skill scope: {agent_id!r} (private skills are identity-only)",
            )
        return scope
    raise RpcError(
        RPC_ERROR_INVALID_REQUEST,
        f"unsupported skill scope: {scope!r} (use 'global' or 'agent:<id>')",
    )


def _scope_root(state: Any, scope: str) -> Path:
    if scope == _GLOBAL_SCOPE:
        return cast(Path, state.runtime.global_skills_dir)
    return cast(Path, state.runtime.agent_skills_dir(scope[len(_AGENT_SCOPE_PREFIX) :]))


async def _invalidate_scope(state: Any, scope: str) -> None:
    if scope == _GLOBAL_SCOPE:
        # A global write changes the shared pool every project/agent registry layers
        # over, so reload the whole registry (which also drops those caches).
        await state.runtime.reload_skills_async()
    else:
        state.runtime.invalidate_agent_skills(scope[len(_AGENT_SCOPE_PREFIX) :])


async def _write(
    state: Any,
    scope: str,
    write: Callable[[Path], SkillWriteResult],
    *,
    refresh: bool = True,
) -> JsonObject:
    """Run one authoring write, map its diagnostics to an RpcError, then invalidate.

    Every authoring operation changes the scope's packages or their history, so
    each success publishes one Skills invalidation for open views. ``refresh``
    false skips the registry invalidation for writes no registry sees (pins).
    """
    try:
        result = await _SKILL_READ_WORKERS.run(write, _scope_root(state, scope))
    except SkillAuthoringError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "; ".join(exc.diagnostics)) from exc
    except OSError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    if refresh:
        await _invalidate_scope(state, scope)
    _LOGGER.info(
        "Skill mutated (skill=%s scope=%s operation=%s)",
        result.name,
        scope,
        result.operation,
    )
    publish_resource_changed(state, RESOURCE_KIND_SKILLS)
    response: JsonObject = {
        "name": result.name,
        "operation": result.operation,
        "warnings": list(result.warnings),
        "revision": result.revision,
    }
    if result.archive_id is not None:
        response["archive_id"] = result.archive_id
    return response


async def _skill_read(state: Any, params: JsonObject) -> JsonObject:
    return await _SKILL_READ_WORKERS.run(_read_skills, state, params)


def _read_skills(state: Any, params: JsonObject) -> JsonObject:
    """Return the editable skills of one scope, each with its full ``SKILL.md`` text.

    Scans only the scope's own directory (the data-dir global pool or an agent's
    private home), so bundled and project skills never appear — those are not
    editable here. The content lets the UI view and pre-fill an edit form.
    """
    scope = _validated_scope(state, params)
    registry = SkillRegistry.load(_scope_root(state, scope))
    skills: list[JsonObject] = []
    for skill in registry.list_all():
        try:
            content = skill.path.read_text(encoding="utf-8")
        except OSError:
            content = ""
        skills.append({"name": skill.name, "description": skill.description, "content": content})
    return {"skills": skills}


async def _skill_create(state: Any, params: JsonObject) -> JsonObject:
    scope = await _SKILL_READ_WORKERS.run(_validated_scope, state, params)
    name = _required_string(params, "name")
    content = _required_string(params, "content")
    source = _optional_string(params, "source")
    return await _write(
        state,
        scope,
        lambda root: state.runtime.skill_authoring.create(
            root, name, content, writer=HUMAN_WRITER, source=source
        ),
    )


async def _skill_install(
    state: Any, params: JsonObject, *, archive: bytes | None = None
) -> JsonObject:
    unknown = set(params) - {
        "scope",
        "source",
        "path",
        "ref",
        "replace",
        "dry_run",
        "expected_sha256",
    }
    if unknown:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Unsupported Skill installation parameters")
    scope = await _SKILL_READ_WORKERS.run(_validated_scope, state, params)
    source = _required_string(params, "source")
    path = _required_string(params, "path") if "path" in params else None
    ref = _required_string(params, "ref") if "ref" in params else None
    replace = _required_bool(params, "replace") if "replace" in params else False
    dry_run = _required_bool(params, "dry_run") if "dry_run" in params else False
    expected_sha256 = (
        _required_string(params, "expected_sha256") if "expected_sha256" in params else None
    )
    if expected_sha256 is not None and (
        len(expected_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_sha256)
    ):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "expected_sha256 must be a SHA-256 digest")
    try:
        result = await _SKILL_READ_WORKERS.run(
            state.runtime.skill_authoring.install,
            _scope_root(state, scope),
            source,
            path=path,
            ref=ref,
            replace=replace,
            dry_run=dry_run,
            archive=archive,
            expected_sha256=expected_sha256,
        )
    except (SkillAuthoringError, OSError) as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error
    if result.operation in {"installed", "replaced", "unchanged"}:
        await _invalidate_scope(state, scope)
        if result.operation != "unchanged":
            _LOGGER.info(
                "Skill installed (skill=%s scope=%s operation=%s)",
                result.name,
                scope,
                result.operation,
            )
            publish_resource_changed(state, RESOURCE_KIND_SKILLS)
    return {**result.to_dict(), "scope": scope}


async def install_skill_upload(
    state: Any, params: JsonObject, *, data: bytes, filename: str
) -> JsonObject:
    """Binary HTTP ingress, sharing RPC validation, serialization and publication."""

    async def install(state: Any, params: JsonObject) -> JsonObject:
        return await _skill_install(state, params, archive=data)

    return await _serialized_skill_mutation(install)(state, {**params, "source": filename})


async def _skill_update(state: Any, params: JsonObject) -> JsonObject:
    scope = await _SKILL_READ_WORKERS.run(_validated_scope, state, params)
    name = _required_string(params, "name")
    content = _required_string(params, "content")
    source = _optional_string(params, "source")
    return await _write(
        state,
        scope,
        lambda root: state.runtime.skill_authoring.edit(
            root, name, content, writer=HUMAN_WRITER, source=source
        ),
    )


async def _skill_delete(state: Any, params: JsonObject) -> JsonObject:
    scope = await _SKILL_READ_WORKERS.run(_validated_scope, state, params)
    name = _required_string(params, "name")
    return await _write(
        state,
        scope,
        lambda root: state.runtime.skill_authoring.delete(root, name, writer=HUMAN_WRITER),
    )


async def _skill_set_pinned(state: Any, params: JsonObject) -> JsonObject:
    scope = await _SKILL_READ_WORKERS.run(_validated_scope, state, params)
    name = _required_string(params, "name")
    pinned = _required_bool(params, "pinned")
    return await _write(
        state,
        scope,
        lambda root: state.runtime.skill_authoring.set_pinned(
            root, name, pinned, writer=HUMAN_WRITER
        ),
        refresh=False,
    )


async def _skill_history(state: Any, params: JsonObject) -> JsonObject:
    return await _SKILL_READ_WORKERS.run(_read_history, state, params)


def _read_history(state: Any, params: JsonObject) -> JsonObject:
    """Return a writable home's revisions, or one Skill's, newest first."""
    _reject_unknown(params, {"scope", "name", "limit"}, "skill.history")
    scope = _validated_scope(state, params)
    name = _optional_string(params, "name")
    limit = params.get("limit", _DEFAULT_HISTORY_LIMIT)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAX_HISTORY_LIMIT
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.limit must be an integer from 1 to {_MAX_HISTORY_LIMIT}",
        )
    try:
        revisions = state.runtime.skill_authoring.history(
            _scope_root(state, scope), name, limit=limit
        )
    except (SkillAuthoringError, OSError) as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    return {"scope": scope, "revisions": [revision.to_dict() for revision in revisions]}


async def _skill_revert(state: Any, params: JsonObject) -> JsonObject:
    """Undo revisions of one home, all or none, as a person."""
    _reject_unknown(params, {"scope", "revisions"}, "skill.revert")
    scope = await _SKILL_READ_WORKERS.run(_validated_scope, state, params)
    revision_ids = params.get("revisions")
    if (
        not isinstance(revision_ids, list)
        or not revision_ids
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in revision_ids
        )
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.revisions must be a non-empty list of positive integers",
        )
    try:
        revisions = await _SKILL_READ_WORKERS.run(
            state.runtime.skill_authoring.revert,
            _scope_root(state, scope),
            revision_ids,
            writer=HUMAN_WRITER,
        )
    except SkillRevertConflictError as exc:
        raise RpcError(
            RPC_ERROR_DOMAIN,
            str(exc),
            data={"revision": exc.revision, "later": exc.later, "skill": exc.skill_name},
        ) from exc
    except (SkillAuthoringError, OSError) as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    await _invalidate_scope(state, scope)
    _LOGGER.info("Skill revisions reverted (scope=%s revisions=%s)", scope, revision_ids)
    publish_resource_changed(state, RESOURCE_KIND_SKILLS)
    return {"scope": scope, "revisions": [revision.to_dict() for revision in revisions]}


async def _skill_archived(state: Any, params: JsonObject) -> JsonObject:
    return await _SKILL_READ_WORKERS.run(_read_archived, state, params)


def _read_archived(state: Any, params: JsonObject) -> JsonObject:
    """Return the archived packages of one writable home, newest first."""
    _reject_unknown(params, {"scope"}, "skill.archived")
    scope = _validated_scope(state, params)
    try:
        archived = state.runtime.skill_authoring.archived(_scope_root(state, scope))
    except (SkillAuthoringError, OSError) as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    return {"scope": scope, "archived": [entry.to_dict() for entry in archived]}


async def _skill_restore(state: Any, params: JsonObject) -> JsonObject:
    _reject_unknown(params, {"scope", "archive_id"}, "skill.restore")
    scope = await _SKILL_READ_WORKERS.run(_validated_scope, state, params)
    archive_id = _required_string(params, "archive_id")
    return await _write(
        state,
        scope,
        lambda root: state.runtime.skill_authoring.restore(root, archive_id, writer=HUMAN_WRITER),
    )


async def _skill_purge(state: Any, params: JsonObject) -> JsonObject:
    """Permanently delete one archived package; no registry sees the archive."""
    _reject_unknown(params, {"scope", "archive_id"}, "skill.purge")
    scope = await _SKILL_READ_WORKERS.run(_validated_scope, state, params)
    archive_id = _required_string(params, "archive_id")
    try:
        purged = await _SKILL_READ_WORKERS.run(
            state.runtime.skill_authoring.purge, _scope_root(state, scope), archive_id
        )
    except (SkillAuthoringError, OSError) as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    publish_resource_changed(state, RESOURCE_KIND_SKILLS)
    return {"scope": scope, "purged": purged.to_dict()}


def _reject_unknown(params: JsonObject, allowed: set[str], method: str) -> None:
    unknown = set(params) - allowed
    if unknown:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"{method} does not accept: {', '.join(sorted(unknown))}",
        )


async def _skill_write_file(state: Any, params: JsonObject) -> JsonObject:
    scope = await _SKILL_READ_WORKERS.run(_validated_scope, state, params)
    name = _required_string(params, "name")
    path = _required_string(params, "path")
    content = params.get("content")
    if not isinstance(content, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.content must be a string")
    return await _write(
        state,
        scope,
        lambda root: state.runtime.skill_authoring.write_file(
            root, name, path, content, writer=HUMAN_WRITER
        ),
    )


async def _skill_remove_file(state: Any, params: JsonObject) -> JsonObject:
    scope = await _SKILL_READ_WORKERS.run(_validated_scope, state, params)
    name = _required_string(params, "name")
    path = _required_string(params, "path")
    return await _write(
        state,
        scope,
        lambda root: state.runtime.skill_authoring.remove_file(
            root, name, path, writer=HUMAN_WRITER
        ),
    )


async def _skill_inspect(state: Any, params: JsonObject) -> JsonObject:
    entry_id = _required_string(params, "id")
    try:
        return cast(
            JsonObject, await _SKILL_READ_WORKERS.run(state.runtime.inspect_skill, entry_id)
        )
    except (ValueError, OSError) as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc


async def _skill_inventory(state: Any, params: JsonObject) -> JsonObject:
    """Return every Skill from every source with status/share/owner annotations.

    Each entry also carries ``uses`` and ``last_used_at`` from Statistics; when
    Statistics cannot answer, the entries carry neither.
    """
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "skill.inventory does not accept params")
    inventory = cast(JsonObject, await _SKILL_READ_WORKERS.run(state.runtime.skill_inventory))
    try:
        usage = await statistics_service(state).skill_usage_async()
    except Exception as exc:  # Skill use only annotates the inventory.
        _LOGGER.warning("Skill use is unavailable for the Skill inventory: %s", exc)
    else:
        _annotate_usage(inventory, usage)
    return inventory


def _annotate_usage(inventory: JsonObject, usage: Mapping[tuple[str, str], SkillUse]) -> None:
    """Add each entry's ``uses`` and ``last_used_at`` from per-Agent Skill use.

    Use is keyed by Agent and bare Skill name, like the Statistics report. A
    private package counts its owner and the Agents it is shared with (unless
    they own a package of that name); any other package counts every Agent that
    neither owns nor receives a private package of that name.
    """
    entries: list[JsonObject] = inventory["skills"]
    private_owners: dict[str, set[str]] = {}
    private_users: dict[str, set[str]] = {}
    for entry in entries:
        owner = entry.get("owner_id")
        if owner:
            private_owners.setdefault(entry["name"], set()).add(owner)
            private_users.setdefault(entry["name"], set()).update(
                {owner, *(entry.get("shared_with") or ())}
            )
    by_name: dict[str, list[tuple[str, SkillUse]]] = {}
    for (agent_id, name), use in usage.items():
        by_name.setdefault(name, []).append((agent_id, use))
    for entry in entries:
        name = entry["name"]
        owner = entry.get("owner_id")
        if owner:
            users = {owner} | (set(entry.get("shared_with") or ()) - private_owners[name])
            uses = [use for agent_id, use in by_name.get(name, ()) if agent_id in users]
        else:
            excluded = private_users.get(name, set())
            uses = [use for agent_id, use in by_name.get(name, ()) if agent_id not in excluded]
        entry["uses"] = sum(use.count for use in uses)
        # Canonical UTC timestamps order as text.
        entry["last_used_at"] = max((use.last_activated for use in uses), default=None)


def _required_bool(params: JsonObject, key: str) -> bool:
    value = params.get(key)
    if not isinstance(value, bool):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a boolean")
    return value


async def _skill_set_disabled(state: Any, params: JsonObject) -> JsonObject:
    """Toggle the policy disable switch for one Skill name (master switch)."""
    name = _required_string(params, "name")
    disabled = _required_bool(params, "disabled")
    inventory = await _SKILL_READ_WORKERS.run(state.runtime.skill_inventory)
    if not any(entry["name"] == name for entry in inventory["skills"]):
        raise RpcError(RPC_ERROR_SKILL_NOT_FOUND, f"unknown skill: {name!r}")
    try:
        await _SKILL_READ_WORKERS.run(
            state.runtime.skill_policy.set_disabled, name, disabled=disabled
        )
    except SkillPolicyError as exc:
        # An unreadable or invalid policy document is refused rather than
        # overwritten; report it as an expected domain failure.
        raise RpcError(RPC_ERROR_DOMAIN, str(exc)) from exc
    await state.runtime.reload_skills_async()
    publish_resource_changed(state, RESOURCE_KIND_SKILLS)
    return {"name": name, "disabled": disabled}


async def _skill_share(state: Any, params: JsonObject) -> JsonObject:
    result = await _SKILL_READ_WORKERS.run(_share_skill_policy, state, params)
    state.runtime.invalidate_agent_skills(None)
    publish_resource_changed(state, RESOURCE_KIND_SKILLS)
    return result


def _share_skill_policy(state: Any, params: JsonObject) -> JsonObject:
    """Share or unshare one Identity Agent's private Skill with specific Agents."""
    agent_id = _required_string(params, "agent_id")
    if not is_valid_agent_id(agent_id):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"invalid agent id: {agent_id!r}")
    if not state.runtime.agents.exists(agent_id):
        raise RpcError(
            RPC_ERROR_AGENT_NOT_FOUND,
            f"unknown agent: {agent_id!r} (sharing is identity-agent-only)",
        )
    name = _required_string(params, "name")
    shared = _required_bool(params, "shared")
    if not state.runtime.agent_owns_private_skill(agent_id, name):
        raise RpcError(
            RPC_ERROR_SKILL_NOT_FOUND,
            f"agent {agent_id!r} owns no private skill named {name!r}",
        )
    receivers: list[str] = []
    if shared:
        raw_receivers = params.get("receivers", [])
        if not isinstance(raw_receivers, list):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST, "params.receivers must be a list of agent ids"
            )
        for receiver_id in raw_receivers:
            if not isinstance(receiver_id, str) or not is_valid_agent_id(receiver_id):
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    f"invalid receiver agent id: {receiver_id!r}",
                )
            if receiver_id == agent_id:
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    "an agent cannot share a skill with itself",
                )
            if not state.runtime.agents.exists(receiver_id):
                raise RpcError(
                    RPC_ERROR_AGENT_NOT_FOUND,
                    f"unknown receiver agent: {receiver_id!r}",
                )
            receivers.append(receiver_id)
        if not receivers:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "at least one receiver agent is required to share a skill",
            )
    try:
        state.runtime.skill_policy.set_shared(agent_id, name, shared=shared, receivers=receivers)
    except SkillPolicyError as exc:
        raise RpcError(RPC_ERROR_DOMAIN, str(exc)) from exc
    return {"agent_id": agent_id, "name": name, "shared": shared, "receivers": receivers}


def _serialized_skill_mutation(
    handler: MutationHandler, *, references_agents: bool = False
) -> MutationHandler:
    mutate = serialized_mutation(handler, lock_attribute="_skill_mutation_lock")

    async def run(state: Any, params: JsonObject) -> JsonObject:
        scope = params.get("scope")
        if references_agents or (isinstance(scope, str) and scope.startswith(_AGENT_SCOPE_PREFIX)):
            # Admission must precede scope validation and remain held through
            # publication, including settlement after caller cancellation.
            async with _agent_reference_lock(state):
                return await mutate(state, params)
        return await mutate(state, params)

    return run


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the skill mutation RPC handlers."""
    return {
        "skill.read": _skill_read,
        "skill.create": _serialized_skill_mutation(_skill_create),
        "skill.install": _serialized_skill_mutation(_skill_install),
        "skill.update": _serialized_skill_mutation(_skill_update),
        "skill.delete": _serialized_skill_mutation(_skill_delete),
        "skill.write_file": _serialized_skill_mutation(_skill_write_file),
        "skill.remove_file": _serialized_skill_mutation(_skill_remove_file),
        "skill.set_pinned": _serialized_skill_mutation(_skill_set_pinned),
        "skill.history": _skill_history,
        "skill.revert": _serialized_skill_mutation(_skill_revert),
        "skill.archived": _skill_archived,
        "skill.restore": _serialized_skill_mutation(_skill_restore),
        "skill.purge": _serialized_skill_mutation(_skill_purge),
        "skill.inventory": _skill_inventory,
        "skill.inspect": _skill_inspect,
        "skill.set_disabled": serialized_mutation(
            _skill_set_disabled, lock_attribute="_skill_mutation_lock"
        ),
        "skill.share": _serialized_skill_mutation(_skill_share, references_agents=True),
    }
