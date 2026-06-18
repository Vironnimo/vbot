"""Scanner protocol, the scanned-agent profile form, and the detector registry.

A project's Team is discovered by *scanning* its repo. The seam mirrors the
provider adapters (see ``core/runtime/runtime.py`` → ``_ADAPTER_MAP``): one
**detector per format**, each pluggable, so a new agent format is a new detector
and never a rewrite of the scan. A detector parses its own format at its own
**known location** at the project root (non-recursive — no full-tree walk) and
emits a uniform :class:`ScannedAgent` profile; the resolver (a later builder)
maps that profile onto the runtime agent object.

The scan orchestration here:

1. runs every registered detector at its known location, in **fixed registry
   order** (format precedence: OpenCode first), each detector's output sorted
   stably by source filename — never filesystem order (Windows != Linux);
2. collects the emitted profiles into the Team, resolving ``agent_id``
   collisions deterministically (first in that fixed order wins);
3. builds the :class:`ScanReport` of everything *unclean* under what exists.

An empty folder (no team, no detectors that matched) is **normal**: it yields an
empty Team and a clean empty report, not an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from core.projects.scan_report import ScanReport

# Tools and skills are granted wholesale to every scanned project agent in v1
# (see add-projects.md → "OpenCode-Agent lesen"): build the system cleanly first,
# narrow later. The wildcard is the project-wide allow-all token.
ALLOW_ALL = "*"


@dataclass(frozen=True)
class ScannedAgent:
    """One agent profile emitted by a detector — the detector→resolver contract.

    This is the **uniform profile form** every detector produces, regardless of
    source format. The resolver (a separate builder) maps it onto the runtime
    agent object, so the field set is the load-bearing contract:

    - ``agent_id`` — slugified, project-local id (``slugify_agent_id``); unique
      *within the project* (collision resolution guarantees it on the Team).
    - ``display_name`` — human-facing name (the raw source name before slugging).
    - ``description`` — short description from the source, or ``""``.
    - ``model`` — the **raw** model string exactly as written in the source
      (``<provider>/<model-id>``), no rewriting. May be empty (no model in the
      source) or unresolvable in this instance — the resolver runs the model
      chain and the scan reports a bad/unconfigured model; the detector never
      judges the model.
    - ``temperature`` — optional float, or ``None`` when the source omits it.
    - ``body`` — the source file body, **verbatim**, used as the system prompt.
      Treated as opaque text: ``{...}`` in it is *not* expanded here (the prompt
      builder inserts it via the ``{include}`` path later).
    - ``tools`` / ``skills`` — the allow-lists; ``["*"]`` for every v1 agent.
    - ``source_format`` — the detector's format key (e.g. ``"opencode"``), used
      for format precedence in collision resolution and for the report.
    - ``source_path`` — absolute path of the file the profile was read from, so
      the report can point at the exact offending file.
    """

    agent_id: str
    display_name: str
    description: str
    model: str
    temperature: float | None
    body: str
    source_format: str
    source_path: Path
    tools: tuple[str, ...] = (ALLOW_ALL,)
    skills: tuple[str, ...] = (ALLOW_ALL,)


@runtime_checkable
class AgentDetector(Protocol):
    """One detector per agent format — the pluggable scan seam.

    A detector knows its own format's **known location** relative to the project
    root and parses only that location, non-recursively. It never walks the full
    tree and never reaches into nested repos. Implementations live beside this
    file (e.g. ``opencode.py``) and are registered in :data:`DETECTOR_REGISTRY`.
    """

    @property
    def format_key(self) -> str:
        """Stable identifier of the format this detector reads (e.g. ``"opencode"``)."""
        ...

    def detect(self, project_root: Path) -> list[DetectedFile]:
        """Parse this format's known location under ``project_root``.

        Returns one :class:`DetectedFile` per source file found, **sorted stably
        by filename** (never filesystem order). A missing location yields an
        empty list (normal — not every project uses every format). Each result
        carries either a parsed :class:`ScannedAgent` or a parse failure reason,
        so the report can surface unslugifiable names and other per-file
        problems without the detector deciding policy.
        """
        ...


@dataclass(frozen=True)
class DetectedFile:
    """A single source file a detector read, with its parse outcome.

    Exactly one of ``agent`` / ``error_reason`` is set. ``agent`` carries a
    successfully parsed :class:`ScannedAgent`; ``error_reason`` describes why the
    file could not become an agent (e.g. an unslugifiable name) so the report can
    raise a structural finding pointing at ``source_path``.
    """

    source_path: Path
    raw_name: str
    agent: ScannedAgent | None = None
    error_reason: str | None = None


@dataclass(frozen=True)
class DetectorRegistration:
    """A detector plus its fixed precedence rank in the registry.

    ``rank`` defines **format precedence** for collision resolution: a lower rank
    wins (OpenCode is rank 0). The rank is an explicit, stable number rather than
    list position so the precedence intent is visible at the registration site.
    """

    detector: AgentDetector
    rank: int


# Format precedence is fixed and explicit: OpenCode wins first. New formats append
# with a higher rank; the rank — not list order or filesystem order — decides who
# wins a cross-format ``agent_id`` collision.
OPENCODE_FORMAT_RANK = 0


def build_default_registry() -> list[DetectorRegistration]:
    """Return the default detector registry in fixed precedence order.

    Imported lazily inside the function so ``base`` does not import the concrete
    detectors at module load (and so a detector can import ``base`` without a
    cycle). OpenCode is the only v1 format; later formats append here with the
    next rank.
    """
    from core.projects.scanners.opencode import OpenCodeDetector

    return [DetectorRegistration(detector=OpenCodeDetector(), rank=OPENCODE_FORMAT_RANK)]


@dataclass(frozen=True)
class ScanResult:
    """The outcome of scanning a project: the resolved Team and the report.

    ``team`` is the deterministic list of winning :class:`ScannedAgent` profiles
    (collision losers excluded, present in the report instead). ``report`` is the
    :class:`ScanReport` of everything unclean under what exists; both are empty
    for a bare project.
    """

    team: list[ScannedAgent]
    report: ScanReport


@dataclass(frozen=True)
class RankedFile:
    """A detected file tagged with its detector's precedence rank.

    The rank travels with the file into the report builder so cross-format
    collision resolution stays deterministic without re-consulting the registry.
    """

    rank: int
    file: DetectedFile


def scan_project(
    project_root: Path,
    *,
    registry: list[DetectorRegistration] | None = None,
) -> ScanResult:
    """Scan a project root into a deterministic Team plus a scan report.

    Runs every registered detector at its own known location in fixed precedence
    order, then hands the per-file results to the report builder, which resolves
    ``agent_id`` collisions deterministically and collects structural findings.
    A bare/empty project yields an empty Team and a clean empty report.
    """
    # Imported here (not at module top) to keep the report's collision/finding
    # logic in its own module while ``base`` owns the orchestration entry point;
    # scan_report imports the dataclasses from base, so this avoids a cycle.
    from core.projects.scan_report import build_scan_report

    active_registry = registry if registry is not None else build_default_registry()
    # Run detectors in registry (precedence) order; each detector already returns
    # its files sorted stably by filename, so the concatenation is deterministic.
    ranked_files: list[RankedFile] = []
    for registration in active_registry:
        for detected_file in registration.detector.detect(project_root):
            ranked_files.append(RankedFile(rank=registration.rank, file=detected_file))
    team, report = build_scan_report(ranked_files)
    return ScanResult(team=team, report=report)
