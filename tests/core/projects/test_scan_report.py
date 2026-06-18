"""Tests for scan-report assembly, finding taxonomy, and collision resolution."""

from __future__ import annotations

from pathlib import Path

from core.projects.scan_report import (
    FindingType,
    ScanFinding,
    ScanReport,
    build_scan_report,
)
from core.projects.scanners.base import DetectedFile, RankedFile, ScannedAgent


def _agent(agent_id: str, source_format: str, source_path: Path) -> ScannedAgent:
    return ScannedAgent(
        agent_id=agent_id,
        display_name=agent_id,
        description="",
        model="",
        temperature=None,
        body="",
        source_format=source_format,
        source_path=source_path,
    )


def _ranked(rank: int, agent: ScannedAgent) -> RankedFile:
    return RankedFile(
        rank=rank,
        file=DetectedFile(
            source_path=agent.source_path,
            raw_name=agent.display_name,
            agent=agent,
        ),
    )


def _ranked_failure(rank: int, source_path: Path, reason: str) -> RankedFile:
    return RankedFile(
        rank=rank,
        file=DetectedFile(source_path=source_path, raw_name=source_path.stem, error_reason=reason),
    )


def test_empty_input_yields_empty_team_and_clean_report() -> None:
    team, report = build_scan_report([])

    assert team == []
    assert report.is_clean


def test_clean_team_has_no_findings() -> None:
    files = [
        _ranked(0, _agent("builder", "opencode", Path("/repo/builder.md"))),
        _ranked(0, _agent("planner", "opencode", Path("/repo/planner.md"))),
    ]

    team, report = build_scan_report(files)

    assert [member.agent_id for member in team] == ["builder", "planner"]
    assert report.is_clean


def test_team_sorted_by_agent_id() -> None:
    files = [
        _ranked(0, _agent("zeta", "opencode", Path("/repo/zeta.md"))),
        _ranked(0, _agent("alpha", "opencode", Path("/repo/alpha.md"))),
    ]

    team, _ = build_scan_report(files)

    assert [member.agent_id for member in team] == ["alpha", "zeta"]


def test_collision_winner_is_first_by_filename_within_format() -> None:
    # Same id from two files in the same format: lexicographically first filename wins.
    files = [
        _ranked(0, _agent("builder", "opencode", Path("/repo/b_builder.md"))),
        _ranked(0, _agent("builder", "opencode", Path("/repo/a_builder.md"))),
    ]

    team, report = build_scan_report(files)

    assert len(team) == 1
    assert team[0].source_path.name == "a_builder.md"
    collisions = report.findings_of(FindingType.SLUG_COLLISION)
    assert len(collisions) == 1
    assert collisions[0].agent_id == "builder"
    assert collisions[0].source_path == Path("/repo/b_builder.md")


def test_collision_winner_is_format_precedence_first() -> None:
    # Same id across formats: lower rank (OpenCode rank 0) wins regardless of filename.
    files = [
        _ranked(1, _agent("builder", "copilot", Path("/repo/aaa_builder.md"))),
        _ranked(0, _agent("builder", "opencode", Path("/repo/zzz_builder.md"))),
    ]

    team, report = build_scan_report(files)

    assert len(team) == 1
    assert team[0].source_format == "opencode"
    losers = report.findings_of(FindingType.SLUG_COLLISION)
    assert len(losers) == 1
    assert losers[0].source_path == Path("/repo/aaa_builder.md")


def test_collision_resolution_is_independent_of_input_order() -> None:
    # Reversing the input must not change the deterministic winner.
    forward = [
        _ranked(0, _agent("builder", "opencode", Path("/repo/a_builder.md"))),
        _ranked(0, _agent("builder", "opencode", Path("/repo/b_builder.md"))),
    ]
    reversed_input = list(reversed(forward))

    team_forward, _ = build_scan_report(forward)
    team_reversed, _ = build_scan_report(reversed_input)

    assert team_forward[0].source_path == team_reversed[0].source_path == Path("/repo/a_builder.md")


def test_unslugifiable_name_becomes_finding() -> None:
    files = [_ranked_failure(0, Path("/repo/***.md"), "name cannot be slugified")]

    team, report = build_scan_report(files)

    assert team == []
    findings = report.findings_of(FindingType.UNSLUGIFIABLE_NAME)
    assert len(findings) == 1
    assert findings[0].source_path == Path("/repo/***.md")


def test_with_model_findings_appends_bad_model_seam() -> None:
    _, report = build_scan_report(
        [_ranked(0, _agent("builder", "opencode", Path("/repo/builder.md")))]
    )

    model_finding = ScanFinding(
        type=FindingType.BAD_MODEL,
        detail="model 'opencode-go/glm-5.1' not configured",
        agent_id="builder",
        source_path=Path("/repo/builder.md"),
    )
    enriched = report.with_model_findings([model_finding])

    assert report.is_clean  # original is unchanged (immutable)
    assert enriched.findings_of(FindingType.BAD_MODEL) == (model_finding,)


def test_with_pointer_findings_appends_orphan_seam() -> None:
    _, report = build_scan_report([])

    orphan = ScanFinding(
        type=FindingType.ORPHAN,
        detail="default-agent 'gone' is not in the scanned team",
        agent_id="gone",
    )
    enriched = report.with_pointer_findings([orphan])

    assert report.is_clean
    assert enriched.findings_of(FindingType.ORPHAN) == (orphan,)


def test_report_immutability_keeps_originals_clean() -> None:
    report = ScanReport()

    enriched = report.with_model_findings([ScanFinding(type=FindingType.BAD_MODEL, detail="x")])

    assert report.is_clean
    assert not enriched.is_clean
