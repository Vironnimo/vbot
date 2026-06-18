"""core.projects.scanners — pluggable agent-format detectors and the scan seam.

Small public interface over the scan subpackage: the scanned-agent profile form
and detector protocol (:mod:`base`), the OpenCode detector, and the
:func:`scan_project` orchestration entry point. One detector per format, mirroring
the provider adapters — a new format is a new detector, not a rewrite.
"""

from core.projects.scanners.base import (
    ALLOW_ALL,
    OPENCODE_FORMAT_RANK,
    AgentDetector,
    DetectedFile,
    DetectorRegistration,
    RankedFile,
    ScannedAgent,
    ScanResult,
    build_default_registry,
    scan_project,
)
from core.projects.scanners.opencode import (
    OPENCODE_AGENTS_SUBPATH,
    OPENCODE_FORMAT_KEY,
    OpenCodeDetector,
)

__all__ = [
    "ALLOW_ALL",
    "OPENCODE_AGENTS_SUBPATH",
    "OPENCODE_FORMAT_KEY",
    "OPENCODE_FORMAT_RANK",
    "AgentDetector",
    "DetectedFile",
    "DetectorRegistration",
    "OpenCodeDetector",
    "RankedFile",
    "ScanResult",
    "ScannedAgent",
    "build_default_registry",
    "scan_project",
]
