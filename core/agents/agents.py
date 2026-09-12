"""Identity Agent catalog, Session coordination and compensated mutations."""

from __future__ import annotations

import builtins
import json
import shutil
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from threading import RLock
from typing import Any

from core.agents import _workspace as workspace_ops
from core.agents._config import (
    DEFAULT_ALLOWED_ITEMS,
    DEFAULT_FALLBACK_MODELS,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_THINKING_EFFORT,
    _agent_from_dict,
    _apply_agent_order,
    _apply_defaults,
    _normalize_agent_name,
    _normalize_agent_tools,
    _replace_list_item_once,
    _utc_now,
    _validate_agent_id,
    _validate_allowed_items,
    _validate_bool_field,
    _validate_fallback_models,
    _validate_memory_prompt_mode,
    _validate_root_project_id,
    _validate_string_field,
    _validate_temperature,
    _validate_thinking_effort,
    _validate_tool_access,
    _validated_agent_data,
    load_validated_agent_json,
    validate_agent_data,
    validate_agent_file,
    validate_agent_order_data,
    validate_agent_order_file,
)
from core.agents._types import (
    DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED,
    Agent,
    AgentAlreadyExistsError,
    AgentError,
    AgentListResult,
    AgentNotFoundError,
    AgentOrderConflictError,
    AgentReferenceUpdateResult,
    AgentRenameResult,
    AgentUpdateResult,
    InvalidAgentIdError,
    InvalidAgentOrderError,
    _AgentOrderDocument,
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
    ToolAccess,
)
from core.utils.atomic import atomic_write_text
from core.utils.logging import get_logger

__all__ = [
    "Agent",
    "AgentAlreadyExistsError",
    "AgentError",
    "AgentListResult",
    "AgentNotFoundError",
    "AgentOrderConflictError",
    "AgentReferenceUpdateResult",
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
]

_BOOTSTRAP_AGENT_ID = "main"

_BOOTSTRAP_AGENT_NAME = "Main"

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
    ) -> None:
        self._data_dir = Path(data_dir).expanduser().resolve()
        self._template_dir = (
            Path(template_dir) if template_dir is not None else _DEFAULT_TEMPLATE_DIR
        )
        self._defaults_provider = defaults_provider
        self._sessions = sessions
        self._owns_sessions = sessions is None
        self._reported_order_error: str | None = None
        # Agent updates can arrive from separate RPC worker pools. Serialize
        # replacement of the same config files so Windows never sees two
        # concurrent os.replace calls targeting one agent.json or order.json.
        self._write_lock = RLock()

    def close(self) -> None:
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
        thinking_effort: str | None = DEFAULT_THINKING_EFFORT,
        memory_prompt_mode: MemoryPromptMode = DEFAULT_MEMORY_PROMPT_MODE,
        tool_access: ToolAccess | Mapping[str, Any] | None = None,
        allowed_skills: list[str] | None = None,
        tools: Mapping[str, Any] | None = None,
        custom_system_prompt_enabled: bool = DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED,
        compaction_policy: dict[str, Any] | None = None,
    ) -> Agent:
        """Create and persist a new Agent, initial Session, and Workspace."""
        _validate_agent_id(agent_id)
        agent_dir = self._agent_dir(agent_id)
        if agent_dir.exists():
            raise AgentAlreadyExistsError(f"Agent already exists: {agent_id}")

        validated_name = _normalize_agent_name(agent_id, name)
        validated_model = _validate_string_field("model", model, allow_empty=True)
        validated_fallback_models = _validate_fallback_models(
            "fallback_models", fallback_models or []
        )
        validated_temperature = _validate_temperature(temperature)
        validated_thinking_effort = _validate_thinking_effort(thinking_effort)
        validated_memory_prompt_mode = _validate_memory_prompt_mode(memory_prompt_mode)
        validated_tool_access = _validate_tool_access(tool_access)
        validated_allowed_skills = _validate_allowed_items("allowed_skills", allowed_skills)
        validated_tools = _normalize_agent_tools(tools)
        validated_custom_system_prompt_enabled = _validate_bool_field(
            "custom_system_prompt_enabled", custom_system_prompt_enabled
        )
        validated_compaction_policy = (
            normalize_compaction_policy(compaction_policy)
            if compaction_policy is not None
            else None
        )
        now = _utc_now()
        workspace_value = workspace
        if workspace_value is None or (
            isinstance(workspace_value, str) and not workspace_value.strip()
        ):
            workspace_value = self._default_workspace(agent_id)
        workspace_path = _resolve_workspace(workspace_value, data_dir=self._data_dir)

        # Create the Session first so a failure cannot leave a ghost Agent directory.
        session = self._session_manager().create(agent_id)
        try:
            agent_dir.mkdir(parents=True)
        except Exception:
            with suppress(Exception):
                session.delete()
            raise
        agent = Agent(
            id=agent_id,
            name=validated_name,
            model=validated_model,
            fallback_models=validated_fallback_models,
            workspace=str(workspace_path.resolve()),
            root_project_id=None,
            temperature=validated_temperature,
            thinking_effort=validated_thinking_effort,
            memory_prompt_mode=validated_memory_prompt_mode,
            tool_access=validated_tool_access,
            allowed_skills=validated_allowed_skills,
            tools=validated_tools,
            custom_system_prompt_enabled=validated_custom_system_prompt_enabled,
            compaction_policy=validated_compaction_policy,
            current_session_id=session.id,
            created_at=now,
            updated_at=now,
        )

        try:
            self._seed_workspace(Path(agent.workspace))
            self._write_agent(agent)
        except Exception:
            session.delete()
            shutil.rmtree(agent_dir, ignore_errors=True)
            raise
        # ``list_with_order`` appends this newly valid Agent after every existing
        # roster entry and persists that projection. The config write remains the
        # creation commit point; an auxiliary order write failure is logged there
        # and never turns a successfully created identity into a false failure.
        self.list_with_order()
        return _apply_defaults(agent, self._agent_defaults())

    def get(self, agent_id: str) -> Agent:
        """Load an agent from disk."""
        _validate_agent_id(agent_id)
        agent_path = self._agent_path(agent_id)
        if not agent_path.exists():
            raise AgentNotFoundError(f"Agent not found: {agent_id}")

        raw_agent = self._load_raw_agent(agent_path)
        return _apply_defaults(raw_agent, self._agent_defaults())

    def get_raw(self, agent_id: str) -> Agent:
        """Load an agent with its **un-baked** persisted values (no defaults applied).

        Same load path as :meth:`get` (workspace seeding, current-session
        normalization) but **without** the ``defaults.agent`` injection, so the
        returned Agent carries the raw ``model``/``fallback_models`` ("" / [] when unset)
        and raw ``temperature``/``thinking_effort`` (``None`` when unset). This is the
        provenance seam the resolver's identity ``effective_config`` reads to tell an
        own persisted value from a baked global default; ``get``/``list``/``update``
        keep baking for every other consumer.
        """
        _validate_agent_id(agent_id)
        agent_path = self._agent_path(agent_id)
        if not agent_path.exists():
            raise AgentNotFoundError(f"Agent not found: {agent_id}")

        return self._load_raw_agent(agent_path)

    def exists(self, agent_id: str) -> bool:
        """Return whether a valid identity Agent with this id can be loaded.

        The probe never raises. Invalid ids, missing files, malformed configs, and
        configs whose persisted id disagrees with their directory all yield
        ``False`` so a broken Agent is never treated as an available target.
        """
        if not is_valid_agent_id(agent_id):
            return False
        agent_path = self._agent_path(agent_id)
        if not agent_path.exists():
            return False
        try:
            self._read_agent_config(agent_path)
        except (AgentError, OSError):
            return False
        return True

    def list(self) -> list[Agent]:
        """Return valid persisted Agents in the canonical roster order."""
        return list(self.list_with_order().agents)

    def list_with_order(self) -> AgentListResult:
        """Return the canonical roster plus its conflict-detection revision.

        A missing order document preserves the historical id order and is
        materialized on the first non-empty read. Stale ids are discarded and
        newly discovered valid Agents append at the end. A malformed order file
        never hides Agents: the roster falls back to id order and the invalid
        file remains available for ``doctor config`` diagnostics.
        """
        agents_dir = self._data_dir / "agents"
        if not agents_dir.exists():
            return AgentListResult(agents=(), order_revision=0)

        defaults = self._agent_defaults()
        agents: list[Agent] = []
        try:
            agent_paths = sorted(agents_dir.glob("*/agent.json"))
        except OSError as error:
            _LOGGER.warning("Could not scan Agent configs in %s: %s", agents_dir, error)
            agent_paths = []

        for agent_path in agent_paths:
            try:
                raw_agent = self._load_raw_agent(agent_path)
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
        order_is_invalid = order is None and order_path.exists()
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
        if self.list():
            return None

        candidate = _BOOTSTRAP_AGENT_ID
        suffix = 2
        while self._agent_dir(candidate).exists():
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
        _validate_agent_id(agent_id)
        if "id" in changes and changes["id"] != agent_id:
            raise AgentError("Agent id is immutable")

        changes.pop("id", None)
        agent_path = self._agent_path(agent_id)
        if not agent_path.exists():
            raise AgentNotFoundError(f"Agent not found: {agent_id}")

        agent = self._load_raw_agent(agent_path)
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
        string_fields = {"model", "current_session_id"}
        for field_name in sorted(string_fields & set(changes)):
            changes[field_name] = _validate_string_field(
                field_name,
                changes[field_name],
                allow_empty=field_name == "model",
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
                    raise AgentError("copy_workspace_identity_files requires a changed workspace")
                changes.pop("workspace")
        elif copy_workspace_identity_files:
            raise AgentError("copy_workspace_identity_files requires a workspace change")
        if "root_project_id" in changes:
            changes["root_project_id"] = _validate_root_project_id(changes["root_project_id"])
        if "temperature" in changes:
            changes["temperature"] = _validate_temperature(changes["temperature"])
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
        if "tools" in changes:
            changes["tools"] = _normalize_agent_tools(changes["tools"])
        if "custom_system_prompt_enabled" in changes:
            changes["custom_system_prompt_enabled"] = _validate_bool_field(
                "custom_system_prompt_enabled", changes["custom_system_prompt_enabled"]
            )
        if "compaction_policy" in changes:
            policy = changes["compaction_policy"]
            changes["compaction_policy"] = (
                normalize_compaction_policy(policy) if policy is not None else None
            )
        if "current_session_id" in changes:
            self._validate_current_session(agent_id, changes["current_session_id"])

        if not changes:
            return AgentUpdateResult(_apply_defaults(agent, self._agent_defaults()))

        updated_agent = replace(agent, **changes, updated_at=_utc_now())
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

    def rename(self, agent_id: str, new_agent_id: str) -> AgentRenameResult:
        """Rename one complete Identity Agent tree as a rollback-capable mutation.

        Sessions, prompts, private Skills, the default Workspace, and every other
        Agent-owned file live below the same directory, so moving that directory
        preserves the whole identity. A Workspace anywhere inside the tree is
        rebased to the same relative location; an external Workspace is unchanged.
        """
        _validate_agent_id(agent_id)
        _validate_agent_id(new_agent_id)
        if agent_id == new_agent_id:
            raise AgentError("new agent id must differ from the current id")

        source_dir = self._agent_dir(agent_id)
        destination_dir = self._agent_dir(new_agent_id)
        if not self._agent_path(agent_id).is_file():
            raise AgentNotFoundError(f"Agent not found: {agent_id}")
        if destination_dir.exists() and not _paths_are_same_location(source_dir, destination_dir):
            raise AgentAlreadyExistsError(f"Agent already exists: {new_agent_id}")

        previous_listing = self.list_with_order()
        previous_order = self._load_agent_order()
        previous_agent = self._load_raw_agent(self._agent_path(agent_id))
        renamed_workspace = _rebase_path_with_tree(
            previous_agent.workspace,
            source_dir,
            destination_dir,
        )
        renamed_agent = replace(
            previous_agent,
            id=new_agent_id,
            workspace=str(renamed_workspace),
            updated_at=_utc_now(),
        )

        order_updated = False
        agent_config_updated = False
        sessions_retargeted = False
        tree_moved = False
        try:
            self._session_manager().retarget_identity_agent_sessions(agent_id, new_agent_id)
            sessions_retargeted = True
            workspace_ops._move_agent_tree(source_dir, destination_dir)
            tree_moved = True
            self._write_agent(renamed_agent)
            agent_config_updated = True
            if previous_order is not None:
                renamed_ids = tuple(
                    new_agent_id if listed.id == agent_id else listed.id
                    for listed in previous_listing.agents
                )
                self._write_agent_order(
                    _AgentOrderDocument(
                        agent_ids=renamed_ids,
                        revision=previous_order.revision + 1,
                    )
                )
                order_updated = True
        except Exception:
            if tree_moved:
                workspace_ops._move_agent_tree(destination_dir, source_dir)
            if sessions_retargeted:
                self._session_manager().retarget_identity_agent_sessions(new_agent_id, agent_id)
            if agent_config_updated:
                self._write_agent(previous_agent)
            raise

        return AgentRenameResult(
            agent=_apply_defaults(renamed_agent, self._agent_defaults()),
            previous_agent=previous_agent,
            previous_order=previous_order,
            order_updated=order_updated,
        )

    def restore_rename(self, result: AgentRenameResult) -> None:
        """Restore the exact pre-rename Agent tree and config snapshot."""
        self._session_manager().retarget_identity_agent_sessions(
            result.agent.id, result.previous_agent.id
        )
        try:
            workspace_ops._move_agent_tree(
                self._agent_dir(result.agent.id),
                self._agent_dir(result.previous_agent.id),
            )
        except Exception:
            self._session_manager().retarget_identity_agent_sessions(
                result.previous_agent.id, result.agent.id
            )
            raise
        self._write_agent(result.previous_agent)
        if result.order_updated:
            self._restore_agent_order(result.previous_order)

    def retarget_allowed_agent_references(
        self,
        old_agent_id: str,
        new_agent_id: str,
    ) -> AgentReferenceUpdateResult:
        """Retarget bare Identity Agent ids in every delegation allow-list.

        Project-qualified addresses such as ``builder@project`` name Config
        Agents and are a separate address space, so they are deliberately left
        untouched. Exact config snapshots make this mutation reversible without
        reconstructing prior list order or timestamps.
        """
        _validate_agent_id(old_agent_id)
        _validate_agent_id(new_agent_id)
        previous_agents: list[Agent] = []
        try:
            for listed_agent in self.list():
                agent = self.get_raw(listed_agent.id)
                tools = deepcopy(agent.tools)
                subagent = tools.get("subagent")
                if not isinstance(subagent, dict):
                    continue
                allowed_agents = subagent.get("allowed_agents")
                if not isinstance(allowed_agents, list) or old_agent_id not in allowed_agents:
                    continue
                retargeted = _replace_list_item_once(
                    allowed_agents,
                    old_agent_id,
                    new_agent_id,
                )
                subagent["allowed_agents"] = retargeted
                previous_agents.append(agent)
                self._write_agent(replace(agent, tools=tools, updated_at=_utc_now()))
        except Exception:
            for previous_agent in reversed(previous_agents):
                self._write_agent(previous_agent)
            raise
        return AgentReferenceUpdateResult(previous_agents=tuple(previous_agents))

    def restore_allowed_agent_references(self, result: AgentReferenceUpdateResult) -> None:
        """Restore exact Agent configs changed by a reference retarget."""
        for previous_agent in reversed(result.previous_agents):
            self._write_agent(previous_agent)

    def agents_rooted_in(self, project_id: str) -> builtins.list[Agent]:
        """Return Identity Agents explicitly referencing one Project."""
        return [
            self.get_raw(agent.id) for agent in self.list() if agent.root_project_id == project_id
        ]

    def delete(self, agent_id: str) -> Path:
        """Archive the agent directory, then remove the active copy.

        A default workspace lives inside the agent directory, so it travels into
        the archive with the first move; the ``exists`` check below is then False
        (its live path is already gone) and the second move is skipped. Only a
        custom workspace outside the agent tree (e.g. a repo an identity agent is
        rooted in) still exists after the first move and is archived beside it.
        """
        agent = self.get(agent_id)
        archive_dir = self._archive_dir(agent_id)
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{agent_id}-archive-",
            dir=archive_dir.parent,
            ignore_cleanup_errors=True,
        ) as backup_root:
            previous_archive = Path(backup_root) / "previous"
            if archive_dir.exists():
                shutil.move(str(archive_dir), str(previous_archive))
            try:
                archive_dir.mkdir()
                agent_archive = archive_dir / "agent"
                workspace_archive = archive_dir / "workspace"
                workspace_path = Path(agent.workspace)
                agent_moved = False
                workspace_moved = False
                try:
                    shutil.move(str(self._agent_dir(agent_id)), str(agent_archive))
                    agent_moved = True
                    if workspace_path.exists():
                        shutil.move(str(workspace_path), str(workspace_archive))
                        workspace_moved = True
                    self._session_manager().archive_identity_agent_sessions(agent_id)
                except Exception:
                    if workspace_moved:
                        shutil.move(str(workspace_archive), str(workspace_path))
                    if agent_moved:
                        shutil.move(str(agent_archive), str(self._agent_dir(agent_id)))
                    shutil.rmtree(archive_dir, ignore_errors=True)
                    if previous_archive.exists():
                        shutil.move(str(previous_archive), str(archive_dir))
                    raise
            except Exception:
                # Covers mkdir failure after previous archive was staged away.
                if not archive_dir.exists() and previous_archive.exists():
                    shutil.move(str(previous_archive), str(archive_dir))
                raise

        # Reconcile the collection document after the archive is committed. A
        # stale id is filtered even if persistence fails, so delete never reports
        # failure after the irreversible archive already succeeded.
        self.list_with_order()
        return archive_dir

    def reset_current_after_session_removed(self, agent_id: str, removed_session_id: str) -> Agent:
        """Re-point an identity agent's current session after one is gone.

        Invoked as the final step of removing a session from this home — a move
        to another agent, or a deletion. If the removed session was this agent's
        current session, the pointer lands on the most recently active
        *remaining* session (max ``last_active_at``), or a fresh empty session
        when none remain. If the removed session was not the current one, the
        pointer is left untouched.

        Reads the stored config side-effect-free (not through :meth:`get`):
        ``get`` would auto-create a fresh empty current session the instant it
        sees the pointer dangling at the just-removed id, preempting the
        last-active landing this method exists to provide.
        """
        _validate_agent_id(agent_id)
        agent_path = self._agent_path(agent_id)
        if not agent_path.exists():
            raise AgentNotFoundError(f"Agent not found: {agent_id}")

        agent = self._read_agent_config(agent_path)
        if agent.current_session_id != removed_session_id:
            return _apply_defaults(agent, self._agent_defaults())

        remaining = self._session_manager().list_with_metadata(agent_id)
        if remaining:
            newest: dict[str, Any] = max(remaining, key=lambda session: session["last_active_at"])
            landing_session_id = newest["id"]
        else:
            landing_session_id = self._session_manager().create(agent_id).id

        updated_agent = replace(agent, current_session_id=landing_session_id, updated_at=_utc_now())
        self._write_agent(updated_agent)
        return _apply_defaults(updated_agent, self._agent_defaults())

    def _agent_dir(self, agent_id: str) -> Path:
        return self._data_dir / "agents" / agent_id

    def _agent_path(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "agent.json"

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

    def _archive_dir(self, agent_id: str) -> Path:
        # Agent archives live under their own ``agents/`` subtree, mirroring the
        # legacy Session sources (``archive/sessions/``) and Project archives
        # (``archive/projects/``): a flat ``archive/<agent-id>`` would let an Agent named
        # ``sessions`` or ``projects`` collide with those roots — and delete's
        # replace-archive rmtree would then wipe them wholesale.
        return self._data_dir / "archive" / "agents" / agent_id

    def _write_agent(self, agent: Agent) -> None:
        with self._write_lock:
            agent_path = self._agent_path(agent.id)
            persisted = asdict(agent)
            persisted["tool_access"] = agent.tool_access.to_dict()
            if not persisted["tools"]:
                persisted.pop("tools")
            persisted["workspace"] = _workspace_for_storage(
                agent.workspace,
                data_dir=self._data_dir,
            )
            atomic_write_text(
                agent_path,
                json.dumps(persisted, ensure_ascii=False, indent=2) + "\n",
            )

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
        return _AgentOrderDocument(
            agent_ids=tuple(data["agent_ids"]),
            revision=data["revision"],
        )

    def _write_agent_order(self, order: _AgentOrderDocument) -> None:
        payload = {
            "revision": order.revision,
            "agent_ids": list(order.agent_ids),
        }
        with self._write_lock:
            atomic_write_text(
                self._agent_order_path(),
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            )
        self._reported_order_error = None

    def _restore_agent_order(self, order: _AgentOrderDocument | None) -> None:
        if order is None:
            self._agent_order_path().unlink(missing_ok=True)
            self._reported_order_error = None
            return
        self._write_agent_order(order)

    def _agent_defaults(self) -> AgentDefaults:
        if self._defaults_provider is None:
            return AgentDefaults()

        defaults = self._defaults_provider()
        if not isinstance(defaults, dict):
            raise AgentError("defaults provider must return a dictionary")
        return AgentDefaults.from_dict(defaults)

    def _load_raw_agent(self, agent_path: Path) -> Agent:
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
        return self._ensure_current_session(agent)

    def _read_agent_config(self, agent_path: Path) -> Agent:
        """Load and construct an agent from its config file with no side effects.

        Unlike :meth:`_load_raw_agent` this seeds no workspace and runs no
        current-session normalization, so a caller can inspect a dangling current
        pointer before it would otherwise be silently replaced.
        """
        data = _validated_agent_data(agent_path)
        return _agent_from_dict(
            data,
            data_dir=self._data_dir,
            default_workspace=self._default_workspace(data["id"]),
        )

    def _ensure_current_session(self, agent: Agent) -> Agent:
        if agent.current_session_id and self._session_exists(agent.id, agent.current_session_id):
            return agent

        session = self._session_manager().create(agent.id)
        updated_agent = replace(agent, current_session_id=session.id, updated_at=_utc_now())
        try:
            self._write_agent(updated_agent)
        except Exception:
            session.delete()
            raise
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
        if self._sessions is None:
            from core.sessions import ChatSessionManager
            from core.storage.layout import initialize_data_directory

            # Standalone/test usage: ensure a current-format marker exists for a
            # freshly created data directory without silently manufacturing
            # authorization for an already-initialized root that deliberately
            # lacks one. ``initialize_data_directory`` only writes the bootstrap
            # marker when it created the root itself.
            marker = self._data_dir / "session-store.json"
            if not marker.exists():
                with suppress(Exception):
                    initialize_data_directory(self._data_dir)
            self._sessions = ChatSessionManager(self._data_dir)
            self._owns_sessions = True
        return self._sessions

    def _seed_workspace(self, workspace_path: Path) -> None:
        workspace_ops.seed_workspace(self._template_dir, workspace_path)
