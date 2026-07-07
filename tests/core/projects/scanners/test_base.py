"""Tests for the scanner registry, protocol, and scan orchestration."""

from __future__ import annotations

from pathlib import Path

from core.projects.scan_report import FindingType
from core.projects.scanners.base import (
    AgentDetector,
    DetectedFile,
    DetectorRegistration,
    ScannedAgent,
    build_default_registry,
    detect_project_formats,
    scan_project,
)
from core.projects.scanners.claude import (
    CLAUDE_AGENTS_SUBPATH,
    ClaudeDetector,
)
from core.projects.scanners.opencode import (
    OPENCODE_AGENTS_SUBPATH,
    OpenCodeDetector,
)


def _write_opencode_agent(project_root: Path, filename: str, content: str) -> None:
    agents_dir = project_root.joinpath(*OPENCODE_AGENTS_SUBPATH)
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / filename).write_text(content, encoding="utf-8")


def _write_claude_agent(project_root: Path, filename: str, content: str) -> None:
    agents_dir = project_root.joinpath(*CLAUDE_AGENTS_SUBPATH)
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / filename).write_text(content, encoding="utf-8")


def _write_skill(project_root: Path, skills_subpath: tuple[str, ...], name: str) -> None:
    skill_dir = project_root.joinpath(*skills_subpath) / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A test skill.\n---\nBody.\n",
        encoding="utf-8",
    )


class _FakeDetector:
    """A second detector for testing the pluggable seam and cross-format precedence."""

    def __init__(self, agents: list[ScannedAgent]) -> None:
        self._agents = agents

    @property
    def format_key(self) -> str:
        return "fake"

    def detect(self, project_root: Path) -> list[DetectedFile]:
        return [
            DetectedFile(source_path=agent.source_path, raw_name=agent.display_name, agent=agent)
            for agent in self._agents
        ]


def _fake_agent(agent_id: str, source_path: Path) -> ScannedAgent:
    return ScannedAgent(
        agent_id=agent_id,
        display_name=agent_id,
        description="",
        model="",
        temperature=None,
        body="",
        source_format="fake",
        source_path=source_path,
    )


def test_opencode_detector_satisfies_protocol() -> None:
    # The detector must structurally satisfy the runtime-checkable Protocol.
    assert isinstance(OpenCodeDetector(), AgentDetector)


def test_scanned_agent_defaults_thinking_effort_to_none() -> None:
    # The thinking_effort field is additive with a default, so existing
    # constructions that omit it stay valid (agent declares no effort).
    agent = _fake_agent("builder", Path("/repo/builder"))

    assert agent.thinking_effort is None


def test_scanned_agent_defaults_denied_tools_to_empty() -> None:
    # An agent that declares no denials turns nothing off (the project ceiling
    # applies whole). The field is an immutable frozenset.
    agent = _fake_agent("builder", Path("/repo/builder"))

    assert agent.denied_tools == frozenset()


def test_default_registry_has_opencode_first_then_claude() -> None:
    registry = build_default_registry()

    assert [(entry.detector.format_key, entry.rank) for entry in registry] == [
        ("opencode", 0),
        ("claude", 1),
    ]


def test_claude_detector_satisfies_protocol() -> None:
    assert isinstance(ClaudeDetector(), AgentDetector)


def test_scan_project_builds_deterministic_team_and_report(tmp_path: Path) -> None:
    # Arrange: a fixture repo with two well-formed agents.
    _write_opencode_agent(
        tmp_path,
        "builder.md",
        "---\ndescription: Builds.\nmodel: opencode-go/minimax-m3\n---\nBuilder body.\n",
    )
    _write_opencode_agent(
        tmp_path,
        "orchestrator.md",
        "---\ndescription: Orchestrates.\n---\nOrchestrator body.\n",
    )

    # Act
    result = scan_project(tmp_path)

    # Assert
    assert [member.agent_id for member in result.team] == ["builder", "orchestrator"]
    assert result.report.is_clean


def test_scan_empty_project_is_clean_empty_report(tmp_path: Path) -> None:
    result = scan_project(tmp_path)

    assert result.team == []
    assert result.report.is_clean


def test_scan_reports_unslugifiable_name(tmp_path: Path) -> None:
    # Valid on disk, slugifies to nothing (only separators) → finding, no team member.
    _write_opencode_agent(tmp_path, "___.md", "---\n---\nBody.\n")

    result = scan_project(tmp_path)

    assert result.team == []
    assert len(result.report.findings_of(FindingType.UNSLUGIFIABLE_NAME)) == 1


def test_scan_is_non_recursive(tmp_path: Path) -> None:
    # Nested repo with its own agents must not be swept into the parent's team.
    nested = tmp_path / "subproject"
    _write_opencode_agent(nested, "child.md", "---\n---\nBody.\n")
    _write_opencode_agent(tmp_path, "parent.md", "---\n---\nBody.\n")

    result = scan_project(tmp_path)

    assert [member.agent_id for member in result.team] == ["parent"]


def test_scan_resolves_cross_format_collision_by_precedence(tmp_path: Path) -> None:
    # OpenCode (rank 0) wins over the fake detector (rank 1) for the same id.
    _write_opencode_agent(tmp_path, "builder.md", "---\n---\nOpenCode builder.\n")
    fake = _FakeDetector([_fake_agent("builder", tmp_path / "fake_builder")])
    registry = [
        DetectorRegistration(detector=OpenCodeDetector(), rank=0),
        DetectorRegistration(detector=fake, rank=1),
    ]

    result = scan_project(tmp_path, registry=registry)

    assert len(result.team) == 1
    assert result.team[0].source_format == "opencode"
    assert len(result.report.findings_of(FindingType.SLUG_COLLISION)) == 1


def test_scan_with_custom_registry_runs_only_given_detectors(tmp_path: Path) -> None:
    # An OpenCode agent on disk is ignored when only the fake detector is registered.
    _write_opencode_agent(tmp_path, "builder.md", "---\n---\nBody.\n")
    fake = _FakeDetector([_fake_agent("solo", tmp_path / "solo")])
    registry = [DetectorRegistration(detector=fake, rank=0)]

    result = scan_project(tmp_path, registry=registry)

    assert [member.agent_id for member in result.team] == ["solo"]


def _write_both_formats(tmp_path: Path) -> None:
    """A fixture repo carrying agents in both formats."""
    _write_opencode_agent(tmp_path, "builder.md", "---\ndescription: oc\n---\nBody.\n")
    _write_claude_agent(tmp_path, "reviewer.md", "---\nname: reviewer\n---\nBody.\n")


def test_scan_filters_to_opencode_source_format(tmp_path: Path) -> None:
    # The project's single-format truth: an opencode project never sees Claude agents.
    _write_both_formats(tmp_path)

    result = scan_project(tmp_path, source_format="opencode")

    assert [member.agent_id for member in result.team] == ["builder"]
    assert all(member.source_format == "opencode" for member in result.team)


def test_scan_filters_to_claude_source_format(tmp_path: Path) -> None:
    _write_both_formats(tmp_path)

    result = scan_project(tmp_path, source_format="claude")

    assert [member.agent_id for member in result.team] == ["reviewer"]
    assert all(member.source_format == "claude" for member in result.team)


def test_scan_without_source_format_runs_all_detectors(tmp_path: Path) -> None:
    # None keeps the unfiltered all-detectors behavior.
    _write_both_formats(tmp_path)

    result = scan_project(tmp_path)

    assert sorted(member.agent_id for member in result.team) == ["builder", "reviewer"]


def test_detect_project_formats_reports_counts(tmp_path: Path) -> None:
    _write_both_formats(tmp_path)
    _write_claude_agent(tmp_path, "helper.md", "---\nname: helper\n---\nBody.\n")
    _write_skill(tmp_path, (".opencode", "skills"), "deploy")
    _write_skill(tmp_path, (".claude", "skills"), "review")
    _write_skill(tmp_path, (".claude", "skills"), "test")

    detection = detect_project_formats(tmp_path)

    assert detection.formats["opencode"].agents == 1
    assert detection.formats["opencode"].skills == 1
    assert detection.formats["claude"].agents == 2
    assert detection.formats["claude"].skills == 2
    assert detection.formats["opencode"].present
    assert detection.formats["claude"].present


def test_detect_project_formats_empty_repo_reports_absent_formats(tmp_path: Path) -> None:
    detection = detect_project_formats(tmp_path)

    assert detection.formats["opencode"].present is False
    assert detection.formats["claude"].present is False
    assert detection.agents_md is False
    assert detection.claude_md is None


def test_detect_project_formats_skills_alone_make_format_present(tmp_path: Path) -> None:
    # A format counts as present on ≥1 agent OR ≥1 skill.
    _write_skill(tmp_path, (".claude", "skills"), "review")

    detection = detect_project_formats(tmp_path)

    assert detection.formats["claude"].present
    assert detection.formats["claude"].agents == 0


def test_detect_project_formats_reports_context_files(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")

    detection = detect_project_formats(tmp_path)

    assert detection.agents_md is True
    assert detection.claude_md == "CLAUDE.md"


def test_detect_project_formats_finds_nested_claude_md(tmp_path: Path) -> None:
    # Repo-root CLAUDE.md wins; .claude/CLAUDE.md is the fallback location.
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")

    detection = detect_project_formats(tmp_path)

    assert detection.claude_md == ".claude/CLAUDE.md"
