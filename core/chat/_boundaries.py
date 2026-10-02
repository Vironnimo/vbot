"""Cancellation protection for already-visible Assistant boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.runs import Run


async def _finish_visible_boundary[BoundaryResult](
    work: Awaitable[BoundaryResult], run: Run, preserve_after_cancel: bool
) -> BoundaryResult:
    """Finish already-visible output preparation/persistence before honoring Stop."""
    if not preserve_after_cancel:
        return await work
    task = asyncio.ensure_future(work)
    try:
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    return task.result()
                if not run.cancel_requested:
                    raise
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
