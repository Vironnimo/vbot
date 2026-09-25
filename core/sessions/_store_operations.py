"""Compound Session operations: each one domain step in one transaction.

A Compaction commit and a history edit each change history, prompt state and
Continuation state together, so no caller ever observes (or crashes between)
half of one.
"""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from typing import TYPE_CHECKING

from core.chat.errors import ChatSessionError
from core.sessions import (
    _store_codec,
    _store_continuation,
    _store_fts,
    _store_history,
    _store_lineage,
    _store_mutations,
    _store_prompts,
    _store_timeline,
    _store_values,
)
from core.sessions._types import JsonObject, PromptEpoch, SeenSkillsUpdate

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._types import SessionAddress, SessionReadCursor

_NOT_EDITABLE = (
    "Only a current User message with plain text, sent after the latest Agent takeover, "
    "can be edited"
)


def commit_compaction(
    connection: sqlite3.Connection,
    address: SessionAddress,
    checkpoint: ChatMessage,
    *,
    since: SessionReadCursor,
    epoch: PromptEpoch,
    run_id: str | None,
) -> tuple[_store_history.HistoryDelta, str] | None:
    """Append a Compaction *checkpoint* only while *since* still names the newest entry.

    The checkpoint starts a new prompt epoch: its pins and seen Skills replace
    the Session's, and a new prompt-cache affinity id starts a new cache
    lineage. Returns the entries after *since* with that id, or ``None``
    (nothing written) when another writer advanced the Session first.
    """
    if checkpoint.role != "compaction_checkpoint":
        raise ChatSessionError("a Compaction commit appends one Compaction checkpoint")
    if not _store_history.cursor_is_current(connection, address, since):
        return None
    _store_mutations.append_messages(connection, address, [checkpoint], run_id=run_id)
    session_key = int(_store_values._require_live(connection, address)["session_key"])
    _store_prompts.replace_pins(connection, session_key, epoch.pins)
    if epoch.seen_skills is not None:
        _store_prompts.replace_seen_skills(connection, session_key, epoch.seen_skills)
    affinity_id = _store_prompts.rotate_affinity(connection, session_key)
    delta = _store_history.message_rows_since(connection, address, since)
    assert delta is not None
    return delta, affinity_id


def _edit_target(
    connection: sqlite3.Connection,
    ranges: Sequence[_store_lineage.ViewRange],
    target_message_id: str,
) -> int:
    rows = _store_lineage.ordered_rows(
        connection,
        ranges,
        columns=_store_codec.ENTRY_COLUMNS,
        where="e.entry_id = ? AND e.role = 'user'",
        params=(target_message_id,),
        descending=True,
        limit=1,
    )
    if not rows:
        raise ChatSessionError(_NOT_EDITABLE)
    batch = _store_codec.select_batch(connection, rows)
    takeover = _store_timeline.latest_takeover(connection, ranges)
    if not _store_timeline._editable(batch, rows[0], takeover):
        raise ChatSessionError(_NOT_EDITABLE)
    return int(rows[0]["seq"])


def apply_edit(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    target_message_id: str,
    messages: Sequence[ChatMessage],
    run_id: str | None,
    seen_skills: SeenSkillsUpdate | None = None,
    continuation_records: Sequence[JsonObject] = (),
) -> tuple[_store_history.HistoryDelta, str]:
    """Replace history from one User message on, in one transaction.

    Everything from the target on leaves the current view: the Session's own
    entries are superseded and inherited history is cut at the target. A
    ``history_edit`` marker (kept only in the own audit) and *messages* follow
    at the end. The Continuation starts over from *continuation_records*, a
    new prompt-cache lineage starts, and a generated title is cleared when the
    edit replaces the first User message. Returns the complete history after
    the edit with the new prompt-cache affinity id.
    """
    from core.chat.messages import ChatMessage

    if not any(message.role == "user" for message in messages):
        raise ChatSessionError("a history edit appends its replacement User message")
    for message in messages:
        _store_codec.validate_appendable(message)
    state, ranges = _store_history.current_view(connection, address)
    session_key = int(state["session_key"])
    target = _edit_target(connection, ranges, target_message_id)
    reset_title = (
        _store_history.view_seq(connection, ranges, where="e.role = 'user'", upper=target) is None
    )
    run_key = None
    if run_id is not None:
        run = connection.execute(
            "SELECT run_key FROM runs WHERE session_key = ? AND run_id = ? "
            "AND status = 'running' AND inherited = 0",
            (session_key, run_id),
        ).fetchone()
        if run is None:
            raise ChatSessionError("Messages require an admitted, running Run in this Session")
        run_key = int(run[0])
    marker_seq = int(state["next_seq"])

    # Forget every entry whose search membership the edit may end, then re-index
    # the same set: only those a view still holds come back.
    own_sql = (
        "SELECT entry_key FROM entries WHERE session_key = ? AND seq >= ? "
        "AND superseded_at_seq IS NULL"
    )
    candidates = [int(row[0]) for row in connection.execute(own_sql, (session_key, target))]
    truncated = _store_lineage.truncated_ranges(connection, session_key, target)
    if truncated:
        sql, params = _store_lineage.superseded_candidates(truncated)
        candidates.extend(int(row[0]) for row in connection.execute(sql, params))
    _store_fts.fts_forget_keys(connection, candidates)
    connection.execute(
        "UPDATE entries SET superseded_at_seq = ? WHERE session_key = ? AND seq >= ? "
        "AND superseded_at_seq IS NULL",
        (marker_seq, session_key, target),
    )
    _store_lineage.truncate(connection, session_key, target)
    marker = ChatMessage.history_edit(target_message_id)
    marker.validate()
    _store_codec.insert_entry(
        connection, session_key, marker_seq, marker, run_key=run_key, superseded_at_seq=marker_seq
    )
    connection.execute(
        "UPDATE sessions SET next_seq = ?, cursor_floor_seq = ? WHERE session_key = ?",
        (marker_seq + 1, marker_seq + 1, session_key),
    )
    _store_mutations.append_messages(connection, address, messages, run_id=run_id)
    _store_fts.fts_index_keys(connection, candidates)

    connection.execute("DELETE FROM continuations WHERE session_key = ?", (session_key,))
    _store_continuation.append_continuation(connection, address, continuation_records)
    if seen_skills is not None:
        _store_prompts.record_seen_skills(connection, session_key, seen_skills)
    affinity_id = _store_prompts.rotate_affinity(connection, session_key)
    if reset_title:
        connection.execute(
            "UPDATE sessions SET auto_title = NULL, auto_title_initialized = 0, "
            "state_revision = state_revision + 1 WHERE session_key = ? "
            "AND (auto_title IS NOT NULL OR auto_title_initialized = 1)",
            (session_key,),
        )
    delta = _store_history.message_rows_since(connection, address, None)
    assert delta is not None
    return delta, affinity_id
