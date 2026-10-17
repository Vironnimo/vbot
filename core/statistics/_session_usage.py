"""Session tree usage: the Run aggregates of one Session and its Sub-Agents.

Like an Extension group report, it sums the ``agg_run_models`` rows of the
Sessions' own Runs; requests without a Run stay in global accounting only.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from core.statistics._rollups import USAGE_MEASURES
from core.statistics._sections.common import JsonObject, Totals


def session_tree_usage(
    connection: sqlite3.Connection, session_key: int | None, subagent_keys: Sequence[int]
) -> JsonObject:
    """Return ``{session, subagents, total}`` usage Totals of the given index Sessions.

    ``session_key`` is the Session's own index key and ``subagent_keys`` those
    of its Sub-Agents; an absent key or a Run not yet aggregated contributes
    nothing.
    """
    own = Totals()
    below = Totals()
    keys = [key for key in (session_key, *subagent_keys) if key is not None]
    # One bound JSON array keeps any tree size below SQLite's variable limit.
    for key, *measures in connection.execute(
        f"""
        SELECT session_key, {", ".join(f"SUM({name})" for name in USAGE_MEASURES)}
        FROM agg_run_models
        WHERE session_key IN (SELECT value FROM json_each(?))
        GROUP BY session_key
        """,
        (json.dumps(list(dict.fromkeys(keys))),),
    ):
        (own if key == session_key else below).add(measures)
    total = Totals()
    total.merge(own)
    total.merge(below)
    return {"session": own.json(), "subagents": below.json(), "total": total.json()}
