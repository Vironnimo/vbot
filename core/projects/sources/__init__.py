"""Repository source adapters and their format-neutral profiles."""

from core.projects.sources.catalog import (
    SOURCE_CATALOG,
    SOURCE_DEFINITIONS,
    AgentAdapter,
    DetectedSource,
    default_adapters,
    detect_sources,
    instruction_files,
    normalize_sources,
    refresh_sources,
    skill_roots,
)
from core.projects.sources.profile import (
    AgentProfile,
    AgentTargetRule,
    SourceSelection,
    Translation,
)
from core.projects.sources.sources import ScanResult, read_profile, scan_project

__all__ = [
    "AgentAdapter",
    "AgentProfile",
    "AgentTargetRule",
    "DetectedSource",
    "SOURCE_CATALOG",
    "SOURCE_DEFINITIONS",
    "ScanResult",
    "SourceSelection",
    "Translation",
    "default_adapters",
    "detect_sources",
    "instruction_files",
    "normalize_sources",
    "read_profile",
    "refresh_sources",
    "scan_project",
    "skill_roots",
]
