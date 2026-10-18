"""Identity Agent enumeration, roster revisions and order persistence."""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import TYPE_CHECKING

from core.agents._config import (
    AGENT_ORDER_FORMAT,
    AGENT_ORDER_SHAPE,
    _agent_order_document,
    _apply_agent_order,
    _apply_defaults,
    validate_agent_order_data,
)
from core.agents._types import (
    BUILTIN_AGENT_IDS,
    Agent,
    AgentError,
    AgentListResult,
    AgentOrderConflictError,
    InvalidAgentOrderError,
    _AgentOrderDocument,
)
from core.config_validation import JsonConfigValidationError, load_validated_json_file
from core.json_documents import (
    JsonDocumentWriteError,
    document_change,
    strip_unknown_fields,
    write_json_document,
)
from core.settings import is_valid_agent_id
from core.utils.file_status import exists_strict
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.agents.agents import AgentStore


_LOGGER = get_logger("agents")


class AgentRoster:
    def __init__(self, owner: AgentStore) -> None:
        self._owner = owner

    def list(self) -> list[Agent]:
        """Return valid persisted Agents in the canonical roster order.

        The roster holds the user's Agents; built-in Agents are never part of it.
        """
        return list(self.list_with_order().agents)

    def list_with_builtins(self) -> builtins.list[Agent]:
        """Return the roster followed by the built-in Agents that are available."""
        available = (self._owner.builtin_agent(builtin) for builtin in BUILTIN_AGENT_IDS)
        return [*self.list(), *(agent for agent in available if agent is not None)]

    def list_with_order(self) -> AgentListResult:
        """Return the canonical roster plus its conflict-detection revision.

        A missing order document preserves the historical id order and is
        materialized on the first non-empty read. Stale ids are discarded and
        newly discovered valid Agents append at the end. A malformed order file
        never hides Agents: the roster falls back to id order and the invalid
        file remains available for ``doctor config`` diagnostics.
        """
        return self._owner._repairing_read(self._list_with_order)

    def _list_with_order(self) -> AgentListResult:
        """:meth:`list_with_order` under the store lock."""
        agents_dir = self._owner._data_dir / "agents"
        if not agents_dir.exists():
            return AgentListResult(agents=(), order_revision=0)

        defaults = self._owner._agent_defaults()
        try:
            agent_paths = sorted(agents_dir.glob("*/agent.json"))
        except OSError as error:
            _LOGGER.warning("Could not scan Agent configs in %s: %s", agents_dir, error)
            agent_paths = []

        loaded: list[tuple[Path, Agent]] = []
        for agent_path in agent_paths:
            try:
                agent = self._owner._load_seeded_agent(agent_path)
            except (AgentError, OSError) as error:
                _LOGGER.warning("Skipping invalid Agent config %s: %s", agent_path, error)
                continue
            if agent.builtin is None:
                loaded.append((agent_path, agent))
        # One Session read verifies every current pointer of the roster.
        live = self._owner._live_current_session_agent_ids([agent for _path, agent in loaded])
        agents: list[Agent] = []
        for agent_path, raw_agent in loaded:
            try:
                if raw_agent.current_session_id and raw_agent.id not in live:
                    raw_agent = self._owner._clear_current_session(raw_agent)
                agents.append(_apply_defaults(raw_agent, defaults))
            except (AgentError, OSError) as error:
                _LOGGER.warning("Skipping invalid Agent config %s: %s", agent_path, error)

        order = self._load_agent_order()
        ordered_agents = _apply_agent_order(agents, order)
        if not agents and order is None:
            return AgentListResult(
                agents=(),
                order_revision=0,
            )

        effective_ids = tuple(agent.id for agent in ordered_agents)
        order_path = self._owner._agent_order_path()
        try:
            order_is_invalid = order is None and exists_strict(order_path)
        except OSError:
            order_is_invalid = True  # an order that cannot be checked is never replaced
        if order is None and not order_is_invalid:
            materialized = _AgentOrderDocument(agent_ids=effective_ids, revision=1)
            try:
                self._write_agent_order(materialized)
            except OSError as error:
                _LOGGER.warning("Could not persist Identity Agent order: %s", error)
            else:
                order = materialized
        elif order is not None and order.agent_ids != effective_ids:
            reconciled = _AgentOrderDocument(
                agent_ids=effective_ids,
                revision=order.revision + 1,
            )
            try:
                self._write_agent_order(reconciled)
            except OSError as error:
                _LOGGER.warning("Could not reconcile Identity Agent order: %s", error)
            else:
                order = reconciled

        return AgentListResult(
            agents=tuple(ordered_agents),
            order_revision=order.revision if order is not None else 0,
        )

    def reorder(
        self,
        agent_ids: builtins.list[str],
        *,
        expected_revision: int,
    ) -> AgentListResult:
        """Atomically replace the canonical order when roster and revision match."""
        with self._owner._change():
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise InvalidAgentOrderError("expected_revision must be a non-negative integer")
            if expected_revision < 0:
                raise InvalidAgentOrderError("expected_revision must be a non-negative integer")
            if not isinstance(agent_ids, list) or not all(
                isinstance(agent_id, str) for agent_id in agent_ids
            ):
                raise InvalidAgentOrderError("agent_ids must be a list of strings")
            if any(not is_valid_agent_id(agent_id) for agent_id in agent_ids):
                raise InvalidAgentOrderError("agent_ids must contain only valid Agent ids")
            if len(agent_ids) != len(set(agent_ids)):
                raise InvalidAgentOrderError("agent_ids must not contain duplicates")

            current = self.list_with_order()
            current_ids = [agent.id for agent in current.agents]
            if len(agent_ids) != len(current_ids) or set(agent_ids) != set(current_ids):
                raise AgentOrderConflictError(
                    "Identity Agent roster changed; reload it before reordering",
                    current_revision=current.order_revision,
                )

            order_needs_repair = self._load_agent_order() is None
            if agent_ids == current_ids and not order_needs_repair:
                return current
            if expected_revision != current.order_revision:
                raise AgentOrderConflictError(
                    "Identity Agent order changed; reload it before reordering",
                    current_revision=current.order_revision,
                )

            next_order = _AgentOrderDocument(
                agent_ids=tuple(agent_ids),
                revision=current.order_revision + 1,
            )
            self._write_agent_order(next_order)
            agents_by_id = {agent.id: agent for agent in current.agents}
            return AgentListResult(
                agents=tuple(agents_by_id[agent_id] for agent_id in agent_ids),
                order_revision=next_order.revision,
                order_changed=True,
            )

    def _load_agent_order(self) -> _AgentOrderDocument | None:
        order_path = self._owner._agent_order_path()
        try:
            data = load_validated_json_file(
                order_path,
                validate_agent_order_data,
                missing_ok=True,
                missing_default=None,
            )
        except JsonConfigValidationError as error:
            message = str(error)
            if message != self._owner._reported_order_error:
                _LOGGER.warning("Ignoring invalid Identity Agent order: %s", message)
                self._owner._reported_order_error = message
            return None

        self._owner._reported_order_error = None
        if data is None:
            return None
        data = strip_unknown_fields(data, AGENT_ORDER_SHAPE)
        return _AgentOrderDocument(
            agent_ids=tuple(data["agent_ids"]),
            revision=data["revision"],
        )

    def _write_agent_order(self, order: _AgentOrderDocument) -> None:
        """Write the roster order; it never waits under the lock (see ``_repairing_read``)."""
        order_path = self._owner._agent_order_path()
        with self._owner._write_lock, document_change(order_path, wait=False):
            try:
                write_json_document(order_path, _agent_order_document(order), AGENT_ORDER_FORMAT)
            except JsonDocumentWriteError as error:
                raise AgentError(str(error)) from error
        self._owner._reported_order_error = None
