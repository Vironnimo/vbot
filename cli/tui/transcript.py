"""The conversation transcript as one Line API view over server-owned rows.

The whole transcript is one widget that paints only the lines on screen. Each
display row (a message, a Tool call, a reasoning block, a Run's children one by
one) renders once per width into cached strips; rows the operator has not seen
yet carry an estimated height until they are first painted, and a reading
anchor (row id and line offset) keeps the view still while estimates turn into
exact heights, rows expand or collapse, or earlier rows arrive above. Nothing
mounts or unmounts while scrolling, so scrolling, expanding and streaming cost
only the rows they touch.

Disclosure state, measured heights and the reading position belong to the
Conversation, so a reopened Session paints at once where the operator left it.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from time import monotonic
from typing import Any, ClassVar, override

from rich.console import RenderableType
from rich.markdown import Markdown
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from rich.theme import Theme as RichTheme
from textual import events
from textual.binding import Binding, BindingType
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.selection import Selection
from textual.strip import Strip

from cli.tui.rendering import item_heading, item_text, tool_detail, tool_title
from cli.tui.state import Conversation, JsonObject

# Rows within this many screens of the viewport are painted before they show,
# and the cached strips of rows further away are dropped (their heights stay).
_OVERSCAN_SCREENS = 1
_KEEP_SCREENS = 4
# Rows measured per idle tick while the transcript converges on exact heights.
_MEASURE_BATCH = 24
# An expanded Tool shows at most this many detail lines; Ctrl+G copies them all.
_DETAIL_LINES = 400
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FAILED = {"failed", "error"}
_LIVE = {"running", "pending", "queued", "streaming"}
# Rows the operator opens and closes: one header line, an optional body.
_DISCLOSURES = {"tool_call", "reasoning"}


def display_rows(items: list[JsonObject]) -> list[JsonObject]:
    """Flatten Runs into the rows the transcript paints, one per child."""
    rows: list[JsonObject] = []
    for item in items:
        if item.get("type") != "assistant_run":
            rows.append(item)
            continue
        rows.extend(item.get("items", []))
        status = str(item.get("status") or "")
        if status in {"failed", "cancelled", "interrupted"}:
            rows.append({**item, "id": f"{item['id']}:status", "type": "run_status", "items": []})
        elif status == "completed" and isinstance(item.get("durationMs"), (int, float)):
            rows.append({**item, "id": f"{item['id']}:footer", "type": "run_footer", "items": []})
    return rows


def _duration(milliseconds: float) -> str:
    seconds = max(0, round(milliseconds / 1000))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


@dataclass(eq=False)
class _Row:
    key: str
    item: JsonObject
    gap: int = 0
    # Painted lines at `width`, without the gap; None until painted or after
    # they were dropped far from the viewport.
    lines: list[Strip] | None = None
    width: int = 0
    # Height without the gap: exact once measured at the current width.
    height: int = 1
    exact: bool = False
    # Plain text of the painted lines, for selection and copying.
    text: list[str] | None = None

    @property
    def kind(self) -> str:
        kind = str(self.item.get("type") or "")
        if kind == "message":
            role = (self.item.get("message") or {}).get("role")
            return "user" if role == "user" else "text" if role == "assistant" else "notice"
        if kind == "assistant_output":
            return "text"
        return kind

    @property
    def live(self) -> bool:
        """A row whose header animates: a running Tool or streaming reasoning."""
        if self.kind == "tool_call":
            return str(self.item.get("status") or "") in _LIVE
        return self.kind == "reasoning" and bool(self.item.get("streaming"))


class Transcript(ScrollView, can_focus=True):
    """Paint the visible lines of the displayed conversation."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "transcript--muted",
        "transcript--user",
        "transcript--bar",
        "transcript--tool",
        "transcript--success",
        "transcript--warning",
        "transcript--error",
        "transcript--hover",
        "transcript--placeholder",
    }

    DEFAULT_CSS = """
    Transcript { overflow-x: hidden; background: $background; }
    Transcript > .transcript--muted { color: $text-muted; }
    Transcript > .transcript--user { background: $panel; color: $foreground; }
    Transcript > .transcript--bar { color: $accent; background: $panel; }
    Transcript > .transcript--tool { color: $foreground; text-style: bold; }
    Transcript > .transcript--success { color: $success; }
    Transcript > .transcript--warning { color: $warning; }
    Transcript > .transcript--error { color: $error; }
    Transcript > .transcript--hover { background: $panel; }
    Transcript > .transcript--placeholder { color: $text-muted; }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor(-1)", "Previous step", show=False),
        Binding("down", "cursor(1)", "Next step", show=False),
        Binding("enter,space", "open_close", "Open / close", show=False),
        Binding("escape", "leave", "Back to the composer", show=False),
    ]

    class LoadOlder(Message):
        """The operator reached the loaded history's leading edge."""

    class LoadHistory(Message):
        """A Run's omitted earlier steps came near the viewport."""

        def __init__(self, item: JsonObject) -> None:
            super().__init__()
            self.item = item

    class Leave(Message):
        """The operator left the transcript for the composer."""

    class LinkClicked(Message):
        """A link in a message was clicked."""

        def __init__(self, href: str) -> None:
            super().__init__()
            self.href = href

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.view: Conversation | None = None
        self.source: list[JsonObject] | None = None
        self._rows: list[_Row] = []
        self._index: dict[str, int] = {}
        self._offsets: list[int] = [0]
        self._placeholder = ""
        self._adjusting = False
        self._hover: str | None = None
        self._cursor: str | None = None
        self._requested_gaps: set[tuple[str, str]] = set()
        self._measure_scheduled = False
        self._painted: list[JsonObject] | None = None
        self._markdown_styles: tuple[str, RichTheme] | None = None

    # --- Public interface -------------------------------------------------

    @override
    def on_mount(self) -> None:
        super().on_mount()
        # Repaint after the new theme's styles apply.
        self.watch(self.app, "theme", self._theme_switched, init=False)
        self.set_interval(0.1, self._tick_live)

    @property
    def rows(self) -> dict[str, JsonObject]:
        """The displayed rows by id."""
        return {row.key: row.item for row in self._rows}

    def displays(self, key: str) -> bool:
        """Whether the row was painted in the latest frame of the current data."""
        return key in self._index and self._painted is self.source

    def placeholder(self, text: str) -> None:
        """Show `text` instead of rows, such as while a conversation loads."""
        self._placeholder = text
        self.refresh()

    async def clear(self) -> None:
        self.view = None
        self.source = None
        self._rows = []
        self._index = {}
        self._offsets = [0]
        self._placeholder = ""
        self._hover = self._cursor = None
        self._requested_gaps.clear()
        self._painted = None
        self._set_virtual_height(0)
        self.refresh()

    async def show(self, view: Conversation) -> None:
        """Display `view`'s current rows, keeping its reading position."""
        changed = self.view is not view
        if changed:
            self.remember_position()
            self.view = view
            self.source = None
            self._rows = []
            self._index = {}
            self._hover = self._cursor = None
            self._requested_gaps.clear()
        width = self._width()
        if view.measured_width != width:
            view.measured_width = width
            view.row_heights.clear()
        if self.source is not view.items:
            self.source = view.items
            self._rebuild(display_rows(view.items))
        self._placeholder = ""
        if changed:
            self._restore(view)
        self._settle()

    def remember_position(self) -> None:
        view = self.view
        if view is None:
            return
        view.follow_tail = self._at_end()
        view.scroll_y = self.scroll_y
        view.reading_anchor = self.reading_anchor()

    def reading_anchor(self) -> tuple[str, float] | None:
        if not self._rows:
            return None
        index = self._row_at(round(self.scroll_y))
        return self._rows[index].key, self.scroll_y - self._offsets[index]

    def reveal(self, key: str) -> None:
        """Scroll `key` to the top and keep reading there."""
        index = self._index.get(key)
        if index is None or self.view is None:
            return
        self.view.follow_tail = False
        self._cursor = key if self._rows[index].kind in _DISCLOSURES else self._cursor
        self._place(index, 0)
        self.refresh()

    def scroll_to_latest(self) -> None:
        if self.view is not None:
            self.view.follow_tail = True
        self._settle()

    @property
    @override
    def is_vertical_scroll_end(self) -> bool:
        return self._at_end()

    # --- Rows and heights -------------------------------------------------

    def _width(self) -> int:
        return max(20, self.scrollable_content_region.width or self.size.width or 80)

    def _rebuild(self, items: list[JsonObject]) -> None:
        assert self.view is not None
        known = {row.key: row for row in self._rows}
        heights = self.view.row_heights
        rows: list[_Row] = []
        for item in items:
            key = str(item["id"])
            row = known.get(key)
            if row is None:
                row = _Row(key, item)
                cached = heights.get(key)
                row.height, row.exact = (cached, True) if cached else (self._estimate(row), False)
            elif row.item is not item:
                # The previous height stays as the estimate, so a growing row
                # does not jump before it is painted again.
                row.item, row.lines, row.text, row.exact = item, None, None, False
            rows.append(row)
        for index, row in enumerate(rows):
            row.gap = self._gap(rows[index - 1] if index else None, row)
        self._rows = rows
        self._index = {row.key: index for index, row in enumerate(rows)}
        heights_known = {row.key for row in rows}
        for key in [key for key in heights if key not in heights_known]:
            del heights[key]
        self._offsets_changed()

    @staticmethod
    def _gap(previous: _Row | None, row: _Row) -> int:
        if previous is None:
            return 0
        # Consecutive steps of one Run read as one list; prose gets air.
        compact = {"tool_call", "reasoning", "run_footer", "history_gap"}
        if row.kind in compact and previous.kind in compact - {"run_footer"}:
            return 0
        return 1

    def _estimate(self, row: _Row) -> int:
        kind = row.kind
        if kind in _DISCLOSURES:
            if self.view is None or row.key not in self.view.expanded_rows:
                return 1
        elif kind in {"run_footer", "run_status", "history_gap", "model_fallback"}:
            return 1
        width = max(10, self._width() - 4)
        text = (tool_detail(row.item) if kind == "tool_call" else item_text(row.item)) or " "
        lines = sum(max(1, math.ceil(len(line) / width)) for line in text.splitlines())
        return lines + (1 if kind in _DISCLOSURES else 0)

    def _offsets_changed(self) -> None:
        offsets = [0]
        for row in self._rows:
            offsets.append(offsets[-1] + row.gap + row.height)
        self._offsets = offsets
        self._set_virtual_height(offsets[-1])

    def _set_virtual_height(self, height: int) -> None:
        self._adjusting = True
        try:
            self.virtual_size = Size(self._width(), height)
        finally:
            self._adjusting = False

    def _row_at(self, y: int) -> int:
        return min(len(self._rows) - 1, max(0, bisect_right(self._offsets, y) - 1))

    def _paint_row(self, row: _Row, width: int) -> None:
        lines = self._render_row(row, width)
        row.lines, row.width = lines, width
        row.text = [line.text for line in lines]
        row.height, row.exact = len(lines), True
        if self.view is not None:
            self.view.row_heights[row.key] = row.height

    def _ensure_painted(self, start: int, end: int) -> bool:
        """Paint the rows overlapping lines `start..end`; report height changes."""
        if not self._rows:
            return False
        width = self._width()
        changed = False
        for index in range(self._row_at(max(0, start)), self._row_at(max(0, end)) + 1):
            row = self._rows[index]
            if row.lines is None or row.width != width:
                before = row.height
                self._paint_row(row, width)
                changed = changed or row.height != before
        return changed

    # --- Position ---------------------------------------------------------

    def _at_end(self) -> bool:
        return self.scroll_y >= self.max_scroll_y - 1

    def _restore(self, view: Conversation) -> None:
        if view.follow_tail or not view.reading_anchor:
            return
        key, delta = view.reading_anchor
        index = self._index.get(key)
        if index is not None:
            self._place(index, delta)

    def _place(self, index: int, delta: float) -> None:
        """Put line `delta` of row `index` at the top, painting what shows."""
        for _ in range(4):
            y = self._offsets[index] + delta
            height = self.scrollable_content_region.height or self.size.height
            if not self._ensure_painted(round(y), round(y) + height):
                break
            self._offsets_changed()
        self._scroll_quietly(self._offsets[index] + delta)

    def _scroll_quietly(self, y: float) -> None:
        self._adjusting = True
        try:
            y = max(0, min(round(y), self.max_scroll_y))
            self.scroll_target_y = y
            self.set_scroll(None, y)
        finally:
            self._adjusting = False
        self.refresh()

    def _settle(self) -> None:
        """Paint what shows and hold the reading position or the tail."""
        view = self.view
        if view is None or not self._rows:
            self.refresh()
            return
        height = self.scrollable_content_region.height or self.size.height or 24
        if view.follow_tail:
            for _ in range(4):
                bottom = self._offsets[-1]
                if not self._ensure_painted(bottom - height * (1 + _OVERSCAN_SCREENS), bottom):
                    break
                self._offsets_changed()
            self._scroll_quietly(self.max_scroll_y)
        else:
            anchor = view.reading_anchor or self.reading_anchor()
            index = self._index.get(anchor[0]) if anchor else None
            if anchor is not None and index is not None:
                for _ in range(4):
                    y = self._offsets[index] + anchor[1]
                    if not self._ensure_painted(
                        round(y - height * _OVERSCAN_SCREENS),
                        round(y + height * (1 + _OVERSCAN_SCREENS)),
                    ):
                        break
                    self._offsets_changed()
                self._scroll_quietly(self._offsets[index] + anchor[1])
        self._painted = self.source
        self._after_move()
        self._schedule_measure()
        self.refresh()

    @override
    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self._adjusting or self.view is None or not self._rows:
            return
        # Motion the transcript did not make itself: the operator's (wheel,
        # keys, scrollbar) or an explicit jump. Paint what comes into view and
        # keep the row that was at the top where it is.
        anchor_key, delta = self.reading_anchor() or ("", 0.0)
        height = self.scrollable_content_region.height or self.size.height
        if self._ensure_painted(
            round(new_value - height * _OVERSCAN_SCREENS),
            round(new_value + height * (1 + _OVERSCAN_SCREENS)),
        ):
            self._offsets_changed()
            index = self._index.get(anchor_key)
            if index is not None and not self._at_end():
                self._scroll_quietly(self._offsets[index] + delta)
        self.view.follow_tail = self._at_end()
        self.view.reading_anchor = self.reading_anchor()
        self._after_move()

    def _after_move(self) -> None:
        """Ask for history that came within reach, and drop far-away strips."""
        view = self.view
        if view is None or not self._rows:
            return
        height = self.scrollable_content_region.height or self.size.height or 24
        top, bottom = self.scroll_y - height, self.scroll_y + height * 2
        if not view.follow_tail and self.scroll_y < height and view.next_before:
            self.post_message(self.LoadOlder())
        for index in range(self._row_at(max(0, round(top))), self._row_at(round(bottom)) + 1):
            row = self._rows[index]
            if row.kind == "history_gap":
                request = (str(row.item.get("run_id")), str(row.item.get("before")))
                if request not in self._requested_gaps:
                    self._requested_gaps.add(request)
                    self.post_message(self.LoadHistory(row.item))
        keep_top, keep_bottom = (
            self.scroll_y - height * _KEEP_SCREENS,
            self.scroll_y + height * (_KEEP_SCREENS + 1),
        )
        for index, row in enumerate(self._rows):
            if row.lines is not None and (
                self._offsets[index + 1] < keep_top or self._offsets[index] > keep_bottom
            ):
                row.lines = row.text = None

    def history_failed(self, item: JsonObject) -> None:
        """Let a gap whose page did not load ask again when it next shows."""
        self._requested_gaps.discard((str(item.get("run_id")), str(item.get("before"))))

    def _schedule_measure(self) -> None:
        if not self._measure_scheduled and any(not row.exact for row in self._rows):
            self._measure_scheduled = True
            self.set_timer(0.05, self._measure_some)

    def _measure_some(self) -> None:
        """Turn estimates into exact heights near the viewport first, off-screen."""
        self._measure_scheduled = False
        if self.view is None or not self._rows:
            return
        width = self._width()
        center = self._row_at(round(self.scroll_y))
        pending = sorted(
            (index for index, row in enumerate(self._rows) if not row.exact),
            key=lambda index: abs(index - center),
        )[:_MEASURE_BATCH]
        if not pending:
            return
        anchor = None if self.view.follow_tail else self.reading_anchor()
        for index in pending:
            row = self._rows[index]
            self._paint_row(row, width)
            row.lines = row.text = None
        self._offsets_changed()
        if self.view.follow_tail:
            self._scroll_quietly(self.max_scroll_y)
        elif anchor is not None and (anchored := self._index.get(anchor[0])) is not None:
            self._scroll_quietly(self._offsets[anchored] + anchor[1])
        self._schedule_measure()

    # --- Rendering --------------------------------------------------------

    def _style(self, name: str) -> Style:
        # Combined with the transcript's own style, so translucent theme colours
        # such as $text-muted resolve against the background.
        style = self.get_component_rich_style(f"transcript--{name}")
        if name == "hover":
            return Style(bgcolor=style.bgcolor)
        return style

    def _markdown(self, text: str, width: int, style: Style | None = None) -> list[Strip]:
        dark = self.app.current_theme.dark
        renderable = Markdown(
            text or " ",
            code_theme="github-dark" if dark else "default",
            hyperlinks=True,
            inline_code_lexer=None,
        )
        with self.app.console.use_theme(self._markdown_theme()):
            return self._lines(renderable, width, style)

    def _markdown_theme(self) -> RichTheme:
        """Rich's Markdown styles in the accessor's palette instead of its defaults."""
        if self._markdown_styles is None or self._markdown_styles[0] != self.app.theme:
            accent = self._style("bar").color
            muted = self._style("muted").color
            styles = {
                "markdown.code": Style(color=accent, bold=True),
                "markdown.h1": Style(color=accent, bold=True),
                "markdown.h1.border": Style(color=muted),
                "markdown.h2": Style(bold=True, underline=True),
                "markdown.h3": Style(bold=True),
                "markdown.h4": Style(bold=True, italic=True),
                "markdown.link": Style(color=accent, underline=True),
                "markdown.link_url": Style(color=muted, underline=True),
                "markdown.block_quote": Style(color=muted, italic=True),
                "markdown.hr": Style(color=muted),
                "markdown.item.bullet": Style(color=muted),
                "markdown.item.number": Style(color=muted),
                "markdown.table.border": Style(color=muted),
            }
            self._markdown_styles = (self.app.theme, RichTheme(styles, inherit=True))
        return self._markdown_styles[1]

    def _lines(
        self, renderable: RenderableType, width: int, style: Style | None = None
    ) -> list[Strip]:
        console = self.app.console
        options = console.options.update(width=max(1, width), highlight=False)
        rendered = console.render_lines(renderable, options, style=style or Style(), pad=False)
        lines = [Strip(line).simplify() for line in rendered]
        while lines and not lines[-1].text.strip():
            lines.pop()
        return lines or [Strip([])]

    @staticmethod
    def _prefixed(lines: list[Strip], first: Segment, rest: Segment) -> list[Strip]:
        return [
            Strip.join([Strip([first if index == 0 else rest]), line])
            for index, line in enumerate(lines)
        ]

    def _render_row(self, row: _Row, width: int) -> list[Strip]:
        item, kind = row.item, row.kind
        muted = self._style("muted")
        if kind == "user" or kind == "user_message":
            background = self._style("user")
            bar = Segment("▌ ", self._style("bar"))
            body = self._markdown(item_text(item), width - 3, background)
            lines = [line.extend_cell_length(width - 2, background) for line in body]
            lines = self._prefixed(lines, bar, bar)
            if kind == "user_message":
                heading = item_heading(item) or "Steer"
                lines.insert(0, Strip([Segment(f"  {heading}", muted + Style(italic=True))]))
            return lines
        if kind == "text":
            pad = Segment("  ")
            return self._prefixed(self._markdown(item_text(item), width - 2), pad, pad)
        if kind in _DISCLOSURES:
            lines = [self._header(row, width)]
            if self.view is not None and row.key in self.view.expanded_rows:
                lines.extend(self._disclosure_body(row, width))
            return lines
        if kind == "history_gap":
            return [Strip([Segment("  ⋯ earlier steps load as they come into view", muted)])]
        if kind == "run_status":
            status = str(item.get("status") or "")
            style = self._style("error") if status == "failed" else self._style("warning")
            mark = "✗" if status == "failed" else "■"
            return [Strip([Segment(f"  {mark} Run {status}", style)])]
        if kind == "run_footer":
            return [
                Strip([Segment(f"  ─ Worked for {_duration(float(item['durationMs']))}", muted)])
            ]
        if kind == "model_fallback":
            text = " ".join((item_text(item) or "Model fallback").split())
            return [Strip([Segment(f"  ⚠ {text}", self._style("warning"))])]
        if kind in {"compaction_separator", "takeover_separator"}:
            label = f" {item_heading(item) or ''} "
            side = max(2, (width - len(label)) // 2)
            rule = Strip([Segment("─" * side + label + "─" * side, muted)]).crop(0, width)
            details = item_text(item).splitlines()[1:]
            return [rule, *[Strip([Segment(f"  {line}", muted)]) for line in details]]
        title = item_heading(item)
        lines = self._markdown(item_text(item), width - 2)
        pad = Segment("  ")
        lines = self._prefixed(lines, pad, pad)
        style = self._style("error") if kind in {"notice", "error"} else muted
        if title:
            lines.insert(0, Strip([Segment(f"  {title}", style + Style(bold=True))]))
        return lines

    def _header(self, row: _Row, width: int) -> Strip:
        item = row.item
        muted = self._style("muted")
        if row.kind == "reasoning":
            if item.get("streaming"):
                label = "Thinking…"
            else:
                duration = item.get("durationMs", item.get("durationEstimateMs"))
                label = (
                    f"Thought for {_duration(float(duration))}"
                    if isinstance(duration, (int, float)) and duration >= 1000
                    else "Thought"
                )
            glyph = _SPINNER[int(monotonic() * 10) % len(_SPINNER)] if row.live else "∴"
            text = Text.assemble(("  " + glyph + " ", muted), (label, muted + Style(italic=True)))
        else:
            status = str(item.get("status") or "")
            if row.live:
                mark = (_SPINNER[int(monotonic() * 10) % len(_SPINNER)], self._style("warning"))
            elif status in _FAILED:
                mark = ("●", self._style("error"))
            elif status in {"cancelled", "partial", "interrupted"}:
                mark = ("●", self._style("warning"))
            else:
                mark = ("●", self._style("success"))
            name, _, summary = tool_title(item).partition(" · ")
            if row.live:
                # The spinner already says it runs.
                summary = summary.removesuffix(f" · {status}").removesuffix(status)
            text = Text.assemble(
                ("  ", ""),
                mark,
                (" ", ""),
                (name, self._style("tool")),
                (f"  {summary}" if summary else "", muted),
            )
        text.truncate(width, overflow="ellipsis")
        if row.key in {self._hover, self._cursor}:
            text.stylize(self._style("hover"))
            text.pad_right(width - text.cell_len)
            if row.key == self._cursor and self.has_focus:
                text.stylize(Style(bold=True), 0, 3)
        return Strip(list(text.render(self.app.console)), text.cell_len)

    def _disclosure_body(self, row: _Row, width: int) -> list[Strip]:
        muted = self._style("muted")
        if row.kind == "reasoning":
            body = self._markdown(item_text(row.item), width - 4, muted + Style(italic=True))
            pad = Segment("    ")
            return self._prefixed(body, pad, pad)
        detail = tool_detail(row.item) or "No details."
        source = detail.splitlines()
        shown = source[:_DETAIL_LINES]
        if len(source) > len(shown):
            shown.append(f"… {len(source) - len(shown)} more lines · Ctrl+G copies everything")
        text = Text("\n".join(shown), style=muted, no_wrap=False)
        body = self._lines(text, width - 6, muted)
        return self._prefixed(body, Segment("    ⎿ ", muted), Segment("      "))

    @override
    def render_line(self, y: int) -> Strip:
        width = self._width()
        style = self.rich_style
        if not self._rows:
            if self._placeholder and y == 1:
                return Strip(
                    [Segment(f"  {self._placeholder}", self._style("placeholder"))]
                ).extend_cell_length(width, style)
            return Strip.blank(width, style)
        line = round(self.scroll_y) + y
        if line >= self._offsets[-1]:
            return Strip.blank(width, style)
        index = self._row_at(line)
        row = self._rows[index]
        within = line - self._offsets[index] - row.gap
        if within < 0:
            return Strip.blank(width, style)
        if row.lines is None or row.width != width:
            # Painted on demand when a jump outran the settle step; the next
            # settle fixes the height around it.
            self._paint_row(row, width)
            assert row.lines is not None
        if within >= len(row.lines):
            return Strip.blank(width, style)
        strip = row.lines[within]
        if within == 0 and (row.live or row.key in {self._hover, self._cursor}):
            strip = self._header(row, width)
        strip = strip.extend_cell_length(width, style).crop(0, width)
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(line)) is not None:
            start, end = span
            end = strip.cell_length if end == -1 else end
            selected = self.screen.get_component_rich_style("screen--selection")
            strip = Strip.join(
                [
                    strip.crop(0, start),
                    strip.crop(start, end).apply_style(selected),
                    strip.crop(end, strip.cell_length),
                ]
            )
        return strip.apply_style(style).apply_offsets(0, line)

    def _theme_switched(self) -> None:
        self.call_after_refresh(self._theme_changed)

    def _theme_changed(self) -> None:
        for row in self._rows:
            row.lines = row.text = None
        self._settle()

    def _tick_live(self) -> None:
        if not self._rows:
            return
        height = self.scrollable_content_region.height or self.size.height
        top = round(self.scroll_y)
        for index in range(self._row_at(top), self._row_at(top + height) + 1):
            if self._rows[index].live:
                self.refresh_line(self._offsets[index] + self._rows[index].gap)

    def on_resize(self, event: events.Resize) -> None:
        if self.view is not None:
            width = self._width()
            if self.view.measured_width != width:
                anchor = self.reading_anchor()
                self.view.measured_width = width
                self.view.row_heights.clear()
                for row in self._rows:
                    row.lines = row.text = None
                    row.exact = False
                self._offsets_changed()
                if not self.view.follow_tail:
                    self.view.reading_anchor = anchor
            self._settle()

    # --- Selection --------------------------------------------------------

    @override
    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        lines: list[str] = []
        for index, row in enumerate(self._rows):
            lines.extend([""] * row.gap)
            if row.text is None:
                self._paint_row(row, self._width())
            assert row.text is not None
            lines.extend(row.text)
            del index
        return selection.extract("\n".join(lines)), "\n"

    @override
    def selection_updated(self, selection: Selection | None) -> None:
        self.refresh()

    # --- Mouse and keyboard -----------------------------------------------

    def _disclosure_at(self, event: events.MouseEvent) -> _Row | None:
        offset = event.get_content_offset(self)
        if offset is None or not self._rows:
            return None
        line = round(self.scroll_y) + offset.y
        if line >= self._offsets[-1]:
            return None
        index = self._row_at(line)
        row = self._rows[index]
        if row.kind in _DISCLOSURES and line == self._offsets[index] + row.gap:
            return row
        return None

    def on_mouse_move(self, event: events.MouseMove) -> None:
        row = self._disclosure_at(event)
        key = row.key if row else None
        if key != self._hover:
            previous, self._hover = self._hover, key
            self._refresh_header(previous)
            self._refresh_header(key)

    def on_leave(self, event: events.Leave) -> None:
        previous, self._hover = self._hover, None
        self._refresh_header(previous)

    def on_click(self, event: events.Click) -> None:
        selection = self.text_selection
        if selection is not None and selection.start != selection.end:
            return
        link = event.style.link
        if link:
            event.stop()
            self.post_message(self.LinkClicked(link))
            return
        row = self._disclosure_at(event)
        if row is not None:
            event.stop()
            self._cursor = row.key
            self.toggle(row.key)

    def _refresh_header(self, key: str | None) -> None:
        index = self._index.get(key) if key else None
        if index is not None:
            self.refresh_line(self._offsets[index] + self._rows[index].gap)

    def toggle(self, key: str) -> None:
        """Open or close one Tool or reasoning row, keeping its header still."""
        view = self.view
        index = self._index.get(key)
        if view is None or index is None:
            return
        header_top = self._offsets[index] + self._rows[index].gap
        screen_line = header_top - self.scroll_y
        if key in view.expanded_rows:
            view.expanded_rows.discard(key)
        else:
            view.expanded_rows.add(key)
        row = self._rows[index]
        self._paint_row(row, self._width())
        self._offsets_changed()
        visible = 0 <= screen_line < (self.scrollable_content_region.height or self.size.height)
        if visible:
            # The clicked header stays under the pointer; the body opens below.
            self._scroll_quietly(header_top - screen_line)
            view.follow_tail = self._at_end() and view.follow_tail
        else:
            self._place(index, 0)
        view.reading_anchor = self.reading_anchor()
        self.refresh()

    def _disclosure_keys(self) -> list[str]:
        return [row.key for row in self._rows if row.kind in _DISCLOSURES]

    def action_cursor(self, step: int) -> None:
        keys = self._disclosure_keys()
        if not keys:
            self.scroll_relative(y=step, animate=False)
            return
        if self._cursor not in keys:
            # Start from the step nearest the bottom of the screen.
            height = self.scrollable_content_region.height or self.size.height
            visible = [
                key
                for key in keys
                if self.scroll_y <= self._offsets[self._index[key]] < self.scroll_y + height
            ]
            target = (visible[-1] if step < 0 else visible[0]) if visible else keys[-1]
        else:
            position = keys.index(self._cursor) + step
            target = keys[max(0, min(len(keys) - 1, position))]
        previous, self._cursor = self._cursor, target
        self._refresh_header(previous)
        index = self._index[target]
        top = self._offsets[index] + self._rows[index].gap
        height = self.scrollable_content_region.height or self.size.height
        if not self.scroll_y <= top < self.scroll_y + height - 1:
            self._place(index, -max(0, height // 3))
            if self.view is not None:
                self.view.follow_tail = self._at_end()
                self.view.reading_anchor = self.reading_anchor()
        self._refresh_header(target)

    def action_open_close(self) -> None:
        if self._cursor is None:
            self.action_cursor(-1)
        if self._cursor is not None:
            self.toggle(self._cursor)

    def action_leave(self) -> None:
        self.post_message(self.Leave())

    def on_focus(self) -> None:
        self._refresh_header(self._cursor)

    def on_blur(self) -> None:
        self._refresh_header(self._cursor)
