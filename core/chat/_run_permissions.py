"""The Permission Boundary of one Run, built once from its resolved Agent and Session.

The boundary rides from the Run's execution context to every Tool Call
(``ToolContext.permissions``). An Agent without rules, in a Session without an
inherited ceiling, gets ``PermissionBoundary.UNRESTRICTED`` before any
filesystem access: full-access Runs pay nothing for the feature.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from core.chat._scratch_context import SCRATCH_RUN_KINDS
from core.chat._tool_context import _agent_workspace, _resolve_tool_cwd
from core.chat.errors import ChatError
from core.permissions import (
    BoundaryContext,
    PermissionAccess,
    PermissionBoundary,
    PermissionCeiling,
)
from core.prompts import BLOCK_KIND_DATA, BLOCK_OWNER_ALWAYS, BlockDefinition
from core.storage.layout import DataDirectoryLayout
from core.utils.config import VBOT_ROOT
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat._run_state import ChatLoopDependencies
    from core.projects import Project
    from core.runs import Run
    from core.sessions import SessionAddress
    from core.skills import SkillRegistry

_LOGGER = get_logger("chat.permissions")

# Temporary-file categories (``core.storage.temp_files``) holding output vBot
# hands to Sessions: command and terminal logs, web pages, Sub-Agent activity.
_ARTIFACT_CATEGORIES = ("commands", "computer_use", "subagents", "terminals", "web_fetch")

_INVALID_CEILING = (
    "This Session's inherited permission rules cannot be read, so it does not run. "
    "Ask the user to check the Session."
)


@cache
def installation_dirs() -> tuple[Path, ...]:
    """vBot's installation and the running interpreter's environment, never writable.

    A source checkout (``.git`` beside the packages) is the user's working tree,
    not an installation: there only the interpreter environment is protected,
    so a restricted Agent may still change vBot's sources where its rules allow.
    """
    candidates: list[Path] = []
    if not (VBOT_ROOT / ".git").exists():
        candidates.append(VBOT_ROOT)
    for prefix in (sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix):
        if prefix:
            candidates.append(Path(prefix))
    candidates.append(Path(sys.executable).parent)
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def artifact_dirs(data_dir: Path) -> tuple[Path, ...]:
    """vBot-owned folders whose files a Session's Tools hand it to read."""
    layout = DataDirectoryLayout(data_dir)
    return (
        *(layout.temporary / category for category in _ARTIFACT_CATEGORIES),
        layout.speech,
    )


def _skill_dirs(skill_registry: SkillRegistry, allowed_skills: Sequence[str] | None) -> list[Path]:
    skills = (
        skill_registry.list_all()
        if allowed_skills is None
        else skill_registry.filter_allowed(list(allowed_skills))
    )
    return [skill.path.parent if skill.path.suffix else skill.path for skill in skills]


async def _session_ceiling(
    dependencies: ChatLoopDependencies, address: SessionAddress
) -> PermissionCeiling | None:
    """The ceiling a Sub-Agent Session inherited from the Session that started it."""
    try:
        overrides = await dependencies.agent_resolver.session_overrides_async(address)
        return overrides.ceiling()
    except ValueError as error:
        _LOGGER.warning("Unreadable inherited permission ceiling (session=%s)", address.session_id)
        raise ChatError(_INVALID_CEILING) from error


async def run_permission_boundary(
    dependencies: ChatLoopDependencies,
    run: Run,
    agent: Any,
    *,
    cron_ceiling: PermissionCeiling | None,
    address: SessionAddress,
    project_cwd: Path | None,
    prompt_project: Project | None,
    skill_registry: SkillRegistry,
    subagent_session: bool,
) -> PermissionBoundary:
    """Build the boundary of one Run: the Agent's rules plus every inherited ceiling.

    Ceilings come from the Session that started a Sub-Agent Session (stored in its
    Agent overrides), from the Cron job that fired this Run (``cron_ceiling``),
    and from the Extension that runs a temporary Agent.
    """
    rules = getattr(agent, "permission_rules", None)
    ceilings: list[PermissionCeiling] = []
    if subagent_session:
        inherited = await _session_ceiling(dependencies, address)
        if inherited is not None:
            ceilings.append(inherited)
    if cron_ceiling is not None:
        ceilings.append(cron_ceiling)
    extension_ceiling = getattr(agent, "permission_ceiling", None)
    if isinstance(extension_ceiling, PermissionCeiling):
        ceilings.append(extension_ceiling)
    if rules is None and not ceilings:
        return PermissionBoundary.UNRESTRICTED

    data_dir = Path(dependencies.storage.data_dir)
    workspace = _agent_workspace(agent, data_dir)
    scratch_root: Path | None = None
    scratch = dependencies.scratch_areas
    if scratch is not None and run.run_kind in SCRATCH_RUN_KINDS:
        from core.tools.scratch_areas import ScratchUnavailableError

        try:
            # The conversation's whole area: a Parent reads its Sub-Agents' folders.
            scratch_root = (await scratch.folder_for(address)).area
        except ScratchUnavailableError:
            scratch_root = None
    agent_workspace = getattr(agent, "workspace", "")
    context = BoundaryContext(
        workdir=_resolve_tool_cwd(project_cwd, workspace),
        home=Path.home(),
        data_dir=data_dir,
        project_root=Path(prompt_project.cwd) if prompt_project is not None else None,
        scratch_root=scratch_root,
        workspace=Path(agent_workspace) if agent_workspace else None,
        agent_config=_agent_config(agent, data_dir),
        install_dirs=installation_dirs(),
        readable_dirs=(
            *artifact_dirs(data_dir),
            *_skill_dirs(skill_registry, getattr(agent, "allowed_skills", None)),
        ),
        loopback_port=dependencies.server_port() if dependencies.server_port else None,
    )
    return PermissionBoundary.build(rules, context, ceilings=ceilings)


def _agent_config(agent: Any, data_dir: Path) -> Path | None:
    """The file that holds this Agent's rules, which it may never read or change.

    An Identity Agent's ``agent.json``; a Team member's repository profile.
    """
    source_path = getattr(agent, "source_path", None)
    if isinstance(source_path, Path):
        return source_path
    if getattr(agent, "workspace", "") and not hasattr(agent, "cwd"):
        return DataDirectoryLayout(data_dir).agents / str(agent.id) / "agent.json"
    return None


# The System Prompt block telling a restricted Agent its boundary. Chat adds it
# to the request's temporary blocks, so it is pinned per prompt epoch and a
# change within an epoch arrives as a ``[prompt-block-change]`` System Reminder.
PERMISSIONS_BLOCK_ID = "core:permissions"
_PERMISSIONS_HEADING = "## Permission rules"
_PERMISSIONS_LEAD = (
    "The user set permission rules for you: every Tool and command refuses access "
    "outside these limits."
)
_PERMISSIONS_LINES: Final[tuple[tuple[PermissionAccess, str, str], ...]] = (
    ("read", "You can read files only in: {items}.", "You cannot read any files."),
    ("write", "You can change files only in: {items}.", "You cannot change any files."),
    ("command", "You can run only these commands: {items}.", "You cannot run any commands."),
    ("web", "You can fetch only from: {items}.", "You cannot fetch from any host."),
)
_PERMISSIONS_DENIED = "These rules refuse access everywhere: {items}."


def permissions_block_text(boundary: PermissionBoundary) -> str:
    """The ``core:permissions`` text for *boundary*; empty when it limits nothing to list."""
    if not boundary.restricted:
        return ""
    lines: list[str] = []
    for access, listed, nothing in _PERMISSIONS_LINES:
        allowed = boundary.allowed_summary(access)
        if allowed is None:
            continue
        lines.append(listed.format(items=_items(allowed)) if allowed else nothing)
    denied = tuple(dict.fromkeys(rule.text for layer in boundary.layers for rule in layer.deny))
    if denied:
        lines.append(_PERMISSIONS_DENIED.format(items=_items(denied)))
    if not lines:
        return ""
    return "\n".join(
        (_PERMISSIONS_HEADING, "", _PERMISSIONS_LEAD, *(f"- {line}" for line in lines))
    )


def permissions_prompt_block(boundary: PermissionBoundary) -> BlockDefinition | None:
    """The Run's ``core:permissions`` block, or ``None`` for a boundary with nothing to show."""
    text = permissions_block_text(boundary)
    if not text:
        return None
    return BlockDefinition(
        id=PERMISSIONS_BLOCK_ID,
        owner=BLOCK_OWNER_ALWAYS,
        kind=BLOCK_KIND_DATA,
        default_text=text,
        default_rank=10_000,
    )


def _items(values: Sequence[str]) -> str:
    return ", ".join(f"`{value}`" for value in values)
