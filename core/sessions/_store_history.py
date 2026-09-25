"""History reads in a supplied snapshot, each against one explicit view.

- **Current view**: the Session's own non-superseded entries plus the entries
  it inherits through lineage (see ``_store_lineage``). Provider history, the
  history Tool, Recall context, counts, notes and the Skill cache read it.
- **Own audit**: every entry the Session wrote itself, superseded ones
  included, from its fork point on. Materialized copies of inherited history
  precede the fork point and are not the Session's own.
- **Own spend**: the usage of the own audit's Assistant entries.
"""
# ruff: noqa: E501

from __future__ import annotations

import bisect
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.chat.errors import ChatSessionError
from core.sessions import _store_codec, _store_lineage, _store_values
from core.sessions._types import (
    SKILL_CONTEXT_NOTE_PREFIX,
    SKILL_TOOL_MESSAGE_NAME,
    JsonObject,
    SessionReadBatch,
    SessionReadCursor,
)

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._store_lineage import ViewRange
    from core.sessions._types import SessionAddress

# Entry predicates over ``entries AS e`` that read one side table each.
HAS_TEXT = (
    "EXISTS (SELECT 1 FROM entry_text AS t WHERE t.entry_key = e.entry_key "
    "AND (NULLIF(t.content, '') IS NOT NULL "
    "OR (t.blocks_json IS NOT NULL AND json_array_length(t.blocks_json) > 0)))"
)
TEXT_PREFIX = (
    "EXISTS (SELECT 1 FROM entry_text AS t WHERE t.entry_key = e.entry_key "
    "AND substr(t.content, 1, ?) = ?)"
)
RESULT_OF_TOOL = (
    "EXISTS (SELECT 1 FROM tool_calls AS c WHERE c.result_entry_key = e.entry_key AND c.name = ?)"
)
_REFLECTION_KINDS = "('reflection', 'memory_reflection', 'skill_reflection')"


def current_view(
    connection: sqlite3.Connection, address: SessionAddress
) -> tuple[sqlite3.Row, tuple[ViewRange, ...]]:
    """Return one live Session's row and its current view."""
    state = _store_values._require_live(connection, address)
    return state, _store_lineage.view_ranges(connection, int(state["session_key"]))


def own_floor(state: sqlite3.Row) -> int:
    """The first seq of the Session's own history: its fork point, else 0."""
    return int(state["fork_point_seq"] or 0)


def view_batch(
    connection: sqlite3.Connection,
    ranges: Sequence[ViewRange],
    *,
    where: str = "",
    params: Sequence[Any] = (),
    lower: int = 0,
    upper: int = _store_lineage.MAX_SEQ,
    descending: bool = False,
    limit: int | None = None,
) -> _store_codec.EntryBatch:
    """Read current-view entries in seq order with their side rows."""
    rows = _store_lineage.ordered_rows(
        connection,
        ranges,
        columns=_store_codec.ENTRY_COLUMNS,
        where=where,
        params=params,
        lower=lower,
        upper=upper,
        descending=descending,
        limit=limit,
    )
    return _store_codec.select_batch(connection, rows)


def view_seq(
    connection: sqlite3.Connection,
    ranges: Sequence[ViewRange],
    *,
    where: str,
    params: Sequence[Any] = (),
    lower: int = 0,
    upper: int = _store_lineage.MAX_SEQ,
    newest: bool = True,
) -> int | None:
    """Return the newest (or oldest) current-view seq matching *where*."""
    rows = _store_lineage.ordered_rows(
        connection,
        ranges,
        columns="e.seq",
        where=where,
        params=params,
        lower=lower,
        upper=upper,
        descending=newest,
        limit=1,
    )
    return None if not rows else int(rows[0][0])


def _own_audit(connection: sqlite3.Connection, state: sqlite3.Row, lower: int) -> list[sqlite3.Row]:
    return connection.execute(
        f"SELECT {_store_codec.ENTRY_COLUMNS} FROM entries AS e "
        "WHERE e.session_key = ? AND e.seq >= ? ORDER BY e.seq",
        (state["session_key"], max(lower, own_floor(state))),
    ).fetchall()


# -- Usage -------------------------------------------------------------------


_INPUT_ESTIMATED = (
    "CASE WHEN a.input_tokens_estimated IS NOT NULL THEN a.input_tokens_estimated "
    "WHEN a.output_tokens_estimated IS NOT NULL THEN 0 "
    "ELSE COALESCE(a.usage_estimated, 0) END"
)
_OUTPUT_ESTIMATED = (
    "CASE WHEN a.output_tokens_estimated IS NOT NULL THEN a.output_tokens_estimated "
    "WHEN a.input_tokens_estimated IS NOT NULL THEN 0 "
    "ELSE COALESCE(a.usage_estimated, 0) END"
)
_USAGE_SQL = f"""
SELECT
  COALESCE(SUM(CASE WHEN a.usage_present = 1
    AND ({_INPUT_ESTIMATED}) = 0 AND ({_OUTPUT_ESTIMATED}) = 0 THEN 1 ELSE 0 END), 0)
    AS measured_turns,
  COALESCE(SUM(CASE WHEN a.usage_present = 1
    AND (({_INPUT_ESTIMATED}) = 1 OR ({_OUTPUT_ESTIMATED}) = 1) THEN 1 ELSE 0 END), 0)
    AS estimated_turns,
  COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({_INPUT_ESTIMATED}) = 0
    AND (a.cache_read_tokens IS NOT NULL OR a.cache_write_tokens IS NOT NULL)
    THEN 1 ELSE 0 END), 0) AS cache_turns,
  COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({_OUTPUT_ESTIMATED}) = 0
    AND a.reasoning_tokens IS NOT NULL THEN 1 ELSE 0 END), 0) AS reasoning_turns,
  COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({_INPUT_ESTIMATED}) = 0
    THEN COALESCE(a.input_tokens, 0) ELSE 0 END), 0) AS input_tokens,
  COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({_OUTPUT_ESTIMATED}) = 0
    THEN COALESCE(a.output_tokens, 0) ELSE 0 END), 0) AS output_tokens,
  COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({_INPUT_ESTIMATED}) = 0
    THEN COALESCE(a.cache_read_tokens, 0) ELSE 0 END), 0) AS cache_read_tokens,
  COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({_INPUT_ESTIMATED}) = 0
    THEN COALESCE(a.cache_write_tokens, 0) ELSE 0 END), 0) AS cache_write_tokens,
  COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({_OUTPUT_ESTIMATED}) = 0
    THEN COALESCE(a.reasoning_tokens, 0) ELSE 0 END), 0) AS reasoning_tokens,
  COALESCE(SUM(CASE WHEN a.usage_present = 1 AND ({_INPUT_ESTIMATED}) = 0
    AND (a.cache_read_tokens IS NOT NULL OR a.cache_write_tokens IS NOT NULL)
    THEN COALESCE(a.input_tokens, 0) ELSE 0 END), 0) AS cache_input_tokens
FROM entries AS e
JOIN assistant_entries AS a ON a.entry_key = e.entry_key
WHERE e.session_key = ? AND e.role = 'assistant' AND e.seq >= ?
"""


def session_usage(connection: sqlite3.Connection, state: sqlite3.Row) -> tuple[JsonObject, int]:
    """Sum the Session's own spend: its Assistant usage, superseded turns included.

    A fork's inherited prefix is the origin's spend and is not counted.
    """
    row = connection.execute(_USAGE_SQL, (state["session_key"], own_floor(state))).fetchone()
    usage: JsonObject = {
        "measured_turns": int(row["measured_turns"]),
        "estimated_turns": int(row["estimated_turns"]),
        "cache_turns": int(row["cache_turns"]),
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
        "cache_read_tokens": int(row["cache_read_tokens"]),
        "cache_write_tokens": int(row["cache_write_tokens"]),
    }
    if int(row["reasoning_turns"]) > 0:
        usage["reasoning_turns"] = int(row["reasoning_turns"])
        usage["reasoning_tokens"] = int(row["reasoning_tokens"])
    return usage, int(row["cache_input_tokens"])


def status_snapshot(
    connection: sqlite3.Connection, address: SessionAddress
) -> Callable[[], tuple[str | None, int, JsonObject | None, JsonObject, int]]:
    """Read the status facts: first entry time, User count, latest usage and spend."""
    state, ranges = current_view(connection, address)
    first = _store_lineage.ordered_rows(connection, ranges, columns="e.created_at", limit=1)
    user_count = sum(
        int(
            connection.execute(
                "SELECT COUNT(*) FROM entries AS e WHERE e.role = 'user' AND "
                + _store_lineage.range_predicate(),
                _store_lineage.range_params(view_range),
            ).fetchone()[0]
        )
        for view_range in ranges
    )
    latest = view_batch(
        connection,
        ranges,
        where=(
            "e.role = 'assistant' AND EXISTS (SELECT 1 FROM assistant_entries AS a "
            "WHERE a.entry_key = e.entry_key AND a.usage_present = 1)"
        ),
        descending=True,
        limit=1,
    )
    usage, cache_input_tokens = session_usage(connection, state)
    return lambda: (
        None if not first else str(first[0][0]),
        user_count,
        None if not latest.rows else latest.message(latest.rows[0]).usage,
        usage,
        cache_input_tokens,
    )


# -- Whole views -----------------------------------------------------------------


def messages(
    connection: sqlite3.Connection, address: SessionAddress
) -> Callable[[], list[ChatMessage]]:
    """The own audit: every entry this Session wrote, superseded ones included."""
    state = _store_values._require_live(connection, address)
    batch = _store_codec.select_batch(connection, _own_audit(connection, state, 0))
    return batch.messages


def active_messages(
    connection: sqlite3.Connection, address: SessionAddress
) -> Callable[[], list[ChatMessage]]:
    """The current view, inherited history included."""
    _state, ranges = current_view(connection, address)
    return view_batch(connection, ranges).messages


def active_user_message_count(
    connection: sqlite3.Connection, address: SessionAddress, *, limit: int
) -> int:
    """Count at most ``limit`` current User entries without loading their content."""
    if limit <= 0:
        raise ChatSessionError("user Message count limit must be positive")
    _state, ranges = current_view(connection, address)
    return len(
        _store_lineage.ordered_rows(
            connection, ranges, columns="e.entry_key", where="e.role = 'user'", limit=limit
        )
    )


def tool_result_persisted(
    connection: sqlite3.Connection, address: SessionAddress, tool_call_id: str
) -> bool:
    """Report whether a Tool call this Session stored already has its result."""
    state = _store_values._require_live(connection, address)
    row = connection.execute(
        "SELECT 1 FROM tool_calls AS c JOIN entries AS e ON e.entry_key = c.entry_key "
        "WHERE c.call_id = ? AND e.session_key = ? AND c.result_entry_key IS NOT NULL LIMIT 1",
        (tool_call_id, state["session_key"]),
    ).fetchone()
    return row is not None


def tool_result_payload(
    connection: sqlite3.Connection, address: SessionAddress, payload_id: str, owner_name: str
) -> str | None:
    """Return the JSON text of one payload *owner_name* attached to a visible Tool Result.

    The payload is visible while the Tool Result entry it was stored with is in
    the Session's current view, own or inherited. A materialized copy keeps the
    payload id, but one view never shows the original and a copy together.
    """
    _state, ranges = current_view(connection, address)
    visible = " OR ".join(f"({_store_lineage.range_predicate('r')})" for _ in ranges)
    params: list[Any] = [payload_id, owner_name]
    for view_range in ranges:
        params.extend(_store_lineage.range_params(view_range))
    # CROSS JOIN fixes the order: the payload id index finds the few candidate
    # rows, and only their result entries are tested against the view.
    row = connection.execute(
        "SELECT p.payload_json FROM tool_result_payloads AS p "
        "CROSS JOIN tool_calls AS c ON c.call_key = p.call_key "
        "CROSS JOIN entries AS r ON r.entry_key = c.result_entry_key "
        f"WHERE p.payload_id = ? AND p.owner_name = ? AND ({visible}) LIMIT 1",
        params,
    ).fetchone()
    return None if row is None else str(row[0])


def latest_note(
    connection: sqlite3.Connection, address: SessionAddress, *, content_prefix: str
) -> Callable[[], ChatMessage | None]:
    """Load the newest current Note whose content starts with *content_prefix*."""
    if not content_prefix:
        raise ChatSessionError("note prefix must be non-empty")
    _state, ranges = current_view(connection, address)
    batch = view_batch(
        connection,
        ranges,
        where=f"e.role = 'note' AND {TEXT_PREFIX}",
        params=(len(content_prefix), content_prefix),
        descending=True,
        limit=1,
    )
    return lambda: None if not batch.rows else batch.message(batch.rows[0])


def current_skill_activation_messages(
    connection: sqlite3.Connection, address: SessionAddress
) -> Callable[[], list[ChatMessage]]:
    """Load the Skill activation candidates the current context can still see.

    Candidates are ``[skill-context]`` Notes and ``skill`` Tool Results after the
    newest current Compaction checkpoint; the caller decides which of them carry
    a valid activation.
    """
    _state, ranges = current_view(connection, address)
    checkpoint = view_seq(connection, ranges, where="e.role = 'compaction_checkpoint'")
    batch = view_batch(
        connection,
        ranges,
        where=(f"(e.role = 'note' AND {TEXT_PREFIX}) OR (e.role = 'tool' AND {RESULT_OF_TOOL})"),
        params=(len(SKILL_CONTEXT_NOTE_PREFIX), SKILL_CONTEXT_NOTE_PREFIX, SKILL_TOOL_MESSAGE_NAME),
        lower=0 if checkpoint is None else checkpoint + 1,
    )
    return batch.messages


# -- Read cursors ------------------------------------------------------------


def _cursor_of(state: sqlite3.Row) -> SessionReadCursor:
    next_seq = int(state["next_seq"])
    return SessionReadCursor(
        str(state["generation_id"]),
        int(state["history_revision"]),
        next_seq,
        state["last_entry_id"],
    )


def _cursor_continues(state: sqlite3.Row, cursor: SessionReadCursor) -> bool:
    """An edit or takeover raises the floor, so no cursor before it continues."""
    return (
        cursor.generation_id == str(state["generation_id"])
        and 0 <= cursor.next_seq <= int(state["next_seq"])
        and cursor.next_seq >= int(state["cursor_floor_seq"])
    )


def cursor_is_current(
    connection: sqlite3.Connection, address: SessionAddress, cursor: SessionReadCursor
) -> bool:
    """Whether *cursor* still names the Session's newest entry, without reading history."""
    state = _store_values._require_live(connection, address)
    return (
        _cursor_continues(state, cursor)
        and cursor.next_seq == int(state["next_seq"])
        and cursor.last_message_id == state["last_entry_id"]
    )


@dataclass
class HistoryDelta:
    """Entries selected after a cursor, decoded only after the transaction ends."""

    batch: _store_codec.EntryBatch
    audit_rows: list[sqlite3.Row]
    view_rows: list[sqlite3.Row]
    cursor: SessionReadCursor
    inherited_count: int


def message_rows_since(
    connection: sqlite3.Connection, address: SessionAddress, cursor: SessionReadCursor | None
) -> HistoryDelta | None:
    """Select the own audit and the current view after *cursor*, or all of both.

    ``None`` means the cursor cannot be continued: another generation, an edit
    or takeover after it, or a different entry before it.
    """
    state, ranges = current_view(connection, address)
    current = _cursor_of(state)
    start = 0
    if cursor is not None:
        if not _cursor_continues(state, cursor):
            return None
        if (
            cursor.next_seq == current.next_seq
            and cursor.last_message_id == current.last_message_id
        ):
            # The Session row names its newest entry, so a current cursor needs no read.
            return HistoryDelta(_store_codec.EntryBatch([]), [], [], current, 0)
        start = cursor.next_seq
        anchor = None if start == 0 else _store_lineage.entry_id_at(connection, ranges, start - 1)
        if anchor != cursor.last_message_id:
            return None
    audit = _own_audit(connection, state, start)
    view = _store_lineage.ordered_rows(
        connection, ranges, columns=_store_codec.ENTRY_COLUMNS, lower=start
    )
    floor = own_floor(state)
    unique = {int(row["entry_key"]): row for row in (*audit, *view)}
    return HistoryDelta(
        _store_codec.select_batch(connection, list(unique.values())),
        audit,
        view,
        current,
        sum(1 for row in view if int(row["seq"]) < floor),
    )


def read_batch(delta: HistoryDelta) -> SessionReadBatch:
    """Decode a selected delta into Messages, each entry once."""
    return SessionReadBatch(
        tuple(delta.batch.message(row) for row in delta.audit_rows),
        delta.cursor,
        tuple(delta.batch.message(row) for row in delta.view_rows),
        delta.inherited_count,
    )


# -- Runs ------------------------------------------------------------------------

_RUN_COLUMNS = "r.run_key, r.run_id, r.work_id, r.start_seq, r.end_entry_key"


def find_run(
    connection: sqlite3.Connection,
    state: sqlite3.Row,
    ranges: Sequence[ViewRange],
    *,
    run_id: str | None = None,
    work_id: str | None = None,
    finished: bool = False,
) -> sqlite3.Row | None:
    """Find a Run by id or Work id: the Session's own first, then the ones it inherits.

    An inherited Run counts when its summary entry is in the current view.
    With several matches, the latest-started one wins.
    """
    field, value = ("r.run_id", run_id) if run_id is not None else ("r.work_id", work_id)
    own = connection.execute(
        f"SELECT {_RUN_COLUMNS} FROM runs AS r WHERE r.session_key = ? AND {field} = ? "
        + ("AND r.end_entry_key IS NOT NULL " if finished else "")
        + "ORDER BY r.start_seq DESC, r.run_key DESC LIMIT 1",
        (state["session_key"], value),
    ).fetchone()
    if own is not None:
        return own  # type: ignore[no-any-return]
    session_key = int(state["session_key"])
    for view_range in reversed(ranges):
        if view_range.source_key == session_key:
            continue
        row = connection.execute(
            f"SELECT {_RUN_COLUMNS} FROM runs AS r JOIN entries AS e "
            f"ON e.entry_key = r.end_entry_key WHERE {field} = ? AND "
            + _store_lineage.range_predicate()
            + " ORDER BY r.start_seq DESC, r.run_key DESC LIMIT 1",
            (value, *_store_lineage.range_params(view_range)),
        ).fetchone()
        if row is not None:
            return row  # type: ignore[no-any-return]
    return None


def run_messages(
    connection: sqlite3.Connection, address: SessionAddress, run_id: str
) -> Callable[[], list[ChatMessage]]:
    """Load the current entries one Run wrote."""
    state, ranges = current_view(connection, address)
    run = find_run(connection, state, ranges, run_id=run_id)
    if run is None:
        return list
    return view_batch(connection, ranges, where="e.run_key = ?", params=(run["run_key"],)).messages


def _summary_batch(connection: sqlite3.Connection, run: sqlite3.Row) -> _store_codec.EntryBatch:
    return _store_codec.select_entries(connection, "e.entry_key = ?", (run["end_entry_key"],))


def run_summary(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    run_id: str | None = None,
    work_id: str | None = None,
) -> Callable[[], ChatMessage | None]:
    if (run_id is None) == (work_id is None):
        raise ChatSessionError("exactly one of run_id or work_id is required")
    state, ranges = current_view(connection, address)
    run = find_run(connection, state, ranges, run_id=run_id, work_id=work_id, finished=True)
    if run is None:
        return lambda: None
    batch = _summary_batch(connection, run)
    return lambda: batch.message(batch.rows[0])


def run_result(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    run_id: str | None = None,
    work_id: str | None = None,
    require_latest: bool = False,
) -> Callable[[], tuple[ChatMessage | None, ChatMessage, str | None] | None]:
    """Project one finished Run: its last text answer, summary and latest Tool."""
    if run_id is not None and work_id is not None:
        raise ChatSessionError("run_id and work_id cannot be combined")
    state, ranges = current_view(connection, address)
    if run_id is None and work_id is None:
        run = connection.execute(
            f"SELECT {_RUN_COLUMNS} FROM runs AS r WHERE r.session_key = ? "
            "AND r.end_entry_key IS NOT NULL ORDER BY r.start_seq DESC, r.run_key DESC LIMIT 1",
            (state["session_key"],),
        ).fetchone()
    else:
        run = find_run(connection, state, ranges, run_id=run_id, work_id=work_id, finished=True)
    if run is None:
        return lambda: None
    if require_latest:
        latest = connection.execute(
            "SELECT run_key FROM runs WHERE session_key = ? AND inherited = 0 "
            "ORDER BY start_seq DESC, run_key DESC LIMIT 1",
            (state["session_key"],),
        ).fetchone()
        if latest is None or int(latest[0]) != int(run["run_key"]):
            return lambda: None
    summary = _summary_batch(connection, run)
    assistant = _store_codec.select_entries(
        connection,
        f"e.run_key = ? AND e.role = 'assistant' AND {HAS_TEXT}",
        (run["run_key"],),
        tail="ORDER BY e.seq DESC LIMIT 1",
    )
    latest_tool = connection.execute(
        "SELECT c.name FROM entries AS e JOIN tool_calls AS c ON c.entry_key = e.entry_key "
        "WHERE e.run_key = ? ORDER BY e.seq DESC, c.ordinal DESC LIMIT 1",
        (run["run_key"],),
    ).fetchone()
    return lambda: (
        None if not assistant.rows else assistant.message(assistant.rows[0]),
        summary.message(summary.rows[0]),
        None if latest_tool is None else str(latest_tool[0]),
    )


def reflection_runs(connection: sqlite3.Connection, address: SessionAddress) -> list[JsonObject]:
    """Read the first finished own Run of each reflection fork of this Session.

    Only forks in the Session's own scope count: a reflection reviews its
    source for the same Agent.
    """
    source = _store_values._require_live(connection, address)
    rows = connection.execute(
        f"""
        SELECT s.session_id, r.run_id, r.status, r.started_at,
          (SELECT k.run_kind FROM session_run_kinds AS k
           WHERE k.session_key = s.session_key AND k.run_kind IN {_REFLECTION_KINDS}
           ORDER BY k.run_kind LIMIT 1) AS run_kind
        FROM sessions AS s
        JOIN runs AS r ON r.run_key = (
          SELECT run_key FROM runs
          WHERE session_key = s.session_key AND status <> 'running' AND inherited = 0
          ORDER BY start_seq, run_key LIMIT 1
        )
        WHERE s.fork_parent_key = ? AND s.state = 'live'
          AND s.project_id = ? AND s.agent_id = ?
          AND EXISTS (SELECT 1 FROM session_run_kinds AS k
            WHERE k.session_key = s.session_key AND k.run_kind IN {_REFLECTION_KINDS})
        ORDER BY r.started_at, r.run_id
        """,
        (source["session_key"], address.project_id or "", address.agent_id),
    ).fetchall()
    return [dict(row) for row in rows]


# -- History Tool reads -----------------------------------------------------------


def history_record_filter(roles: Sequence[str], excluded_tool_name: str) -> tuple[str, list[Any]]:
    """Match entries of *roles*, minus results of *excluded_tool_name* and empty Assistant turns.

    An Assistant turn is empty when it has no text, no reasoning and no Tool
    call other than *excluded_tool_name*.
    """
    selected_roles = tuple(dict.fromkeys(roles))
    if not selected_roles:
        return "0", []
    where = f"""e.role IN ({", ".join("?" for _ in selected_roles)})
      AND NOT (e.role = 'tool' AND {RESULT_OF_TOOL})
      AND NOT (
        e.role = 'assistant'
        AND NOT {HAS_TEXT}
        AND NOT EXISTS (
          SELECT 1 FROM assistant_reasoning AS r WHERE r.entry_key = e.entry_key
          AND (NULLIF(r.reasoning, '') IS NOT NULL
            OR (r.meta_json IS NOT NULL AND EXISTS (SELECT 1 FROM json_each(r.meta_json))))
        )
        AND NOT EXISTS (
          SELECT 1 FROM tool_calls AS k WHERE k.entry_key = e.entry_key AND k.name <> ?
        )
      )"""
    return where, [*selected_roles, excluded_tool_name, excluded_tool_name]


def _checkpoints(
    connection: sqlite3.Connection, ranges: Sequence[ViewRange], *, upper: int
) -> list[sqlite3.Row]:
    return _store_lineage.ordered_rows(
        connection,
        ranges,
        columns=(
            "e.seq, e.entry_id, e.created_at, "
            "(SELECT COALESCE(t.content, '') FROM entry_text AS t "
            "WHERE t.entry_key = e.entry_key) AS summary"
        ),
        where="e.role = 'compaction_checkpoint'",
        upper=upper,
    )


def _snapshot_is_current(
    connection: sqlite3.Connection,
    state: sqlite3.Row,
    ranges: Sequence[ViewRange],
    *,
    expected_generation_id: str,
    snapshot_sequence: int,
) -> bool:
    if str(state["generation_id"]) != expected_generation_id:
        return False
    return (
        view_seq(
            connection,
            ranges,
            where="e.role = 'compaction_checkpoint'",
            lower=snapshot_sequence,
            upper=snapshot_sequence + 1,
        )
        is not None
    )


def history_snapshot(
    connection: sqlite3.Connection, address: SessionAddress, *, snapshot_sequence: int | None = None
) -> tuple[str, list[tuple[int, str, str, str]]] | None:
    """Resolve the current Compaction checkpoints up to one snapshot."""
    state, ranges = current_view(connection, address)
    if snapshot_sequence is None:
        latest = view_seq(connection, ranges, where="e.role = 'compaction_checkpoint'")
        if latest is None:
            return None
        snapshot_sequence = latest
    elif not _snapshot_is_current(
        connection,
        state,
        ranges,
        expected_generation_id=str(state["generation_id"]),
        snapshot_sequence=snapshot_sequence,
    ):
        return None
    rows = _checkpoints(connection, ranges, upper=snapshot_sequence + 1)
    if not rows:
        return None
    return str(state["generation_id"]), [
        (int(row["seq"]), str(row["entry_id"]), str(row["created_at"]), str(row["summary"] or ""))
        for row in rows
    ]


def _records(batch: _store_codec.EntryBatch) -> Callable[[], list[tuple[int, ChatMessage]]]:
    return lambda: [(int(row["seq"]), batch.message(row)) for row in batch.rows]


def history_records(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    expected_generation_id: str,
    snapshot_sequence: int,
    lower_sequence: int,
    upper_sequence: int,
    roles: Sequence[str],
    direction: str,
    cursor_sequence: int | None,
    limit: int,
    excluded_tool_name: str,
) -> Callable[[], list[tuple[int, ChatMessage]] | None]:
    """Read one bounded batch of current entries strictly between two seqs."""
    if direction not in {"start", "end"}:
        raise ChatSessionError("history direction must be start or end")
    if limit <= 0:
        raise ChatSessionError("history record limit must be positive")
    record_filter, filter_params = history_record_filter(roles, excluded_tool_name)
    state, ranges = current_view(connection, address)
    if not _snapshot_is_current(
        connection,
        state,
        ranges,
        expected_generation_id=expected_generation_id,
        snapshot_sequence=snapshot_sequence,
    ):
        return lambda: None
    lower, upper = lower_sequence + 1, upper_sequence
    if cursor_sequence is not None:
        if direction == "start":
            lower = max(lower, cursor_sequence)
        else:
            upper = min(upper, cursor_sequence + 1)
    batch = view_batch(
        connection,
        ranges,
        where=record_filter,
        params=filter_params,
        lower=lower,
        upper=upper,
        descending=direction == "end",
        limit=limit,
    )
    return _records(batch)


def history_section_stats(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    expected_generation_id: str,
    snapshot_sequence: int,
    sections: Sequence[tuple[int, int]],
    excluded_tool_name: str,
) -> dict[int, tuple[int, str | None, str | None]] | None:
    """Aggregate default-role History overview facts for selected sections."""
    record_filter, filter_params = history_record_filter(
        ("user", "assistant", "error"), excluded_tool_name
    )
    state, ranges = current_view(connection, address)
    if not _snapshot_is_current(
        connection,
        state,
        ranges,
        expected_generation_id=expected_generation_id,
        snapshot_sequence=snapshot_sequence,
    ):
        return None
    result: dict[int, tuple[int, str | None, str | None]] = {}
    if not sections:
        return result
    # One pass over the covering range; each section is a slice of its seqs.
    rows = _store_lineage.ordered_rows(
        connection,
        ranges,
        columns="e.seq, e.created_at",
        where=record_filter,
        params=filter_params,
        lower=min(lower for lower, _upper in sections) + 1,
        upper=max(upper for _lower, upper in sections),
    )
    sequences = [int(row["seq"]) for row in rows]
    for lower_sequence, upper_sequence in sections:
        start = bisect.bisect_right(sequences, lower_sequence)
        end = bisect.bisect_left(sequences, upper_sequence)
        if start >= end:
            result[upper_sequence] = (0, None, None)
            continue
        result[upper_sequence] = (
            end - start,
            str(rows[start]["created_at"]),
            str(rows[end - 1]["created_at"]),
        )
    return result


def history_around(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    expected_generation_id: str,
    snapshot_sequence: int,
    lower_sequence: int,
    upper_sequence: int,
    roles: Sequence[str],
    message_id: str,
    before: int,
    after: int,
    excluded_tool_name: str,
) -> Callable[[], tuple[bool, list[tuple[int, ChatMessage]]] | None]:
    """Read a bounded eligible neighborhood around the earliest matching entry id."""
    record_filter, filter_params = history_record_filter(roles, excluded_tool_name)
    state, ranges = current_view(connection, address)
    if not _snapshot_is_current(
        connection,
        state,
        ranges,
        expected_generation_id=expected_generation_id,
        snapshot_sequence=snapshot_sequence,
    ):
        return lambda: None
    exists = view_seq(connection, ranges, where="e.entry_id = ?", params=(message_id,)) is not None
    lower, upper = lower_sequence + 1, upper_sequence
    anchor = view_seq(
        connection,
        ranges,
        where=f"e.entry_id = ? AND {record_filter}",
        params=(message_id, *filter_params),
        lower=lower,
        upper=upper,
        newest=False,
    )
    if anchor is None:
        return lambda: (exists, [])
    earlier = _store_lineage.ordered_rows(
        connection,
        ranges,
        columns=_store_codec.ENTRY_COLUMNS,
        where=record_filter,
        params=filter_params,
        lower=lower,
        upper=anchor,
        descending=True,
        limit=before,
    )
    rest = _store_lineage.ordered_rows(
        connection,
        ranges,
        columns=_store_codec.ENTRY_COLUMNS,
        where=record_filter,
        params=filter_params,
        lower=anchor,
        upper=upper,
        limit=after + 1,
    )
    batch = _store_codec.select_batch(connection, [*reversed(earlier), *rest])
    return lambda: (exists, _records(batch)())


# -- Recall ------------------------------------------------------------------------

_RECALL_TEXT = (
    "(SELECT substr(COALESCE(t.content, t.search_text, ''), 1, 801) FROM entry_text AS t "
    "WHERE t.entry_key = e.entry_key)"
)
_RECALL_HAS_TEXT = (
    "EXISTS (SELECT 1 FROM entry_text AS t WHERE t.entry_key = e.entry_key "
    "AND length(COALESCE(t.content, t.search_text, '')) > 0)"
)


def recall_context(
    connection: sqlite3.Connection, address: SessionAddress, message_id: str
) -> list[JsonObject]:
    """Read the enclosing question and final answer of one current hit.

    Only current conversation text is projected; the caller already has the
    hit. An anchor an edit replaced returns no context.
    """
    state = _store_values._find_live(connection, address)
    if state is None:
        return []
    ranges = _store_lineage.view_ranges(connection, int(state["session_key"]))
    anchor = _store_lineage.ordered_rows(
        connection,
        ranges,
        columns="e.seq, e.role",
        where="e.entry_id = ?",
        params=(message_id,),
        descending=True,
        limit=1,
    )
    if not anchor or anchor[0]["role"] not in {"user", "assistant"}:
        return []
    seq = int(anchor[0]["seq"])
    first = view_seq(connection, ranges, where="e.role = 'user'", upper=seq + 1)
    if first is None:
        return []
    following = view_seq(connection, ranges, where="e.role = 'user'", lower=seq + 1, newest=False)
    answer = view_seq(
        connection,
        ranges,
        where=f"e.role = 'assistant' AND {_RECALL_HAS_TEXT}",
        lower=first + 1,
        upper=_store_lineage.MAX_SEQ if following is None else following,
    )
    wanted = sorted({value for value in (first, answer) if value is not None and value != seq})
    rows = [
        row
        for value in wanted
        for row in _store_lineage.ordered_rows(
            connection,
            ranges,
            columns=f"e.seq, e.entry_id, e.role, e.created_at, {_RECALL_TEXT} AS text",
            lower=value,
            upper=value + 1,
        )
    ]
    return [
        {
            "message_index": int(row["seq"]),
            "message_id": str(row["entry_id"]),
            "role": str(row["role"]),
            "timestamp": str(row["created_at"]),
            "text": str(row["text"])[:800],
            "truncated": len(str(row["text"])) > 800,
        }
        for row in rows
        if row["text"]
    ]
