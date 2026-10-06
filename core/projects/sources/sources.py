"""Assemble one Team and a visible report from an ordered source list."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.projects.scan_report import FindingType, ScanFinding, ScanReport
from core.projects.sources.catalog import (
    SOURCE_CATALOG,
    AgentAdapter,
    DetectedSource,
    default_adapters,
    detect_sources,
)
from core.projects.sources.profile import AgentProfile, SourceSelection


@dataclass(frozen=True)
class ScanResult:
    team: list[AgentProfile]
    report: ScanReport
    shadowed: tuple[AgentProfile, ...] = ()
    sources: tuple[dict[str, Any], ...] = ()


def scan_project(
    root: Path,
    *,
    sources: list[SourceSelection],
    adapters: dict[str, AgentAdapter] | None = None,
    detected: list[DetectedSource] | None = None,
) -> ScanResult:
    detected = detect_sources(root, adapters=adapters) if detected is None else detected
    by_id = {item.definition.id: item for item in detected}
    winners: dict[str, AgentProfile] = {}
    skills: dict[str, str] = {}
    shadowed: list[AgentProfile] = []
    findings: list[ScanFinding] = []
    source_rows: list[dict[str, Any]] = []
    for selection in sources:
        item = by_id.get(selection.id)
        if item is None:
            definition = SOURCE_CATALOG.get(selection.id)
            source_rows.append(
                {
                    "id": selection.id,
                    "enabled": selection.enabled,
                    "detected": False,
                    "kind": definition.kind if definition else "unknown",
                    "ecosystem": definition.ecosystem if definition else "unknown",
                    "agents": 0,
                    "skills": 0,
                    "paths": [],
                }
            )
            continue
        source_rows.append(item.to_dict(enabled=selection.enabled))
        if selection.agent_paths is not None:
            source_rows[-1]["agent_paths"] = list(selection.agent_paths)
        if not selection.enabled:
            continue
        if item.problem:
            findings.append(ScanFinding(FindingType.INVALID_SOURCE, item.problem))
            continue
        for agent in item.agents:
            if not selection.includes(root, agent.source_path):
                shadowed.append(agent)
                findings.append(
                    ScanFinding(
                        FindingType.SOURCE_SCOPE,
                        "Definition is outside this Source's selected Agent paths; "
                        "expand its paths to include all detected definitions.",
                        agent.agent_id,
                        agent.source_path,
                    )
                )
                continue
            if not agent.agent_id:
                findings.append(
                    ScanFinding(
                        FindingType.UNSLUGIFIABLE_NAME,
                        agent.unavailable_reason or "Invalid Agent name.",
                        source_path=agent.source_path,
                    )
                )
                shadowed.append(agent)
            elif agent.agent_id in winners:
                shadowed.append(agent)
                winner = winners[agent.agent_id]
                findings.append(
                    ScanFinding(
                        FindingType.SLUG_COLLISION,
                        f"{agent.display_name} from {agent.source} is shadowed by "
                        f"{winner.source} ({winner.source_path.as_posix()}).",
                        agent.agent_id,
                        agent.source_path,
                    )
                )
            else:
                winners[agent.agent_id] = agent
                if agent.unavailable_reason:
                    findings.append(
                        ScanFinding(
                            FindingType.INVALID_SOURCE,
                            agent.unavailable_reason,
                            agent.agent_id,
                            agent.source_path,
                        )
                    )
        for name in sorted(item.skill_names):
            if name in skills:
                findings.append(
                    ScanFinding(
                        FindingType.SKILL_COLLISION,
                        f"Skill '{name}' from {selection.id} is shadowed by {skills[name]}.",
                    )
                )
            else:
                skills[name] = selection.id
    return ScanResult(
        sorted(winners.values(), key=lambda agent: agent.agent_id),
        ScanReport(tuple(findings)),
        tuple(shadowed),
        tuple(source_rows),
    )


def read_profile(
    root: Path,
    sources: list[SourceSelection],
    agent_id: str,
    adapters: dict[str, AgentAdapter] | None = None,
) -> AgentProfile | None:
    """Read live configuration, including unavailable winners, without scanning Skills."""
    adapters = default_adapters() if adapters is None else adapters
    for selection in sources:
        definition = SOURCE_CATALOG.get(selection.id)
        if not selection.enabled or definition is None or definition.kind != "agents":
            continue
        for agent in adapters[definition.ecosystem].scan(root):
            if agent.agent_id == agent_id and selection.includes(root, agent.source_path):
                return agent
    return None
