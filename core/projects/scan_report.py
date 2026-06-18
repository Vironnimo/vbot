"""Scan report: finding taxonomy, deterministic collision resolution, assembly.

The scan of a project is a **visible report**, not a silent step (see
add-projects.md → "Projekt anlegen: Ordner scannen"). It collects only what is
*unclean under what already exists* — an empty folder, no Team, and no AGENTS.md
are all **normal** and produce a clean, empty report.

Finding taxonomy (every finding points at a source file or pointer so it is
actionable):

- ``BAD_MODEL`` — a scanned agent's model does not exist / is not configured in
  this instance. The detector never judges the model and this module has **no**
  access to the model/provider registry, so it does *not* decide this itself.
  The later resolver builder, which has the model context, appends these via
  :func:`ScanReport.with_model_findings`. The finding *type* and the insertion
  seam live here so the resolver builds on a stable contract.
- ``SLUG_COLLISION`` — two source files resolve to the same ``agent_id``. The
  deterministic winner is on the Team; every loser is a finding.
- ``UNSLUGIFIABLE_NAME`` — a source file's name cannot become a valid
  ``agent_id``.
- ``ORPHAN`` — a default-agent pointer (or, supplied by callers, a session
  pointer) names an ``agent_id`` that the current scan did not produce. Surfaced
  via :func:`ScanReport.with_pointer_findings` because the pointers
  (project default-agent, existing session owners) come from the project anchor,
  not from the scan input.

Collision resolution is deterministic and platform-neutral: the winner is the
**first in a fixed order** — format precedence (detector rank, OpenCode first),
then within a format stably by **filename** — *never* filesystem order
(Windows != Linux).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.projects.scanners.base import RankedFile

from core.projects.scanners.base import DetectedFile, ScannedAgent


class FindingType(StrEnum):
    """The classes of unclean conditions a scan reports."""

    BAD_MODEL = "bad_model"
    SLUG_COLLISION = "slug_collision"
    UNSLUGIFIABLE_NAME = "unslugifiable_name"
    ORPHAN = "orphan"


@dataclass(frozen=True)
class ScanFinding:
    """One reported problem, pointing at the source file or pointer that caused it.

    ``agent_id`` is the affected id when known (the resolved id for a collision,
    the orphaned pointer's id for an orphan); empty when no id could be formed
    (an unslugifiable name). ``source_path`` is the offending file when the
    finding originates from a scanned file, ``None`` for a pointer-origin orphan.
    ``detail`` is a short human-readable explanation.
    """

    type: FindingType
    detail: str
    agent_id: str = ""
    source_path: Path | None = None


@dataclass(frozen=True)
class ScanReport:
    """Everything unclean under what the scan found — empty when all is clean.

    Immutable: the model-finding and pointer-finding seams return a *new* report
    with the extra findings appended, so the resolver and the anchor-aware caller
    can each contribute their findings without this module reaching into their
    domains.
    """

    findings: tuple[ScanFinding, ...] = ()

    @property
    def is_clean(self) -> bool:
        """Return whether the scan found nothing unclean."""
        return not self.findings

    def findings_of(self, finding_type: FindingType) -> tuple[ScanFinding, ...]:
        """Return the findings of one type, preserving order."""
        return tuple(finding for finding in self.findings if finding.type == finding_type)

    def with_model_findings(self, model_findings: list[ScanFinding]) -> ScanReport:
        """Return a new report with the resolver's bad-model findings appended.

        The resolver builder owns the "model exists/configured in this instance?"
        check (it has the model/provider context this module deliberately does
        not). It builds :class:`ScanFinding` of type :attr:`FindingType.BAD_MODEL`
        and merges them through here, keeping the finding type and merge point on
        one contract.
        """
        return ScanReport(findings=(*self.findings, *model_findings))

    def with_pointer_findings(self, pointer_findings: list[ScanFinding]) -> ScanReport:
        """Return a new report with orphan/pointer findings appended.

        Default-agent and session pointers come from the project anchor, not from
        the scan input, so the anchor-aware caller supplies these orphan findings
        through this seam rather than this module reading the anchor.
        """
        return ScanReport(findings=(*self.findings, *pointer_findings))


@dataclass(frozen=True)
class _Candidate:
    """A successfully parsed file plus the precedence keys that order it."""

    rank: int
    filename: str
    file: DetectedFile

    @property
    def agent(self) -> ScannedAgent:
        # build_scan_report only wraps files whose agent parsed successfully.
        assert self.file.agent is not None
        return self.file.agent


def build_scan_report(ranked_files: list[RankedFile]) -> tuple[list[ScannedAgent], ScanReport]:
    """Resolve detected files into a deterministic Team and a structural report.

    Splits the per-file results into parse failures (unslugifiable names →
    findings) and successful candidates, then resolves ``agent_id`` collisions:
    candidates are ordered by ``(detector rank, filename)`` — a fixed, platform-
    neutral order — and the first per ``agent_id`` wins onto the Team; every
    later candidate for the same id becomes a :attr:`FindingType.SLUG_COLLISION`
    finding. The Team is returned sorted by ``agent_id`` for a stable result.
    """
    findings: list[ScanFinding] = []
    candidates: list[_Candidate] = []

    for ranked in ranked_files:
        detected_file = ranked.file
        if detected_file.agent is None:
            findings.append(
                ScanFinding(
                    type=FindingType.UNSLUGIFIABLE_NAME,
                    detail=detected_file.error_reason
                    or "name cannot be slugified into an agent id",
                    source_path=detected_file.source_path,
                )
            )
            continue
        candidates.append(
            _Candidate(
                rank=ranked.rank,
                filename=detected_file.source_path.name,
                file=detected_file,
            )
        )

    # Deterministic, platform-neutral order: format precedence (rank) first, then
    # stably by filename. Never filesystem iteration order.
    candidates.sort(key=lambda candidate: (candidate.rank, candidate.filename))

    winners: dict[str, ScannedAgent] = {}
    for candidate in candidates:
        agent = candidate.agent
        if agent.agent_id in winners:
            winner = winners[agent.agent_id]
            findings.append(
                ScanFinding(
                    type=FindingType.SLUG_COLLISION,
                    detail=(
                        f"agent id '{agent.agent_id}' already taken by "
                        f"{winner.source_path.name} ({winner.source_format}); "
                        f"{candidate.file.source_path.name} ({agent.source_format}) skipped"
                    ),
                    agent_id=agent.agent_id,
                    source_path=candidate.file.source_path,
                )
            )
            continue
        winners[agent.agent_id] = agent

    team = sorted(winners.values(), key=lambda member: member.agent_id)
    return team, ScanReport(findings=tuple(findings))
