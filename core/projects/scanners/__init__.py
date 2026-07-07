"""core.projects.scanners — pluggable agent-format detectors and the scan seam.

Small public interface over the scan subpackage: the scanned-agent profile form
and detector protocol (:mod:`base`), the OpenCode and Claude Code detectors, the
:func:`scan_project` orchestration entry point (format-filterable — a project
declares exactly one source format), and :func:`detect_project_formats` (the
creation-time presence report). One detector per format, mirroring the provider
adapters — a new format is a new detector, not a rewrite.
"""

from core.projects.scanners.base import (
    CLAUDE_FORMAT_RANK,
    OPENCODE_FORMAT_RANK,
    AgentDetector,
    DetectedFile,
    DetectorRegistration,
    FormatPresence,
    ProjectFormatDetection,
    RankedFile,
    ScannedAgent,
    ScanResult,
    build_default_registry,
    detect_project_formats,
    scan_project,
)
from core.projects.scanners.claude import (
    CLAUDE_AGENTS_SUBPATH,
    CLAUDE_FORMAT_KEY,
    ClaudeDetector,
)
from core.projects.scanners.opencode import (
    OPENCODE_AGENTS_SUBPATH,
    OPENCODE_FORMAT_KEY,
    OpenCodeDetector,
)

__all__ = [
    "CLAUDE_AGENTS_SUBPATH",
    "CLAUDE_FORMAT_KEY",
    "CLAUDE_FORMAT_RANK",
    "OPENCODE_AGENTS_SUBPATH",
    "OPENCODE_FORMAT_KEY",
    "OPENCODE_FORMAT_RANK",
    "AgentDetector",
    "ClaudeDetector",
    "DetectedFile",
    "DetectorRegistration",
    "FormatPresence",
    "OpenCodeDetector",
    "ProjectFormatDetection",
    "RankedFile",
    "ScanResult",
    "ScannedAgent",
    "build_default_registry",
    "detect_project_formats",
    "scan_project",
]
