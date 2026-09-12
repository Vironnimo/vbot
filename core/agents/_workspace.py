"""Identity Workspace paths, template seeding and compensated file relocation."""

from __future__ import annotations

import os
import shutil
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.agents._types import (
    Agent,
    AgentAlreadyExistsError,
    AgentError,
)
from core.utils.logging import get_logger

WORKSPACE_TEMPLATE_FILES = ("SOUL.md",)

WORKSPACE_IDENTITY_FILES = ("SOUL.md", "USER.md", "MEMORY.md")


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


def _validate_workspace(workspace: str | Path) -> Path:
    if not isinstance(workspace, str | os.PathLike):
        raise AgentError("workspace must be a path string")
    if not str(workspace).strip():
        raise AgentError("workspace must be a non-empty path string")
    return Path(workspace)


def _resolve_workspace(workspace: str | Path, *, data_dir: str | Path) -> Path:
    workspace_path = _validate_workspace(workspace).expanduser()
    if not workspace_path.is_absolute():
        workspace_path = Path(data_dir) / workspace_path
    return workspace_path.resolve()


def _workspace_for_storage(workspace: str | Path, *, data_dir: str | Path) -> str:
    data_root = Path(data_dir).expanduser().resolve()
    workspace_path = _resolve_workspace(workspace, data_dir=data_root)
    try:
        return workspace_path.relative_to(data_root).as_posix()
    except ValueError:
        return str(workspace_path)


def _paths_are_same_location(left: Path, right: Path) -> bool:
    """Return whether two path spellings differ only by platform case rules."""
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _rebase_path_with_tree(value: str | Path, source: Path, destination: Path) -> Path:
    """Keep an in-tree path at the same relative location after a tree move."""
    path = Path(value).expanduser().resolve()
    source_root = source.resolve(strict=False)
    try:
        relative = path.relative_to(source_root)
    except ValueError:
        return path
    return (destination / relative).resolve(strict=False)


def _workspace_from_data(
    workspace: Any,
    *,
    data_dir: str | Path,
    default_workspace: str | Path | None,
) -> Path:
    if _is_missing_workspace(workspace):
        if default_workspace is None:
            raise AgentError("workspace must be a path string")
        return Path(default_workspace).resolve()
    return _resolve_workspace(workspace, data_dir=data_dir)


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


def seed_workspace(template_dir: Path, workspace_path: Path) -> None:
    workspace_path.mkdir(parents=True, exist_ok=True)
    for filename in WORKSPACE_TEMPLATE_FILES:
        target = workspace_path / filename
        if target.exists():
            continue
        template = template_dir / filename
        try:
            template_content = template.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            get_logger("agents").warning(
                "Skipping unreadable Workspace template %s: %s", template, error
            )
            continue
        target.write_text(template_content, encoding="utf-8")


def relocate_workspace(
    agent_dir: Path,
    template_dir: Path,
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
                    backup_dir = relocation.ensure_backup_dir(agent_dir)
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
            seed_workspace(template_dir, destination)
            relocation.created_files += ("SOUL.md",)
        return relocation
    except Exception:
        relocation.rollback()
        raise


def _move_agent_tree(source: Path, destination: Path) -> None:
    """Move one Agent tree, including a Windows-safe case-only rename."""
    if _paths_are_same_location(source, destination):
        temporary = source.with_name(f".{source.name}.rename-{uuid.uuid4().hex}.tmp")
        os.replace(source, temporary)
        try:
            os.replace(temporary, destination)
        except Exception:
            os.replace(temporary, source)
            raise
        return
    if destination.exists():
        raise AgentAlreadyExistsError(f"Agent already exists: {destination.name}")
    os.replace(source, destination)
