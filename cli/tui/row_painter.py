"""Rich/Markdown row painting, independent of transcript virtualization and caches."""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING, override

from rich.console import RenderableType
from rich.markdown import Markdown
from rich.segment import Segment
from rich.style import Style
from rich.syntax import PygmentsSyntaxTheme, TokenType
from rich.text import Text
from rich.theme import Theme as RichTheme
from textual.strip import Strip
from textual.widget import Widget

from cli.tui.rendering import (
    duration_text,
    item_heading,
    item_text,
    tool_detail,
    tool_time,
    tool_title,
)

if TYPE_CHECKING:
    from cli.tui.transcript import _Row

# A disclosure paints one header and, when expanded, its bounded detail body.
_DISCLOSURES = {"tool_call", "reasoning"}
_DETAIL_LINES = 400
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_FAILED = {"failed", "error"}


class _CodeTheme(PygmentsSyntaxTheme):
    """A Pygments token palette on the accessor's own code surface."""

    def __init__(self, name: str, surface: str) -> None:
        super().__init__(name)
        self._surface = Style(bgcolor=surface)

    @override
    def get_background_style(self) -> Style:
        return self._surface

    @override
    def get_style_for_token(self, token_type: TokenType) -> Style:
        # Tokens keep their colour, not the palette's own background.
        style = super().get_style_for_token(token_type)
        return Style(
            color=style.color, bold=style.bold, italic=style.italic, underline=style.underline
        )


class RowPainter:
    """Paint rows in the widget's palette without accessing viewport state."""

    def __init__(self, widget: Widget) -> None:
        self.widget = widget
        self._markdown_styles: tuple[str, RichTheme, PygmentsSyntaxTheme] | None = None

    def style(self, name: str) -> Style:
        # Combined with the transcript's own style, so translucent theme colours
        # such as $text-muted resolve against the background.
        style = self.widget.get_component_rich_style(f"transcript--{name}")
        if name == "hover":
            return Style(bgcolor=style.bgcolor)
        return style

    def _markdown(self, text: str, width: int, style: Style | None = None) -> list[Strip]:
        styles, code_theme = self._markdown_theme()
        renderable = Markdown(
            text or " ",
            # Rich hands the theme object to its Syntax, which accepts one.
            code_theme=code_theme,  # type: ignore[arg-type]
            hyperlinks=True,
            inline_code_lexer=None,
        )
        with self.widget.app.console.use_theme(styles):
            return self._lines(renderable, width, style)

    def _markdown_theme(self) -> tuple[RichTheme, PygmentsSyntaxTheme]:
        """Rich's Markdown styles in the accessor's palette instead of its defaults."""
        if self._markdown_styles is None or self._markdown_styles[0] != self.widget.app.theme:
            variables = self.widget.app.get_css_variables()
            accent = self.style("bar").color
            muted = self.style("muted").color
            # Code reads as the WebUI's: a warm token palette on the surface
            # one step off the background, and inline code as a raised chip.
            code_theme = _CodeTheme(
                "gruvbox-dark" if self.widget.app.current_theme.dark else "gruvbox-light",
                variables["surface"],
            )
            styles = {
                "markdown.code": Style(bgcolor=variables["raised"]),
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
            self._markdown_styles = (
                self.widget.app.theme,
                RichTheme(styles, inherit=True),
                code_theme,
            )
        return self._markdown_styles[1], self._markdown_styles[2]

    def _lines(
        self, renderable: RenderableType, width: int, style: Style | None = None
    ) -> list[Strip]:
        console = self.widget.app.console
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

    def paint(
        self, row: _Row, width: int, *, expanded: bool, highlighted: bool, cursor: bool
    ) -> list[Strip]:
        item, kind = row.item, row.kind
        muted = self.style("muted")
        if kind == "user" or kind == "user_message":
            background = self.style("user")
            bar = Segment("▌ ", self.style("bar"))
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
            lines = [self.header(row, width, highlighted=highlighted, cursor=cursor)]
            if expanded:
                lines.extend(self._disclosure_body(row, width))
            return lines
        if kind == "history_gap":
            return [Strip([Segment("  ⋯ earlier steps load as they come into view", muted)])]
        if kind == "run_status":
            status = str(item.get("status") or "")
            style = self.style("error") if status == "failed" else self.style("warning")
            mark = "✗" if status == "failed" else "■"
            return [Strip([Segment(f"  {mark} Run {status}", style)])]
        if kind == "run_footer":
            worked = duration_text(float(item["durationMs"]))
            return [Strip([Segment(f"  ─ Worked for {worked}", muted)])]
        if kind == "model_fallback":
            text = " ".join((item_text(item) or "Model fallback").split())
            return [Strip([Segment(f"  ⚠ {text}", self.style("warning"))])]
        if kind == "goal_event":
            text = " ".join(item_text(item).split())
            return [Strip([Segment(f"  ◆ {text}", muted)]).crop(0, width)]
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
        style = self.style("error") if kind in {"notice", "error"} else muted
        if title:
            lines.insert(0, Strip([Segment(f"  {title}", style + Style(bold=True))]))
        return lines

    def header(self, row: _Row, width: int, *, highlighted: bool, cursor: bool) -> Strip:
        item = row.item
        muted = self.style("muted")
        if row.kind == "reasoning":
            if item.get("streaming"):
                label = "Thinking…"
            else:
                duration = item.get("durationMs", item.get("durationEstimateMs"))
                label = (
                    f"Thought for {duration_text(float(duration))}"
                    if isinstance(duration, (int, float)) and duration >= 1000
                    else "Thought"
                )
            glyph = _SPINNER[int(monotonic() * 10) % len(_SPINNER)] if row.live else "∴"
            text = Text.assemble(("  " + glyph + " ", muted), (label, muted + Style(italic=True)))
        else:
            status = str(item.get("status") or "")
            if row.live:
                mark = (_SPINNER[int(monotonic() * 10) % len(_SPINNER)], self.style("warning"))
            elif status in _FAILED:
                mark = ("●", self.style("error"))
            elif status in {"cancelled", "partial", "interrupted"}:
                mark = ("●", self.style("warning"))
            else:
                mark = ("●", self.style("success"))
            name, _, summary = tool_title(item).partition(" · ")
            if row.live:
                # The spinner already says it runs.
                summary = summary.removesuffix(f" · {status}").removesuffix(status)
            text = Text.assemble(
                ("  ", ""),
                mark,
                (" ", ""),
                (name, self.style("tool")),
                (f"  {summary}" if summary else "", muted),
            )
            # The time stays whole; a long summary gives way to it.
            if elapsed := tool_time(item):
                text.truncate(max(1, width - len(elapsed) - 2), overflow="ellipsis")
                text.append(f"  {elapsed}", muted)
        text.truncate(width, overflow="ellipsis")
        if highlighted:
            text.stylize(self.style("hover"))
            text.pad_right(width - text.cell_len)
            if cursor:
                text.stylize(Style(bold=True), 0, 3)
        return Strip(list(text.render(self.widget.app.console)), text.cell_len)

    def _disclosure_body(self, row: _Row, width: int) -> list[Strip]:
        muted = self.style("muted")
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
