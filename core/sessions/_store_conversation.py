"""Bounded conversation inspection over canonical current-view entries."""

from __future__ import annotations

import sqlite3

from core.chat.errors import ChatSessionError
from core.sessions import _store_lineage, _store_values
from core.sessions._types import (
    SessionAddress,
    SessionConversationMessage,
    SessionConversationPage,
)
from core.sessions.errors import SessionNotFoundError

_TEXT = (
    "(SELECT COALESCE(t.content, t.search_text, '') FROM entry_text AS t "
    "WHERE t.entry_key = e.entry_key)"
)
_HAS_TEXT = (
    "EXISTS (SELECT 1 FROM entry_text AS t WHERE t.entry_key = e.entry_key "
    "AND length(COALESCE(t.content, t.search_text, '')) > 0)"
)
_CONVERSATION = f"e.role IN ('user', 'assistant') AND {_HAS_TEXT}"
_SENDER_KIND = "(SELECT s.source_kind FROM user_entry_senders AS s WHERE s.entry_key = e.entry_key)"
_SENDER_ID = "(SELECT s.sender_id FROM user_entry_senders AS s WHERE s.entry_key = e.entry_key)"


def conversation_page(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    expected_generation_id: str,
    limit: int,
    before_sequence: int | None,
    after_sequence: int | None,
    max_text_chars: int,
    include_peer_messages: bool,
) -> SessionConversationPage:
    """Read text and continuation facts in one transaction, bounded in SQLite."""
    if not 1 <= limit <= 100 or not 1 <= max_text_chars <= 20_000:
        raise ChatSessionError("Conversation limits are outside their supported range")
    if before_sequence is not None and after_sequence is not None:
        raise ChatSessionError("Use one conversation paging direction")
    if any(value is not None and value < 0 for value in (before_sequence, after_sequence)):
        raise ChatSessionError("Conversation sequence cannot be negative")
    state = _store_values._require_live(connection, address)
    if str(state["generation_id"]) != expected_generation_id:
        raise SessionNotFoundError("The Session reference belongs to an earlier generation")
    ranges = _store_lineage.view_ranges(connection, int(state["session_key"]))
    columns = (
        "e.seq, e.entry_id, e.role, e.created_at, "
        f"substr({_TEXT}, 1, {max_text_chars + 1}) AS text, "
        f"{_SENDER_KIND} AS sender_kind, {_SENDER_ID} AS sender_id"
    )
    rows = _store_lineage.ordered_rows(
        connection,
        ranges,
        columns=columns,
        where=(
            _CONVERSATION
            if include_peer_messages
            else f"{_CONVERSATION} AND COALESCE({_SENDER_KIND}, 'user') <> 'session'"
        ),
        lower=after_sequence if after_sequence is not None else 0,
        upper=before_sequence if before_sequence is not None else _store_lineage.MAX_SEQ,
        descending=after_sequence is None,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    if after_sequence is None:
        rows.reverse()
    latest_user = _store_lineage.ordered_rows(
        connection,
        ranges,
        columns=columns,
        where=f"e.role = 'user' AND {_HAS_TEXT} AND COALESCE({_SENDER_KIND}, 'user') <> 'session'",
        descending=True,
        limit=1,
    )
    latest_message = _store_lineage.ordered_rows(
        connection, ranges, columns="e.seq", where=_CONVERSATION, descending=True, limit=1
    )
    latest_response = _store_lineage.ordered_rows(
        connection,
        ranges,
        columns="e.seq",
        where=f"{_CONVERSATION} AND COALESCE({_SENDER_KIND}, 'user') <> 'session'",
        descending=True,
        limit=1,
    )
    latest_completion = _store_lineage.ordered_rows(
        connection,
        ranges,
        columns=(
            "e.seq, (SELECT r.status FROM runs AS r WHERE r.end_entry_key = e.entry_key) AS status"
        ),
        where="e.role = 'run_summary'",
        descending=True,
        limit=1,
    )
    return SessionConversationPage(
        generation_id=expected_generation_id,
        history_revision=int(state["history_revision"]),
        next_sequence=int(state["next_seq"]),
        cursor_floor_sequence=int(state["cursor_floor_seq"]),
        messages=tuple(_message(row, max_text_chars) for row in rows),
        latest_user=_message(latest_user[0], max_text_chars) if latest_user else None,
        latest_message_sequence=int(latest_message[0]["seq"]) if latest_message else None,
        latest_response_sequence=int(latest_response[0]["seq"]) if latest_response else None,
        latest_completion_sequence=int(latest_completion[0]["seq"]) if latest_completion else None,
        latest_completion_status=str(latest_completion[0]["status"]) if latest_completion else None,
        has_more=has_more,
    )


def _message(row: sqlite3.Row, limit: int) -> SessionConversationMessage:
    text = str(row["text"] or "")
    return SessionConversationMessage(
        sequence=int(row["seq"]),
        message_id=str(row["entry_id"]),
        role=str(row["role"]),
        timestamp=str(row["created_at"]),
        text=text[:limit],
        truncated=len(text) > limit,
        sender_kind=str(row["sender_kind"]) if row["sender_kind"] is not None else None,
        sender_id=str(row["sender_id"]) if row["sender_id"] is not None else None,
    )
