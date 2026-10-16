"""A bounded terminal viewport over server-owned conversation rows.

Run children are independent display rows: a single long Run must not bypass
the viewport. Disclosure and reading state belong to the Conversation rather
than to its short-lived widgets.
"""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from typing import Any, override

from textual.containers import VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from cli.tui.rendering import TimelineEntry
from cli.tui.state import Conversation, JsonObject

_MAX_ROWS = 80
_ESTIMATED_HEIGHT = 5


def display_rows(items: list[JsonObject]) -> list[JsonObject]:
    rows = []
    for item in items:
        if item.get("type") == "assistant_run":
            if item.get("status") in {"failed", "cancelled", "interrupted"}:
                rows.append({**item, "type": "run_status", "items": []})
            rows.extend(item.get("items", []))
        else:
            rows.append(item)
    return rows


class Transcript(VerticalScroll):
    """Keep only the visible rows, overscan and keyboard-focused rows mounted."""

    DEFAULT_CSS = """
    Transcript > .transcript-spacer { height: 0; padding: 0; margin: 0; }
    """

    class LoadOlder(Message):
        """The operator reached the loaded history's leading edge."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rows: dict[str, TimelineEntry] = {}
        self.view: Conversation | None = None
        self.source: list[JsonObject] | None = None
        self.items: list[JsonObject] = []
        self.offsets: list[int] = [0]
        self.top = Static("", classes="transcript-spacer", markup=False)
        self.bottom = Static("", classes="transcript-spacer", markup=False)
        self._lock = asyncio.Lock()
        self._scheduled = False
        self._stamp: tuple[Any, ...] | None = None
        self._range = (0, 0)
        self.gaps: dict[str, Static] = {}
        self._adjusting = False
        self._navigation = 0

    def on_mount(self) -> None:
        self.set_interval(0.08, self._check_window)

    @override
    def scroll_to(self, x: float | None = None, y: float | None = None, **kwargs: Any) -> None:
        if self.view is not None and y is not None and not self._adjusting:
            # Record intent before Textual's deferred scroll runs. A queued
            # layout measurement must not undo a newer navigation request.
            self.view.follow_tail = y >= self.max_scroll_y
            self._navigation += 1
        super().scroll_to(x, y, **kwargs)

    def _check_window(self) -> None:
        stamp = (round(self.scroll_y), self.size, id(self.source))
        if self.view is None or self._scheduled or stamp == self._stamp:
            return
        self._stamp = stamp
        self._scheduled = True
        self.run_worker(self._refresh_window(), group="transcript-window", exit_on_error=True)

    async def clear(self) -> None:
        async with self._lock:
            self.view = None
            self.source = None
            self.items = []
            self.offsets = [0]
            self.rows.clear()
            self._range = (0, 0)
            self.gaps.clear()
            await self.remove_children()

    async def show(self, view: Conversation) -> None:
        async with self._lock:
            changed = self.view is not view
            if changed:
                await self.remove_children()
                self.rows.clear()
                self.top = Static("", classes="transcript-spacer", markup=False)
                self.bottom = Static("", classes="transcript-spacer", markup=False)
                self.view = view
                self.source = None
                self._range = (0, 0)
                self.gaps.clear()
            if not self.top.is_mounted:
                await self.mount(self.top, self.bottom)
            data_changed = self.source is not view.items
            if data_changed:
                self.source = view.items
                self.items = display_rows(view.items)
                known = {str(item["id"]) for item in self.items}
                view.row_heights = {
                    key: value for key, value in view.row_heights.items() if key in known
                }
            width_changed = view.measured_width != self.size.width
            if width_changed:
                view.measured_width = self.size.width
                view.row_heights.clear()
            if data_changed or width_changed:
                self._offsets()
            await self._window(follow=view.follow_tail, restoring=changed or data_changed)

    def _offsets(self) -> None:
        assert self.view is not None
        heights = self.view.row_heights
        offsets = [0]
        for item in self.items:
            offsets.append(offsets[-1] + heights.get(str(item["id"]), _ESTIMATED_HEIGHT))
        self.offsets = offsets

    def reading_anchor(self) -> tuple[str, float] | None:
        if not self.items:
            return None
        index = min(len(self.items) - 1, max(0, bisect_right(self.offsets, self.scroll_y) - 1))
        return str(self.items[index]["id"]), self.scroll_y - self.offsets[index]

    def remember_position(self) -> None:
        if self.view is not None:
            self.view.follow_tail = self.is_vertical_scroll_end
            self.view.scroll_y = self.scroll_y
            self.view.reading_anchor = self.reading_anchor()

    def reveal(self, key: str) -> None:
        if self.view is None:
            return
        index = next((i for i, item in enumerate(self.items) if item["id"] == key), None)
        if index is not None:
            self.view.follow_tail = False
            self.view.reading_anchor = (key, 0)
            self.scroll_to(y=self.offsets[index], animate=False)
            self._stamp = None

    async def _refresh_window(self) -> None:
        try:
            async with self._lock:
                if self.view is not None:
                    self.remember_position()
                    if self.view.measured_width != self.size.width:
                        self.view.measured_width = self.size.width
                        self.view.row_heights.clear()
                        self._offsets()
                    await self._window(follow=self.view.follow_tail)
                    if (
                        not self.view.follow_tail
                        and self.scroll_y < self.size.height
                        and self.view.next_before
                    ):
                        self.post_message(self.LoadOlder())
        finally:
            self._scheduled = False

    async def _window(self, *, follow: bool, restoring: bool = False) -> None:
        view = self.view
        if view is None:
            return
        height = max(1, self.size.height)
        y = max(0, self.offsets[-1] - height) if follow else self.scroll_y
        if restoring and not follow:
            y = view.scroll_y
            if view.reading_anchor:
                key, delta = view.reading_anchor
                index = next((i for i, item in enumerate(self.items) if item["id"] == key), None)
                if index is not None:
                    y = self.offsets[index] + delta
        start = max(0, bisect_right(self.offsets, max(0, y - height)) - 1)
        end = min(len(self.items), bisect_right(self.offsets, y + height * 2))
        end = min(end, start + _MAX_ROWS)
        if follow:
            end = len(self.items)
            start = max(start, end - _MAX_ROWS)
        positions = {str(item["id"]): index for index, item in enumerate(self.items)}
        held = {
            positions[key]
            for key, row in self.rows.items()
            if row.has_focus_within and key in positions
        }
        indices = sorted(set(range(start, end)) | held)
        selected = [self.items[index] for index in indices]
        wanted = {str(item["id"]) for item in selected}
        for key, row in list(self.rows.items()):
            if key not in wanted:
                await row.remove()
                del self.rows[key]
        # Batch the mounts; yielding once per nested widget caused repeated
        # transcript-wide layouts in the old renderer.
        new = []
        for item in selected:
            key = str(item["id"])
            if key not in self.rows:
                row = self.rows[key] = TimelineEntry(expanded=view.expanded_rows)
                new.append(row)
        if new:
            await self.mount(*new, before=self.bottom)
        for item in selected:
            row = self.rows[str(item["id"])]
            if row.item is not item:
                await row.update_item(item)
        previous: Widget = self.top
        previous_index = indices[0] - 1 if indices else 0
        gap_keys = set()
        for index, item in zip(indices, selected, strict=True):
            if index > previous_index + 1:
                key = str(item["id"])
                gap_keys.add(key)
                if key not in self.gaps:
                    self.gaps[key] = Static("", classes="transcript-spacer", markup=False)
                    await self.mount(self.gaps[key], before=self.bottom)
                gap = self.gaps[key]
                gap.styles.height = self.offsets[index] - self.offsets[previous_index + 1]
                self.move_child(gap, after=previous)
                previous = gap
            row = self.rows[str(item["id"])]
            self.move_child(row, after=previous)
            previous = row
            previous_index = index
        for key in self.gaps.keys() - gap_keys:
            await self.gaps.pop(key).remove()
        self._range = (indices[0], indices[-1] + 1) if indices else (0, 0)
        start, end = self._range
        self.top.styles.height = self.offsets[start]
        self.bottom.styles.height = self.offsets[-1] - self.offsets[end]
        anchor = view.reading_anchor if restoring and not follow else None
        self.call_after_refresh(self._measure, view, follow, y, anchor, self._navigation)

    def _measure(
        self,
        view: Conversation,
        follow: bool,
        y: float,
        restore_anchor: tuple[str, float] | None,
        navigation: int,
    ) -> None:
        if self.view is not view:
            return
        anchor = (
            restore_anchor
            if restore_anchor is not None and navigation == self._navigation
            else self.reading_anchor()
        )
        changed = False
        for key, row in self.rows.items():
            height = row.region.height + row.styles.margin.top + row.styles.margin.bottom
            if height > 0 and view.row_heights.get(key) != height:
                view.row_heights[key] = height
                changed = True
        if changed:
            self._offsets()
            start, end = self._range
            self.top.styles.height = self.offsets[start]
            self.bottom.styles.height = self.offsets[-1] - self.offsets[end]
        self._adjusting = True
        try:
            if follow and view.follow_tail:
                self.scroll_end(animate=False, immediate=True)
            else:
                if anchor:
                    key, delta = anchor
                    index = next(
                        (i for i, item in enumerate(self.items) if item["id"] == key), None
                    )
                    if index is not None:
                        y = self.offsets[index] + delta
                self.scroll_to(y=y, animate=False, immediate=True)
        finally:
            self._adjusting = False
        self._stamp = None if changed else (round(self.scroll_y), self.size, id(self.source))
