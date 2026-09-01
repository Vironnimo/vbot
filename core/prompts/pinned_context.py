"""Pinned prompt-epoch snapshots stored in Session metadata.

The rendered Skill catalog, Working Project Context, SOUL block, and
pinned-memory text are prompt-cache state: they stay byte-identical between
successful Compactions so ordinary mid-epoch changes cannot break the System
Prompt prefix. Assembly reads and writes those snapshots through the narrow
dependency slice declared by :class:`PinnedContextDependencies`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from core.prompts.prompts import PinnedSkillCatalog

if TYPE_CHECKING:
    from core.prompts.prompts import ProjectPromptContext, SystemPromptManager
    from core.sessions import ChatSessionManager
    from core.skills.skills import SkillRegistry
    from core.tools.file_state import FileReadState

# Prompt-epoch Skill catalog snapshot (the rendered ``<available_skills>`` text),
# stored in Session metadata so ordinary Runs reuse one stable prefix. A successful
# Compaction rescans Skill sources and replaces this snapshot.
PINNED_SKILL_CATALOG_META_KEY = "pinned_skill_catalog"
# Rooted Identity Agent Working Project Context, rendered from the selected Project's
# identity and auto-load files and reused verbatim until the next Compaction.
PINNED_WORKING_PROJECT_CONTEXT_META_KEY = "pinned_working_project_context"
# Session-pinned rendered SOUL block text and pinned-memory text: prompt-epoch
# snapshots like the Skill catalog above. The first request of an epoch renders
# them once from the workspace files; every later request reuses the exact text so
# on-disk changes cannot break the System Prompt prefix mid-epoch. Successful
# Compaction replaces all three snapshots when the new epoch starts.
PINNED_SOUL_CONTEXT_META_KEY = "pinned_soul_context"
PINNED_MEMORY_FILES_META_KEY = "pinned_memory_files"


class PinnedContextDependencies(Protocol):
    """The dependency slice the pinned prompt-epoch assembly needs."""

    def get_system_prompts(self) -> SystemPromptManager:
        """Return the live System Prompt manager."""
        ...

    @property
    def sessions(self) -> ChatSessionManager:
        """Session metadata store holding the pinned snapshots."""
        ...

    @property
    def file_read_state(self) -> FileReadState:
        """Read-before-write state stamped with auto-injected prompt files."""
        ...


def stamp_prompt_files_read(
    file_state: FileReadState,
    session_id: str,
    paths: list[Path],
) -> None:
    """Register auto-injected prompt files as read-before-write for a session.

    Files whose content the System Prompt places into the model's context — SOUL,
    pinned-memory files, a Project's auto-load files, and workspace includes —
    are treated as already read, so the agent can edit one directly with
    ``write``/``edit`` without a redundant ``read`` call.
    The guard still forces a re-read if such a file changes on disk afterwards
    (its ``(mtime, size)`` no longer matches), so the "only while unchanged"
    contract holds. ``paths`` is the resolved-absolute-path list the prompt build
    reported; empty is a no-op. The explicit Project Tool stamps its own result
    files directly through the same ``FileReadState`` instance.
    """
    if not paths:
        return
    for path in paths:
        file_state.record_read(session_id, path)


def pinned_skill_catalog(
    dependencies: PinnedContextDependencies,
    agent_id: str,
    session_id: str,
    agent: Any,
    skill_registry: SkillRegistry,
    project_id: str | None,
) -> PinnedSkillCatalog:
    """Return the current prompt epoch's Skill catalog, snapshotting on first build.

    The catalog text is stable between successful Compactions (persisted in
    Session metadata under the Session's own ``project_id`` anchor), so an
    ordinary mid-epoch Skill write leaves the System Prompt prefix unchanged.
    Skill activation and ``/``-``$`` triggers still resolve the live registry.
    A successful Compaction rescans every Skill source and replaces the snapshot;
    a new Session starts with a fresh snapshot too.
    """
    # Local import: core.sessions transitively imports core.chat at module load,
    # and core.chat imports this package back (runtime cycle).
    from core.sessions import SessionAddress

    address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
    metadata = dependencies.sessions.get_metadata(address)
    pinned = metadata.get(PINNED_SKILL_CATALOG_META_KEY)
    if isinstance(pinned, dict) and isinstance(pinned.get("catalog_text"), str):
        return PinnedSkillCatalog(catalog_text=pinned["catalog_text"])
    snapshot = dependencies.get_system_prompts().render_skill_catalog(agent, skill_registry)
    selected = snapshot

    def update(current: dict[str, Any]) -> None:
        nonlocal selected
        pinned = current.get(PINNED_SKILL_CATALOG_META_KEY)
        if isinstance(pinned, dict) and isinstance(pinned.get("catalog_text"), str):
            selected = PinnedSkillCatalog(catalog_text=pinned["catalog_text"])
        else:
            current[PINNED_SKILL_CATALOG_META_KEY] = {"catalog_text": snapshot.catalog_text}

    dependencies.sessions.mutate_metadata(address, update)
    return selected


def _pinned_epoch_text(
    dependencies: PinnedContextDependencies,
    meta_key: str,
    agent_id: str,
    session_id: str,
    project_id: str | None,
    render: Callable[[], str],
) -> str:
    """Return the prompt epoch's pinned text under *meta_key*, snapshotting on first build.

    The rendered text is stable between successful Compactions (persisted in
    Session metadata under the Session's own ``project_id`` anchor), so an
    ordinary mid-epoch file change leaves the System Prompt prefix unchanged.
    A successful Compaction replaces the snapshot; a new Session starts with a
    fresh snapshot too.
    """
    # Local import: core.sessions transitively imports core.chat at module load,
    # and core.chat imports this package back (runtime cycle).
    from core.sessions import SessionAddress

    address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
    metadata = dependencies.sessions.get_metadata(address)
    pinned = metadata.get(meta_key)
    pinned_text = pinned.get("text") if isinstance(pinned, dict) else None
    if isinstance(pinned_text, str):
        return pinned_text
    text = render()
    selected = text

    def update(current: dict[str, Any]) -> None:
        nonlocal selected
        pinned = current.get(meta_key)
        pinned_text = pinned.get("text") if isinstance(pinned, dict) else None
        if isinstance(pinned_text, str):
            selected = pinned_text
        else:
            current[meta_key] = {"text": text}

    dependencies.sessions.mutate_metadata(address, update)
    return selected


def pinned_working_project_context(
    dependencies: PinnedContextDependencies,
    agent_id: str,
    session_id: str,
    prompt_project: Any | None,
    project_context: ProjectPromptContext | None,
    project_id: str | None,
) -> str | None:
    """Return the pinned Working Project Context for this prompt epoch.

    This snapshot governs only the automatic Working Project block and covers
    Rooted Identity Agents and Project Config Agents alike. The rest of the
    System Prompt keeps its existing live assembly behavior. A successful
    Compaction replaces the snapshot from the current Project and auto-load
    files. An unrooted Identity Agent has no Working Project block and no pin.
    """
    if prompt_project is None or project_context is None:
        return None

    read_paths: list[Path] = []
    text = _pinned_epoch_text(
        dependencies,
        PINNED_WORKING_PROJECT_CONTEXT_META_KEY,
        agent_id,
        session_id,
        project_id,
        lambda: dependencies.get_system_prompts().render_working_project_context(
            project_context,
            on_read=read_paths.append,
        ),
    )
    stamp_prompt_files_read(dependencies.file_read_state, session_id, read_paths)
    return text


def pinned_soul_context(
    dependencies: PinnedContextDependencies,
    agent_id: str,
    session_id: str,
    agent: Any,
    project_id: str | None,
) -> str | None:
    """Return the prompt epoch's pinned SOUL block text for an Identity Agent.

    ``None`` when the Agent has no Identity/Memory Workspace (a config agent):
    its SOUL block gates out regardless, so no pin is stored or needed.
    """
    if not getattr(agent, "workspace", None):
        return None
    read_paths: list[Path] = []

    def render() -> str:
        return dependencies.get_system_prompts().render_soul(
            agent,
            on_read=read_paths.append,
        )

    return _pinned_epoch_text(
        dependencies,
        PINNED_SOUL_CONTEXT_META_KEY,
        agent_id,
        session_id,
        project_id,
        render,
    )


def pinned_memory_files(
    dependencies: PinnedContextDependencies,
    agent_id: str,
    session_id: str,
    agent: Any,
    project_id: str | None,
) -> str | None:
    """Return the prompt epoch's pinned pinned-memory text for an Identity Agent.

    ``None`` when the Agent has no Identity/Memory Workspace (a config agent):
    its memory producer renders empty regardless, so no pin is stored or needed.
    """
    if not getattr(agent, "workspace", None):
        return None
    read_paths: list[Path] = []

    def render() -> str:
        return dependencies.get_system_prompts().render_memory_files(
            agent,
            on_read=read_paths.append,
        )

    return _pinned_epoch_text(
        dependencies,
        PINNED_MEMORY_FILES_META_KEY,
        agent_id,
        session_id,
        project_id,
        render,
    )
