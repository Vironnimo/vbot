"""Terminal event streams, rendered snapshots and Agent attention notices."""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncGenerator
from typing import Any

from core.utils.paths import model_path

from ._terminal_catalog import TerminalCatalog
from ._terminal_state import (
    TERMINAL_DELIVERY_TAIL_LINES,
    TERMINAL_NOTICE_MESSAGE_CAP_CHARS,
    TERMINAL_STATUS_DEFAULT_LINES,
    TerminalAttention,
    TerminalCursorError,
    TerminalSession,
    TerminalStreamEvent,
    _attention_data,
)


class TerminalEvents:
    """Publish ordered Terminal output, authoritative snapshots and catalog state."""

    def __init__(self, catalog: TerminalCatalog) -> None:
        self._catalog = catalog

    def _publish_output(self, session: TerminalSession, text: str) -> None:
        session.stream_sequence += 1
        session.stream.publish(
            {
                "type": "terminal_output",
                "sequence": session.stream_sequence,
                "data": text,
            }
        )

    def _publish_snapshot(self, session: TerminalSession) -> None:
        session.stream_sequence += 1
        session.stream.publish(
            {
                "type": "terminal_snapshot",
                "sequence": session.stream_sequence,
                "terminal": self._catalog._operator_summary(session),
                "ansi": session.renderer.ansi_snapshot(),
            }
        )

    def _publish_state(self, session: TerminalSession) -> None:
        session.stream_sequence += 1
        session.stream.publish(
            {
                "type": "terminal_state",
                "sequence": session.stream_sequence,
                "terminal": self._catalog._operator_summary(session),
            }
        )
        self._catalog._notify_changed(session.terminal_id)

    async def snapshot(
        self,
        session: TerminalSession,
        *,
        lines: int = TERMINAL_STATUS_DEFAULT_LINES,
        start_line: int | None = None,
        include_name: bool = True,
    ) -> dict[str, Any]:
        """Return one bounded rendered status page.

        ``start_line`` addresses the whole buffer by absolute zero-based line
        (Hermes ``read_terminal`` contract); omit it for the newest page.
        """
        async with session.lock:
            try:
                if start_line is not None:
                    scrollback = session.renderer.page_from(start_line, lines)
                else:
                    scrollback = session.renderer.page(before=None, limit=lines)
            except ValueError as error:
                raise TerminalCursorError(str(error)) from error
            data = _snapshot_data(session, scrollback)
            if include_name:
                data["name"] = session.name
            return data

    def read_for_operator(self, session: TerminalSession) -> dict[str, Any]:
        """Read a bounded screen without changing any Agent's observation or binding."""
        return {
            "terminal": self._catalog._operator_summary(session),
            "screen": session.renderer.screen_text(),
            "scrollback": session.renderer.page(before=None, limit=30),
            "bracketed_paste": session.renderer.bracketed_paste_enabled,
        }

    async def watch_for_operator(
        self, session: TerminalSession
    ) -> AsyncGenerator[TerminalStreamEvent, None]:
        """Yield an authoritative VT snapshot followed by sequenced live events."""
        async with session.lock:
            after_sequence = session.stream_sequence
            ready: TerminalStreamEvent = {
                "type": "terminal_ready",
                "sequence": after_sequence,
                "terminal": self._catalog._operator_summary(session),
                "ansi": session.renderer.ansi_snapshot(),
            }
        yield ready
        if session.state in {"exited", "error"}:
            return
        async with contextlib.aclosing(
            session.stream.subscribe(after_sequence=after_sequence)
        ) as events:
            async for event in events:
                yield event


def _snapshot_data(session: TerminalSession, scrollback: dict[str, Any]) -> dict[str, Any]:
    attention = session.attention
    data = {
        "terminal_id": session.terminal_id,
        "state": session.state,
        "command": session.command,
        "title": session.renderer.title,
        "arguments": list(session.arguments),
        "workdir": model_path(session.cwd),
        "exit_code": session.exit_code,
        "started_at": session.started_at.isoformat(),
        "finished_at": session.finished_at.isoformat() if session.finished_at else None,
        "columns": session.renderer.columns,
        "rows": session.renderer.rows,
        "screen_revision": session.renderer.revision,
        "attention_revision": session.attention_revision,
        "screen": session.renderer.screen_text(),
        "scrollback": scrollback,
        "attention": _attention_data(attention),
        "log_file": model_path(session.log_path) if session.log_path is not None else None,
    }
    observed = session.observed_screen
    if observed is not None and observed[0] < session.last_resize_screen_revision:
        _, previous_columns, previous_rows = observed
        data["size_change"] = {
            "previous_columns": previous_columns,
            "previous_rows": previous_rows,
            "notice": (
                "The terminal was resized since your previous screen result. "
                f"Previous size: {previous_columns} columns x {previous_rows} rows. "
                f"Current size: {session.renderer.columns} columns x "
                f"{session.renderer.rows} rows. Use positions from the current screen."
            ),
        }
    return data


def _attention_body(session: TerminalSession, attention: TerminalAttention) -> str:
    heading = {
        "output_settled": "Terminal output settled",
        "exited": "Terminal process exited",
        "error": "Terminal failure",
    }[attention.kind]
    sections = [
        f"### Terminal Session — {heading}",
        f"Terminal id: {session.terminal_id}",
        f"State: {session.state}",
        f"Attention revision: {attention.revision}",
        attention.summary,
    ]
    if attention.kind == "output_settled":
        sections.extend(
            (
                (
                    "Decide from the screen tail below whether to act or keep waiting; "
                    "quiet output does not prove completion. For prompt input, pass the "
                    "screen_revision below as expected_screen_revision. Use status only for "
                    "missing screen/history context. Send replies to this terminal_id."
                ),
                f"screen_revision: {session.renderer.revision}",
                "",
                "```",
                session.renderer.screen_tail(TERMINAL_DELIVERY_TAIL_LINES),
                "```",
            )
        )
    elif attention.details:
        sections.append(json.dumps(attention.details, ensure_ascii=False, indent=2))
    body = "\n".join(sections)
    if len(body) <= TERMINAL_NOTICE_MESSAGE_CAP_CHARS:
        return body
    return body[:TERMINAL_NOTICE_MESSAGE_CAP_CHARS] + "\n[attention details truncated]"
