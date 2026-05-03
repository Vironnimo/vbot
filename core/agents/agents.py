"""Agent persistence and workspace lifecycle management."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_FALLBACK_MODEL = ""
DEFAULT_MODEL = ""
DEFAULT_TEMPERATURE = 0.1
DEFAULT_THINKING_EFFORT = ""
DEFAULT_ALLOWED_ITEMS = ("*",)
WORKSPACE_TEMPLATE_FILES = ("SOUL.md", "IDENTITY.md", "AGENTS.md", "USER.md")
AGENT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

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


@dataclass(frozen=True)
class Agent:
    """Persisted agent configuration stored in ``agent.json``."""

    id: str
    name: str
    model: str
    fallback_model: str
    workspace: str
    temperature: float
    thinking_effort: str
    allowed_tools: list[str]
    allowed_skills: list[str]
    created_at: str
    updated_at: str


class AgentStore:
    """CRUD store for persisted agent configs and workspaces."""

    def __init__(
        self,
        data_dir: str | Path,
        template_dir: str | Path | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._template_dir = (
            Path(template_dir) if template_dir is not None else _DEFAULT_TEMPLATE_DIR
        )

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
        temperature: float = DEFAULT_TEMPERATURE,
        thinking_effort: str = DEFAULT_THINKING_EFFORT,
        allowed_tools: list[str] | None = None,
        allowed_skills: list[str] | None = None,
    ) -> Agent:
        """Create and persist a new agent, sessions directory, and workspace."""
        self._validate_agent_id(agent_id)
        agent_dir = self._agent_dir(agent_id)
        if agent_dir.exists():
            raise AgentAlreadyExistsError(f"Agent already exists: {agent_id}")

        now = _utc_now()
        workspace_path = (
            Path(workspace) if workspace is not None else self._default_workspace(agent_id)
        )
        agent = Agent(
            id=agent_id,
            name=name,
            model=model,
            fallback_model=fallback_model,
            workspace=str(workspace_path.resolve()),
            temperature=temperature,
            thinking_effort=thinking_effort,
            allowed_tools=_copy_allowed_items(allowed_tools),
            allowed_skills=_copy_allowed_items(allowed_skills),
            created_at=now,
            updated_at=now,
        )

        agent_dir.mkdir(parents=True)
        self._sessions_dir(agent_id).mkdir()
        self._seed_workspace(Path(agent.workspace))
        self._write_agent(agent)
        return agent

    def get(self, agent_id: str) -> Agent:
        """Load an agent from disk."""
        self._validate_agent_id(agent_id)
        agent_path = self._agent_path(agent_id)
        if not agent_path.exists():
            raise AgentNotFoundError(f"Agent not found: {agent_id}")

        data = json.loads(agent_path.read_text(encoding="utf-8"))
        return _agent_from_dict(data)

    def list(self) -> list[Agent]:
        """Return all persisted agents sorted by ID."""
        agents_dir = self._data_dir / "agents"
        if not agents_dir.exists():
            return []

        agents: list[Agent] = []
        for agent_path in sorted(agents_dir.glob("*/agent.json")):
            agents.append(_agent_from_dict(json.loads(agent_path.read_text(encoding="utf-8"))))
        return agents

    def update(self, agent_id: str, **changes: Any) -> Agent:
        """Update mutable fields for an existing agent."""
        if "id" in changes and changes["id"] != agent_id:
            raise AgentError("Agent id is immutable")

        changes.pop("id", None)
        agent = self.get(agent_id)
        if not changes:
            return agent

        allowed_fields = set(Agent.__dataclass_fields__) - {"id", "created_at", "updated_at"}
        unknown_fields = sorted(set(changes) - allowed_fields)
        if unknown_fields:
            raise AgentError(f"Unknown agent fields: {', '.join(unknown_fields)}")

        if "workspace" in changes:
            changes["workspace"] = str(Path(changes["workspace"]).resolve())
        if "allowed_tools" in changes:
            changes["allowed_tools"] = list(changes["allowed_tools"])
        if "allowed_skills" in changes:
            changes["allowed_skills"] = list(changes["allowed_skills"])

        updated_agent = replace(agent, **changes, updated_at=_utc_now())
        self._write_agent(updated_agent)
        return updated_agent

    def delete(self, agent_id: str) -> Path:
        """Archive an agent directory and workspace, then remove active copies."""
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

    def _agent_dir(self, agent_id: str) -> Path:
        return self._data_dir / "agents" / agent_id

    def _agent_path(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "agent.json"

    def _sessions_dir(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / "sessions"

    def _default_workspace(self, agent_id: str) -> Path:
        return self._data_dir / f"workspace-{agent_id}"

    def _archive_dir(self, agent_id: str) -> Path:
        return self._data_dir / "archive" / agent_id

    def _write_agent(self, agent: Agent) -> None:
        agent_path = self._agent_path(agent.id)
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = agent_path.with_name(f".{agent_path.name}.tmp")
        temp_path.write_text(
            json.dumps(asdict(agent), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_path, agent_path)

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
        if not AGENT_ID_PATTERN.fullmatch(agent_id):
            raise InvalidAgentIdError(
                "Agent id must be 1-64 characters using only letters, numbers, "
                "hyphen, or underscore"
            )


def _copy_allowed_items(items: list[str] | None) -> list[str]:
    return list(DEFAULT_ALLOWED_ITEMS if items is None else items)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _agent_from_dict(data: dict[str, Any]) -> Agent:
    return Agent(
        id=data["id"],
        name=data["name"],
        model=data["model"],
        fallback_model=data["fallback_model"],
        workspace=data["workspace"],
        temperature=data["temperature"],
        thinking_effort=data["thinking_effort"],
        allowed_tools=list(data["allowed_tools"]),
        allowed_skills=list(data["allowed_skills"]),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )
