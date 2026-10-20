"""Source detection, ordered selection and collision-safe refresh, owned by Projects."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from core.projects.sources._reading import SourceError, is_dir_strict, is_file_strict
from core.projects.sources.profile import AgentProfile, SourceSelection
from core.utils.file_status import is_link_status

if TYPE_CHECKING:
    from core.skills.skills import SkillScan

type SourceKind = Literal["agents", "skills", "instructions"]


class AgentAdapter(Protocol):
    source: str

    def scan(self, root: Path) -> list[AgentProfile]: ...

    def read(self, root: Path, selected: AgentProfile) -> AgentProfile | None: ...


@dataclass(frozen=True)
class SourceDefinition:
    id: str
    ecosystem: str
    kind: SourceKind
    paths: tuple[str, ...]


SOURCE_DEFINITIONS = (
    SourceDefinition(
        "opencode.agents",
        "opencode",
        "agents",
        (
            ".opencode/agents",
            ".opencode/agent",
            "opencode.json",
            "opencode.jsonc",
            ".opencode/opencode.json",
            ".opencode/opencode.jsonc",
        ),
    ),
    SourceDefinition("opencode.skills", "opencode", "skills", (".opencode/skills",)),
    SourceDefinition("claude.agents", "claude", "agents", (".claude/agents",)),
    SourceDefinition("claude.skills", "claude", "skills", (".claude/skills",)),
    SourceDefinition("shared.skills", "shared", "skills", (".agents/skills",)),
    SourceDefinition("codex.agents", "codex", "agents", (".codex/agents", ".codex/config.toml")),
    SourceDefinition("codex.skills", "codex", "skills", (".codex/skills",)),
    SourceDefinition("copilot.agents", "copilot", "agents", (".github/agents",)),
    SourceDefinition("copilot.skills", "copilot", "skills", (".github/skills",)),
    SourceDefinition("cursor.agents", "cursor", "agents", (".cursor/agents",)),
    SourceDefinition("cursor.skills", "cursor", "skills", (".cursor/skills",)),
    SourceDefinition("gemini.agents", "gemini", "agents", (".gemini/agents",)),
    SourceDefinition("gemini.skills", "gemini", "skills", (".gemini/skills",)),
    SourceDefinition(
        "claude.instructions", "claude", "instructions", ("CLAUDE.md", ".claude/CLAUDE.md")
    ),
    SourceDefinition("gemini.instructions", "gemini", "instructions", ("GEMINI.md",)),
    SourceDefinition(
        "copilot.instructions", "copilot", "instructions", (".github/copilot-instructions.md",)
    ),
)
SOURCE_CATALOG = {source.id: source for source in SOURCE_DEFINITIONS}

# A Skill folder the user added to a Project: ``folder:<path>``, the path
# repository-relative when the folder lies inside the repository, else absolute.
FOLDER_SOURCE_PREFIX = "folder:"


def folder_source_path(source_id: str) -> str | None:
    """The stored folder path of a Skill folder Source id, else ``None``."""
    if not source_id.startswith(FOLDER_SOURCE_PREFIX):
        return None
    return source_id.removeprefix(FOLDER_SOURCE_PREFIX) or None


def source_definition(source_id: str) -> SourceDefinition | None:
    """The definition of a catalog or Skill folder Source; ``None`` for unknown ids."""
    if (path := folder_source_path(source_id)) is not None:
        return SourceDefinition(source_id, "folder", "skills", (path,))
    return SOURCE_CATALOG.get(source_id)


def default_adapters() -> dict[str, AgentAdapter]:
    from core.projects.sources.codex import CodexAdapter
    from core.projects.sources.markdown import MarkdownAdapter
    from core.projects.sources.opencode import OpenCodeAdapter

    return {
        adapter.source: adapter
        for adapter in (
            OpenCodeAdapter(),
            MarkdownAdapter("claude", ".claude/agents"),
            CodexAdapter(),
            MarkdownAdapter("copilot", ".github/agents"),
            MarkdownAdapter("cursor", ".cursor/agents"),
            MarkdownAdapter("gemini", ".gemini/agents"),
        )
    }


@dataclass(frozen=True)
class DetectedSource:
    definition: SourceDefinition
    agents: tuple[AgentProfile, ...] = ()
    skill_names: frozenset[str] = frozenset()
    paths: tuple[str, ...] = ()
    problem: str | None = None

    def to_dict(self, *, enabled: bool = True) -> dict[str, Any]:
        return {
            "id": self.definition.id,
            "ecosystem": self.definition.ecosystem,
            "kind": self.definition.kind,
            "enabled": enabled,
            "detected": bool(self.paths),
            "paths": list(self.paths),
            "agents": len(self.agents),
            "skills": len(self.skill_names),
            "problem": self.problem,
        }


def detect_sources(
    root: Path,
    *,
    adapters: dict[str, AgentAdapter] | None = None,
    skill_scan: SkillScan | None = None,
    selections: list[SourceSelection] | None = None,
) -> list[DetectedSource]:
    """Detect the catalog Sources present in ``root`` plus the Skill folders ``selections`` add."""
    from core.skills import scan_skill_names

    adapters = default_adapters() if adapters is None else adapters
    definitions = [
        *SOURCE_DEFINITIONS,
        *(
            definition
            for selection in selections or ()
            if folder_source_path(selection.id) is not None
            and (definition := source_definition(selection.id)) is not None
        ),
    ]
    skill_folders = [path for item in definitions if item.kind == "skills" for path in item.paths]
    detected: list[DetectedSource] = []
    for definition in definitions:
        paths: list[str] = []
        try:
            for relative in definition.paths:
                path = root / relative
                if definition.kind == "skills" and _links_to_other(root, relative, skill_folders):
                    continue
                if is_dir_strict(path) or is_file_strict(path):
                    paths.append(relative)
            if not paths:
                continue
            agents: tuple[AgentProfile, ...] = ()
            skill_names: frozenset[str] = frozenset()
            if definition.kind == "agents":
                agents = tuple(adapters[definition.ecosystem].scan(root))
            elif definition.kind == "skills":
                skill_names = frozenset(
                    name
                    for relative in paths
                    for name in scan_skill_names(root / relative, scan=skill_scan)
                )
            detected.append(DetectedSource(definition, agents, skill_names, tuple(paths)))
        except (OSError, SourceError) as error:
            detected.append(
                DetectedSource(
                    definition, paths=tuple(paths or definition.paths), problem=str(error)
                )
            )
    return detected


def refresh_sources(
    selections: list[SourceSelection],
    detected: list[DetectedSource],
    *,
    root: Path | None = None,
    instructions_loaded: bool = False,
) -> list[SourceSelection]:
    """Append new sources without replacing any existing name's definition.

    Instruction files usually repeat each other (``CLAUDE.md`` often restates or
    imports ``AGENTS.md``), so a new instruction source starts active only while no
    instruction file loads yet; ``instructions_loaded`` reports the Project's own.
    """
    existing = {item.id for item in selections}
    active = {item.id for item in selections if item.enabled}
    by_id = {selection.id: selection for selection in selections}
    agent_names = {
        agent.agent_id
        for item in detected
        if item.definition.id in active
        for agent in item.agents
        if agent.agent_id
        and (root is None or by_id[item.definition.id].includes(root, agent.source_path))
    }
    skill_names = {
        name for item in detected if item.definition.id in active for name in item.skill_names
    }
    instructions = instructions_loaded or any(
        item.definition.kind == "instructions" and item.definition.id in active for item in detected
    )
    result = list(selections)
    for item in detected:
        if item.definition.id in existing:
            continue
        incoming = {agent.agent_id for agent in item.agents if agent.agent_id}
        if item.definition.kind == "instructions":
            enabled = not instructions
            instructions |= enabled
        else:
            enabled = not (incoming & agent_names or item.skill_names & skill_names)
        result.append(SourceSelection(item.definition.id, enabled))
        if enabled:
            agent_names |= incoming
            skill_names |= item.skill_names
    return result


def initial_sources(
    detected: list[DetectedSource], *, instructions_loaded: bool
) -> list[SourceSelection]:
    """Select every detected source for a new Project, with one instruction file at most."""
    selections = refresh_sources(
        [],
        [item for item in detected if item.definition.kind == "instructions"],
        instructions_loaded=instructions_loaded,
    )
    chosen = {item.id for item in selections if item.enabled}
    return [
        SourceSelection(
            item.definition.id,
            item.definition.kind != "instructions" or item.definition.id in chosen,
        )
        for item in detected
    ]


def skill_roots(root: Path, selections: list[SourceSelection]) -> list[Path]:
    folders = [
        path
        for selection in selections
        if selection.enabled
        if (definition := source_definition(selection.id)) is not None
        and definition.kind == "skills"
        for path in definition.paths
    ]
    return [root / path for path in folders if not _links_to_other(root, path, folders)]


def _links_to_other(root: Path, relative: str, folders: list[str]) -> bool:
    """Whether a folder is a link to another Skill folder, so both name one set.

    Repositories often link one tool's Skill folder to the shared one
    (``.claude/skills`` -> ``.agents/skills``); the real folder supplies them once.
    """
    path = root / relative
    try:
        if not is_link_status(path.lstat()):
            return False
        target = path.resolve()
    except OSError:
        return False
    return any(other != relative and (root / other).resolve() == target for other in folders)


def instruction_files(root: Path, selections: list[SourceSelection]) -> list[str]:
    return [
        path
        for selection in selections
        if selection.enabled
        if (definition := source_definition(selection.id)) is not None
        and definition.kind == "instructions"
        for path in definition.paths
        if is_file_strict(root / path)
    ]


def normalize_sources(value: Any) -> list[SourceSelection]:
    if not isinstance(value, list):
        raise ValueError("sources must be an ordered list.")
    seen: set[str] = set()
    result: list[SourceSelection] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - {"id", "enabled", "agent_paths"}:
            raise ValueError("Each source must contain id, enabled and optional agent_paths only.")
        source_id = entry.get("id")
        enabled = entry.get("enabled", True)
        if (
            not isinstance(source_id, str)
            or not source_id
            or source_id in seen
            or not isinstance(enabled, bool)
        ):
            raise ValueError("Sources require unique ids and boolean switches.")
        if source_id.startswith(FOLDER_SOURCE_PREFIX) and (
            folder_source_path(source_id) is None or "agent_paths" in entry
        ):
            raise ValueError("A Skill folder Source names its folder after 'folder:' only.")
        # Unknown ids remain inert for future adapters and are preserved on disk.
        seen.add(source_id)
        paths = entry.get("agent_paths")
        if paths is not None and (
            not isinstance(paths, list)
            or not all(isinstance(path, str) and path.strip() for path in paths)
        ):
            raise ValueError("agent_paths must be a list of repository-relative patterns.")
        result.append(SourceSelection(source_id, enabled, None if paths is None else tuple(paths)))
    return result


def adopt_folder_sources(
    root: Path, selections: list[SourceSelection], *, known: set[str]
) -> list[SourceSelection]:
    """Store each newly added Skill folder Source in its canonical form.

    Source ids in ``known`` are already stored and stay as they are, even when
    their folder is gone. A new folder must be an existing directory; inside the
    repository it is kept repository-relative, so it follows a re-pointed
    Project, and elsewhere absolute. Two Sources may not name one folder.
    """
    result: list[SourceSelection] = []
    added: list[str] = []
    for selection in selections:
        raw = folder_source_path(selection.id)
        if raw is None or selection.id in known:
            result.append(selection)
            continue
        folder = Path(raw).expanduser()
        folder = folder if folder.is_absolute() else root / folder
        if not is_dir_strict(folder):
            raise ValueError(f"Skill folder is not an existing directory: {raw}")
        # Normalized, not resolved: a folder reached through a link stays that link.
        folder = Path(os.path.normpath(folder))
        try:
            stored = folder.relative_to(root).as_posix()
        except ValueError:
            stored = folder.as_posix()
        added.append(stored)
        result.append(replace(selection, id=f"{FOLDER_SOURCE_PREFIX}{stored}"))
    claimed = {
        (root / path).resolve()
        for item in [
            *SOURCE_DEFINITIONS,
            *(source_definition(selection.id) for selection in selections if selection.id in known),
        ]
        if item is not None and item.kind == "skills"
        for path in item.paths
    }
    for path in added:
        target = (root / path).resolve()
        if target in claimed:
            raise ValueError(f"Skill folder {path} is already a Source of this Project.")
        claimed.add(target)
    return result
