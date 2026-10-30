"""Which Connections serve each Model in the user's own Chat requests."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from core.statistics._projection import MICROSECONDS_PER_HOUR


@dataclass(frozen=True)
class ModelRouteUse:
    """Completed Chat requests of one Model over one Connection.

    ``model`` is the bare ``provider/model``; ``connection_id`` is
    ``provider:connection``, or ``""`` for requests that recorded none (Chat
    before 2026-10-30).
    """

    model: str
    connection_id: str
    calls: int


# The user's own conversations: their Runs, Channel messages and scheduled
# instructions; Sub-Agent, Extension and background requests do not count.
_OWN_ORIGINS = ("user", "channel", "automation")


def load_model_route_use(
    connection: sqlite3.Connection, *, since: datetime
) -> tuple[ModelRouteUse, ...]:
    """Sum the usage cube from the hour containing ``since``, most requests first."""
    since_hour = int(since.timestamp() * 1_000_000) // MICROSECONDS_PER_HOUR
    rows = connection.execute(
        f"""
        SELECT model_key, connection_id, SUM(calls) FROM agg_usage
        WHERE hour >= ? AND kind = 'chat' AND status = 'completed'
            AND origin IN ({", ".join("?" for _ in _OWN_ORIGINS)})
        GROUP BY model_key, connection_id
        HAVING SUM(calls) > 0
        ORDER BY SUM(calls) DESC, model_key, connection_id
        """,
        (since_hour, *_OWN_ORIGINS),
    )
    return tuple(ModelRouteUse(model, connection_id, calls) for model, connection_id, calls in rows)
