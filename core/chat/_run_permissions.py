"""The Permission Boundary of a Session, composed in one place.

:func:`session_permissions` gathers what a Session's boundary is built from:
the Agent's rules, every ceiling the Session inherited, its folders and the
files its rules come from. A Run (:func:`run_permission_boundary`), the
Session's description for the WebUI and a Cron watch check all start there, so
they never disagree on what a Session may reach.

The boundary rides from the Run's execution context to every Tool Call
(``ToolContext.permissions``). An Agent without rules, in a Session without an
inherited ceiling, gets ``PermissionBoundary.UNRESTRICTED`` without building
anything: full-access Runs pay nothing for the feature. Before each Tool round
:class:`RunPermissions` notices a changed rule source, and the Run rebuilds it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Final

from core.agents import Agent, TemporaryAgent, default_workspace_dir
from core.chat._scratch_context import SCRATCH_RUN_KINDS
from core.chat._workers import _CHAT_TRANSFORM_WORKERS
from core.chat.errors import ChatError
from core.json_documents import documents_written
from core.permissions import (
    BoundaryContext,
    PermissionAccess,
    PermissionBoundary,
    PermissionCeiling,
    PermissionRules,
)
from core.prompts import (
    BLOCK_KIND_DATA,
    BLOCK_OWNER_ALWAYS,
    BlockDefinition,
    ProjectPromptContext,
)
from core.storage.layout import DataDirectoryLayout
from core.storage.temp_files import artifact_owner, session_artifact_dirs
from core.utils.config import VBOT_ROOT
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat._run_state import ChatLoopDependencies
    from core.projects import Project, RuntimeAgent
    from core.runs import Run
    from core.sessions import SessionAddress
    from core.skills import SkillRegistry

_LOGGER = get_logger("chat.permissions")

_INVALID_CEILING = (
    "This Session's inherited permission rules cannot be read, so it does not run. "
    "Continue the work in a new Session."
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


@dataclass(frozen=True, slots=True)
class SessionPermissions:
    """What one Session's Permission Boundary is composed of, before it is built.

    ``rules`` are the Agent's own (``None``: they restrict nothing); ``ceilings``
    are the Session's stored ceiling and the one a temporary Agent's Extension
    imposes. ``agent_config`` is an Identity Agent's ``agent.json``, which the
    Session may neither read nor change; ``rule_files`` are a Team member's
    repository profile and settings files, readable but never writable.
    """

    rules: PermissionRules | None
    ceilings: tuple[PermissionCeiling, ...]
    workdir: Path
    project_root: Path | None
    workspace: Path | None
    agent_config: Path | None
    rule_files: tuple[Path, ...]
    allowed_skills: tuple[str, ...] | None

    @property
    def restricted(self) -> bool:
        return self.rules is not None or bool(self.ceilings)

    def without_agent_rules(self) -> SessionPermissions:
        """Only the ceilings, as a Cron watch check applies them: it runs no Agent."""
        return replace(self, rules=None)

    def placeholder_values(self) -> dict[str, str | None]:
        """The Agent's rule placeholders in this Session; ``{scratch}`` stays per conversation."""
        return {
            "workdir": str(self.workdir),
            "project": str(self.project_root) if self.project_root is not None else None,
            "workspace": str(self.workspace) if self.workspace is not None else None,
            "home": str(Path.home()),
        }

    def rule_sources(self) -> tuple[Path, ...]:
        """The files whose change can change this boundary without a vBot write."""
        own = (self.agent_config,) if self.agent_config is not None else ()
        return (*own, *self.rule_files)

    def boundary(
        self,
        data_dir: Path,
        *,
        owner: SessionAddress | None,
        scratch_root: Path | None = None,
        skill_dirs: Sequence[Path] = (),
        loopback_port: int | None = None,
        own_hosts: Callable[[], tuple[str, ...]] | None = None,
    ) -> PermissionBoundary:
        """Build the boundary; ``owner`` grants that Session's own temporary files.

        ``own_hosts`` returns what else reaches the server on ``loopback_port``
        (``BoundaryContext.own_hosts``); only a restricted Session asks it.
        Blocking for a restricted Session: rule paths resolve on the filesystem.
        """
        if not self.restricted:
            return PermissionBoundary.UNRESTRICTED
        artifacts = (
            session_artifact_dirs(
                data_dir, artifact_owner(owner.project_id, owner.agent_id, owner.session_id)
            )
            if owner is not None
            else ()
        )
        context = BoundaryContext(
            workdir=self.workdir,
            home=Path.home(),
            data_dir=data_dir,
            project_root=self.project_root,
            scratch_root=scratch_root,
            workspace=self.workspace,
            agent_config=self.agent_config,
            rule_files=self.rule_files,
            install_dirs=installation_dirs(),
            readable_dirs=(*artifacts, *skill_dirs),
            loopback_port=loopback_port,
            own_hosts=own_hosts() if own_hosts is not None and loopback_port is not None else (),
        )
        return PermissionBoundary.build(self.rules, context, ceilings=self.ceilings)


def session_permissions(
    agent: RuntimeAgent,
    *,
    data_dir: Path,
    project_root: Path | None,
    ceiling: PermissionCeiling | None,
) -> SessionPermissions:
    """Compose the boundary of a Session of *agent* working in *project_root*.

    ``ceiling`` is the Session's stored one (``AgentOverrides.ceiling``). The
    working directory follows Tool path resolution: a temporary Agent's own
    folder, else the Working Project's repository, else the Agent's Workspace.
    """
    rules = agent.permission_rules
    if rules is not None and not rules.restricts:
        rules = None
    ceilings = tuple(item for item in (ceiling, agent.permission_ceiling) if item is not None)
    workspace = Path(agent.workspace) if agent.workspace else None
    workdir: Path
    if isinstance(agent, TemporaryAgent):
        workdir = agent.cwd
    elif project_root is not None:
        workdir = project_root
    else:
        workdir = workspace or default_workspace_dir(data_dir, agent.id)
    allowed = agent.allowed_skills
    return SessionPermissions(
        rules=rules,
        ceilings=ceilings,
        workdir=workdir,
        project_root=project_root,
        workspace=workspace,
        agent_config=(
            DataDirectoryLayout(data_dir).agents / agent.id / "agent.json"
            if isinstance(agent, Agent)
            else None
        ),
        rule_files=tuple(agent.rule_files),
        allowed_skills=tuple(allowed) if allowed is not None else None,
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
    """The ceiling the Session stores: a Parent's, a Cron job's, or both joined."""
    try:
        overrides = await dependencies.agent_resolver.session_overrides_async(address)
        return overrides.ceiling()
    except ValueError as error:
        _LOGGER.warning("Unreadable inherited permission ceiling (session=%s)", address.session_id)
        raise ChatError(_INVALID_CEILING) from error


# A rule source file's state: (modification time, size), or None when missing.
_Stamp = tuple[int, int] | None


def _stamps(paths: Sequence[Path]) -> tuple[_Stamp, ...]:
    """Blocking: the state of each rule source file."""
    stamps: list[_Stamp] = []
    for path in paths:
        try:
            status = os.stat(path)
        except OSError:
            stamps.append(None)
        else:
            stamps.append((status.st_mtime_ns, status.st_size))
    return tuple(stamps)


@dataclass(frozen=True, slots=True)
class RunPermissions:
    """A Run's boundary and what it was built from, to notice a change of its rules.

    ``documents`` counts the configuration documents vBot had written
    (:func:`core.json_documents.documents_written`): every rule edit through
    vBot changes it, so even a full-access Run notices one without any I/O. A
    restricted Run also compares its rule source files, which the user may edit
    outside vBot.
    """

    boundary: PermissionBoundary
    documents: int
    address: SessionAddress
    project_root: Path | None
    sources: tuple[Path, ...] = ()
    stamps: tuple[_Stamp, ...] = ()

    async def changed(self) -> bool:
        """Whether a rule source may have changed since the build."""
        if documents_written() != self.documents:
            return True
        if not self.boundary.restricted or not self.sources:
            return False
        return await _CHAT_TRANSFORM_WORKERS.run(_stamps, self.sources) != self.stamps


async def run_permission_boundary(
    dependencies: ChatLoopDependencies,
    run: Run,
    agent: RuntimeAgent,
    *,
    address: SessionAddress,
    project_root: Path | None,
    skill_registry: SkillRegistry,
) -> RunPermissions:
    """Build the boundary of one Run: the Agent's rules plus every inherited ceiling.

    The Session stores the ceilings of whoever caused its Runs (the Session that
    started a Sub-Agent Session, the Cron jobs that fired into it); a temporary
    Agent carries its Extension's. The document count is read before anything
    else, so an edit during the build still shows at the next Tool round.
    """
    documents = documents_written()
    ceiling = await _session_ceiling(dependencies, address)
    data_dir = Path(dependencies.storage.data_dir)
    permissions = session_permissions(
        agent, data_dir=data_dir, project_root=project_root, ceiling=ceiling
    )
    if not permissions.restricted:
        return RunPermissions(PermissionBoundary.UNRESTRICTED, documents, address, project_root)
    scratch_root: Path | None = None
    scratch = dependencies.scratch_areas
    if scratch is not None and run.run_kind in SCRATCH_RUN_KINDS:
        from core.tools.scratch_areas import ScratchUnavailableError

        try:
            # The conversation's whole area: a Parent reads its Sub-Agents' folders.
            scratch_root = (await scratch.folder_for(address)).area
        except ScratchUnavailableError:
            scratch_root = None
    port = dependencies.server_port() if dependencies.server_port else None
    return await _CHAT_TRANSFORM_WORKERS.run(
        _build_run_permissions,
        permissions,
        documents,
        data_dir,
        address,
        project_root,
        scratch_root,
        skill_registry,
        port,
        dependencies.server_hosts,
    )


def _build_run_permissions(
    permissions: SessionPermissions,
    documents: int,
    data_dir: Path,
    address: SessionAddress,
    project_root: Path | None,
    scratch_root: Path | None,
    skill_registry: SkillRegistry,
    port: int | None,
    own_hosts: Callable[[], tuple[str, ...]] | None,
) -> RunPermissions:
    """Blocking: the sources' stamps are taken before the boundary is built from them."""
    sources = permissions.rule_sources()
    stamps = _stamps(sources)
    boundary = permissions.boundary(
        data_dir,
        owner=address,
        scratch_root=scratch_root,
        skill_dirs=_skill_dirs(skill_registry, permissions.allowed_skills),
        loopback_port=port,
        own_hosts=own_hosts,
    )
    return RunPermissions(boundary, documents, address, project_root, sources, stamps)


def permitted_project_context(
    project: Project, boundary: PermissionBoundary, cwd: Path | None = None
) -> ProjectPromptContext:
    """The Working Project frame's input, with the instruction files *boundary* lets a Run read.

    A file the boundary refuses (a deny rule, a link out of the readable area)
    is left out, as the ``project`` Tool leaves it out, so its content never
    reaches the System Prompt. *cwd* replaces the Project's directory where the
    Run works elsewhere. Blocking: resolves links.
    """
    root = cwd if cwd is not None else Path(project.cwd)
    files = [name for name in project.auto_load if boundary.check_path(root / name, "read") is None]
    return ProjectPromptContext.from_project(project.project_id, project.display_name, root, files)


# The System Prompt block telling a restricted Agent its boundary. Chat adds it
# to the request's temporary blocks, so it is pinned per prompt epoch and a
# change within an epoch arrives as a ``[prompt-block-change]`` System Reminder.
PERMISSIONS_BLOCK_ID = "core:permissions"
_PERMISSIONS_HEADING = "## Permission rules"
_PERMISSIONS_LEAD = "The user set permission rules for you{inherited}."
_PERMISSIONS_SCOPE = (
    "vBot's Tools and commands refuse access outside these limits, and the limits also "
    "apply to scripts and programs that vBot does not check."
)
# Where a ceiling's rules come from, as the lead names them.
_INHERITED_FROM: Final[dict[str, str]] = {
    "parent": "the Session that started you",
    "cron": "the Session that scheduled this job",
    "extension": "the Extension that runs you",
}
# What stays readable beyond the listed folders: the Agent's Skill folders and
# the files vBot's Tool results hand it (command logs, fetched pages, media).
_ALSO_READABLE = "your Skills and the files vBot's Tool results point you to"
_PERMISSIONS_LINES: Final[tuple[tuple[PermissionAccess, str, str], ...]] = (
    (
        "read",
        f"You can read files only in: {{items}}, plus {_ALSO_READABLE}.",
        f"You can read only {_ALSO_READABLE}.",
    ),
    ("write", "You can change files only in: {items}.", "You cannot change any files."),
    ("command", "You can run only these commands: {items}.", "You cannot run any commands."),
    ("web", "You can fetch only from: {items}.", "You cannot fetch from any host."),
)
_PERMISSIONS_DENIED = "Always refused, even inside these folders: {items}."
_PERMISSIONS_DENIED_ONLY = "Always refused: {items}."


def permissions_block_text(boundary: PermissionBoundary) -> str:
    """The ``core:permissions`` text for *boundary*; empty when it limits nothing to list."""
    if not boundary.restricted:
        return ""
    lines: list[str] = []
    folders = False
    for access, listed, nothing in _PERMISSIONS_LINES:
        allowed = boundary.allowed_summary(access)
        if allowed is None:
            continue
        folders = folders or access in ("read", "write")
        lines.append(listed.format(items=_items(allowed)) if allowed else nothing)
    denied = tuple(dict.fromkeys(rule.text for layer in boundary.layers for rule in layer.deny))
    if denied:
        template = _PERMISSIONS_DENIED if folders else _PERMISSIONS_DENIED_ONLY
        lines.append(template.format(items=_items(denied)))
    if not lines:
        return ""
    sources = dict.fromkeys(
        _INHERITED_FROM[layer.source] for layer in boundary.layers if layer.source != "agent"
    )
    inherited = f", including those of {' and '.join(sources)}" if sources else ""
    lead = f"{_PERMISSIONS_LEAD.format(inherited=inherited)} {_PERMISSIONS_SCOPE}"
    return "\n".join((_PERMISSIONS_HEADING, "", lead, *(f"- {line}" for line in lines)))


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
