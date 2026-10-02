"""Statistics report sections computed from the aggregate tier and small fact queries.

Each section module builds one JSON section of ``statistics.report`` from a
:class:`ReportContext`: the reconciled index connection, the hour-aligned
window with its calendar zone, and the listed Sessions.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from core.statistics._sections import (
    diagnostics,
    extensions,
    overview,
    runs,
    skills,
    tools,
    usage,
)
from core.statistics._sections.common import JsonObject, LiveSession, ReportContext
from core.statistics._sections.window import ReportWindow, zone_for

_BUILDERS: dict[str, Callable[[ReportContext], JsonObject]] = {
    "overview": overview.build,
    "usage": usage.build,
    "runs": runs.build,
    "tools": tools.build,
    "skills": skills.build,
    "extensions": extensions.build,
    "diagnostics": diagnostics.build,
}

SECTION_NAMES = tuple(_BUILDERS)


def build_sections(context: ReportContext, names: Iterable[str]) -> JsonObject:
    """Build the named sections, in ``SECTION_NAMES`` order."""
    wanted = set(names)
    return {name: build(context) for name, build in _BUILDERS.items() if name in wanted}


__all__ = [
    "SECTION_NAMES",
    "LiveSession",
    "ReportContext",
    "ReportWindow",
    "build_sections",
    "zone_for",
]
