"""Pinned prompt-epoch snapshots stored as Session prompt pins.

The rendered Skill catalog, Working Project Context, SOUL block, and
pinned-memory text are prompt-cache state: they stay byte-identical between
successful Compactions (Memory also refreshes when its mode changes) so ordinary
file changes cannot break the System Prompt prefix. Assembly reads and writes
those snapshots through the narrow dependency slice declared by
:class:`PinnedContextDependencies`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from core.memory import DEFAULT_MEMORY_PROMPT_MODE
from core.prompts.prompts import PinnedSkillCatalog

if TYPE_CHECKING:
    from core.prompts.prompts import ProjectPromptContext, SystemPromptManager
    from core.sessions import ChatSessionManager
    from core.skills.skills import SkillRegistry
    from core.tools.file_state import FileReadState

# Prompt-epoch Skill catalog snapshot (the rendered ``<available_skills>`` text),
# pinned on the Session so ordinary Runs reuse one stable prefix. A successful
# Compaction rescans Skill sources and replaces this snapshot.
PINNED_SKILL_CATALOG_SLOT = "pinned_skill_catalog"
# Rooted Identity Agent Working Project Context, rendered from the selected Project's
# identity and auto-load files and reused verbatim until the next Compaction.
PINNED_WORKING_PROJECT_CONTEXT_SLOT = "pinned_working_project_context"
# Session-pinned rendered SOUL block text and pinned-memory text: prompt-epoch
# snapshots like the Skill catalog above. The first request of an epoch renders
# them once from the workspace files; every later request reuses the exact text so
# on-disk changes cannot break the System Prompt prefix mid-epoch. Successful
# Compaction replaces all three snapshots when the new epoch starts.
PINNED_SOUL_CONTEXT_SLOT = "pinned_soul_context"
PINNED_MEMORY_FILES_SLOT = "pinned_memory_files"
# Qualifies the Project-dependent snapshots (Working Project Context and Skill
# catalog) with the Project they were rendered for. The working Project is
# re-resolved at every Run admission (a Rooted Identity Agent may be re-rooted
# mid-Session), so a pin rendered for another Project, or for none, re-renders.
PINNED_PROJECT_ATTRIBUTE = "working_project_id"


class PinnedContextDependencies(Protocol):
    """The dependency slice the pinned prompt-epoch assembly needs."""

    def get_system_prompts(self) -> SystemPromptManager:
        """Return the live System Prompt manager."""
        ...

    @property
    def sessions(self) -> ChatSessionManager:
        """Session service holding the pinned snapshots as prompt pins."""
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
    full-file replacement without a redundant ``read`` call.
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
    *,
    skill_project_id: str | None,
) -> PinnedSkillCatalog:
    """Return the current prompt epoch's Skill catalog, snapshotting on first build.

    The catalog text is stable between successful Compactions (pinned on the
    Session at its own ``project_id`` anchor), so an ordinary mid-epoch Skill
    write leaves the System Prompt prefix unchanged.
    Skill activation and ``/``-``$`` triggers still resolve the live registry.
    The snapshot is qualified with ``skill_project_id``, the Project whose Skill
    pool *skill_registry* resolves: when a re-rooted Identity Agent's Run resolves
    another Project (or none), the catalog re-renders from the current registry.
    A successful Compaction rescans every Skill source and replaces the snapshot;
    a new Session starts with a fresh snapshot too.
    """
    text = _pinned_epoch_text(
        dependencies,
        PINNED_SKILL_CATALOG_SLOT,
        agent_id,
        session_id,
        project_id,
        lambda: (
            dependencies.get_system_prompts()
            .render_skill_catalog(agent, skill_registry)
            .catalog_text
        ),
        attributes={PINNED_PROJECT_ATTRIBUTE: skill_project_id},
        text_key="catalog_text",
    )
    return PinnedSkillCatalog(catalog_text=text)


def prompt_epoch_pins(
    *,
    skill_catalog: PinnedSkillCatalog,
    skill_project_id: str | None,
    working_project_context: str | None,
    working_project_id: str | None,
    soul_context: str | None,
    memory_files_context: str | None,
    memory_prompt_mode: str | None,
) -> dict[str, dict[str, Any] | None]:
    """Return every prompt-epoch pin a new epoch starts with, by pin slot.

    A successful Compaction commits these with its checkpoint. Pins carry the
    same qualifiers the per-Run readers check, so the next Run reuses them
    exactly; a ``None`` value removes that pin.
    """
    pins: dict[str, dict[str, Any] | None] = {
        PINNED_SKILL_CATALOG_SLOT: {
            "catalog_text": skill_catalog.catalog_text,
            PINNED_PROJECT_ATTRIBUTE: skill_project_id,
        }
    }
    texts: tuple[tuple[str, str | None, dict[str, str | None]], ...] = (
        (
            PINNED_WORKING_PROJECT_CONTEXT_SLOT,
            working_project_context,
            {PINNED_PROJECT_ATTRIBUTE: working_project_id},
        ),
        (PINNED_SOUL_CONTEXT_SLOT, soul_context, {}),
        (PINNED_MEMORY_FILES_SLOT, memory_files_context, {"mode": memory_prompt_mode}),
    )
    for slot, text, qualifiers in texts:
        pins[slot] = None if text is None else {"text": text, **qualifiers}
    return pins


def _pinned_epoch_text(
    dependencies: PinnedContextDependencies,
    slot: str,
    agent_id: str,
    session_id: str,
    project_id: str | None,
    render: Callable[[], str],
    *,
    attributes: Mapping[str, str | None] | None = None,
    text_key: str = "text",
) -> str:
    """Return the prompt epoch's pinned text in *slot*, snapshotting on first build.

    The rendered text is stable between successful Compactions (pinned on the
    Session at its own ``project_id`` anchor), so an ordinary mid-epoch file
    change leaves the System Prompt prefix unchanged. A successful Compaction
    replaces the snapshot; a new Session starts with a fresh snapshot too.
    Attributes qualify the snapshot; a pin lacking an attribute or carrying a
    different value is replaced, re-rendering only this text (the Memory
    rendering mode, the Project of Project-dependent pins).
    """
    # Local import: core.sessions transitively imports core.chat at module load,
    # and core.chat imports this package back (runtime cycle).
    from core.sessions import SessionAddress

    address = SessionAddress(project_id=project_id, agent_id=agent_id, session_id=session_id)
    qualifiers = dict(attributes or {})

    def matching_text(pinned: object) -> str | None:
        if not isinstance(pinned, dict):
            return None
        pinned_text = pinned.get(text_key)
        if not isinstance(pinned_text, str):
            return None
        if any(key not in pinned or pinned[key] != value for key, value in qualifiers.items()):
            return None
        return pinned_text

    reused = matching_text(dependencies.sessions.prompt_pin(address, slot))
    if reused is not None:
        return reused
    text = render()
    # A concurrent first build may have pinned an acceptable value meanwhile;
    # every request of the epoch then uses that one.
    pinned = dependencies.sessions.ensure_prompt_pin(
        address,
        slot,
        {text_key: text, **qualifiers},
        lambda current: matching_text(current) is not None,
    )
    return matching_text(pinned) or text


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
    files. The snapshot is qualified with the working Project id, so a Rooted
    Identity Agent re-rooted to another Project re-renders it at its next Run.
    An unrooted Identity Agent has no Working Project block: a stale pin from an
    earlier Rooting is ignored (never shown) and replaced by the next Project's.
    """
    if prompt_project is None or project_context is None:
        return None

    read_paths: list[Path] = []
    text = _pinned_epoch_text(
        dependencies,
        PINNED_WORKING_PROJECT_CONTEXT_SLOT,
        agent_id,
        session_id,
        project_id,
        lambda: dependencies.get_system_prompts().render_working_project_context(
            project_context,
            on_read=read_paths.append,
        ),
        attributes={PINNED_PROJECT_ATTRIBUTE: project_context.project_id},
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

    text = _pinned_epoch_text(
        dependencies,
        PINNED_SOUL_CONTEXT_SLOT,
        agent_id,
        session_id,
        project_id,
        render,
    )
    stamp_prompt_files_read(dependencies.file_read_state, session_id, read_paths)
    return text


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

    text = _pinned_epoch_text(
        dependencies,
        PINNED_MEMORY_FILES_SLOT,
        agent_id,
        session_id,
        project_id,
        render,
        attributes={"mode": getattr(agent, "memory_prompt_mode", DEFAULT_MEMORY_PROMPT_MODE)},
    )
    stamp_prompt_files_read(dependencies.file_read_state, session_id, read_paths)
    return text
