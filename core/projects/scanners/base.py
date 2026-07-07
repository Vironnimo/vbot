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
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import yaml

from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.projects.scan_report import ScanReport

_LOGGER = get_logger("projects")

FRONT_MATTER_DELIMITER = "---"


def split_front_matter(content: str) -> tuple[str, str]:
    """Split a Markdown file into (front matter, body), preserving the body verbatim.

    Shared by the Markdown-based detectors (OpenCode, Claude). Recognizes a leading
    ``---`` fence and returns the text up to the closing ``---`` as front matter and
    everything after it as the body, **unchanged** (a single leading newline after
    the closing fence is dropped so the body does not start with a blank line, but
    its content — including any ``{...}`` — is otherwise untouched). A file without
    a proper fence has an empty front matter and the whole content as body.
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
        return "", content

    for index in range(1, len(lines)):
        if lines[index].strip() == FRONT_MATTER_DELIMITER:
            front_matter = "".join(lines[1:index])
            body = "".join(lines[index + 1 :])
            return front_matter, _strip_one_leading_newline(body)

    # Unterminated front matter: treat the whole file as body so nothing is lost.
    return "", content


def _strip_one_leading_newline(body: str) -> str:
    if body.startswith("\r\n"):
        return body[2:]
    if body.startswith("\n"):
        return body[1:]
    return body


def parse_front_matter(front_matter: str, path: Path) -> dict[str, Any]:
    """Parse YAML front matter fail-open: malformed YAML yields ``{}``, never raises."""
    if not front_matter.strip():
        return {}
    try:
        loaded = yaml.safe_load(front_matter)
    except yaml.YAMLError as error:
        _LOGGER.warning("Invalid YAML front matter in %s: %s", path, error)
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def string_field(value: Any) -> str:
    """Return a trimmed string for a scalar front-matter field, or ``""`` otherwise."""
    if isinstance(value, str):
        return value.strip()
    return ""


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
    - ``denied_tools`` — the set of vBot tool names this agent turns **off**
      (default empty = nothing turned off). OpenCode is deny-by-exception with no
      clean allow-list form, so the detector emits what the agent *denies*; the
      resolver computes the effective tools as the project's Tool Whitelist ceiling
      minus this set. The agent can only narrow the ceiling, never widen it. Skills
      are no longer carried on the profile — a config agent's allowed skills are
      resolved from the project (OpenCode does not narrow skills per agent in v1).
    - ``source_format`` — the detector's format key (e.g. ``"opencode"``), used
      for format precedence in collision resolution and for the report.
    - ``source_path`` — absolute path of the file the profile was read from, so
      the report can point at the exact offending file.
    - ``thinking_effort`` — optional reasoning-effort level from the source, or
      ``None`` when the source omits it / names an effort vBot does not know. This
      is the **agent tier** of the resolver's thinking-effort chain (agent →
      project default → global default → provider default).
    """

    agent_id: str
    display_name: str
    description: str
    model: str
    temperature: float | None
    body: str
    source_format: str
    source_path: Path
    denied_tools: frozenset[str] = frozenset()
    thinking_effort: str | None = None


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
# wins a cross-format ``agent_id`` collision. With the single-format scan filter
# (a project declares exactly one ``source_format``) a cross-format collision can
# only occur in an unfiltered scan (``source_format=None``).
OPENCODE_FORMAT_RANK = 0
CLAUDE_FORMAT_RANK = 1


def build_default_registry() -> list[DetectorRegistration]:
    """Return the default detector registry in fixed precedence order.

    Imported lazily inside the function so ``base`` does not import the concrete
    detectors at module load (and so a detector can import ``base`` without a
    cycle). Later formats append here with the next rank.
    """
    from core.projects.scanners.claude import ClaudeDetector
    from core.projects.scanners.opencode import OpenCodeDetector

    return [
        DetectorRegistration(detector=OpenCodeDetector(), rank=OPENCODE_FORMAT_RANK),
        DetectorRegistration(detector=ClaudeDetector(), rank=CLAUDE_FORMAT_RANK),
    ]


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
    source_format: str | None = None,
) -> ScanResult:
    """Scan a project root into a deterministic Team plus a scan report.

    Runs every registered detector at its own known location in fixed precedence
    order, then hands the per-file results to the report builder, which resolves
    ``agent_id`` collisions deterministically and collects structural findings.
    A bare/empty project yields an empty Team and a clean empty report.

    ``source_format`` is the project's single-format filter (decision: one format
    per project, no mixing): when set, only the detector whose ``format_key``
    matches runs, so the other format's agents are invisible to every consumer.
    ``None`` keeps the unfiltered all-detectors behavior (format detection,
    generic callers).
    """
    # Imported here (not at module top) to keep the report's collision/finding
    # logic in its own module while ``base`` owns the orchestration entry point;
    # scan_report imports the dataclasses from base, so this avoids a cycle.
    from core.projects.scan_report import build_scan_report

    active_registry = registry if registry is not None else build_default_registry()
    if source_format is not None:
        active_registry = [
            registration
            for registration in active_registry
            if registration.detector.format_key == source_format
        ]
    # Run detectors in registry (precedence) order; each detector already returns
    # its files sorted stably by filename, so the concatenation is deterministic.
    ranked_files: list[RankedFile] = []
    for registration in active_registry:
        for detected_file in registration.detector.detect(project_root):
            ranked_files.append(RankedFile(rank=registration.rank, file=detected_file))
    team, report = build_scan_report(ranked_files)
    return ScanResult(team=team, report=report)


@dataclass(frozen=True)
class FormatPresence:
    """What one source format contributes in a repo: agent and skill counts.

    A format counts as *present* when it yields at least one parsed agent OR one
    loadable skill — the creation-time auto-detection rule.
    """

    agents: int
    skills: int

    @property
    def present(self) -> bool:
        """Whether this format contributes anything (≥1 agent or ≥1 skill)."""
        return self.agents > 0 or self.skills > 0


# The context files reported alongside format presence: the tool-neutral
# AGENTS.md convention at the repo root, and CLAUDE.md at its two conventional
# locations (repo root, then .claude/). Only facts — the "suggest CLAUDE.md as a
# project file" rule lives in the WebUI add dialog, not here.
_AGENTS_MD_FILENAME = "AGENTS.md"
_CLAUDE_MD_CANDIDATES = ("CLAUDE.md", ".claude/CLAUDE.md")


@dataclass(frozen=True)
class ProjectFormatDetection:
    """The per-format presence of a repo plus its context-file facts.

    ``formats`` is keyed by detector ``format_key`` (every registered format gets
    an entry, present or not). ``agents_md`` reports a repo-root ``AGENTS.md``;
    ``claude_md`` carries the relative path of a found ``CLAUDE.md`` (repo root
    first, else ``.claude/CLAUDE.md``) or ``None``.
    """

    formats: dict[str, FormatPresence]
    agents_md: bool
    claude_md: str | None


def detect_project_formats(
    project_root: Path,
    *,
    registry: list[DetectorRegistration] | None = None,
) -> ProjectFormatDetection:
    """Report what each known source format contributes in a repo.

    Runs every registered detector (counting parsed agent profiles only, not parse
    failures) and scans each format's skill directory, so the add dialog / RPC can
    make the informed format choice at project creation. Purely read-only and
    fail-soft: a missing location counts zero; nothing is created or judged here.
    """
    # Imported lazily like scan_project's report import: core.skills imports
    # nothing from core.projects (verified — the cycle risk from the plan), and
    # the lazy form keeps base free of a module-load skills dependency.
    from core.skills import project_skills_dir, scan_skill_names

    active_registry = registry if registry is not None else build_default_registry()
    formats: dict[str, FormatPresence] = {}
    for registration in active_registry:
        format_key = registration.detector.format_key
        detected = registration.detector.detect(project_root)
        agents = sum(1 for detected_file in detected if detected_file.agent is not None)
        try:
            skills_dir = project_skills_dir(project_root, format_key)
        except KeyError:
            # A registered detector whose format has no known skill location
            # (custom test registries) still reports its agents, fail-soft.
            skills = 0
        else:
            skills = len(scan_skill_names(skills_dir))
        formats[format_key] = FormatPresence(agents=agents, skills=skills)

    claude_md: str | None = None
    for candidate in _CLAUDE_MD_CANDIDATES:
        if (project_root / candidate).is_file():
            claude_md = candidate
            break

    return ProjectFormatDetection(
        formats=formats,
        agents_md=(project_root / _AGENTS_MD_FILENAME).is_file(),
        claude_md=claude_md,
    )
