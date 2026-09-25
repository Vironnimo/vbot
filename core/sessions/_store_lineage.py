"""Session views over own and inherited entries, fork lineage and its upkeep.

A Session's *current view* is a sorted list of disjoint seq ranges covering
``[0, MAX_SEQ)``. Each range reads one source Session: a lineage segment reads
an ancestor as of the ancestor's history at fork time, and every seq no segment
covers reads the Session's own non-superseded entries. One predicate decides
visibility for every range (:func:`admits`), so every current read, the search
indexes and the search candidates share one definition:

    session_key = source AND from_seq <= seq < upto_seq
    AND (superseded_at_seq IS NULL OR superseded_at_seq >= as_of_seq)

Own ranges use ``as_of_seq = MAX_SEQ``, which admits only non-superseded rows
(:func:`own_current`). This module has no store dependencies, so the schema's
search views can build on it.
"""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# The exclusive upper bound of every view; larger than any stored seq.
MAX_SEQ = (1 << 63) - 1


def admits(entry: str, source: str, from_seq: str, upto_seq: str, as_of_seq: str) -> str:
    """The visibility predicate: whether a range admits the entry aliased *entry*.

    The other arguments are SQL expressions for the range's source Session key,
    its seq bounds ``[from_seq, upto_seq)`` and the source's ``next_seq`` it is
    frozen at. Every variant of the predicate is built from this fragment.
    """
    return (
        f"{entry}.session_key = {source} AND {entry}.seq >= {from_seq} AND {entry}.seq < {upto_seq} "
        f"AND ({entry}.superseded_at_seq IS NULL OR {entry}.superseded_at_seq >= {as_of_seq})"
    )


def own_current(entry: str = "e") -> str:
    """The supersession part of :func:`admits` for an own range (``as_of_seq = MAX_SEQ``).

    A Session's own entries never lie in a seq its lineage covers, so this
    alone decides whether an own entry is in its owner's current view.
    """
    return f"{entry}.superseded_at_seq IS NULL"


def segment_admits(entry: str = "e", segment: str = "l") -> str:
    """:func:`admits` for the ``session_lineage`` row aliased *segment*."""
    return admits(
        entry,
        f"{segment}.ancestor_key",
        f"{segment}.from_seq",
        f"{segment}.upto_seq",
        f"{segment}.as_of_seq",
    )


@dataclass(frozen=True, slots=True)
class ViewRange:
    """One contiguous seq range of a view, read from ``source_key``."""

    source_key: int
    from_seq: int
    upto_seq: int
    as_of_seq: int


def range_predicate(alias: str = "e") -> str:
    """:func:`admits` for one range, with four ``?`` parameters (:func:`range_params`)."""
    return admits(alias, "?", "?", "?", "?")


def range_params(view_range: ViewRange, lower: int = 0, upper: int = MAX_SEQ) -> tuple[int, ...]:
    """Parameters of :func:`range_predicate` for *view_range* clipped to ``[lower, upper)``."""
    return (
        view_range.source_key,
        max(view_range.from_seq, lower),
        min(view_range.upto_seq, upper),
        view_range.as_of_seq,
    )


# Joins a ``view_sources`` CTE (see :func:`view_query`) to the entries it admits.
VIEW_MATCH = admits("e", "v.source_key", "v.from_seq", "v.upto_seq", "v.as_of_seq")


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
