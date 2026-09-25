"""The WebUI chat history projection: pages, appends and their Runs, from one snapshot."""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from core.chat.errors import ChatSessionError
from core.sessions import _store_codec, _store_history, _store_lineage, _store_values
from core.sessions._io import _encode_chat_history_cursor
from core.sessions._types import (
    JsonObject,
    SessionBackgroundRecord,
    SessionChatHistorySnapshot,
    SessionMessagePage,
)
from core.sessions.errors import SessionPageCursorError

if TYPE_CHECKING:
    from core.sessions._store_lineage import ViewRange
    from core.sessions._types import SessionAddress


def _role_filter(excluded_roles: Sequence[str]) -> tuple[str, list[Any]]:
    excluded = tuple(dict.fromkeys(excluded_roles))
    if not excluded:
        return "", []
    return f"e.role NOT IN ({', '.join('?' for _ in excluded)})", list(excluded)


def _can_append(state: sqlite3.Row, after: tuple[str, int] | None) -> bool:
    """An edit changes an existing prefix; an append cursor cannot describe that."""
    if after is None:
        return False
    generation, sequence = after
    return (
        generation == state["generation_id"]
        and 0 <= sequence <= int(state["next_seq"])
        and sequence >= int(state["cursor_floor_seq"])
    )


def _editable(batch: _store_codec.EntryBatch, row: sqlite3.Row, takeover_seq: int) -> bool:
    """A User entry with plain text, no named sender and no later Agent takeover."""
    key = int(row["entry_key"])
    text = batch.text.get(key)
    return (
        str(row["role"]) == "user"
        and text is not None
        and text["content"] is not None
        and key not in batch.senders
        and int(row["seq"]) > takeover_seq
    )


def latest_takeover(connection: sqlite3.Connection, ranges: Sequence[ViewRange]) -> int:
    """The seq of the newest current Agent takeover, or -1."""
    seq = _store_history.view_seq(connection, ranges, where="e.role = 'agent_takeover'")
    return -1 if seq is None else seq


def _page(
    connection: sqlite3.Connection,
    state: sqlite3.Row,
    ranges: Sequence[ViewRange],
    *,
    limit: int | None,
    before_message_id: str | None,
    before_sequence: int | None,
    expected_generation_id: str | None,
    excluded_roles: Sequence[str],
    complete_run_segment: bool,
) -> tuple[_store_codec.EntryBatch, bool, frozenset[str], int | None]:
    where, params = _role_filter(excluded_roles)
    cutoff = int(state["next_seq"])
    if before_sequence is not None:
        if str(state["generation_id"]) != expected_generation_id:
            raise SessionPageCursorError("before cursor is invalid")
        if (
            _store_history.view_seq(
                connection,
                ranges,
                where=where,
                params=params,
                lower=before_sequence,
                upper=before_sequence + 1,
            )
            is None
        ):
            raise SessionPageCursorError("before cursor is invalid")
        cutoff = before_sequence
    elif before_message_id is not None:
        found = _store_history.view_seq(
            connection,
            ranges,
            where=" AND ".join(filter(None, [where, "e.entry_id = ?"])),
            params=[*params, before_message_id],
            newest=False,
        )
        if found is None:
            raise SessionPageCursorError("before must reference an active message id")
        cutoff = found
    rows = _store_lineage.ordered_rows(
        connection,
        ranges,
        columns=_store_codec.ENTRY_COLUMNS,
        where=where,
        params=params,
        upper=cutoff,
        descending=limit is not None,
        limit=limit,
    )
    if limit is not None:
        rows.reverse()
        if rows and complete_run_segment and rows[0]["run_key"] is not None:
            start = connection.execute(
                "SELECT start_seq FROM runs WHERE run_key = ?", (rows[0]["run_key"],)
            ).fetchone()
            if start is not None and int(start[0]) < int(rows[0]["seq"]):
                rows = _store_lineage.ordered_rows(
                    connection,
                    ranges,
                    columns=_store_codec.ENTRY_COLUMNS,
                    where=where,
                    params=params,
                    lower=int(start[0]),
                    upper=cutoff,
                )
    batch = _store_codec.select_batch(connection, rows)
    if not rows:
        return batch, False, frozenset(), None
    page_floor = int(rows[0]["seq"])
    has_more = (
        _store_history.view_seq(connection, ranges, where=where, params=params, upper=page_floor)
        is not None
    )
    takeover = latest_takeover(connection, ranges)
    editable = frozenset(str(row["entry_id"]) for row in rows if _editable(batch, row, takeover))
    return batch, has_more, editable, page_floor


def _background_records(
    connection: sqlite3.Connection,
    ranges: Sequence[ViewRange],
    *,
    tool_names: Sequence[str],
    note_marker: str | None,
    lower: int,
    upper: int,
) -> list[SessionBackgroundRecord]:
    """Read current Tool results of ``tool_names`` and Notes holding ``note_marker``.

    Only the values a status fold reads are selected, in seq order.
    """
    clipped = _store_lineage.clip(ranges, lower, upper)
    names = tuple(dict.fromkeys(tool_names))
    candidates: list[str] = []
    params: list[Any] = []
    if note_marker:
        candidates.append("(e.role = 'note' AND instr(t.content, ?) > 0)")
        params.append(note_marker)
    if names:
        candidates.append(f"(e.role = 'tool' AND c.name IN ({', '.join('?' for _ in names)}))")
        params.extend(names)
    if not candidates or not clipped:
        return []
    sql, view_params = _store_lineage.view_query(
        clipped,
        "e.role, c.name, t.content",
        joins=(
            "LEFT JOIN entry_text AS t ON t.entry_key = e.entry_key "
            "LEFT JOIN tool_calls AS c ON c.result_entry_key = e.entry_key"
        ),
        where=f"e.role IN ('note', 'tool') AND ({' OR '.join(candidates)})",
        tail="ORDER BY e.seq",
    )
    return [
        SessionBackgroundRecord(
            role=str(row["role"]),
            name=None if row["name"] is None else str(row["name"]),
            content=None if row["content"] is None else str(row["content"]),
        )
        for row in connection.execute(sql, [*view_params, *params])
    ]


def _context_batch(
    connection: sqlite3.Connection, ranges: Sequence[ViewRange]
) -> _store_codec.EntryBatch:
    """The current entries from the newest usage anchor on: context-usage inputs."""
    anchor = _store_history.view_seq(
        connection,
        ranges,
        where=(
            "(e.role = 'assistant' AND EXISTS (SELECT 1 FROM assistant_entries AS a "
            "WHERE a.entry_key = e.entry_key AND a.usage_present = 1)) "
            "OR (e.role = 'compaction_checkpoint' AND EXISTS (SELECT 1 FROM checkpoint_entries AS k "
            "WHERE k.entry_key = e.entry_key AND k.context_tokens_after IS NOT NULL))"
        ),
    )
    if anchor is None:
        return _store_codec.EntryBatch([])
    return _store_history.view_batch(connection, ranges, lower=anchor)


def _page_runs(
    connection: sqlite3.Connection,
    ranges: Sequence[ViewRange],
    batch: _store_codec.EntryBatch,
    *,
    through: int,
    incremental: bool,
) -> tuple[JsonObject, ...]:
    """Describe the page's Runs in start order and whether the page holds each one whole."""
    run_keys = sorted({int(row["run_key"]) for row in batch.rows if row["run_key"] is not None})
    if not run_keys:
        return ()
    floor = min(int(row["seq"]) for row in batch.rows)
    runs = connection.execute(
        "SELECT r.run_key, r.run_id, r.status, r.start_seq, t.seq AS terminal_seq "
        "FROM runs AS r LEFT JOIN entries AS t ON t.entry_key = r.end_entry_key "
        "WHERE r.run_key IN (SELECT value FROM json_each(?)) ORDER BY r.start_seq, r.run_key",
        (_store_values._key_list(run_keys),),
    ).fetchall()
    result = []
    for run in runs:
        terminal = run["terminal_seq"]
        result.append(
            {
                "run_id": run["run_id"],
                "status": run["status"],
                "start_sequence": run["start_seq"],
                "terminal_sequence": terminal,
                "complete": terminal is not None
                and terminal < through
                and (
                    incremental
                    or int(run["start_seq"]) >= floor
                    # Only a Run that starts below the page can have records before it.
                    or _store_history.view_seq(
                        connection,
                        ranges,
                        where="e.run_key = ? AND e.role NOT IN ('system', 'note', 'history_edit')",
                        params=(run["run_key"],),
                        upper=floor,
                    )
                    is None
                ),
            }
        )
    return tuple(result)


def chat_history_snapshot(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    limit: int | None,
    before_message_id: str | None,
    before_sequence: int | None,
    expected_generation_id: str | None,
    excluded_roles: Sequence[str],
    complete_run_segment: bool,
    background_tool_names: Sequence[str] = (),
    background_note_marker: str | None = None,
    after: tuple[str, int] | None = None,
    skip_unchanged: bool = False,
) -> Callable[[], SessionChatHistorySnapshot]:
    """Read one WebUI history projection of the current view from a single snapshot.

    With ``skip_unchanged``, an ``after`` cursor already at the Session's end
    reads only the Session row and returns an empty ``unchanged`` snapshot.
    """
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
        raise ChatSessionError("message page limit must be a positive integer")
    state, ranges = _store_history.current_view(connection, address)
    generation = str(state["generation_id"])
    incremental = _can_append(state, after)
    through = int(state["next_seq"])
    if skip_unchanged and incremental and after is not None and after[1] == through:
        unchanged = _unchanged_history_snapshot(generation, through)
        return lambda: unchanged
    if incremental:
        assert after is not None
        through = min(after[1] + (limit or 500), through)
        where, params = _role_filter(excluded_roles)
        # Bound raw entries, hidden Notes included: an empty visible batch still
        # advances the cursor, and every continuation has a finite amount of work.
        batch = _store_history.view_batch(
            connection, ranges, where=where, params=params, lower=after[1], upper=through
        )
        has_more, page_floor = False, None
        takeover = latest_takeover(connection, ranges)
        editable = frozenset(
            str(row["entry_id"]) for row in batch.rows if _editable(batch, row, takeover)
        )
    else:
        batch, has_more, editable, page_floor = _page(
            connection,
            state,
            ranges,
            limit=limit,
            before_message_id=before_message_id,
            before_sequence=before_sequence,
            expected_generation_id=expected_generation_id,
            excluded_roles=excluded_roles,
            complete_run_segment=complete_run_segment,
        )
    usage, _cache_input_tokens = _store_history.session_usage(connection, state)
    context = _context_batch(connection, ranges)
    background = _background_records(
        connection,
        ranges,
        tool_names=background_tool_names,
        note_marker=background_note_marker,
        lower=after[1] if incremental and after is not None else 0,
        upper=through,
    )
    has_newer = through < int(state["next_seq"])
    runs = _page_runs(connection, ranges, batch, through=through, incremental=incremental)

    def decode() -> SessionChatHistorySnapshot:
        return SessionChatHistorySnapshot(
            page=SessionMessagePage(
                messages=tuple(batch.messages()),
                has_more=has_more,
                editable_message_ids=editable,
                before_cursor=(
                    _encode_chat_history_cursor(generation, page_floor)
                    if has_more and page_floor is not None
                    else None
                ),
                record_sequences=tuple(int(row["seq"]) for row in batch.rows),
                record_run_ids=tuple(
                    None if row["run_key"] is None else batch.run_ids[int(row["run_key"])]
                    for row in batch.rows
                ),
            ),
            session_usage=usage,
            context_messages=tuple(context.messages()),
            background_records=tuple(background),
            generation_id=generation,
            after_cursor=_encode_chat_history_cursor(generation, through),
            incremental=incremental,
            has_newer=has_newer,
            runs=runs,
        )

    return decode


def _unchanged_history_snapshot(generation: str, through: int) -> SessionChatHistorySnapshot:
    return SessionChatHistorySnapshot(
        page=SessionMessagePage(messages=(), has_more=False),
        session_usage={},
        context_messages=(),
        background_records=(),
        generation_id=generation,
        after_cursor=_encode_chat_history_cursor(generation, through),
        incremental=True,
        unchanged=True,
    )
