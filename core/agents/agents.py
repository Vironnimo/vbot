"""Identity Agent catalog, Session coordination and compensated mutations."""

from __future__ import annotations

import builtins
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any

from core.agents import _archive, _delegation
from core.agents import _workspace as workspace_ops
from core.agents._builtins import BuiltinAgents
from core.agents._config import (
    AGENT_FORMAT,
    DEFAULT_ALLOWED_ITEMS,
    DEFAULT_FALLBACK_MODELS,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING_EFFORT,
    _agent_document,
    _agent_from_dict,
    _apply_defaults,
    _validate_agent_id,
    _validate_new_agent_id,
    _validated_agent_data,
    load_validated_agent_json,
    normalize_agent_tools,
    validate_agent_data,
    validate_agent_file,
    validate_agent_order_data,
    validate_agent_order_file,
    validate_agent_rename_data,
    validate_agent_rename_file,
)
from core.agents._mutations import AgentMutations
from core.agents._rename import AgentRenames
from core.agents._roster import AgentRoster
from core.agents._types import (
    BUILTIN_AGENT_IDS,
    BUILTIN_AGENT_NAMES,
    DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED,
    DEFAULT_LIBRARIAN_ENABLED,
    LIBRARIAN_AGENT_ID,
    LIBRARIAN_AGENT_NAME,
    LIBRARIAN_TOOLS,
    LIVE_BACKEND_AGENT_ID,
    LIVE_BACKEND_BUILTIN,
    LIVE_VOICE_AGENT_ID,
    LIVE_VOICE_BUILTIN,
    SKILL_AGENT_CREATED_AT_KEY,
    SKILL_AGENT_ID_KEY,
    Agent,
    AgentAlreadyExistsError,
    AgentError,
    AgentListResult,
    AgentNotFoundError,
    AgentOrderConflictError,
    AgentReferencedError,
    AgentRename,
    AgentRenameResult,
    AgentUpdateResult,
    ArchivedAgent,
    ArchivedAgentPayload,
    BuiltinAgent,
    BuiltinAgentError,
    BuiltinAgentProblem,
    InvalidAgentIdError,
    InvalidAgentOrderError,
    LibrarianProblem,
    is_builtin_agent,
    is_librarian,
    is_live_agent,
    librarian_problem_message,
    skill_subject_id,
)
from core.agents._workspace import (
    WORKSPACE_IDENTITY_FILES,
    WORKSPACE_TEMPLATE_FILES,
    _is_missing_workspace,
    _workspace_for_storage,
    default_workspace_dir,
)
from core.database import MemberFrozenError, SnapshotBarrier
from core.json_documents import JsonDocumentWriteError, document_change, write_json_document
from core.memory import DEFAULT_MEMORY_PROMPT_MODE, MemoryPromptMode
from core.permissions import PermissionRules
from core.sessions import ChatSessionManager, SessionAddress
from core.settings import AgentDefaults, is_valid_agent_id
from core.tools.availability import ToolAccess
from core.utils.ids import has_id_entry
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_timestamp

__all__ = [
    "Agent",
    "AgentAlreadyExistsError",
    "AgentError",
    "AgentListResult",
    "AgentNotFoundError",
    "AgentOrderConflictError",
    "AgentReferencedError",
    "AgentRename",
    "AgentRenameResult",
    "AgentStore",
    "AgentUpdateResult",
    "ArchivedAgent",
    "ArchivedAgentPayload",
    "BUILTIN_AGENT_IDS",
    "BUILTIN_AGENT_NAMES",
    "BuiltinAgentError",
    "BuiltinAgentProblem",
    "DEFAULT_ALLOWED_ITEMS",
    "DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED",
    "DEFAULT_FALLBACK_MODELS",
    "DEFAULT_MODEL",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_THINKING_EFFORT",
    "InvalidAgentIdError",
    "InvalidAgentOrderError",
    "LIBRARIAN_AGENT_ID",
    "LIBRARIAN_AGENT_NAME",
    "LIBRARIAN_TOOLS",
    "LIVE_BACKEND_AGENT_ID",
    "LIVE_BACKEND_BUILTIN",
    "LIVE_VOICE_AGENT_ID",
    "LIVE_VOICE_BUILTIN",
    "LibrarianProblem",
    "SKILL_AGENT_CREATED_AT_KEY",
    "SKILL_AGENT_ID_KEY",
    "WORKSPACE_IDENTITY_FILES",
    "WORKSPACE_TEMPLATE_FILES",
    "default_workspace_dir",
    "is_builtin_agent",
    "is_librarian",
    "is_live_agent",
    "librarian_problem_message",
    "load_validated_agent_json",
    "normalize_agent_tools",
    "skill_subject_id",
    "validate_agent_data",
    "validate_agent_file",
    "validate_agent_order_data",
    "validate_agent_order_file",
    "validate_agent_rename_data",
    "validate_agent_rename_file",
]


_AGENT_ORDER_FILE_NAME = "order.json"


_LOGGER = get_logger("agents")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_DEFAULT_TEMPLATE_DIR = _PROJECT_ROOT / "resources" / "workspace-templates"


class AgentStore:
    """CRUD store for persisted agent configs and workspaces."""

    def __init__(
        self,
        data_dir: str | Path,
        template_dir: str | Path | None = None,
        defaults_provider: Callable[[], dict[str, Any]] | None = None,
        sessions: ChatSessionManager | None = None,
        snapshot_barrier: SnapshotBarrier | None = None,
        id_references: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
        # What other owners keep under an Identity Agent id that a new Agent of
        # that id would inherit: Agent Secret grants an archived Agent keeps.
        self._id_references = id_references
        self._template_dir = (
            Path(template_dir) if template_dir is not None else _DEFAULT_TEMPLATE_DIR
        )
        self._defaults_provider = defaults_provider
        self._sessions = sessions
        self._owns_sessions = sessions is None
        self._reported_order_error: str | None = None
        # RPC workers can overlap reads that repair state and lifecycle updates.
        # Hold this across each complete read-modify-write, including Session
        # repair and roster revisions, rather than just the final file replace.
        # Event Loop code takes it for reads, so no thread may wait for a data
        # snapshot while holding it: a mutation admits its document change before
        # the lock (``_change``), and a read that repairs retries through that
        # admission when a snapshot is being taken (``_repairing_read``).
        self._write_lock = RLock()
        # Rename and delete change Sessions and ``agent.json`` together, and create
        # writes the Agent tree with its ``agent.json``; a data snapshot of the
        # Runtime's barrier never copies between the steps of one of them.
        self._snapshot_barrier = (
            snapshot_barrier if snapshot_barrier is not None else SnapshotBarrier()
        )
        self._renames = AgentRenames(self)
        self._roster = AgentRoster(self)
        self._builtins = BuiltinAgents(self)
        self._mutations = AgentMutations(self)

    @contextmanager
    def lifecycle_guard(self) -> Iterator[None]:
        """Keep an Agent-owned filesystem mutation together with its scope lookup.

        Other domain owners use this guard across lookup, writes and invalidation
        so rename/archive cannot detach an already-resolved private home. Callers
        must run blocking guarded work in a worker without callbacks to the loop.
        """
        with self._change():
            yield

    def close(self) -> None:
        with self._write_lock:
            if self._owns_sessions and self._sessions is not None:
                self._sessions.close()
                self._sessions = None
                self._owns_sessions = False

    @property
    def data_dir(self) -> Path:
        """Root directory containing agents, workspaces, and archives."""
        return self._data_dir

    def create(
        self,
        agent_id: str,
        name: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        fallback_models: list[str] | None = None,
        workspace: str | Path | None = None,
        temperature: float | None = DEFAULT_TEMPERATURE,
        top_p: float | None = None,
        thinking_effort: str | None = DEFAULT_THINKING_EFFORT,
        memory_prompt_mode: MemoryPromptMode = DEFAULT_MEMORY_PROMPT_MODE,
        tool_access: ToolAccess | Mapping[str, Any] | None = None,
        allowed_skills: list[str] | None = None,
        excluded_skills: list[str] | None = None,
        tools: Mapping[str, Any] | None = None,
        custom_system_prompt_enabled: bool = DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED,
        compaction_policy: dict[str, Any] | None = None,
        tool_loading: Mapping[str, Any] | None = None,
        permission_rules: PermissionRules | Mapping[str, Any] | None = None,
        librarian_enabled: bool = DEFAULT_LIBRARIAN_ENABLED,
        description: str | None = None,
    ) -> Agent:
        """Create and persist a new Agent and its Workspace; it has no Session yet.

        The id must be free and may not be one vBot reserves (:class:`InvalidAgentIdError`);
        an id other owners still reference is refused with :class:`AgentReferencedError`
        (:meth:`id_references`).
        """
        _validate_new_agent_id(agent_id)
        references = self.id_references(agent_id)
        if references:
            raise AgentReferencedError(agent_id, references, creating=True)
        return self._mutations._create(
            agent_id,
            name,
            builtin=None,
            model=model,
            fallback_models=fallback_models,
            workspace=workspace,
            temperature=temperature,
            top_p=top_p,
            thinking_effort=thinking_effort,
            memory_prompt_mode=memory_prompt_mode,
            tool_access=tool_access,
            allowed_skills=allowed_skills,
            excluded_skills=excluded_skills,
            tools=tools,
            custom_system_prompt_enabled=custom_system_prompt_enabled,
            compaction_policy=compaction_policy,
            tool_loading=tool_loading,
            permission_rules=permission_rules,
            librarian_enabled=librarian_enabled,
            description=description,
        )

    def id_references(self, agent_id: str) -> tuple[str, ...]:
        """What a new Agent named ``agent_id`` would inherit, as ``<kind>:<id>`` labels.

        Agent Secret grants (``secret:<name>``) that still name the id, usually
        kept by an archived Agent of that id for its restore; empty when the id
        is clear.
        """
        if self._id_references is None:
            return ()
        return tuple(self._id_references(agent_id))

    def get(self, agent_id: str) -> Agent:
        """Load an agent from disk by its exact id, with a verified current-Session pointer."""

        def read() -> Agent:
            agent_path = self._require_agent_path(agent_id)
            raw_agent = self._load_verified_agent(agent_path)
            return _apply_defaults(raw_agent, self._agent_defaults())

        return self._repairing_read(read)

    async def get_async(self, agent_id: str) -> Agent:
        """Event-Loop-safe :meth:`get`, run as one unit on the Session database's pool.

        The read seeds the Workspace and verifies, and may repair, the
        current-Session pointer under the store lock, so it runs where Session
        work runs. A closed Session database raises
        :class:`~core.database.DatabaseUnavailableError`.
        """
        sessions = self._sessions
        if sessions is None:
            # A standalone store (tests) opens its own Session service lazily.
            sessions = self._session_manager()
        return await sessions.run_async(self.get, agent_id)

    def get_raw(self, agent_id: str) -> Agent:
        """Load an agent with its **un-baked** persisted values (no defaults applied).

        Same config load as :meth:`get` (including workspace seeding) but **without**
        the ``defaults.agent`` injection, so the returned Agent carries the raw
        ``model``/``fallback_models`` ("" / [] when unset) and raw
        ``temperature``/``thinking_effort`` (``None`` when unset). This is the
        provenance seam the resolver's identity ``effective_config`` reads to tell an
        own persisted value from a baked global default; ``get``/``list``/``update``
        keep baking for every other consumer. Provenance readers never use the
        current-Session pointer, so it is returned as stored, unverified; read it
        through :meth:`get`.
        """

        def read() -> Agent:
            return self._load_seeded_agent(self._require_agent_path(agent_id))

        return self._repairing_read(read)

    def exists(self, agent_id: str) -> bool:
        """Return whether a valid identity Agent with exactly this id can be loaded.

        The probe never raises. Invalid ids, missing files, case variants of a
        stored id, malformed configs, and configs whose persisted id disagrees with
        their directory all yield ``False`` so a broken Agent is never treated as
        an available target.
        """
        return self.find(agent_id) is not None

    def find(self, agent_id: str) -> Agent | None:
        """Return the persisted config of a valid identity Agent, or ``None``.

        The same side-effect-free, never-raising probe as :meth:`exists`: it neither
        seeds the Workspace nor verifies the current-Session pointer, and it returns
        the raw persisted values without ``defaults.agent`` applied.
        """
        with self._write_lock:
            if not is_valid_agent_id(agent_id):
                return None
            try:
                agent_path = self._stored_agent_path(agent_id)
                if agent_path is None:
                    return None
                return self._read_agent_config(agent_path)
            except AgentError, OSError:
                return None

    def list(self) -> list[Agent]:
        """Return valid persisted Agents in the canonical roster order.

        The roster holds the user's Agents; built-in Agents are never part of it.
        """
        return self._roster.list()

    def list_with_builtins(self) -> builtins.list[Agent]:
        """Return the roster followed by the built-in Agents that are available."""
        return self._roster.list_with_builtins()

    def builtin_agent(self, builtin: BuiltinAgent) -> Agent | None:
        """Return a built-in Agent with defaults applied, ``None`` while unavailable."""
        return self._builtins.builtin_agent(builtin)

    def builtin_problem(self, builtin: BuiltinAgent) -> BuiltinAgentProblem | None:
        """Why a built-in Agent is unavailable, ``None`` while it is available.

        ``missing``: no Agent has its id yet (startup creates it); ``agent_id_taken``:
        an Agent of the user or an unfinished rename holds the id;
        ``invalid_config``: its ``agent.json`` cannot be loaded. Never raises.
        """
        return self._builtins.builtin_problem(builtin)

    def librarian(self) -> Agent | None:
        """Return the built-in Librarian with defaults applied, ``None`` while unavailable."""
        return self._builtins.librarian()

    def librarian_problem(self) -> LibrarianProblem | None:
        """Why the built-in Librarian is unavailable (:meth:`builtin_problem`)."""
        return self._builtins.librarian_problem()

    def ensure_builtin_agents(self) -> None:
        """Create each built-in Agent that is missing.

        A new built-in Agent gets a new Agent's defaults, so it runs the default
        Model until the user picks one; the Agents of a Live call start with
        their default Tools. An Agent of the user that holds a reserved id and an
        ``agent.json`` that cannot be loaded stay as they are; the warning names
        the problem.
        """
        return self._builtins.ensure_builtin_agents()

    def list_with_order(self) -> AgentListResult:
        """Return the canonical roster plus its conflict-detection revision.

        A missing order document preserves the historical id order and is
        materialized on the first non-empty read. Stale ids are discarded and
        newly discovered valid Agents append at the end. A malformed order file
        never hides Agents: the roster falls back to id order and the invalid
        file remains available for ``doctor config`` diagnostics.
        """
        return self._roster.list_with_order()

    def reorder(
        self,
        agent_ids: builtins.list[str],
        *,
        expected_revision: int,
    ) -> AgentListResult:
        """Atomically replace the canonical order when roster and revision match."""
        return self._roster.reorder(agent_ids, expected_revision=expected_revision)

    def ensure_bootstrap(self) -> Agent | None:
        """Create one valid bootstrap Agent when the store has none.

        Invalid Agent directories are preserved for diagnosis. If one already
        occupies ``main``, the bootstrap Agent uses the first free ``main-N`` id.
        """
        return self._builtins.ensure_bootstrap()

    def update(self, agent_id: str, **changes: Any) -> Agent:
        """Update mutable fields for an existing agent."""
        return self.update_with_metadata(agent_id, **changes).agent

    def update_with_metadata(
        self,
        agent_id: str,
        *,
        copy_workspace_identity_files: bool = False,
        **changes: Any,
    ) -> AgentUpdateResult:
        """Update an Agent and transactionally relocate its identity files.

        The copy directive is operation input, not persisted configuration. Only
        ``SOUL.md``, ``USER.md``, and ``MEMORY.md`` can move. Sources are preserved,
        replaced destination files are backed up under the Agent's data home, and
        a failure restores every destination touched before leaving the config on
        its original Workspace.
        """
        return self._mutations.update_with_metadata(
            agent_id, copy_workspace_identity_files=copy_workspace_identity_files, **changes
        )

    def restore_update(self, previous_agent: Agent, result: AgentUpdateResult) -> None:
        """Compensate a completed update during a larger coordinated operation."""
        return self._mutations.restore_update(previous_agent, result)

    def rename(
        self, agent_id: str, new_agent_id: str, *, external_references: Iterable[str] = ()
    ) -> AgentRenameResult:
        """Start renaming one Identity Agent and apply the Agent-owned half.

        Sessions, prompts, private Skills, the default Workspace, and every other
        Agent-owned file live below the same directory, so moving that directory
        preserves the whole identity. A Workspace anywhere inside the tree is
        rebased to the same relative location; an external Workspace is unchanged.
        Live Session addresses, the Sub-Agent parent links to them, the roster
        order and delegation allow-lists move to the new id with it.

        The new id must be unused. No Agent, live Session or
        ``external_references`` entry (the references other owners hold to
        ``new_agent_id``) may name it; references raise
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
        return self._renames.rename(agent_id, new_agent_id, external_references=external_references)

    def revert_rename(self, rename: AgentRename) -> AgentRename:
        """Reverse the pending ``rename`` and apply its Agent-owned half back.

        Returns the reversed record, which stays pending until the caller has
        retargeted the other domains' references back and calls
        :meth:`finish_rename` with it. On failure the reversed record stays, and
        the next start completes the rollback.
        """
        return self._renames.revert_rename(rename)

    def finish_rename(self, rename: AgentRename) -> None:
        """Remove the pending ``rename`` record once every reference is retargeted."""
        return self._renames.finish_rename(rename)

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
        return self._renames.recover_rename()

    def _remove_from_allow_lists(
        self, agent_id: str, *, best_effort: bool = False
    ) -> tuple[str, ...]:
        """Remove a bare Identity Agent id from every delegation allow-list.

        Returns the ids of the Agents whose configs changed. An allow-list that
        named only ``agent_id`` becomes empty, which means self-delegation only,
        so no grant widens. With ``best_effort``, a config that cannot be
        written keeps its entry and the failure is logged instead of raised.
        """
        try:
            naming = builtins.list(self._allow_lists_naming(agent_id))
        except OSError as error:
            if not best_effort:
                raise
            _LOGGER.warning(
                "Could not read delegation lists to remove an Agent (agent=%s): %s",
                agent_id,
                error,
            )
            return ()
        changed: builtins.list[str] = []
        for agent, allowed_agents in naming:
            tools = deepcopy(agent.tools)
            tools["subagent"]["allowed_agents"] = [
                item for item in allowed_agents if item != agent_id
            ]
            try:
                self._write_agent(replace(agent, tools=tools, updated_at=utc_now_timestamp()))
            except (AgentError, OSError) as error:
                if not best_effort:
                    raise
                _LOGGER.warning(
                    "Could not remove an Agent from a delegation list; the entry stays "
                    "(agent=%s policy_agent=%s): %s",
                    agent_id,
                    agent.id,
                    error,
                )
                continue
            changed.append(agent.id)
        return tuple(changed)

    def _allow_lists_naming(self, agent_id: str) -> Iterator[tuple[Agent, builtins.list[str]]]:
        """Yield each Agent whose delegation allow-list names bare ``agent_id``, with that list.

        Project-qualified addresses such as ``builder@project`` name Config Agents
        and are a separate address space, so they never match. Configs are read
        side-effect free, and an invalid one is skipped as the roster skips it.
        """
        for agent_path in sorted((self._data_dir / "agents").glob("*/agent.json")):
            try:
                agent = self._read_agent_config(agent_path)
            except AgentError, OSError:
                continue
            subagent = agent.tools.get("subagent")
            if not isinstance(subagent, dict):
                continue
            allowed_agents = subagent.get("allowed_agents")
            if isinstance(allowed_agents, list) and agent_id in allowed_agents:
                yield agent, allowed_agents

    def agents_rooted_in(self, project_id: str) -> builtins.list[Agent]:
        """Return Identity Agents explicitly referencing one Project."""
        return [
            self.get_raw(agent.id) for agent in self.list() if agent.root_project_id == project_id
        ]

    def archive_files(self, agent_id: str, tree: Path) -> AbstractContextManager[ArchivedAgent]:
        """Move the Agent's directory to the payload ``tree`` for the caller's commit.

        Use as ``with store.archive_files(agent_id, tree) as archived:`` and commit
        the Sessions inside the body; a failing body moves the tree back. The
        store lock and a compound mutation of the snapshot barrier are held across
        the body. A Workspace outside the Agent's directory stays where it is.
        Product-level reference and Run admission guards belong to the caller.
        """
        return _archive.archive_files(self, agent_id, tree)

    def remove_delegation_grants(
        self, agent_id: str, record: Callable[[builtins.list[_archive.Grant]], None]
    ) -> tuple[str, ...]:
        """Remove an archived Agent's bare id from every delegation list, best effort.

        ``record`` receives each grant (holder id, immutable creation timestamp,
        and position) before any config is written. A qualified ``agent@project``
        entry names a Project's Team Agent and stays. Returns the ids of the Agents
        whose configs changed.
        """
        return _archive.remove_delegation_grants(self, agent_id, record)

    def inspect_archived(self, source: Path) -> ArchivedAgentPayload:
        """Read an archived Agent directory without changing it; report why it cannot return."""
        return _archive.inspect_archived(self, source)

    def restore_target_problem(self, agent_id: str) -> str | None:
        """Why an archived Agent cannot return as ``agent_id``: ``invalid_target_id``,
        ``agent_id_taken``, or ``None`` when it can."""
        return _archive.restore_target_problem(self, agent_id)

    def restore_files(
        self,
        source: Path,
        target_id: str,
        *,
        workspace: str | None,
        root_project_id: str | None,
    ) -> AbstractContextManager[Agent]:
        """Move an archived Agent directory back as ``target_id`` for the caller's commit.

        The archived ``agent.json`` is rewritten to the target id, Workspace and
        root first; ``workspace=None`` keeps the archived Workspace. A failing body
        moves the tree back into ``source``.
        """
        return _archive.restore_files(
            self, source, target_id, workspace=workspace, root_project_id=root_project_id
        )

    def complete_restore_files(
        self,
        target_id: str,
        *,
        source_id: str,
        workspace: str | None,
        root_project_id: str | None,
    ) -> None:
        """Complete an interrupted restore's config rewrite on its already moved tree.

        Uses the same rewrite as ``restore_files`` and preserves the current
        Session pointer while those Sessions are still archived.
        """
        _archive.complete_restore_files(
            self,
            target_id,
            source_id=source_id,
            workspace=workspace,
            root_project_id=root_project_id,
        )

    def delegation_grant_problem(self, grant: Mapping[str, Any]) -> str | None:
        """Why a recorded grant cannot return to its holder, without changing state.

        ``holder_missing``: no exact stored holder; ``holder_unproven``: missing
        or invalid creation stamps or an unreadable/invalid config;
        ``holder_replaced``: valid creation stamps differ. ``None`` identifies
        the original holder incarnation. Never raises or repairs its config.
        """
        return _archive.delegation_grant_problem(self, grant)

    def restore_delegation_grants(
        self, agent_id: str, grants: Iterable[Mapping[str, Any]]
    ) -> tuple[str, ...]:
        """Re-add ``agent_id`` to the recorded delegation lists; best effort, idempotent."""
        return _archive.restore_delegation_grants(self, agent_id, builtins.list(grants))

    def place_in_roster(self, agent_id: str, index: int | None) -> None:
        """Move a restored Agent to its recorded roster position; best effort, idempotent."""
        _archive.place_in_roster(self, agent_id, index)

    def reset_current_after_session_removed(self, agent_id: str, removed_session_id: str) -> Agent:
        """Re-point an identity agent's current session after one is gone.

        Invoked as the final step of removing a session from this home — a move
        to another agent, or a deletion. If the removed session was this agent's
        current session, the pointer lands on the most recently active
        *remaining* session (max ``last_active_at``), or is cleared when none
        remain: the Agent then opens a new Session with its next message. If the
        removed session was not the current one, the pointer is left untouched.

        Reads the stored config side-effect-free (not through :meth:`get`):
        ``get`` would clear the pointer the instant it sees it dangling at the
        just-removed id, preempting the last-active landing this method exists
        to provide.
        """
        with self._change():
            agent_path = self._require_agent_path(agent_id)
            agent = self._read_agent_config(agent_path)
            if agent.current_session_id != removed_session_id:
                return _apply_defaults(agent, self._agent_defaults())

            landing_session_id = self._session_manager().newest_session_id(agent_id) or ""
            updated_agent = replace(
                agent, current_session_id=landing_session_id, updated_at=utc_now_timestamp()
            )
            self._write_agent(updated_agent)
            return _apply_defaults(updated_agent, self._agent_defaults())

    @contextmanager
    def _change(self) -> Iterator[None]:
        """Admit a change of the Agent documents, then hold the store lock.

        In this order a change that waits for a data snapshot waits without the
        lock, which Event Loop readers take.
        """
        with document_change(self._data_dir / "agents"), self._write_lock:
            yield

    def _repairing_read[Read](self, read: Callable[[], Read]) -> Read:
        """Run a read that may repair what it finds; it never waits for a snapshot under the lock.

        A repair writes without waiting (``_write_agent``): while a data snapshot
        is being taken it raises ``MemberFrozenError`` instead, and the read gives
        the lock up, waits for the snapshot and runs again. A read that repairs
        nothing never waits, so Agent resolution goes on during a snapshot.
        """
        try:
            with self._write_lock:
                return read()
        except MemberFrozenError:
            pass
        with self._change():
            return read()

    def _agent_dir(self, agent_id: str) -> Path:
        return self._data_dir / "agents" / agent_id

    def _agent_path(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "agent.json"

    def _stored_agent_path(self, agent_id: str) -> Path | None:
        """Return ``agent.json`` of the Agent stored under exactly ``agent_id``, or ``None``.

        Ids are exact on every platform. A case-insensitive filesystem would open
        the stored ``main`` tree for ``MAIN``; that different id names no Agent.
        """
        agent_path = self._agent_path(agent_id)
        if not has_id_entry(agent_path.parent.parent, agent_id) or not agent_path.is_file():
            return None
        return agent_path

    def _require_agent_path(self, agent_id: str) -> Path:
        """Validate ``agent_id`` and return its stored config path or raise not-found."""
        _validate_agent_id(agent_id)
        agent_path = self._stored_agent_path(agent_id)
        if agent_path is None:
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        return agent_path

    def _agent_order_path(self) -> Path:
        return self._data_dir / "agents" / _AGENT_ORDER_FILE_NAME

    def default_workspace(self, agent_id: str) -> str:
        """Return an agent's default identity home as a resolved absolute path.

        ``<data_dir>/agents/<id>/workspace/`` — the location a workspace is
        seeded to at creation and the target the WebUI "set workspace to default"
        action writes. Returned in the same ``str(Path.resolve())`` form as the
        persisted ``workspace`` field, so a caller can compare the two directly
        to tell whether an agent uses a custom identity/Memory home.
        """
        return str(self._default_workspace(agent_id).resolve())

    def _default_workspace(self, agent_id: str) -> Path:
        return default_workspace_dir(self._data_dir, agent_id)

    def _write_agent(self, agent: Agent) -> None:
        """Write one config; it never waits under the lock (see ``_repairing_read``)."""
        agent_path = self._agent_path(agent.id)
        allowed = agent.tools.get("subagent", {}).get("allowed_agents", [])
        agent = replace(
            agent,
            delegation_bindings={
                key: value for key, value in agent.delegation_bindings.items() if key in allowed
            },
        )
        with self._write_lock, document_change(agent_path, wait=False):
            persisted = _agent_document(
                agent,
                workspace=_workspace_for_storage(agent.workspace, data_dir=self._data_dir),
            )
            try:
                write_json_document(agent_path, persisted, AGENT_FORMAT)
            except JsonDocumentWriteError as error:
                raise AgentError(str(error)) from error

    def delegation_target_allowed(self, target_id: str, binding: str | int) -> bool:
        """Whether a captured explicit grant still addresses this Identity Agent.

        No binding means a legacy grant and is handled by the caller. This check
        fails closed for unavailable or invalid identity history.
        """
        with self._write_lock:
            try:
                return _delegation.target_allowed(self, target_id, binding)
            except AgentError, OSError:
                return False

    def capture_creation_identity(self, agent_id: str) -> str:
        """Bind new work to the persisted Agent incarnation, stamping legacy configs once.

        Call at explicit admission, never to infer the owner of existing work.
        """
        with self._change():
            self._require_agent_path(agent_id)
            identity = _delegation._stamp(self, agent_id)
            assert identity is not None
            return identity

    def capture_delegation_bindings(self, tools: Mapping[str, Any]) -> dict[str, str | int]:
        """Bind a new temporary Agent's explicit Identity targets to their incarnations."""
        from core.tools.availability import subagent_allowed_agents

        targets = subagent_allowed_agents(tools)
        if "*" in targets:
            return {}
        with self._change():
            return _delegation.capture_bindings(self, targets)

    def _agent_defaults(self) -> AgentDefaults:
        if self._defaults_provider is None:
            return AgentDefaults()

        defaults = self._defaults_provider()
        if not isinstance(defaults, dict):
            raise AgentError("defaults provider must return a dictionary")
        return AgentDefaults.from_dict(defaults)

    def _load_verified_agent(self, agent_path: Path) -> Agent:
        """Load a seeded config whose current-Session pointer is empty or names a live Session."""
        with self._write_lock:
            return self._with_live_current_sessions([self._load_seeded_agent(agent_path)])[0]

    def _load_seeded_agent(self, agent_path: Path) -> Agent:
        """Load a config, seeding its Workspace; the current-Session pointer is unverified."""
        with self._write_lock:
            data = _validated_agent_data(agent_path)
            workspace_missing = _is_missing_workspace(data.get("workspace"))
            agent = _agent_from_dict(
                data,
                data_dir=self._data_dir,
                default_workspace=self._default_workspace(data["id"]),
            )
            self._seed_workspace(Path(agent.workspace))
            if workspace_missing:
                self._write_agent(agent)
            return agent

    def _read_agent_config(self, agent_path: Path) -> Agent:
        """Load and construct an agent from its config file with no side effects.

        Unlike :meth:`_load_seeded_agent` this seeds no workspace and runs no
        current-session normalization, so a caller can inspect a dangling current
        pointer before it would otherwise be silently replaced.
        """
        data = _validated_agent_data(agent_path)
        return _agent_from_dict(
            data,
            data_dir=self._data_dir,
            default_workspace=self._default_workspace(data["id"]),
        )

    def _with_live_current_sessions(self, agents: builtins.list[Agent]) -> builtins.list[Agent]:
        live = self._live_current_session_agent_ids(agents)
        return [
            agent
            if agent.id in live or not agent.current_session_id
            else self._clear_current_session(agent)
            for agent in agents
        ]

    def _live_current_session_agent_ids(self, agents: builtins.list[Agent]) -> set[str]:
        """Return the ids of *agents* whose current-Session pointer names a live Session.

        The pointer lives in ``agent.json`` and Sessions live in SQLite, with no
        transaction across both. Session removal re-aims it at write time
        (``reset_current_after_session_removed``), but a failed or interrupted
        re-aim, a restored Session store, or an offline edit can still leave it
        dangling, so reads that return the pointer keep verifying it - all
        pointers of a roster in one Session read.
        """
        pointers = {
            SessionAddress(None, agent.id, agent.current_session_id): agent.id
            for agent in agents
            if agent.current_session_id
        }
        if not pointers:
            return set()
        live = self._session_manager().existing_addresses(builtins.list(pointers))
        return {pointers[address] for address in live}

    def _clear_current_session(self, agent: Agent) -> Agent:
        """Clear *agent*'s dangling current-Session pointer; no Session is created."""
        # A repair that a data snapshot defers retries after it (``_repairing_read``).
        with document_change(self._agent_path(agent.id), wait=False):
            updated_agent = replace(agent, current_session_id="", updated_at=utc_now_timestamp())
            self._write_agent(updated_agent)
        _LOGGER.info("Dangling current Session pointer cleared (agent=%s)", agent.id)
        return updated_agent

    def _validate_current_session(self, agent_id: str, session_id: Any) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise AgentError("current_session_id must be a non-empty string")
        if not self._session_exists(agent_id, session_id):
            raise AgentError(f"current session does not exist: {session_id}")

    def _session_exists(self, agent_id: str, session_id: str) -> bool:
        address = SessionAddress(project_id=None, agent_id=agent_id, session_id=session_id)
        return self._session_manager().exists(address)

    def _session_manager(self) -> ChatSessionManager:
        with self._write_lock:
            if self._sessions is None:
                from core.sessions import ChatSessionManager
                from core.storage.layout import initialize_data_directory

                # Standalone/test usage: ensure a current-format marker exists for a
                # freshly created data directory without silently manufacturing
                # authorization for an already-initialized root that deliberately
                # lacks one. ``initialize_data_directory`` only writes the bootstrap
                # marker when it created the root itself.
                marker = self._data_dir / "data-store.json"
                if not marker.exists():
                    with suppress(Exception):
                        initialize_data_directory(self._data_dir)
                self._sessions = ChatSessionManager(self._data_dir)
                self._owns_sessions = True
            return self._sessions

    def _seed_workspace(self, workspace_path: Path) -> None:
        workspace_ops.seed_workspace(self._template_dir, workspace_path)
