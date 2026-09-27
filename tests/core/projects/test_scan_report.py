"""Tests for scan-report assembly, finding taxonomy, and collision resolution."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

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


def test_clean_team_is_sorted_by_agent_id_without_findings() -> None:
    assert build_scan_report([]) == ([], ScanReport())

    files = [
        _ranked(0, _agent("zeta", "opencode", Path("/repo/zeta.md"))),
        _ranked(0, _agent("alpha", "opencode", Path("/repo/alpha.md"))),
    ]

    team, report = build_scan_report(files)

    assert [member.agent_id for member in team] == ["alpha", "zeta"]
    assert report.is_clean


_OPENCODE_A = (0, "builder", "opencode", "/repo/a_builder.md")
_OPENCODE_B = (0, "builder", "opencode", "/repo/b_builder.md")


@pytest.mark.parametrize(
    ("inputs", "winner", "loser"),
    [
        # Same id in one format: the lexicographically first filename wins,
        # independent of the input order.
        pytest.param(
            [_OPENCODE_B, _OPENCODE_A], "/repo/a_builder.md", "/repo/b_builder.md", id="filename"
        ),
        pytest.param(
            [_OPENCODE_A, _OPENCODE_B],
            "/repo/a_builder.md",
            "/repo/b_builder.md",
            id="filename-reversed-input",
        ),
        # Same id across formats: the lower format rank wins regardless of filename.
        pytest.param(
            [
                (1, "builder", "copilot", "/repo/aaa_builder.md"),
                (0, "builder", "opencode", "/repo/zzz_builder.md"),
            ],
            "/repo/zzz_builder.md",
            "/repo/aaa_builder.md",
            id="format-precedence",
        ),
    ],
)
def test_slug_collision_keeps_one_deterministic_winner(
    inputs: list[tuple[int, str, str, str]], winner: str, loser: str
) -> None:
    files = [
        _ranked(rank, _agent(agent_id, fmt, Path(path))) for rank, agent_id, fmt, path in inputs
    ]

    team, report = build_scan_report(files)

    assert [member.source_path for member in team] == [Path(winner)]
    assert [
        (finding.agent_id, finding.source_path)
        for finding in report.findings_of(FindingType.SLUG_COLLISION)
    ] == [("builder", Path(loser))]


def test_unslugifiable_name_becomes_finding() -> None:
    files = [_ranked_failure(0, Path("/repo/***.md"), "name cannot be slugified")]

    team, report = build_scan_report(files)

    assert team == []
    findings = report.findings_of(FindingType.UNSLUGIFIABLE_NAME)
    assert len(findings) == 1
    assert findings[0].source_path == Path("/repo/***.md")


@pytest.mark.parametrize(
    ("enrich", "finding"),
    [
        pytest.param(
            ScanReport.with_model_findings,
            ScanFinding(
                type=FindingType.BAD_MODEL,
                detail="model 'opencode-go/glm-5.1' not configured",
                agent_id="builder",
                source_path=Path("/repo/builder.md"),
            ),
            id="model",
        ),
        pytest.param(
            ScanReport.with_pointer_findings,
            ScanFinding(
                type=FindingType.ORPHAN,
                detail="default-agent 'gone' is not in the scanned team",
                agent_id="gone",
            ),
            id="pointer",
        ),
        pytest.param(
            ScanReport.with_tool_findings,
            ScanFinding(
                type=FindingType.UNAVAILABLE_TOOL,
                detail="tool 'extension_tool' is not currently registered",
            ),
            id="tool",
        ),
    ],
)
def test_enrichment_returns_a_new_report_with_appended_findings(
    enrich: Callable[[ScanReport, list[ScanFinding]], ScanReport], finding: ScanFinding
) -> None:
    _, report = build_scan_report(
        [_ranked(0, _agent("builder", "opencode", Path("/repo/builder.md")))]
    )

    enriched = enrich(report, [finding])

    assert report.is_clean  # the original report is immutable
    assert not enriched.is_clean
    assert enriched.findings_of(finding.type) == (finding,)
