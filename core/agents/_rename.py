"""Durable Agent rename coordination, rollback and startup recovery."""

from __future__ import annotations

import builtins
import uuid
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from core.agents import _delegation
from core.agents import _workspace as workspace_ops
from core.agents._config import (
    AGENT_RENAME_FORMAT,
    AGENT_RENAME_SHAPE,
    _agent_from_dict,
    _agent_rename_document,
    _agent_rename_from_data,
    _apply_defaults,
    _replace_list_item_once,
    _validate_agent_id,
    _validate_new_agent_id,
    load_validated_agent_json,
    validate_agent_rename_data,
)
from core.agents._types import (
    LIBRARIAN_AGENT_ID,
    SKILL_AGENT_ID_KEY,
    AgentAlreadyExistsError,
    AgentError,
    AgentReferencedError,
    AgentRename,
    AgentRenameResult,
    BuiltinAgentError,
    _AgentOrderDocument,
)
from core.agents._workspace import _paths_are_same_location, _rebase_path_with_tree
from core.config_validation import JsonConfigValidationError, load_validated_json_file
from core.json_documents import JsonDocumentWriteError, strip_unknown_fields, write_json_document
from core.utils.file_status import exists_strict
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_timestamp

if TYPE_CHECKING:
    from core.agents.agents import AgentStore


_LOGGER = get_logger("agents")


class AgentRenames:
    def __init__(self, owner: AgentStore) -> None:
        self._owner = owner

    def rename(
        self, agent_id: str, new_agent_id: str, *, external_references: Iterable[str] = ()
    ) -> AgentRenameResult:
        """Start renaming one Identity Agent and apply the Agent-owned half.

        Sessions, prompts, private Skills, the default Workspace, and every other
        Agent-owned file live below the same directory, so moving that directory
        preserves the whole identity. A Workspace anywhere inside the tree is
        rebased to the same relative location; an external Workspace is unchanged.
        Live and archived Session addresses, the Sub-Agent parent links to them,
        the roster order and delegation allow-lists move to the new id with it;
        only archived Sessions that an archived Agent of the old id holds or that
        an Extension manages stay behind.

        The new id must be unused. No Agent, live Session, archived Session that
        a rename moves or ``external_references`` entry (the references other
        owners hold to ``new_agent_id``) may name it; references and archive
        entries holding such Sessions (``archive:<entry id>``) raise
        :class:`AgentReferencedError` naming each of them. Reverting a rename
        moves what names the new id back to the old one, so it would take along a
        reference that named the new id before the rename. A delegation allow-list
        entry that names the unused id grants nothing - a leftover of a deleted
        Agent, or a grant made before any Agent had the id - so instead of
        refusing, the rename removes it before it starts: the renamed Agent does
        not inherit it, and a revert cannot move it to the old id.

        ``agents/rename-pending.json`` records the rename before its first change
        and every step converges when repeated, so a rename interrupted at any
        point completes on the next start (:meth:`recover_rename`). The record
        stays pending after a successful return: the caller retargets the
        references other domains hold and then calls :meth:`finish_rename`, or
        :meth:`revert_rename` when that fails. A failure here reverts the
        Agent-owned half and removes the record. While a rename is pending, no
        other rename starts and neither id can be created.
        """
        with self._owner._snapshot_barrier.compound_mutation(), self._owner._change():
            _validate_agent_id(agent_id)
            _validate_new_agent_id(new_agent_id)
            if agent_id == new_agent_id:
                raise AgentError("new agent id must differ from the current id")
            if exists_strict(self._owner._renames._rename_record_path()):
                raise AgentError(
                    "An earlier Agent rename is still pending; restart vBot to finish it"
                )

            source_dir = self._owner._agent_dir(agent_id)
            destination_dir = self._owner._agent_dir(new_agent_id)
            agent_path = self._owner._require_agent_path(agent_id)
            source = self._owner._read_agent_config(agent_path)
            if source.builtin is not None:
                raise BuiltinAgentError(
                    f"The {source.name} is built into vBot and cannot be renamed"
                )
            case_only = _paths_are_same_location(source_dir, destination_dir)
            if exists_strict(destination_dir) and not case_only:
                raise AgentAlreadyExistsError(f"Agent already exists: {new_agent_id}")
            sessions = self._owner._session_manager()
            # Rolling back renames the new id's Sessions back, so it must own none yet.
            if sessions.list_addresses(None, agent_id=new_agent_id):
                raise AgentAlreadyExistsError(f"Sessions already exist for Agent: {new_agent_id}")
            # Archived Sessions an earlier Agent of the new id left move with the
            # renamed Agent's and back with a revert, so they refuse like references.
            references = (
                *external_references,
                *(
                    f"archive:{entry_id}"
                    for entry_id in sessions.archive_ledger.identity_agent_entry_ids(new_agent_id)
                ),
            )
            if references:
                raise AgentReferencedError(new_agent_id, references)
            # Before the record, outside every rename direction: an interruption
            # leaves only fewer leftovers, and no revert brings them back.
            leftover_policy_agent_ids = self._owner._remove_from_allow_lists(new_agent_id)
            _delegation.revoke_future_grants(self._owner, new_agent_id)
            # Repairs a dangling current-Session pointer before the Sessions move.
            self._owner._load_verified_agent(agent_path)
            session_ids = tuple(
                address.session_id for address in sessions.list_addresses(None, agent_id=agent_id)
            )
            rename = AgentRename(
                source_id=agent_id,
                target_id=new_agent_id,
                staging_name=f".{agent_id}.rename-{uuid.uuid4().hex}.tmp" if case_only else None,
            )
            self._write_rename_record(rename)
            try:
                applied = self._apply_rename(rename)
                renamed = self._owner._read_agent_config(
                    self._owner._require_agent_path(new_agent_id)
                )
            except Exception as error:
                self._roll_back_rename(rename, error)
                raise
            return AgentRenameResult(
                rename=rename,
                agent=_apply_defaults(renamed, self._owner._agent_defaults()),
                session_ids=session_ids,
                policy_agent_ids=tuple(
                    sorted({*leftover_policy_agent_ids, *applied.policy_agent_ids})
                ),
                session_link_count=applied.session_link_count,
            )

    def revert_rename(self, rename: AgentRename) -> AgentRename:
        """Reverse the pending ``rename`` and apply its Agent-owned half back.

        Returns the reversed record, which stays pending until the caller has
        retargeted the other domains' references back and calls
        :meth:`finish_rename` with it. On failure the reversed record stays, and
        the next start completes the rollback.
        """
        with self._owner._snapshot_barrier.compound_mutation(), self._owner._change():
            self._require_pending_rename(rename)
            reverse = rename.reversed()
            self._write_rename_record(reverse)
            self._apply_rename(reverse)
            return reverse

    def finish_rename(self, rename: AgentRename) -> None:
        """Remove the pending ``rename`` record once every reference is retargeted."""
        with self._owner._change():
            self._require_pending_rename(rename)
            self._owner._renames._rename_record_path().unlink(missing_ok=True)

    def recover_rename(self) -> AgentRename | None:
        """Bring an interrupted rename's Agent-owned half to one consistent end.

        Runs at startup before the roster is read. The pending record is applied
        again in its direction; when that fails, it is reversed and applied back
        instead. Returns the record whose Agent-owned half is now complete: the
        caller retargets the other domains' references in its direction and calls
        :meth:`finish_rename`. Returns ``None`` when no rename is pending, and when
        the record cannot be read or neither direction completes; that record
        stays for the next start and the failure is logged.
        """
        with self._owner._snapshot_barrier.compound_mutation(), self._owner._change():
            try:
                rename = self._load_rename_record()
            except (AgentError, OSError) as error:
                _LOGGER.error("Pending Agent rename cannot be read and stays in place: %s", error)
                return None
            if rename is None:
                return None
            try:
                self._apply_rename(rename)
                return rename
            except Exception as error:
                _LOGGER.warning(
                    "Agent rename could not be completed after restart; reverting it "
                    "(agent=%s new_agent=%s): %s",
                    rename.old_id,
                    rename.new_id,
                    error,
                )
            reverse = rename.reversed()
            try:
                self._write_rename_record(reverse)
                self._apply_rename(reverse)
            except Exception as error:
                _LOGGER.error(
                    "Agent rename recovery failed; the next start retries it "
                    "(agent=%s new_agent=%s)",
                    rename.old_id,
                    rename.new_id,
                    exc_info=(type(error), error, error.__traceback__),
                )
                return None
            return reverse

    def _roll_back_rename(self, rename: AgentRename, error: Exception) -> None:
        """Revert a rename whose Agent-owned half failed; the next start finishes a failure."""
        reverse = rename.reversed()
        try:
            self._write_rename_record(reverse)
            self._apply_rename(reverse)
            self._owner._renames._rename_record_path().unlink(missing_ok=True)
        except Exception as rollback_error:
            _LOGGER.error(
                "Agent rename rollback incomplete; the next start finishes it "
                "(agent=%s new_agent=%s error=%s)",
                rename.old_id,
                rename.new_id,
                error,
                exc_info=(type(rollback_error), rollback_error, rollback_error.__traceback__),
            )

    def _apply_rename(self, rename: AgentRename) -> _AppliedRename:
        """Move every Agent-owned trace of ``source_id`` to ``target_id``.

        Each step selects only what still names ``source_id`` or converges on its
        target state, so repeating the whole sequence after an interruption at any
        point finishes it.
        """
        _delegation.retire(self._owner, rename.source_id)
        sessions = self._owner._session_manager()
        sessions.retarget_identity_agent_sessions(rename.source_id, rename.target_id)
        link_count = len(
            sessions.retarget_identity_agent_references(rename.source_id, rename.target_id)
        )
        # The Librarian Sessions that maintain the Agent's Skills keep doing so.
        sessions.retarget_metadata_value(
            LIBRARIAN_AGENT_ID, SKILL_AGENT_ID_KEY, rename.source_id, rename.target_id
        )
        if workspace_ops._move_renamed_tree(self._owner._data_dir / "agents", rename):
            self._write_renamed_config(rename)
            self._rename_order_entry(rename)
        else:
            _LOGGER.warning(
                "Agent files missing during rename; only references were renamed "
                "(agent=%s new_agent=%s)",
                rename.old_id,
                rename.new_id,
            )
        return _AppliedRename(
            policy_agent_ids=self._retarget_allowed_agents(rename.source_id, rename.target_id),
            session_link_count=link_count,
        )

    def _write_renamed_config(self, rename: AgentRename) -> None:
        """Give the moved config the target id and rebase an in-tree Workspace."""
        agent_path = self._owner._agent_path(rename.target_id)
        data = load_validated_agent_json(agent_path)
        if data["id"] not in (rename.source_id, rename.target_id):
            raise AgentError(f"{agent_path}: Agent id {data['id']!r} does not match the rename")
        agent = _agent_from_dict(
            data,
            data_dir=self._owner._data_dir,
            default_workspace=self._owner._default_workspace(rename.target_id),
        )
        workspace = str(
            _rebase_path_with_tree(
                agent.workspace,
                self._owner._agent_dir(rename.source_id),
                self._owner._agent_dir(rename.target_id),
            )
        )
        if agent.id == rename.target_id and workspace == agent.workspace:
            return
        self._owner._write_agent(
            replace(agent, id=rename.target_id, workspace=workspace, updated_at=utc_now_timestamp())
        )

    def _rename_order_entry(self, rename: AgentRename) -> None:
        """Keep the renamed Agent at its roster position."""
        order = self._owner._roster._load_agent_order()
        if order is None or rename.source_id not in order.agent_ids:
            return
        agent_ids = _replace_list_item_once(
            builtins.list(order.agent_ids), rename.source_id, rename.target_id
        )
        self._owner._roster._write_agent_order(
            _AgentOrderDocument(agent_ids=tuple(agent_ids), revision=order.revision + 1)
        )

    def _retarget_allowed_agents(self, old_agent_id: str, new_agent_id: str) -> tuple[str, ...]:
        """Retarget a bare Identity Agent id in every delegation allow-list.

        Returns the ids of the Agents whose configs changed.
        """
        changed: builtins.list[str] = []
        for agent, allowed_agents in self._owner._allow_lists_naming(old_agent_id):
            tools = deepcopy(agent.tools)
            tools["subagent"]["allowed_agents"] = _replace_list_item_once(
                allowed_agents, old_agent_id, new_agent_id
            )
            updated = _delegation.retarget(agent, old_agent_id, new_agent_id, self._owner)
            self._owner._write_agent(replace(updated, tools=tools, updated_at=utc_now_timestamp()))
            changed.append(agent.id)
        return tuple(changed)

    def _load_rename_record(self) -> AgentRename | None:
        try:
            data = load_validated_json_file(
                self._owner._renames._rename_record_path(),
                validate_agent_rename_data,
                missing_ok=True,
                missing_default=None,
            )
        except JsonConfigValidationError as error:
            raise AgentError(str(error)) from error
        if data is None:
            return None
        return _agent_rename_from_data(strip_unknown_fields(data, AGENT_RENAME_SHAPE))

    def _write_rename_record(self, rename: AgentRename) -> None:
        try:
            write_json_document(
                self._owner._renames._rename_record_path(),
                _agent_rename_document(rename),
                AGENT_RENAME_FORMAT,
            )
        except JsonDocumentWriteError as error:
            raise AgentError(str(error)) from error

    def _require_pending_rename(self, rename: AgentRename) -> None:
        if self._load_rename_record() != rename:
            raise AgentError(
                f"No such pending Agent rename: {rename.source_id} -> {rename.target_id}"
            )

    def _pending_rename_ids(self) -> frozenset[str]:
        """Both ids of a readable pending rename, which no new Agent may take."""
        try:
            rename = self._load_rename_record()
        except AgentError, OSError:
            return frozenset()
        if rename is None:
            return frozenset()
        return frozenset((rename.source_id, rename.target_id))

    def _rename_record_path(self) -> Path:
        return self._owner._data_dir / "agents" / _AGENT_RENAME_FILE_NAME


_AGENT_RENAME_FILE_NAME = "rename-pending.json"


@dataclass(frozen=True)
class _AppliedRename:
    """What the Agent-owned half of one rename direction changed besides the tree."""

    policy_agent_ids: tuple[str, ...]
    session_link_count: int
