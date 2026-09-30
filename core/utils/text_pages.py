"""One page of long text, the unit Tools return it in: at most 50 KB and 2000 lines.

``read`` pages files in this unit, ``skill`` pages Skill instructions and package
files, and the Skill validator warns when instructions do not fit one page. The
limits live in ``core.utils`` because ``core.tools`` and ``core.skills`` both need
them and ``core.skills`` must not import ``core.tools``.
"""

from __future__ import annotations

TEXT_PAGE_MAX_BYTES = 50 * 1024
TEXT_PAGE_MAX_LINES = 2000

__all__ = ["TEXT_PAGE_MAX_BYTES", "TEXT_PAGE_MAX_LINES"]
