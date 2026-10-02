"""Incrementally indexed statistics aggregation over persisted Sessions."""

from __future__ import annotations

from core.statistics._sources import (
    AgentDirectory,
    ProjectDirectory,
    SessionSource,
)
from core.statistics.index import StatisticsIndex, StatisticsUnavailableError
from core.statistics.report import RunActivity, RunActivityReport
from core.statistics.skills import SkillInventorySource, SkillUse, counts_as_skill_use
from core.statistics.statistics import REPORT_SECTIONS, StatisticsService

__all__ = [
    "REPORT_SECTIONS",
    "AgentDirectory",
    "ProjectDirectory",
    "RunActivity",
    "RunActivityReport",
    "SessionSource",
    "SkillInventorySource",
    "SkillUse",
    "StatisticsIndex",
    "StatisticsService",
    "StatisticsUnavailableError",
    "counts_as_skill_use",
]
