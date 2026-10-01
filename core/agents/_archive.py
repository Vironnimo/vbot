"""Moving one Identity Agent's files into an archive payload and back.

AgentStore internals: each function takes the store and runs under its change
admission and lock. Every tree moves through :func:`move_tree`, whose failure
always means "not moved", so compensation never has to guess which copy is
whole; it never deletes payload content.
"""

from __future__ import annotations

import builtins
import json
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core.agents._config import (
    AGENT_FORMAT,
    AGENT_FORMAT_VERSION,
    _agent_document,
    _agent_from_dict,
    _apply_defaults,
    _utc_now,
    _validate_agent_id,
    load_validated_agent_json,
)
from core.agents._types import (
    Agent,
    AgentAlreadyExistsError,
    AgentError,
    ArchivedAgent,
    ArchivedAgentPayload,
    _AgentOrderDocument,
)
from core.agents._workspace import (
    _rebase_path_with_tree,
    _resolve_workspace,
    _workspace_for_storage,
)
from core.json_documents import JsonDocumentWriteError, document_version_state, write_json_document
from core.settings import is_valid_agent_id
from core.utils.atomic import atomic_write_bytes
from core.utils.ids import has_id_entry
from core.utils.logging import get_logger
from core.utils.tree_move import move_tree

if TYPE_CHECKING:
    from core.agents.agents import AgentStore

_LOGGER = get_logger("agents")

# One removed or restored delegation grant: the Agent whose list held the id
# and the id's position in that list.
Grant = Mapping[str, Any]


@contextmanager
def archive_files(store: AgentStore, agent_id: str, tree: Path) -> Iterator[ArchivedAgent]:
    """Move ``agents/<agent_id>`` to ``tree`` for the duration of the caller's commit.

    The body runs with the move done and the store locked; if it raises, the
    tree moves back. A Workspace outside the Agent's directory stays in place.
    """
    with store._snapshot_barrier.compound_mutation(), store._change():
        agent = store._read_agent_config(store._require_agent_path(agent_id))
        order = store._load_agent_order()
        roster_index = (
            order.agent_ids.index(agent_id)
            if order is not None and agent_id in order.agent_ids
            else None
        )
        home = store._agent_dir(agent_id)
        workspace = _workspace_for_storage(agent.workspace, data_dir=store.data_dir)
        archived = ArchivedAgent(
            agent=_apply_defaults(agent, store._agent_defaults()),
            roster_index=roster_index,
            workspace=workspace,
            workspace_external=not Path(agent.workspace).resolve().is_relative_to(home.resolve()),
        )
        try:
            tree.parent.mkdir(parents=True, exist_ok=True)
            move_tree(home, tree)
        except OSError as exc:
            raise AgentError(f"Agent archival failed: {exc}") from exc
        try:
            yield archived
        except BaseException as exc:
            _move_back(tree, home, exc, "Agent archival failed")
            raise


def _move_back(source: Path, destination: Path, error: BaseException, failure: str) -> None:
    """Return a moved tree; when that fails too, say where the files are."""
    try:
        move_tree(source, destination)
    except OSError as move_error:
        raise AgentError(f"{failure} ({error}); Agent files retained at {source}") from move_error


def _return_to_payload(config: Path, archived_document: bytes) -> None:
    """Give a restored ``agent.json`` its archived bytes back before it returns to the payload.

    Should that fail, the payload keeps the rewritten document, which restores
    all the same; the failure is logged.
    """
    try:
        atomic_write_bytes(config, archived_document)
    except OSError as error:
        _LOGGER.warning("Could not return %s to its archived content: %s", config, error)


def remove_delegation_grants(
    store: AgentStore, agent_id: str, record: Callable[[builtins.list[Grant]], None]
) -> tuple[str, ...]:
    """Remove a bare ``agent_id`` from every delegation list; return the changed holders.

    ``record`` receives the grants before any config is written, so a restore
    can re-add them even when this step stops halfway. Best effort: a config
    that cannot be written keeps its entry and the failure is logged.
    """
    with store._change():
        try:
            grants: builtins.list[Grant] = [
                {"agent_id": holder.id, "index": allowed.index(agent_id)}
                for holder, allowed in store._allow_lists_naming(agent_id)
            ]
        except OSError as error:
            _LOGGER.warning(
                "Could not read delegation lists to remove an Agent (agent=%s): %s",
                agent_id,
                error,
            )
            return ()
        if grants:
            record(grants)
        return store._remove_from_allow_lists(agent_id, best_effort=True)


def inspect_archived(store: AgentStore, source: Path) -> ArchivedAgentPayload:
    """Read an archived Agent payload, the directory holding its ``agent.json``."""
    path = source / "agent.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ArchivedAgentPayload(None, "payload_missing")
    except (OSError, ValueError):
        return ArchivedAgentPayload(None, "payload_invalid")
    version = document_version_state(raw, AGENT_FORMAT_VERSION)
    if version != "current":
        return ArchivedAgentPayload(None, f"{version}_format")
    try:
        data = load_validated_agent_json(path)
    except (AgentError, OSError):
        return ArchivedAgentPayload(None, "payload_invalid")
    return ArchivedAgentPayload(
        _agent_from_dict(
            data, data_dir=store.data_dir, default_workspace=store._default_workspace(data["id"])
        )
    )


def restore_target_problem(store: AgentStore, target_id: str) -> str | None:
    """``invalid_target_id``, ``agent_id_taken`` (an Agent or an unfinished rename
    holds it) or ``None`` when an archived Agent can return as ``target_id``."""
    if not is_valid_agent_id(target_id):
        return "invalid_target_id"
    if (
        has_id_entry(store.data_dir / "agents", target_id)
        or os.path.lexists(store._agent_dir(target_id))
        or target_id in store._pending_rename_ids()
    ):
        return "agent_id_taken"
    return None


@contextmanager
def restore_files(
    store: AgentStore,
    source: Path,
    target_id: str,
    *,
    workspace: str | None,
    root_project_id: str | None,
) -> Iterator[Agent]:
    """Move an archived Agent payload to ``agents/<target_id>`` for the caller's commit.

    Once moved, its ``agent.json`` is rewritten to the target id, the Workspace
    and the root; unknown fields stay. ``workspace=None`` keeps the archived
    Workspace, moved along when it lies inside the Agent's directory. If the
    rewrite or the body raises, ``agent.json`` gets its archived bytes back and
    the tree moves back into the payload, so a failed restore leaves the payload
    as it was.
    """
    with store._snapshot_barrier.compound_mutation(), store._change():
        _validate_agent_id(target_id)
        if restore_target_problem(store, target_id) is not None:
            raise AgentAlreadyExistsError(f"Agent already exists: {target_id}")
        agents_dir = store.data_dir / "agents"
        home = store._agent_dir(target_id)
        data = load_validated_agent_json(source / "agent.json")
        archived_id = str(data["id"])
        agent = _agent_from_dict(
            data,
            data_dir=store.data_dir,
            default_workspace=store._default_workspace(archived_id),
        )
        restored_workspace = (
            _rebase_path_with_tree(agent.workspace, store._agent_dir(archived_id), home)
            if workspace is None
            else _resolve_workspace(workspace, data_dir=store.data_dir)
        )
        restored = replace(
            agent,
            id=target_id,
            workspace=str(restored_workspace),
            root_project_id=root_project_id,
            updated_at=_utc_now(),
        )
        try:
            archived_document = (source / "agent.json").read_bytes()
            agents_dir.mkdir(parents=True, exist_ok=True)
            move_tree(source, home)
        except OSError as exc:
            raise AgentError(f"Agent restore failed: {exc}") from exc
        try:
            try:
                write_json_document(
                    home / "agent.json",
                    _agent_document(
                        restored,
                        workspace=_workspace_for_storage(
                            restored.workspace, data_dir=store.data_dir
                        ),
                    ),
                    AGENT_FORMAT,
                )
            except JsonDocumentWriteError as error:
                raise AgentError(str(error)) from error
            store._seed_workspace(Path(restored.workspace))
            yield _apply_defaults(restored, store._agent_defaults())
        except BaseException as exc:
            _return_to_payload(home / "agent.json", archived_document)
            _move_back(home, source, exc, "Agent restore failed")
            raise


def restore_delegation_grants(
    store: AgentStore, target_id: str, grants: Sequence[Grant]
) -> tuple[str, ...]:
    """Re-add ``target_id`` to the delegation lists that held the archived id.

    Each grant goes back at its recorded position when its holder still exists
    and does not list the target yet. Best effort; returns the changed holders.
    """
    changed: builtins.list[str] = []
    with store._change():
        for grant in grants:
            holder_id = grant.get("agent_id")
            index = grant.get("index")
            if not isinstance(holder_id, str) or holder_id == target_id:
                continue
            try:
                holder_path = store._stored_agent_path(holder_id)
                if holder_path is None:
                    continue
                holder = store._read_agent_config(holder_path)
                tools = deepcopy(holder.tools)
                subagent = tools.get("subagent")
                if not isinstance(subagent, dict):
                    continue
                allowed = builtins.list(subagent.get("allowed_agents") or [])
                if target_id in allowed:
                    continue
                position = index if isinstance(index, int) and index >= 0 else len(allowed)
                allowed.insert(min(position, len(allowed)), target_id)
                subagent["allowed_agents"] = allowed
                store._write_agent(replace(holder, tools=tools, updated_at=_utc_now()))
            except (AgentError, OSError) as error:
                _LOGGER.warning(
                    "Could not restore a delegation grant (agent=%s policy_agent=%s): %s",
                    target_id,
                    holder_id,
                    error,
                )
                continue
            changed.append(holder_id)
    return tuple(changed)


def place_in_roster(store: AgentStore, agent_id: str, index: int | None) -> None:
    """Put a restored Agent at its archived roster position; best effort."""
    with store._change():
        try:
            store.list_with_order()
            order = store._load_agent_order()
            if order is None or index is None:
                return
            agent_ids = [item for item in order.agent_ids if item != agent_id]
            agent_ids.insert(min(max(index, 0), len(agent_ids)), agent_id)
            if tuple(agent_ids) == order.agent_ids:
                return
            store._write_agent_order(
                _AgentOrderDocument(agent_ids=tuple(agent_ids), revision=order.revision + 1)
            )
        except (AgentError, OSError) as error:
            _LOGGER.warning(
                "Could not restore an Agent's roster position (agent=%s): %s", agent_id, error
            )
