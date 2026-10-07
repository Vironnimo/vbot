"""Identity Agent catalog, Session coordination and compensated mutations."""

from __future__ import annotations

import builtins
import shutil
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any

from core.agents import _archive
from core.agents import _workspace as workspace_ops
from core.agents._config import (
    AGENT_FORMAT,
    AGENT_ORDER_FORMAT,
    AGENT_ORDER_SHAPE,
    AGENT_RENAME_FORMAT,
    AGENT_RENAME_SHAPE,
    DEFAULT_ALLOWED_ITEMS,
    DEFAULT_FALLBACK_MODELS,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING_EFFORT,
    _agent_document,
    _agent_from_dict,
    _agent_order_document,
    _agent_rename_document,
    _agent_rename_from_data,
    _apply_agent_order,
    _apply_defaults,
    _normalize_agent_name,
    _normalize_agent_tools,
    _replace_list_item_once,
    _validate_agent_id,
    _validate_allowed_items,
    _validate_bool_field,
    _validate_excluded_skills,
    _validate_fallback_models,
    _validate_memory_prompt_mode,
    _validate_new_agent_id,
    _validate_root_project_id,
    _validate_string_field,
    _validate_temperature,
    _validate_thinking_effort,
    _validate_tool_access,
    _validate_top_p,
    _validated_agent_data,
    _with_builtin_capabilities,
    load_validated_agent_json,
    validate_agent_data,
    validate_agent_file,
    validate_agent_order_data,
    validate_agent_order_file,
    validate_agent_rename_data,
    validate_agent_rename_file,
)
from core.agents._types import (
    BUILTIN_AGENT_IDS,
    BUILTIN_AGENT_NAMES,
    DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED,
    DEFAULT_LIBRARIAN_ENABLED,
    LIBRARIAN_AGENT_ID,
    LIBRARIAN_AGENT_NAME,
    LIBRARIAN_BUILTIN,
    LIBRARIAN_TOOLS,
    LIVE_BACKEND_AGENT_ID,
    LIVE_BACKEND_BUILTIN,
    LIVE_VOICE_AGENT_ID,
    LIVE_VOICE_BUILTIN,
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
    _AgentOrderDocument,
    is_librarian,
    is_live_agent,
    librarian_problem_message,
    skill_subject_id,
)
from core.agents._workspace import (
    WORKSPACE_IDENTITY_FILES,
    WORKSPACE_TEMPLATE_FILES,
    _is_missing_workspace,
    _paths_are_same_location,
    _rebase_path_with_tree,
    _resolve_workspace,
    _workspace_for_storage,
    _WorkspaceRelocation,
    default_workspace_dir,
)
from core.config_validation import (
    JsonConfigValidationError,
    load_validated_json_file,
)
from core.database import MemberFrozenError, SnapshotBarrier
from core.json_documents import (
    JsonDocumentWriteError,
    document_change,
    strip_unknown_fields,
    write_json_document,
)
from core.memory import (
    DEFAULT_MEMORY_PROMPT_MODE,
    MemoryPromptMode,
)
from core.sessions import ChatSessionManager, SessionAddress
from core.settings import (
    AgentDefaults,
    is_valid_agent_id,
)
from core.settings.normalizers import normalize_compaction_policy
from core.tools.availability import (
    TOOL_ACCESS_MODE_SELECTED,
    ToolAccess,
)
from core.tools.live import LIVE_TOOL_NAMES
from core.tools.web_fetch import WEB_FETCH_TOOL_NAME
from core.tools.web_search import WEB_SEARCH_TOOL_NAME
from core.utils.file_status import exists_strict
from core.utils.ids import has_id_entry
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_timestamp

__all__ = [
    "BUILTIN_AGENT_IDS",
    "BUILTIN_AGENT_NAMES",
    "LIVE_BACKEND_AGENT_ID",
    "LIVE_BACKEND_BUILTIN",
    "LIVE_VOICE_AGENT_ID",
    "LIVE_VOICE_BUILTIN",
    "BuiltinAgentProblem",
    "is_live_agent",
    "LIBRARIAN_AGENT_ID",
    "LIBRARIAN_AGENT_NAME",
    "LIBRARIAN_TOOLS",
    "SKILL_AGENT_ID_KEY",
    "Agent",
    "AgentAlreadyExistsError",
    "BuiltinAgentError",
    "LibrarianProblem",
    "is_librarian",
    "librarian_problem_message",
    "skill_subject_id",
    "ArchivedAgent",
    "ArchivedAgentPayload",
    "AgentError",
    "AgentListResult",
    "AgentNotFoundError",
    "AgentOrderConflictError",
    "AgentReferencedError",
    "AgentRename",
    "AgentRenameResult",
    "AgentStore",
    "AgentUpdateResult",
    "DEFAULT_ALLOWED_ITEMS",
    "DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED",
    "DEFAULT_FALLBACK_MODELS",
    "DEFAULT_MODEL",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_THINKING_EFFORT",
    "InvalidAgentIdError",
    "InvalidAgentOrderError",
    "WORKSPACE_IDENTITY_FILES",
    "WORKSPACE_TEMPLATE_FILES",
    "default_workspace_dir",
    "load_validated_agent_json",
    "validate_agent_data",
    "validate_agent_file",
    "validate_agent_order_data",
    "validate_agent_order_file",
    "validate_agent_rename_data",
    "validate_agent_rename_file",
]

_BOOTSTRAP_AGENT_ID = "main"

_BOOTSTRAP_AGENT_NAME = "Main"

_AGENT_ORDER_FILE_NAME = "order.json"

_AGENT_RENAME_FILE_NAME = "rename-pending.json"

# What the user may change on a built-in Agent: its Model settings, and on the
# Agents of a Live call also their Tools. The current Session follows the
# Session the user opens.
_BUILTIN_MODEL_FIELDS = frozenset(
    {
        "model",
        "fallback_models",
        "temperature",
        "top_p",
        "thinking_effort",
        "current_session_id",
    }
)
_BUILTIN_EDITABLE_FIELDS: dict[str, frozenset[str]] = {
    LIBRARIAN_BUILTIN: _BUILTIN_MODEL_FIELDS,
    LIVE_VOICE_BUILTIN: _BUILTIN_MODEL_FIELDS | {"tool_access"},
    LIVE_BACKEND_BUILTIN: _BUILTIN_MODEL_FIELDS | {"tool_access"},
}
# The Tools a new Agent of a Live call starts with: the Live Tools, and for the
# backend Agent also web research.
_BUILTIN_DEFAULT_TOOL_ACCESS: dict[str, ToolAccess] = {
    LIVE_VOICE_BUILTIN: ToolAccess(mode=TOOL_ACCESS_MODE_SELECTED, allowed=LIVE_TOOL_NAMES),
    LIVE_BACKEND_BUILTIN: ToolAccess(
        mode=TOOL_ACCESS_MODE_SELECTED,
        allowed=(*LIVE_TOOL_NAMES, WEB_SEARCH_TOOL_NAME, WEB_FETCH_TOOL_NAME),
    ),
}

_LOGGER = get_logger("agents")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_DEFAULT_TEMPLATE_DIR = _PROJECT_ROOT / "resources" / "workspace-templates"


@dataclass(frozen=True)
class _AppliedRename:
    """What the Agent-owned half of one rename direction changed besides the tree."""

    policy_agent_ids: tuple[str, ...]
    session_link_count: int


class AgentStore:
    """CRUD store for persisted agent configs and workspaces."""

    def __init__(
        self,
        data_dir: str | Path,
        template_dir: str | Path | None = None,
        defaults_provider: Callable[[], dict[str, Any]] | None = None,
        sessions: ChatSessionManager | None = None,
        snapshot_barrier: SnapshotBarrier | None = None,
    ) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
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
        librarian_enabled: bool = DEFAULT_LIBRARIAN_ENABLED,
    ) -> Agent:
        """Create and persist a new Agent and its Workspace; it has no Session yet.

        The id must be free and may not be one vBot reserves (:class:`InvalidAgentIdError`).
        """
        _validate_new_agent_id(agent_id)
        return self._create(
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
            librarian_enabled=librarian_enabled,
        )

    def _create(
        self,
        agent_id: str,
        name: str | None,
        *,
        builtin: BuiltinAgent | None,
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
        librarian_enabled: bool = DEFAULT_LIBRARIAN_ENABLED,
    ) -> Agent:
        """:meth:`create` past the reserved-id rules; ``builtin`` marks a built-in Agent."""
        with self._snapshot_barrier.compound_mutation(), self._change():
            _validate_agent_id(agent_id)
            agent_dir = self._agent_dir(agent_id)
            if exists_strict(agent_dir):
                raise AgentAlreadyExistsError(f"Agent already exists: {agent_id}")
            if agent_id in self._pending_rename_ids():
                raise AgentError(
                    f"Agent id {agent_id} belongs to an unfinished rename; "
                    "restart vBot to finish it"
                )

            validated_name = _normalize_agent_name(agent_id, name)
            validated_model = _validate_string_field("model", model, allow_empty=True)
            validated_fallback_models = _validate_fallback_models(
                "fallback_models", fallback_models or []
            )
            validated_temperature = _validate_temperature(temperature)
            validated_top_p = _validate_top_p(top_p)
            validated_thinking_effort = _validate_thinking_effort(thinking_effort)
            validated_memory_prompt_mode = _validate_memory_prompt_mode(memory_prompt_mode)
            validated_tool_access = _validate_tool_access(tool_access)
            validated_allowed_skills = _validate_allowed_items("allowed_skills", allowed_skills)
            validated_excluded_skills = _validate_excluded_skills(excluded_skills)
            validated_tools = _normalize_agent_tools(tools)
            validated_custom_system_prompt_enabled = _validate_bool_field(
                "custom_system_prompt_enabled", custom_system_prompt_enabled
            )
            validated_librarian_enabled = _validate_bool_field(
                "librarian_enabled", librarian_enabled
            )
            validated_compaction_policy = (
                normalize_compaction_policy(compaction_policy)
                if compaction_policy is not None
                else None
            )
            now = utc_now_timestamp()
            workspace_value = workspace
            if workspace_value is None or (
                isinstance(workspace_value, str) and not workspace_value.strip()
            ):
                workspace_value = self._default_workspace(agent_id)
            workspace_path = _resolve_workspace(workspace_value, data_dir=self._data_dir)

            # A new Agent has no Session: its first message creates one.
            agent_dir.mkdir(parents=True)
            agent = Agent(
                id=agent_id,
                name=validated_name,
                model=validated_model,
                fallback_models=validated_fallback_models,
                workspace=str(workspace_path.resolve()),
                root_project_id=None,
                temperature=validated_temperature,
                top_p=validated_top_p,
                thinking_effort=validated_thinking_effort,
                memory_prompt_mode=validated_memory_prompt_mode,
                tool_access=validated_tool_access,
                allowed_skills=validated_allowed_skills,
                excluded_skills=validated_excluded_skills,
                tools=validated_tools,
                custom_system_prompt_enabled=validated_custom_system_prompt_enabled,
                compaction_policy=validated_compaction_policy,
                librarian_enabled=validated_librarian_enabled,
                created_at=now,
                updated_at=now,
                builtin=builtin,
            )
            agent = _with_builtin_capabilities(agent)

            try:
                self._seed_workspace(Path(agent.workspace))
                self._write_agent(agent)
            except Exception:
                shutil.rmtree(agent_dir, ignore_errors=True)
                raise
            # ``list_with_order`` appends this newly valid Agent after every existing
            # roster entry and persists that projection. The config write remains the
            # creation commit point; an auxiliary order write failure is logged there
            # and never turns a successfully created identity into a false failure.
            self.list_with_order()
            return _apply_defaults(agent, self._agent_defaults())

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
        return list(self.list_with_order().agents)

    def list_with_builtins(self) -> builtins.list[Agent]:
        """Return the roster followed by the built-in Agents that are available."""
        available = (self.builtin_agent(builtin) for builtin in BUILTIN_AGENT_IDS)
        return [*self.list(), *(agent for agent in available if agent is not None)]

    def builtin_agent(self, builtin: BuiltinAgent) -> Agent | None:
        """Return a built-in Agent with defaults applied, ``None`` while unavailable."""
        if self.builtin_problem(builtin) is not None:
            return None
        try:
            return self.get(BUILTIN_AGENT_IDS[builtin])
        except AgentError, OSError:
            return None

    def builtin_problem(self, builtin: BuiltinAgent) -> BuiltinAgentProblem | None:
        """Why a built-in Agent is unavailable, ``None`` while it is available.

        ``missing``: no Agent has its id yet (startup creates it); ``agent_id_taken``:
        an Agent of the user or an unfinished rename holds the id;
        ``invalid_config``: its ``agent.json`` cannot be loaded. Never raises.
        """
        agent_id = BUILTIN_AGENT_IDS[builtin]
        with self._write_lock:
            try:
                agent_path = self._stored_agent_path(agent_id)
                if agent_path is None:
                    if agent_id in self._pending_rename_ids() or exists_strict(
                        self._agent_dir(agent_id)
                    ):
                        return "agent_id_taken"
                    return "missing"
                agent = self._read_agent_config(agent_path)
            except AgentError, OSError:
                return "invalid_config"
            return None if agent.builtin == builtin else "agent_id_taken"

    def librarian(self) -> Agent | None:
        """Return the built-in Librarian with defaults applied, ``None`` while unavailable."""
        return self.builtin_agent(LIBRARIAN_BUILTIN)

    def librarian_problem(self) -> LibrarianProblem | None:
        """Why the built-in Librarian is unavailable (:meth:`builtin_problem`)."""
        return self.builtin_problem(LIBRARIAN_BUILTIN)

    def ensure_builtin_agents(self) -> None:
        """Create each built-in Agent that is missing.

        A new built-in Agent gets a new Agent's defaults, so it runs the default
        Model until the user picks one; the Agents of a Live call start with
        their default Tools. An Agent of the user that holds a reserved id and an
        ``agent.json`` that cannot be loaded stay as they are; the warning names
        the problem.
        """
        for builtin, agent_id in BUILTIN_AGENT_IDS.items():
            name = BUILTIN_AGENT_NAMES[builtin]
            with self._snapshot_barrier.compound_mutation(), self._change():
                problem = self.builtin_problem(builtin)
                if problem == "missing":
                    try:
                        self._create(
                            agent_id,
                            name,
                            builtin=builtin,
                            tool_access=_BUILTIN_DEFAULT_TOOL_ACCESS.get(builtin),
                        )
                    except (AgentError, OSError) as error:
                        _LOGGER.warning("Could not create the built-in %s Agent: %s", name, error)
                        continue
                    _LOGGER.info("Built-in %s Agent created (agent=%s)", name, agent_id)
                elif problem is not None:
                    _LOGGER.warning(
                        "The built-in %s Agent is unavailable (agent=%s problem=%s)",
                        name,
                        agent_id,
                        problem,
                    )

    def list_with_order(self) -> AgentListResult:
        """Return the canonical roster plus its conflict-detection revision.

        A missing order document preserves the historical id order and is
        materialized on the first non-empty read. Stale ids are discarded and
        newly discovered valid Agents append at the end. A malformed order file
        never hides Agents: the roster falls back to id order and the invalid
        file remains available for ``doctor config`` diagnostics.
        """
        return self._repairing_read(self._list_with_order)

    def _list_with_order(self) -> AgentListResult:
        """:meth:`list_with_order` under the store lock."""
        agents_dir = self._data_dir / "agents"
        if not agents_dir.exists():
            return AgentListResult(agents=(), order_revision=0)

        defaults = self._agent_defaults()
        try:
            agent_paths = sorted(agents_dir.glob("*/agent.json"))
        except OSError as error:
            _LOGGER.warning("Could not scan Agent configs in %s: %s", agents_dir, error)
            agent_paths = []

        loaded: list[tuple[Path, Agent]] = []
        for agent_path in agent_paths:
            try:
                agent = self._load_seeded_agent(agent_path)
            except (AgentError, OSError) as error:
                _LOGGER.warning("Skipping invalid Agent config %s: %s", agent_path, error)
                continue
            if agent.builtin is None:
                loaded.append((agent_path, agent))
        # One Session read verifies every current pointer of the roster.
        live = self._live_current_session_agent_ids([agent for _path, agent in loaded])
        agents: list[Agent] = []
        for agent_path, raw_agent in loaded:
            try:
                if raw_agent.current_session_id and raw_agent.id not in live:
                    raw_agent = self._clear_current_session(raw_agent)
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
        order_path = self._agent_order_path()
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
        with self._change():
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

    def ensure_bootstrap(self) -> Agent | None:
        """Create one valid bootstrap Agent when the store has none.

        Invalid Agent directories are preserved for diagnosis. If one already
        occupies ``main``, the bootstrap Agent uses the first free ``main-N`` id.
        """
        with self._snapshot_barrier.compound_mutation(), self._change():
            if self.list():
                return None

            candidate = _BOOTSTRAP_AGENT_ID
            suffix = 2
            reserved = self._pending_rename_ids()
            while exists_strict(self._agent_dir(candidate)) or candidate in reserved:
                candidate = f"{_BOOTSTRAP_AGENT_ID}-{suffix}"
                suffix += 1
            return self.create(candidate, _BOOTSTRAP_AGENT_NAME)

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
        with self._change():
            _validate_agent_id(agent_id)
            if "id" in changes and changes["id"] != agent_id:
                raise AgentError("Agent id is immutable")

            changes.pop("id", None)
            agent_path = self._require_agent_path(agent_id)
            agent = self._load_verified_agent(agent_path)
            if agent.builtin is not None:
                editable = _BUILTIN_EDITABLE_FIELDS[agent.builtin]
                fixed_fields = sorted(set(changes) - editable)
                if fixed_fields or copy_workspace_identity_files:
                    names = ", ".join(sorted(editable - {"current_session_id"}))
                    raise BuiltinAgentError(
                        f"The {agent.name} Agent is built into vBot: only its {names} can "
                        "change, not "
                        f"{', '.join(fixed_fields or ['copy_workspace_identity_files'])}"
                    )
            if not changes:
                if copy_workspace_identity_files:
                    raise AgentError("copy_workspace_identity_files requires a workspace change")
                return AgentUpdateResult(_apply_defaults(agent, self._agent_defaults()))

            allowed_fields = set(Agent.__dataclass_fields__) - {
                "id",
                "created_at",
                "updated_at",
            }
            unknown_fields = sorted(set(changes) - allowed_fields)
            if unknown_fields:
                raise AgentError(f"Unknown agent fields: {', '.join(unknown_fields)}")

            if "name" in changes:
                changes["name"] = _normalize_agent_name(agent_id, changes["name"])
            # An empty current_session_id clears the pointer: the Agent opens a new Session.
            string_fields = {"model", "current_session_id"}
            for field_name in sorted(string_fields & set(changes)):
                changes[field_name] = _validate_string_field(
                    field_name, changes[field_name], allow_empty=True
                )
            if "fallback_models" in changes:
                changes["fallback_models"] = _validate_fallback_models(
                    "fallback_models", changes["fallback_models"]
                )
            if "workspace" in changes:
                workspace = changes["workspace"]
                if workspace is None or (isinstance(workspace, str) and not workspace.strip()):
                    workspace = self._default_workspace(agent_id)
                changes["workspace"] = str(_resolve_workspace(workspace, data_dir=self._data_dir))
                if changes["workspace"] == agent.workspace:
                    if copy_workspace_identity_files:
                        raise AgentError(
                            "copy_workspace_identity_files requires a changed workspace"
                        )
                    changes.pop("workspace")
            elif copy_workspace_identity_files:
                raise AgentError("copy_workspace_identity_files requires a workspace change")
            if "root_project_id" in changes:
                changes["root_project_id"] = _validate_root_project_id(changes["root_project_id"])
            if "temperature" in changes:
                changes["temperature"] = _validate_temperature(changes["temperature"])
            if "top_p" in changes:
                changes["top_p"] = _validate_top_p(changes["top_p"])
            if "thinking_effort" in changes:
                changes["thinking_effort"] = _validate_thinking_effort(changes["thinking_effort"])
            if "memory_prompt_mode" in changes:
                changes["memory_prompt_mode"] = _validate_memory_prompt_mode(
                    changes["memory_prompt_mode"]
                )
            if "tool_access" in changes:
                changes["tool_access"] = _validate_tool_access(changes["tool_access"])
            if "allowed_skills" in changes:
                changes["allowed_skills"] = _validate_allowed_items(
                    "allowed_skills", changes["allowed_skills"]
                )
            if "excluded_skills" in changes:
                changes["excluded_skills"] = _validate_excluded_skills(changes["excluded_skills"])
            if "tools" in changes:
                changes["tools"] = _normalize_agent_tools(changes["tools"])
            for bool_field in ("custom_system_prompt_enabled", "librarian_enabled"):
                if bool_field in changes:
                    changes[bool_field] = _validate_bool_field(bool_field, changes[bool_field])
            if "compaction_policy" in changes:
                policy = changes["compaction_policy"]
                changes["compaction_policy"] = (
                    normalize_compaction_policy(policy) if policy is not None else None
                )
            if changes.get("current_session_id"):
                self._validate_current_session(agent_id, changes["current_session_id"])

            if not changes:
                return AgentUpdateResult(_apply_defaults(agent, self._agent_defaults()))

            updated_agent = _with_builtin_capabilities(
                replace(agent, **changes, updated_at=utc_now_timestamp())
            )
            relocation = _WorkspaceRelocation()
            try:
                if "workspace" in changes:
                    relocation = workspace_ops.relocate_workspace(
                        self._agent_dir(agent_id),
                        self._template_dir,
                        agent,
                        Path(updated_agent.workspace),
                        copy_identity_files=copy_workspace_identity_files,
                    )
                self._write_agent(updated_agent)
            except Exception:
                relocation.rollback()
                raise
            return AgentUpdateResult(
                agent=_apply_defaults(updated_agent, self._agent_defaults()),
                copied_files=relocation.copied_files,
                backed_up_files=relocation.backed_up_files,
                backup_dir=str(relocation.backup_dir) if relocation.backup_dir else None,
                created_files=relocation.created_files,
                destination=str(relocation.destination) if relocation.destination else None,
            )

    def restore_update(self, previous_agent: Agent, result: AgentUpdateResult) -> None:
        """Compensate a completed update during a larger coordinated operation."""
        with self._change():
            if result.destination is not None:
                relocation = _WorkspaceRelocation(
                    destination=Path(result.destination),
                    copied_files=result.copied_files,
                    backed_up_files=result.backed_up_files,
                    created_files=result.created_files,
                    backup_dir=Path(result.backup_dir) if result.backup_dir else None,
                )
                relocation.rollback()
            self._write_agent(previous_agent)

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
        with self._snapshot_barrier.compound_mutation(), self._change():
            _validate_agent_id(agent_id)
            _validate_new_agent_id(new_agent_id)
            if agent_id == new_agent_id:
                raise AgentError("new agent id must differ from the current id")
            if exists_strict(self._rename_record_path()):
                raise AgentError(
                    "An earlier Agent rename is still pending; restart vBot to finish it"
                )

            source_dir = self._agent_dir(agent_id)
            destination_dir = self._agent_dir(new_agent_id)
            agent_path = self._require_agent_path(agent_id)
            source = self._read_agent_config(agent_path)
            if source.builtin is not None:
                raise BuiltinAgentError(
                    f"The {source.name} is built into vBot and cannot be renamed"
                )
            case_only = _paths_are_same_location(source_dir, destination_dir)
            if exists_strict(destination_dir) and not case_only:
                raise AgentAlreadyExistsError(f"Agent already exists: {new_agent_id}")
            sessions = self._session_manager()
            # Rolling back renames the new id's Sessions back, so it must own none yet.
            if sessions.list_addresses(None, agent_id=new_agent_id):
                raise AgentAlreadyExistsError(f"Sessions already exist for Agent: {new_agent_id}")
            references = tuple(external_references)
            if references:
                raise AgentReferencedError(new_agent_id, references)
            # Before the record, outside every rename direction: an interruption
            # leaves only fewer leftovers, and no revert brings them back.
            leftover_policy_agent_ids = self._remove_from_allow_lists(new_agent_id)
            # Repairs a dangling current-Session pointer before the Sessions move.
            self._load_verified_agent(agent_path)
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
                renamed = self._read_agent_config(self._require_agent_path(new_agent_id))
            except Exception as error:
                self._roll_back_rename(rename, error)
                raise
            return AgentRenameResult(
                rename=rename,
                agent=_apply_defaults(renamed, self._agent_defaults()),
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
        with self._snapshot_barrier.compound_mutation(), self._change():
            self._require_pending_rename(rename)
            reverse = rename.reversed()
            self._write_rename_record(reverse)
            self._apply_rename(reverse)
            return reverse

    def finish_rename(self, rename: AgentRename) -> None:
        """Remove the pending ``rename`` record once every reference is retargeted."""
        with self._change():
            self._require_pending_rename(rename)
            self._rename_record_path().unlink(missing_ok=True)

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
        with self._snapshot_barrier.compound_mutation(), self._change():
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
            self._rename_record_path().unlink(missing_ok=True)
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
        sessions = self._session_manager()
        sessions.retarget_identity_agent_sessions(rename.source_id, rename.target_id)
        link_count = len(
            sessions.retarget_identity_agent_references(rename.source_id, rename.target_id)
        )
        # The Librarian Sessions that maintain the Agent's Skills keep doing so.
        sessions.retarget_metadata_value(
            LIBRARIAN_AGENT_ID, SKILL_AGENT_ID_KEY, rename.source_id, rename.target_id
        )
        if workspace_ops._move_renamed_tree(self._data_dir / "agents", rename):
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
        agent_path = self._agent_path(rename.target_id)
        data = load_validated_agent_json(agent_path)
        if data["id"] not in (rename.source_id, rename.target_id):
            raise AgentError(f"{agent_path}: Agent id {data['id']!r} does not match the rename")
        agent = _agent_from_dict(
            data,
            data_dir=self._data_dir,
            default_workspace=self._default_workspace(rename.target_id),
        )
        workspace = str(
            _rebase_path_with_tree(
                agent.workspace,
                self._agent_dir(rename.source_id),
                self._agent_dir(rename.target_id),
            )
        )
        if agent.id == rename.target_id and workspace == agent.workspace:
            return
        self._write_agent(
            replace(agent, id=rename.target_id, workspace=workspace, updated_at=utc_now_timestamp())
        )

    def _rename_order_entry(self, rename: AgentRename) -> None:
        """Keep the renamed Agent at its roster position."""
        order = self._load_agent_order()
        if order is None or rename.source_id not in order.agent_ids:
            return
        agent_ids = _replace_list_item_once(
            builtins.list(order.agent_ids), rename.source_id, rename.target_id
        )
        self._write_agent_order(
            _AgentOrderDocument(agent_ids=tuple(agent_ids), revision=order.revision + 1)
        )

    def _retarget_allowed_agents(self, old_agent_id: str, new_agent_id: str) -> tuple[str, ...]:
        """Retarget a bare Identity Agent id in every delegation allow-list.

        Returns the ids of the Agents whose configs changed.
        """
        changed: builtins.list[str] = []
        for agent, allowed_agents in self._allow_lists_naming(old_agent_id):
            tools = deepcopy(agent.tools)
            tools["subagent"]["allowed_agents"] = _replace_list_item_once(
                allowed_agents, old_agent_id, new_agent_id
            )
            self._write_agent(replace(agent, tools=tools, updated_at=utc_now_timestamp()))
            changed.append(agent.id)
        return tuple(changed)

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

        ``record`` receives each grant (holder id and position) before any
        config is written. A qualified ``agent@project`` entry names a Project's
        Team Agent and stays. Returns the ids of the Agents whose configs changed.
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

    def _rename_record_path(self) -> Path:
        return self._data_dir / "agents" / _AGENT_RENAME_FILE_NAME

    def _load_rename_record(self) -> AgentRename | None:
        try:
            data = load_validated_json_file(
                self._rename_record_path(),
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
                self._rename_record_path(), _agent_rename_document(rename), AGENT_RENAME_FORMAT
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
        with self._write_lock, document_change(agent_path, wait=False):
            persisted = _agent_document(
                agent,
                workspace=_workspace_for_storage(agent.workspace, data_dir=self._data_dir),
            )
            try:
                write_json_document(agent_path, persisted, AGENT_FORMAT)
            except JsonDocumentWriteError as error:
                raise AgentError(str(error)) from error

    def _load_agent_order(self) -> _AgentOrderDocument | None:
        order_path = self._agent_order_path()
        try:
            data = load_validated_json_file(
                order_path,
                validate_agent_order_data,
                missing_ok=True,
                missing_default=None,
            )
        except JsonConfigValidationError as error:
            message = str(error)
            if message != self._reported_order_error:
                _LOGGER.warning("Ignoring invalid Identity Agent order: %s", message)
                self._reported_order_error = message
            return None

        self._reported_order_error = None
        if data is None:
            return None
        data = strip_unknown_fields(data, AGENT_ORDER_SHAPE)
        return _AgentOrderDocument(
            agent_ids=tuple(data["agent_ids"]),
            revision=data["revision"],
        )

    def _write_agent_order(self, order: _AgentOrderDocument) -> None:
        """Write the roster order; it never waits under the lock (see ``_repairing_read``)."""
        order_path = self._agent_order_path()
        with self._write_lock, document_change(order_path, wait=False):
            try:
                write_json_document(order_path, _agent_order_document(order), AGENT_ORDER_FORMAT)
            except JsonDocumentWriteError as error:
                raise AgentError(str(error)) from error
        self._reported_order_error = None

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
