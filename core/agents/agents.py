"""Agent persistence and workspace lifecycle management."""

from __future__ import annotations

import builtins
import json
import os
import shutil
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from core.config_validation import (
    JsonConfigValidationError,
    JsonDiagnostic,
    JsonObject,
    JsonValidationReport,
    add_error,
    error_diagnostic,
    load_validated_json_file,
    validate_allowed_string,
    validate_json_file,
    validate_non_empty_string,
    validate_optional_path_string,
    validate_required_fields,
    validate_string,
    validate_string_list,
    warn_unknown_keys,
)
from core.memory import (
    DEFAULT_MEMORY_PROMPT_MODE,
    MEMORY_PROMPT_MODES,
    MemoryPromptMode,
    validate_memory_prompt_mode,
)
from core.sessions import ChatSessionManager
from core.settings import (
    SettingsValidationError,
    is_valid_agent_id,
    validate_temperature,
    validate_thinking_effort,
)
from core.settings.normalizers import normalize_compaction_policy
from core.settings.validation import (
    validate_optional_compaction_policy,
    validate_temperature_diagnostic,
    validate_thinking_effort_diagnostic,
)
from core.tools.availability import sanitize_configured_allowed_tools
from core.utils.atomic import atomic_write_text

DEFAULT_FALLBACK_MODEL = ""
DEFAULT_MODEL = ""
DEFAULT_TEMPERATURE: float | None = None
DEFAULT_THINKING_EFFORT: str | None = None
DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED = False
DEFAULT_ALLOWED_ITEMS = ("*",)
# Only SOUL.md is identity the agent domain owns and seeds. USER.md/MEMORY.md belong
# to the memory system and are created lazily on the first memory write, so a
# memory-off agent never gets them and deleting them does not resurrect them.
WORKSPACE_TEMPLATE_FILES = ("SOUL.md",)
WORKSPACE_IDENTITY_FILES = ("SOUL.md", "USER.md", "MEMORY.md")

_AGENT_CONFIG_FIELDS = frozenset(
    {
        "allowed_skills",
        "allowed_tools",
        "compaction_policy",
        "created_at",
        "current_session_id",
        "custom_system_prompt_enabled",
        "fallback_model",
        "id",
        "memory_prompt_mode",
        "model",
        "name",
        "root_project_id",
        "temperature",
        "thinking_effort",
        "updated_at",
        "workspace",
    }
)
_REQUIRED_AGENT_CONFIG_FIELDS = frozenset(
    {
        "allowed_skills",
        "allowed_tools",
        "created_at",
        "fallback_model",
        "id",
        "model",
        "name",
        "temperature",
        "thinking_effort",
        "updated_at",
    }
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_TEMPLATE_DIR = _PROJECT_ROOT / "resources" / "workspace-templates"


class AgentError(ValueError):
    """Base error for expected agent lifecycle failures."""


class AgentAlreadyExistsError(AgentError):
    """Raised when creating an agent whose ID already exists."""


class AgentNotFoundError(AgentError):
    """Raised when an agent cannot be found."""


class InvalidAgentIdError(AgentError):
    """Raised when an agent ID is unsafe for filesystem use."""


def validate_agent_file(agent_path: str | Path) -> JsonValidationReport:
    """Validate one persisted ``agent.json`` without consuming it."""
    return validate_json_file(agent_path, validate_agent_data, missing_ok=False)


def load_validated_agent_json(agent_path: str | Path) -> JsonObject:
    """Load one schema-valid ``agent.json`` mapping."""
    try:
        return cast(
            "JsonObject",
            load_validated_json_file(agent_path, validate_agent_data, missing_ok=False),
        )
    except JsonConfigValidationError as error:
        raise AgentError(str(error)) from error


def validate_agent_data(data: Any) -> list[JsonDiagnostic]:
    """Validate a decoded raw ``agent.json`` mapping."""
    diagnostics: list[JsonDiagnostic] = []
    if not isinstance(data, dict):
        return [error_diagnostic("$", f"Expected a JSON object, got {type(data).__name__}")]

    warn_unknown_keys(diagnostics, "$", data, _AGENT_CONFIG_FIELDS, "agent field")
    validate_required_fields(diagnostics, "$", data, _REQUIRED_AGENT_CONFIG_FIELDS)
    _validate_agent_config_id(diagnostics, "$.id", data.get("id"))
    validate_non_empty_string(diagnostics, "$.name", data.get("name"), required=True)
    validate_string(diagnostics, "$.model", data.get("model"), required=True)
    validate_string(diagnostics, "$.fallback_model", data.get("fallback_model"), required=True)
    validate_optional_path_string(diagnostics, "$.workspace", data.get("workspace"))
    validate_non_empty_string(
        diagnostics,
        "$.root_project_id",
        data.get("root_project_id"),
        required=False,
    )
    validate_temperature_diagnostic(
        diagnostics, "$.temperature", data.get("temperature"), allow_none=True
    )
    validate_thinking_effort_diagnostic(
        diagnostics,
        "$.thinking_effort",
        data.get("thinking_effort"),
        allow_none=True,
    )
    if "memory_prompt_mode" in data:
        validate_allowed_string(
            diagnostics,
            "$.memory_prompt_mode",
            data["memory_prompt_mode"],
            frozenset(MEMORY_PROMPT_MODES),
        )
    validate_string_list(diagnostics, "$.allowed_tools", data.get("allowed_tools"))
    validate_string_list(diagnostics, "$.allowed_skills", data.get("allowed_skills"))
    if "custom_system_prompt_enabled" in data and not isinstance(
        data["custom_system_prompt_enabled"], bool
    ):
        add_error(diagnostics, "$.custom_system_prompt_enabled", "must be a boolean")
    validate_optional_compaction_policy(
        diagnostics, data.get("compaction_policy"), "$.compaction_policy"
    )
    validate_string(diagnostics, "$.created_at", data.get("created_at"), required=True)
    validate_string(diagnostics, "$.updated_at", data.get("updated_at"), required=True)
    if "current_session_id" in data:
        validate_string(
            diagnostics, "$.current_session_id", data.get("current_session_id"), required=False
        )
    return diagnostics


def _validate_agent_config_id(diagnostics: list[JsonDiagnostic], path: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        add_error(diagnostics, path, "must be a non-empty string")
    elif not is_valid_agent_id(value):
        add_error(
            diagnostics,
            path,
            "must be 1-64 characters using only letters, numbers, hyphen, or underscore",
        )


def default_workspace_dir(data_dir: str | os.PathLike[str], agent_id: str) -> Path:
    """Return an agent's default identity home: ``agents/<id>/workspace/``.

    The single source of the workspace-location convention. Everything that must
    agree on where an agent's workspace lives by default — the store's create
    path, and the chat tool-cwd / ``@``-mention fallbacks — resolves through here
    so the convention can never drift across call sites. The workspace lives
    inside the agent directory, so the whole agent (config, sessions, prompts,
    private skills, identity) is one self-contained tree.
    """
    return Path(data_dir) / "agents" / agent_id / "workspace"


@dataclass(frozen=True)
class Agent:
    """Persisted agent configuration stored in ``agent.json``."""

    id: str
    name: str
    model: str
    fallback_model: str
    workspace: str
    temperature: float | None
    thinking_effort: str | None
    allowed_tools: list[str]
    allowed_skills: list[str]
    created_at: str
    updated_at: str
    root_project_id: str | None = None
    current_session_id: str = ""
    custom_system_prompt_enabled: bool = DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED
    memory_prompt_mode: MemoryPromptMode = DEFAULT_MEMORY_PROMPT_MODE
    compaction_policy: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentUpdateResult:
    """An Agent update plus non-persisted Workspace relocation metadata."""

    agent: Agent
    copied_files: tuple[str, ...] = ()
    backed_up_files: tuple[str, ...] = ()
    backup_dir: str | None = None
    created_files: tuple[str, ...] = field(default=(), repr=False)
    destination: str | None = field(default=None, repr=False)


class AgentStore:
    """CRUD store for persisted agent configs and workspaces."""

    def __init__(
        self,
        data_dir: str | Path,
        template_dir: str | Path | None = None,
        defaults_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._template_dir = (
            Path(template_dir) if template_dir is not None else _DEFAULT_TEMPLATE_DIR
        )
        self._defaults_provider = defaults_provider

    @property
    def data_dir(self) -> Path:
        """Root directory containing agents, workspaces, and archives."""
        return self._data_dir

    def create(
        self,
        agent_id: str,
        name: str,
        *,
        model: str = DEFAULT_MODEL,
        fallback_model: str = DEFAULT_FALLBACK_MODEL,
        workspace: str | Path | None = None,
        temperature: float | None = DEFAULT_TEMPERATURE,
        thinking_effort: str | None = DEFAULT_THINKING_EFFORT,
        memory_prompt_mode: MemoryPromptMode = DEFAULT_MEMORY_PROMPT_MODE,
        allowed_tools: list[str] | None = None,
        allowed_skills: list[str] | None = None,
        custom_system_prompt_enabled: bool = DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED,
        compaction_policy: dict[str, Any] | None = None,
    ) -> Agent:
        """Create and persist a new agent, sessions directory, and workspace."""
        self._validate_agent_id(agent_id)
        agent_dir = self._agent_dir(agent_id)
        if agent_dir.exists():
            raise AgentAlreadyExistsError(f"Agent already exists: {agent_id}")

        validated_name = _validate_string_field("name", name, allow_empty=False)
        validated_model = _validate_string_field("model", model, allow_empty=True)
        validated_fallback_model = _validate_string_field(
            "fallback_model", fallback_model, allow_empty=True
        )
        validated_temperature = _validate_temperature(temperature)
        validated_thinking_effort = _validate_thinking_effort(thinking_effort)
        validated_memory_prompt_mode = _validate_memory_prompt_mode(memory_prompt_mode)
        validated_allowed_tools = _validate_allowed_items("allowed_tools", allowed_tools)
        validated_allowed_skills = _validate_allowed_items("allowed_skills", allowed_skills)
        validated_custom_system_prompt_enabled = _validate_bool_field(
            "custom_system_prompt_enabled", custom_system_prompt_enabled
        )
        validated_compaction_policy = (
            normalize_compaction_policy(compaction_policy)
            if compaction_policy is not None
            else None
        )
        now = _utc_now()
        workspace_path = (
            _validate_workspace(workspace)
            if workspace is not None
            else self._default_workspace(agent_id)
        )

        agent_dir.mkdir(parents=True)
        session = self._session_manager().create(agent_id)
        agent = Agent(
            id=agent_id,
            name=validated_name,
            model=validated_model,
            fallback_model=validated_fallback_model,
            workspace=str(workspace_path.resolve()),
            root_project_id=None,
            temperature=validated_temperature,
            thinking_effort=validated_thinking_effort,
            memory_prompt_mode=validated_memory_prompt_mode,
            allowed_tools=validated_allowed_tools,
            allowed_skills=validated_allowed_skills,
            custom_system_prompt_enabled=validated_custom_system_prompt_enabled,
            compaction_policy=validated_compaction_policy,
            current_session_id=session.id,
            created_at=now,
            updated_at=now,
        )

        self._seed_workspace(Path(agent.workspace))
        self._write_agent(agent)
        return self._apply_defaults(agent, self._agent_defaults())

    def get(self, agent_id: str) -> Agent:
        """Load an agent from disk."""
        self._validate_agent_id(agent_id)
        agent_path = self._agent_path(agent_id)
        if not agent_path.exists():
            raise AgentNotFoundError(f"Agent not found: {agent_id}")

        raw_agent = self._load_raw_agent(agent_path)
        return self._apply_defaults(raw_agent, self._agent_defaults())

    def get_raw(self, agent_id: str) -> Agent:
        """Load an agent with its **un-baked** persisted values (no defaults applied).

        Same load path as :meth:`get` (workspace seeding, current-session
        normalization) but **without** the ``defaults.agent`` injection, so the
        returned Agent carries the raw ``model``/``fallback_model`` ("" when unset)
        and raw ``temperature``/``thinking_effort`` (``None`` when unset). This is the
        provenance seam the resolver's identity ``effective_config`` reads to tell an
        own persisted value from a baked global default; ``get``/``list``/``update``
        keep baking for every other consumer.
        """
        self._validate_agent_id(agent_id)
        agent_path = self._agent_path(agent_id)
        if not agent_path.exists():
            raise AgentNotFoundError(f"Agent not found: {agent_id}")

        return self._load_raw_agent(agent_path)

    def exists(self, agent_id: str) -> bool:
        """Return whether an identity agent with this id is persisted in the store.

        A cheap existence probe that never raises: an id failing the agent-id
        format cannot name a stored agent and yields ``False``, so gating callers
        (e.g. the runtime's private-skill layering) can probe ids that originate
        outside the identity store — such as project-team slugs — safely.
        """
        if not is_valid_agent_id(agent_id):
            return False
        return self._agent_path(agent_id).exists()

    def list(self) -> list[Agent]:
        """Return all persisted agents sorted by ID."""
        agents_dir = self._data_dir / "agents"
        if not agents_dir.exists():
            return []

        defaults = self._agent_defaults()
        agents: list[Agent] = []
        for agent_path in sorted(agents_dir.glob("*/agent.json")):
            raw_agent = self._load_raw_agent(agent_path)
            agents.append(self._apply_defaults(raw_agent, defaults))
        return agents

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
        self._validate_agent_id(agent_id)
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
            return AgentUpdateResult(self._apply_defaults(agent, self._agent_defaults()))

        allowed_fields = set(Agent.__dataclass_fields__) - {
            "id",
            "created_at",
            "updated_at",
        }
        unknown_fields = sorted(set(changes) - allowed_fields)
        if unknown_fields:
            raise AgentError(f"Unknown agent fields: {', '.join(unknown_fields)}")

        string_fields = {
            "name",
            "model",
            "fallback_model",
            "current_session_id",
        }
        for field_name in sorted(string_fields & set(changes)):
            changes[field_name] = _validate_string_field(
                field_name,
                changes[field_name],
                allow_empty=field_name in {"model", "fallback_model"},
            )
        if "workspace" in changes:
            changes["workspace"] = str(_validate_workspace(changes["workspace"]).resolve())
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
        if "allowed_tools" in changes:
            changes["allowed_tools"] = _validate_allowed_items(
                "allowed_tools", changes["allowed_tools"]
            )
        if "allowed_skills" in changes:
            changes["allowed_skills"] = _validate_allowed_items(
                "allowed_skills", changes["allowed_skills"]
            )
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
            return AgentUpdateResult(self._apply_defaults(agent, self._agent_defaults()))

        updated_agent = replace(agent, **changes, updated_at=_utc_now())
        relocation = _WorkspaceRelocation()
        try:
            if "workspace" in changes:
                relocation = self._relocate_workspace(
                    agent,
                    Path(updated_agent.workspace),
                    copy_identity_files=copy_workspace_identity_files,
                )
            self._write_agent(updated_agent)
        except Exception:
            relocation.rollback()
            raise
        return AgentUpdateResult(
            agent=self._apply_defaults(updated_agent, self._agent_defaults()),
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

    def agents_rooted_in(self, project_id: str) -> builtins.list[Agent]:
        """Return Identity Agents explicitly referencing one Project."""
        return [
            self.get_raw(agent.id) for agent in self.list() if agent.root_project_id == project_id
        ]

    def _relocate_workspace(
        self,
        agent: Agent,
        destination: Path,
        *,
        copy_identity_files: bool,
    ) -> _WorkspaceRelocation:
        source = Path(agent.workspace)
        destination_existed = destination.exists()
        destination.mkdir(parents=True, exist_ok=True)
        relocation = _WorkspaceRelocation(
            destination=destination,
            remove_destination_dir=not destination_existed,
        )
        try:
            if copy_identity_files:
                for filename in WORKSPACE_IDENTITY_FILES:
                    source_file = source / filename
                    if not source_file.is_file():
                        continue
                    destination_file = destination / filename
                    if destination_file.exists():
                        backup_dir = relocation.ensure_backup_dir(self._agent_dir(agent.id))
                        shutil.copy2(destination_file, backup_dir / filename)
                        relocation.backed_up_files += (filename,)
                    else:
                        relocation.created_files += (filename,)
                    temporary = destination / f".{filename}.{uuid.uuid4().hex}.tmp"
                    try:
                        shutil.copy2(source_file, temporary)
                        os.replace(temporary, destination_file)
                    finally:
                        temporary.unlink(missing_ok=True)
                    relocation.copied_files += (filename,)

            soul_path = destination / "SOUL.md"
            if not soul_path.exists():
                self._seed_workspace(destination)
                relocation.created_files += ("SOUL.md",)
            return relocation
        except Exception:
            relocation.rollback()
            raise

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
        if archive_dir.exists():
            shutil.rmtree(archive_dir)

        archive_dir.mkdir(parents=True)
        shutil.move(str(self._agent_dir(agent_id)), str(archive_dir / "agent"))

        workspace_path = Path(agent.workspace)
        if workspace_path.exists():
            shutil.move(str(workspace_path), str(archive_dir / "workspace"))

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
        self._validate_agent_id(agent_id)
        agent_path = self._agent_path(agent_id)
        if not agent_path.exists():
            raise AgentNotFoundError(f"Agent not found: {agent_id}")

        agent = self._read_agent_config(agent_path)
        if agent.current_session_id != removed_session_id:
            return self._apply_defaults(agent, self._agent_defaults())

        remaining = self._session_manager().list_with_metadata(agent_id)
        if remaining:
            newest = max(remaining, key=lambda session: session["last_active_at"])
            landing_session_id = newest["id"]
        else:
            landing_session_id = self._session_manager().create(agent_id).id

        updated_agent = replace(agent, current_session_id=landing_session_id, updated_at=_utc_now())
        self._write_agent(updated_agent)
        return self._apply_defaults(updated_agent, self._agent_defaults())

    def _agent_dir(self, agent_id: str) -> Path:
        return self._data_dir / "agents" / agent_id

    def _agent_path(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "agent.json"

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
        # session (``archive/sessions/``) and project (``archive/projects/``)
        # archive roots: a flat ``archive/<agent-id>`` would let an agent named
        # ``sessions`` or ``projects`` collide with those roots — and delete's
        # replace-archive rmtree would then wipe them wholesale.
        return self._data_dir / "archive" / "agents" / agent_id

    def _write_agent(self, agent: Agent) -> None:
        agent_path = self._agent_path(agent.id)
        atomic_write_text(
            agent_path, json.dumps(asdict(agent), ensure_ascii=False, indent=2) + "\n"
        )

    def _agent_defaults(self) -> dict[str, Any]:
        if self._defaults_provider is None:
            return {}

        defaults = self._defaults_provider()
        if not isinstance(defaults, dict):
            raise AgentError("defaults provider must return a dictionary")
        return defaults

    def _load_raw_agent(self, agent_path: Path) -> Agent:
        data = load_validated_agent_json(agent_path)
        workspace_missing = _is_missing_workspace(data.get("workspace"))
        agent = _agent_from_dict(data, default_workspace=self._default_workspace(data["id"]))
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
        data = load_validated_agent_json(agent_path)
        return _agent_from_dict(data, default_workspace=self._default_workspace(data["id"]))

    def _apply_defaults(self, agent: Agent, defaults: dict[str, Any]) -> Agent:
        changes: dict[str, Any] = {}

        if agent.model == "" and "model" in defaults:
            changes["model"] = _validate_string_field("model", defaults["model"], allow_empty=True)
        if agent.fallback_model == "" and "fallback_model" in defaults:
            changes["fallback_model"] = _validate_string_field(
                "fallback_model",
                defaults["fallback_model"],
                allow_empty=True,
            )
        if agent.temperature is None and "temperature" in defaults:
            changes["temperature"] = _validate_temperature(defaults["temperature"])
        if agent.thinking_effort is None and "thinking_effort" in defaults:
            changes["thinking_effort"] = _validate_thinking_effort(defaults["thinking_effort"])

        if not changes:
            return agent
        return replace(agent, **changes)

    def _ensure_current_session(self, agent: Agent) -> Agent:
        if agent.current_session_id and self._session_exists(agent.id, agent.current_session_id):
            return agent

        session = self._session_manager().create(agent.id)
        updated_agent = replace(agent, current_session_id=session.id, updated_at=_utc_now())
        self._write_agent(updated_agent)
        return updated_agent

    def _validate_current_session(self, agent_id: str, session_id: Any) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise AgentError("current_session_id must be a non-empty string")
        if not self._session_exists(agent_id, session_id):
            raise AgentError(f"current session does not exist: {session_id}")

    def _session_exists(self, agent_id: str, session_id: str) -> bool:
        return self._session_manager().exists(agent_id, session_id)

    def _session_manager(self) -> ChatSessionManager:
        return ChatSessionManager(self._data_dir)

    def _seed_workspace(self, workspace_path: Path) -> None:
        workspace_path.mkdir(parents=True, exist_ok=True)
        for filename in WORKSPACE_TEMPLATE_FILES:
            target = workspace_path / filename
            if target.exists():
                continue
            template = self._template_dir / filename
            target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")

    @staticmethod
    def _validate_agent_id(agent_id: str) -> None:
        if not is_valid_agent_id(agent_id):
            raise InvalidAgentIdError(
                "Agent id must be 1-64 characters using only letters, numbers, "
                "hyphen, or underscore"
            )


def _validate_string_field(field: str, value: Any, *, allow_empty: bool) -> str:
    if not isinstance(value, str):
        raise AgentError(f"{field} must be a string")
    if not allow_empty and not value:
        raise AgentError(f"{field} must be a non-empty string")
    return value


def _validate_temperature(value: Any) -> float | None:
    try:
        return validate_temperature(value, label="temperature", allow_none=True)
    except SettingsValidationError as exc:
        raise AgentError(str(exc)) from exc


def _validate_thinking_effort(value: Any) -> str | None:
    try:
        return validate_thinking_effort(value, label="thinking_effort", allow_none=True)
    except SettingsValidationError as exc:
        raise AgentError(str(exc)) from exc


def _validate_memory_prompt_mode(value: Any) -> MemoryPromptMode:
    if not isinstance(value, str):
        raise AgentError("memory_prompt_mode must be a string")
    try:
        return validate_memory_prompt_mode(value)
    except ValueError as exc:
        allowed = ", ".join(repr(item) for item in MEMORY_PROMPT_MODES)
        raise AgentError(f"memory_prompt_mode must be one of: {allowed}") from exc


def _validate_allowed_items(field: str, items: list[str] | None) -> list[str]:
    if items is None:
        return list(DEFAULT_ALLOWED_ITEMS)
    if not isinstance(items, list):
        raise AgentError(f"{field} must be a list of strings")
    if not all(isinstance(item, str) for item in items):
        raise AgentError(f"{field} must be a list of strings")
    if field == "allowed_tools":
        return sanitize_configured_allowed_tools(items)
    return list(items)


def _validate_bool_field(field: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise AgentError(f"{field} must be a boolean")
    return value


def _validate_workspace(workspace: str | Path) -> Path:
    if not isinstance(workspace, str | os.PathLike):
        raise AgentError("workspace must be a path string")
    if not str(workspace).strip():
        raise AgentError("workspace must be a non-empty path string")
    return Path(workspace)


def _validate_root_project_id(project_id: Any) -> str | None:
    if project_id is None:
        return None
    if not isinstance(project_id, str) or not project_id.strip():
        raise AgentError("root_project_id must be null or a non-empty string")
    return project_id


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _agent_from_dict(data: dict[str, Any], *, default_workspace: str | Path | None = None) -> Agent:
    """Build an Agent from a mapping already validated by ``load_validated_agent_json``.

    Field rules are enforced once by this domain's ``validate_agent_data`` at load
    time; this constructor only normalizes shapes (workspace fallback, tool
    sanitization, optional-field defaults) without re-validating.
    """
    temperature = data.get("temperature")
    return Agent(
        id=data["id"],
        name=data["name"],
        model=data["model"],
        fallback_model=data["fallback_model"],
        workspace=str(_workspace_from_data(data.get("workspace"), default_workspace)),
        root_project_id=data.get("root_project_id"),
        temperature=None if temperature is None else float(temperature),
        thinking_effort=data.get("thinking_effort"),
        memory_prompt_mode=cast(
            MemoryPromptMode, data.get("memory_prompt_mode", DEFAULT_MEMORY_PROMPT_MODE)
        ),
        allowed_tools=sanitize_configured_allowed_tools(data["allowed_tools"]),
        allowed_skills=list(data["allowed_skills"]),
        custom_system_prompt_enabled=data.get(
            "custom_system_prompt_enabled", DEFAULT_CUSTOM_SYSTEM_PROMPT_ENABLED
        ),
        compaction_policy=(
            dict(data["compaction_policy"])
            if isinstance(data.get("compaction_policy"), dict)
            else None
        ),
        current_session_id=data.get("current_session_id", ""),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


def _workspace_from_data(workspace: Any, default_workspace: str | Path | None) -> Path:
    if _is_missing_workspace(workspace):
        if default_workspace is None:
            raise AgentError("workspace must be a path string")
        return Path(default_workspace).resolve()
    return _validate_workspace(workspace)


def _is_missing_workspace(workspace: Any) -> bool:
    return workspace is None or workspace == ""


@dataclass
class _WorkspaceRelocation:
    destination: Path | None = None
    remove_destination_dir: bool = False
    copied_files: tuple[str, ...] = ()
    backed_up_files: tuple[str, ...] = ()
    created_files: tuple[str, ...] = ()
    backup_dir: Path | None = None

    def ensure_backup_dir(self, agent_dir: Path) -> Path:
        if self.backup_dir is None:
            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            self.backup_dir = agent_dir / "workspace-backups" / f"{timestamp}-{uuid.uuid4().hex}"
            self.backup_dir.mkdir(parents=True)
        return self.backup_dir

    def rollback(self) -> None:
        if self.destination is None:
            return
        for filename in self.backed_up_files:
            if self.backup_dir is not None:
                backup = self.backup_dir / filename
                if backup.exists():
                    shutil.copy2(backup, self.destination / filename)
        for filename in self.created_files:
            if filename not in self.backed_up_files:
                (self.destination / filename).unlink(missing_ok=True)
        if self.remove_destination_dir and self.destination.exists():
            with suppress(OSError):
                self.destination.rmdir()
