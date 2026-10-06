"""Repository findings shared by source adapters, resolution and management."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class FindingType(StrEnum):
    """The classes of unclean conditions a scan reports."""

    BAD_MODEL = "bad_model"
    SLUG_COLLISION = "slug_collision"
    UNSLUGIFIABLE_NAME = "unslugifiable_name"
    ORPHAN = "orphan"
    UNAVAILABLE_TOOL = "unavailable_tool"
    INVALID_SOURCE = "invalid_source"
    SKILL_COLLISION = "skill_collision"
    SOURCE_SCOPE = "source_scope"


@dataclass(frozen=True)
class ScanFinding:
    """One reported problem, pointing at the source file or pointer that caused it.

    ``agent_id`` is the affected id when known (the resolved id for a collision,
    the orphaned pointer's id for an orphan); empty when the finding is not tied
    to an Agent. ``source_path`` is the offending file when the finding
    originates from a scanned file, otherwise ``None``. ``detail`` is a short
    human-readable explanation.
    """

    type: FindingType
    detail: str
    agent_id: str = ""
    source_path: Path | None = None


@dataclass(frozen=True)
class ScanReport:
    """Everything unclean under what the scan found — empty when all is clean.

    Immutable: :meth:`with_findings` returns a *new* report with the extra
    findings appended, so the resolver, the anchor-aware caller and the server
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

    def with_findings(self, findings: list[ScanFinding]) -> ScanReport:
        """Return a new report with findings from outside the scan appended.

        The scan has no model/provider context, no Project Anchor and no live
        Tool Registry, so the resolver (``BAD_MODEL``), the anchor-aware caller
        (``ORPHAN`` pointers) and the server-facing Project preview
        (``UNAVAILABLE_TOOL``) contribute those findings through this seam.
        """
        return ScanReport(findings=(*self.findings, *findings))
