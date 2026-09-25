"""Session views over own and inherited entries, fork lineage and its upkeep.

A Session's *current view* is a sorted list of disjoint seq ranges covering
``[0, MAX_SEQ)``. Each range reads one source Session: a lineage segment reads
an ancestor as of the ancestor's history at fork time, and every seq no segment
covers reads the Session's own non-superseded entries. One predicate decides
visibility for every range, so every current read shares one definition:

    session_key = source AND from_seq <= seq < upto_seq
    AND (superseded_at_seq IS NULL OR superseded_at_seq >= as_of_seq)

Own ranges use ``as_of_seq = MAX_SEQ``, which admits only non-superseded rows.
"""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from core.sessions import _store_codec, _store_fts

# The exclusive upper bound of every view; larger than any stored seq.
MAX_SEQ = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class ViewRange:
    """One contiguous seq range of a view, read from ``source_key``."""

    source_key: int
    from_seq: int
    upto_seq: int
    as_of_seq: int


def range_predicate(alias: str = "e") -> str:
    """The visibility predicate of one range, with four ``?`` parameters."""
    return (
        f"{alias}.session_key = ? AND {alias}.seq >= ? AND {alias}.seq < ? "
        f"AND ({alias}.superseded_at_seq IS NULL OR {alias}.superseded_at_seq >= ?)"
    )


def range_params(view_range: ViewRange, lower: int = 0, upper: int = MAX_SEQ) -> tuple[int, ...]:
    """Parameters of :func:`range_predicate` for *view_range* clipped to ``[lower, upper)``."""
    return (
        view_range.source_key,
        max(view_range.from_seq, lower),
        min(view_range.upto_seq, upper),
        view_range.as_of_seq,
    )


# Joins a ``view_sources`` CTE (see :func:`view_query`) to the entries it admits.
VIEW_MATCH = (
    "e.session_key = v.source_key AND e.seq >= v.from_seq AND e.seq < v.upto_seq "
    "AND (e.superseded_at_seq IS NULL OR e.superseded_at_seq >= v.as_of_seq)"
)


def view_ranges(connection: sqlite3.Connection, session_key: int) -> tuple[ViewRange, ...]:
    """Return the current view of one Session as sorted disjoint ranges."""
    ranges: list[ViewRange] = []
    cursor = 0
    for row in connection.execute(
        "SELECT from_seq, ancestor_key, upto_seq, as_of_seq FROM session_lineage "
        "WHERE session_key = ? ORDER BY from_seq",
        (session_key,),
    ):
        from_seq, ancestor_key, upto_seq, as_of_seq = (int(value) for value in row)
        if from_seq > cursor:
            ranges.append(ViewRange(session_key, cursor, from_seq, MAX_SEQ))
        ranges.append(ViewRange(ancestor_key, from_seq, upto_seq, as_of_seq))
        cursor = upto_seq
    ranges.append(ViewRange(session_key, cursor, MAX_SEQ, MAX_SEQ))
    return tuple(ranges)


def clip(ranges: Sequence[ViewRange], lower: int, upper: int) -> tuple[ViewRange, ...]:
    """Return *ranges* restricted to ``[lower, upper)``, empty ranges dropped."""
    clipped: list[ViewRange] = []
    for view_range in ranges:
        low = max(view_range.from_seq, lower)
        high = min(view_range.upto_seq, upper)
        if low < high:
            clipped.append(ViewRange(view_range.source_key, low, high, view_range.as_of_seq))
    return tuple(clipped)


def view_query(
    ranges: Sequence[ViewRange],
    select: str,
    *,
    joins: str = "",
    where: str = "",
    tail: str = "",
) -> tuple[str, list[Any]]:
    """Build one set-wise query over the entries *ranges* admit.

    The query reads ``view_sources AS v CROSS JOIN entries AS e`` plus any
    *joins*; *select*, *where* and *tail* may name them all. Callers append
    their own parameters for *joins*, *where* and *tail* after the returned ones.
    """
    if not ranges:
        raise ValueError("a view query needs at least one range")
    values = ", ".join("(?, ?, ?, ?)" for _ in ranges)
    params: list[Any] = []
    for view_range in ranges:
        params.extend(
            (view_range.source_key, view_range.from_seq, view_range.upto_seq, view_range.as_of_seq)
        )
    sql = (
        f"WITH view_sources (source_key, from_seq, upto_seq, as_of_seq) AS (VALUES {values}) "
        f"SELECT {select} FROM view_sources AS v CROSS JOIN entries AS e {joins} "
        f"WHERE {VIEW_MATCH}"
    )
    if where:
        sql += f" AND ({where})"
    if tail:
        sql += f" {tail}"
    return sql, params


def ordered_rows(
    connection: sqlite3.Connection,
    ranges: Sequence[ViewRange],
    *,
    columns: str,
    where: str = "",
    params: Sequence[Any] = (),
    lower: int = 0,
    upper: int = MAX_SEQ,
    descending: bool = False,
    limit: int | None = None,
) -> list[sqlite3.Row]:
    """Read view entries in seq order, one index range scan per range.

    *columns* and *where* name the entry as ``e``; *params* bind *where*.
    Reading stops once *limit* rows were found.
    """
    selected = clip(ranges, lower, upper)
    ordered = reversed(selected) if descending else selected
    direction = "DESC" if descending else "ASC"
    rows: list[sqlite3.Row] = []
    for view_range in ordered:
        remaining = None if limit is None else limit - len(rows)
        if remaining is not None and remaining <= 0:
            break
        sql = f"SELECT {columns} FROM entries AS e WHERE {range_predicate()}"
        if where:
            sql += f" AND ({where})"
        sql += f" ORDER BY e.seq {direction}"
        values: list[Any] = [*range_params(view_range), *params]
        if remaining is not None:
            sql += " LIMIT ?"
            values.append(remaining)
        rows.extend(connection.execute(sql, values))
    return rows


def entry_id_at(
    connection: sqlite3.Connection, ranges: Sequence[ViewRange], seq: int
) -> str | None:
    """Return the entry id stored at *seq* of the range holding it, superseded or not."""
    for view_range in ranges:
        if view_range.from_seq <= seq < view_range.upto_seq:
            row = connection.execute(
                "SELECT entry_id FROM entries WHERE session_key = ? AND seq = ?",
                (view_range.source_key, seq),
            ).fetchone()
            return None if row is None else str(row[0])
    return None


def create_fork_lineage(
    connection: sqlite3.Connection,
    *,
    source_key: int,
    fork_key: int,
    fork_point: int,
    source_next_seq: int,
) -> None:
    """Give a new fork the source's view up to *fork_point*, frozen as of now.

    The source's own ranges become segments on the source as of its current
    ``next_seq``; its segments are copied with their own ``as_of_seq``. The
    result is flat, so no read ever follows lineage recursively.
    """
    segments: list[tuple[int, int, int, int, int]] = []
    for view_range in clip(view_ranges(connection, source_key), 0, fork_point):
        as_of = source_next_seq if view_range.source_key == source_key else view_range.as_of_seq
        segments.append(
            (fork_key, view_range.from_seq, view_range.source_key, view_range.upto_seq, as_of)
        )
    connection.executemany(
        "INSERT INTO session_lineage (session_key, from_seq, ancestor_key, upto_seq, as_of_seq) "
        "VALUES (?, ?, ?, ?, ?)",
        segments,
    )


def truncated_ranges(
    connection: sqlite3.Connection, session_key: int, target_seq: int
) -> tuple[ViewRange, ...]:
    """Return the inherited ranges an edit at *target_seq* removes from the view."""
    return clip(
        tuple(
            view_range
            for view_range in view_ranges(connection, session_key)
            if view_range.source_key != session_key
        ),
        target_seq,
        MAX_SEQ,
    )


def truncate(connection: sqlite3.Connection, session_key: int, target_seq: int) -> None:
    """Drop every inherited seq at or after *target_seq* from one Session's view."""
    connection.execute(
        "DELETE FROM session_lineage WHERE session_key = ? AND from_seq >= ?",
        (session_key, target_seq),
    )
    connection.execute(
        "UPDATE session_lineage SET upto_seq = ? WHERE session_key = ? AND upto_seq > ?",
        (target_seq, session_key, target_seq),
    )


def superseded_candidates(ranges: Sequence[ViewRange]) -> tuple[str, list[Any]]:
    """Select superseded entries *ranges* admit: their search membership may hang on them."""
    return view_query(ranges, "e.entry_key", where="e.superseded_at_seq IS NOT NULL")


def detach_session(connection: sqlite3.Connection, session_key: int) -> None:
    """Prepare deleting one Session: descendants stop depending on it.

    Every descendant receives its own copy of what it inherits from the
    Session, and the Session's search rows go before its entries do. Search
    membership that only this Session's lineage kept alive is recomputed.
    Deleting the ``sessions`` row afterwards cascades the rest.
    """
    # Search rows are forgotten while their membership is still readable.
    _store_fts.fts_forget(
        connection, "SELECT entry_key FROM entries WHERE session_key = ?", (session_key,)
    )
    inherited = tuple(
        view_range
        for view_range in view_ranges(connection, session_key)
        if view_range.source_key != session_key
    )
    candidates: list[int] = []
    if inherited:
        sql, params = superseded_candidates(inherited)
        candidates = [int(row[0]) for row in connection.execute(sql, params)]
        _store_fts.fts_forget_keys(connection, candidates)
    for segment in connection.execute(
        "SELECT session_key, from_seq, upto_seq, as_of_seq FROM session_lineage "
        "WHERE ancestor_key = ? ORDER BY session_key, from_seq",
        (session_key,),
    ).fetchall():
        descendant_key, from_seq, upto_seq, as_of_seq = (int(value) for value in segment)
        copies = _store_codec.copy_entries(
            connection,
            source_key=session_key,
            target_key=descendant_key,
            view_range=ViewRange(session_key, from_seq, upto_seq, as_of_seq),
        )
        connection.execute(
            "DELETE FROM session_lineage WHERE session_key = ? AND from_seq = ?",
            (descendant_key, from_seq),
        )
        _store_fts.fts_index_keys(connection, copies)
        connection.execute(
            "UPDATE sessions SET history_revision = history_revision + 1, "
            "state_revision = state_revision + 1 WHERE session_key = ?",
            (descendant_key,),
        )
    connection.execute("DELETE FROM session_lineage WHERE session_key = ?", (session_key,))
    _store_fts.fts_index_keys(connection, candidates)
