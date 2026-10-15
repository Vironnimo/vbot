"""Identity bindings for explicit delegation grants, owned by AgentStore.

A future grant records the current length of the target's retirement history.
The first identity retired after that cursor is its target forever. Recording
before detaching a live Agent makes failed cleanup safe without a multi-file
creation transaction; an aborted creation never consumes a future grant.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    load_validated_json_file,
    validate_json_file,
    warn_unknown_keys,
)
from core.json_documents import (
    OPAQUE,
    JsonDocumentFormat,
    JsonDocumentWriteError,
    document_change,
    json_document,
    json_map,
    validate_format_version,
    write_json_document,
)
from core.settings import is_valid_agent_id
from core.utils.timestamps import format_canonical_timestamp, parse_timestamp, utc_now_timestamp

from ._types import Agent, AgentError

if TYPE_CHECKING:
    from .agents import AgentStore

_SHAPE = json_document({"origin", "retired"}, {"retired": json_map(OPAQUE)})


@dataclass
class _Retirements:
    origin: int
    retired: dict[str, list[str | None]]


def validate_bindings(value: Any) -> list[JsonDiagnostic]:
    """Validate the optional private grant metadata in an Agent document."""
    if not isinstance(value, dict):
        return [error_diagnostic("$.delegation_bindings", "must be an object")]
    diagnostics: list[JsonDiagnostic] = []
    for target, binding in value.items():
        path = f"$.delegation_bindings.{target}"
        if not isinstance(target, str) or not is_valid_agent_id(target):
            add_error(diagnostics, path, "must name an Identity Agent")
        if type(binding) is int and binding >= 0:
            continue
        try:
            parse_timestamp(binding)
        except TypeError, ValueError:
            add_error(diagnostics, path, "must be a creation timestamp or non-negative cursor")
    return diagnostics


def _validate_retirements(data: Any) -> list[JsonDiagnostic]:
    if not isinstance(data, dict):
        return [error_diagnostic("$", "must be an object")]
    diagnostics: list[JsonDiagnostic] = []
    if not validate_format_version(diagnostics, data, 1):
        return diagnostics
    warn_unknown_keys(diagnostics, "$", data, _SHAPE.fields, "Agent retirement field")
    origin = data.get("origin")
    if type(origin) is not int or origin <= 0:
        add_error(diagnostics, "$.origin", "must be a positive journal identity")
    retired = data.get("retired")
    if not isinstance(retired, dict):
        add_error(diagnostics, "$.retired", "must be an object")
        return diagnostics
    for target, stamps in retired.items():
        path = f"$.retired.{target}"
        if not isinstance(target, str) or not is_valid_agent_id(target):
            add_error(diagnostics, path, "must name an Identity Agent")
        if not isinstance(stamps, list):
            add_error(diagnostics, path, "must be a list of creation timestamps")
            continue
        for index, stamp in enumerate(stamps):
            if stamp is None:
                continue  # Rename destination: existing future grants were revoked.
            try:
                parse_timestamp(stamp)
            except TypeError, ValueError:
                add_error(diagnostics, f"{path}[{index}]", "must be a creation timestamp")
    return diagnostics


_FORMAT = JsonDocumentFormat(
    name="Agent retirements", version=1, shape=_SHAPE, validate=_validate_retirements
)


def validate_agent_retirements_file(path: str | Path) -> JsonValidationReport:
    return validate_json_file(path, _validate_retirements, missing_ok=True)


def _path(store: AgentStore) -> Path:
    return store.data_dir / "agents" / "retirements.json"


def _retirements(store: AgentStore, *, required: bool = False) -> _Retirements:
    try:
        data = load_validated_json_file(
            _path(store),
            _validate_retirements,
            missing_ok=True,
            missing_default=None,
        )
    except JsonConfigValidationError as error:
        raise AgentError(str(error)) from error
    if data is None:
        if required:
            raise AgentError("Agent retirement history is missing")
        return _Retirements(uuid4().int << 64, {})
    return _Retirements(data["origin"], cast(dict[str, list[str | None]], data["retired"]))


def _write_retirements(store: AgentStore, history: _Retirements) -> None:
    try:
        with document_change(_path(store), wait=False):
            write_json_document(
                _path(store), {"origin": history.origin, "retired": history.retired}, _FORMAT
            )
    except JsonDocumentWriteError as error:
        raise AgentError(str(error)) from error


def _stamp(store: AgentStore, target: str) -> str | None:
    from ._config import _validated_agent_data

    if not store.exists(target):
        return None
    data = _validated_agent_data(store._agent_path(target))
    stamp = data.get("created_at")
    try:
        if not isinstance(stamp, str):
            raise ValueError("missing creation identity")
        parse_timestamp(stamp)
    except TypeError, ValueError:
        # Establish an identity once, at an explicit grant/lifecycle boundary.
        # A read-time fallback is never evidence of a persisted incarnation.
        agent = store._read_agent_config(store._agent_path(target))
        stamp = new_creation_timestamp(store)
        store._write_agent(replace(agent, created_at=stamp))
    return cast(str, stamp)


def new_creation_timestamp(store: AgentStore) -> str:
    """Keep the existing creation identity unique even with a repeated clock."""
    from ._config import _validated_agent_data

    stamps = {
        stamp
        for items in _retirements(store).retired.values()
        for stamp in items
        if stamp is not None
    }
    for path in (store.data_dir / "agents").glob("*/agent.json"):
        try:
            stamp = _validated_agent_data(path).get("created_at")
            if not isinstance(stamp, str):
                continue
            parse_timestamp(stamp)
        except AgentError, OSError, TypeError, ValueError:
            continue
        stamps.add(stamp)
    now = parse_timestamp(utc_now_timestamp())
    identities = {parse_timestamp(stamp) for stamp in stamps}
    while now in identities:
        now += timedelta(microseconds=1)
    return format_canonical_timestamp(now)


def bind_changes(store: AgentStore, agent: Agent, previous: Agent | None) -> Agent:
    """Bind newly added grants; an unchanged legacy or bound grant keeps its meaning."""
    from core.tools.availability import subagent_allowed_agents

    allowed = subagent_allowed_agents(agent.tools)
    before = set(subagent_allowed_agents(previous.tools)) if previous is not None else set()
    bindings = {key: value for key, value in agent.delegation_bindings.items() if key in allowed}
    retired: _Retirements | None = None
    for target in allowed:
        if target in before or target in bindings or not is_valid_agent_id(target):
            continue
        if target == agent.id:
            bindings[target] = agent.created_at
            continue
        stamp = _stamp(store, target)
        if stamp is not None:
            bindings[target] = stamp
        else:
            if retired is None:
                retired = _retirements(store)
                # A cursor is valid only together with its durable history.
                # Later absence cannot silently turn it into a fresh grant.
                _write_retirements(store, retired)
            bindings[target] = retired.origin + len(retired.retired.get(target, []))
    return replace(agent, delegation_bindings=bindings)


def retire(store: AgentStore, target: str) -> None:
    """Record one occurrence while its source still exists, before any detachment."""
    stamp = _stamp(store, target)
    if stamp is None:
        return
    retired = _retirements(store)
    retired.retired.setdefault(target, []).append(stamp)
    _write_retirements(store, retired)


def revoke_future_grants(store: AgentStore, target: str) -> None:
    """Make a rename destination's removed grants inert in older Run snapshots too."""
    retired = _retirements(store)
    retired.retired.setdefault(target, []).append(None)
    _write_retirements(store, retired)


def target_allowed(store: AgentStore, target: str, binding: str | int) -> bool:
    """Compare a captured grant with the current persisted target identity."""
    from ._config import _validated_agent_data

    if not store.exists(target):
        return False
    current = _validated_agent_data(store._agent_path(target)).get("created_at")
    try:
        if not isinstance(current, str):
            return False
        parse_timestamp(current)
    except ValueError:
        return False
    if isinstance(binding, str):
        return current == binding
    history = _retirements(store, required=True)
    retired = history.retired.get(target, [])
    cursor = binding - history.origin
    if type(binding) is not int or cursor < 0 or cursor > len(retired):
        return False
    return cursor == len(retired) or current == retired[cursor]


def retarget(agent: Agent, old: str, new: str, store: AgentStore) -> Agent:
    """Move a grant with its identity; materialize a future cursor before moving it."""
    bindings = dict(agent.delegation_bindings)
    binding = bindings.pop(old, None)
    if type(binding) is int and binding != 0:
        retired = _retirements(store, required=True)
        history = retired.retired.get(old, [])
        cursor = binding - retired.origin
        if cursor < 0 or cursor >= len(history):
            raise AgentError("Delegation grant has no matching Agent retirement")
        # A revoked future grant stays inert when an old holder config returns
        # from an archive and its listed target is renamed again.
        binding = history[cursor] or 0
    if binding is not None:
        bindings[new] = binding
    return replace(agent, delegation_bindings=bindings)


def archived_binding(store: AgentStore, target: str, agent: Agent) -> dict[str, str | None]:
    """Preserve a grant's original target proof when archive cleanup records it.

    A string that survived failed cleanup may name a different, older identity
    than the Agent now being archived. Restoration must never turn it into a
    fresh grant to that replacement. Missing proof remains explicitly unusable.
    """
    binding = agent.delegation_bindings.get(target)
    if binding is None:
        return {}  # Legacy records retain their existing interpretation.
    if isinstance(binding, str):
        return {"target_created_at": binding}
    try:
        history = _retirements(store, required=True)
        retired = history.retired.get(target, [])
        cursor = binding - history.origin
        if 0 <= cursor < len(retired):
            return {"target_created_at": retired[cursor]}
        if cursor == len(retired):
            return {"target_created_at": _stamp(store, target)}
    except AgentError, OSError:
        pass
    return {"target_created_at": None}
