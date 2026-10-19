#!/usr/bin/env python
"""Read a vBot Session's stored history read-only: transcript, text search, exact Tool Result.

The first output line summarizes the result; each further line is one JSON object.
Run ``python sessions.py <command> --help`` for the arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

# A Session's current history: its own entries that no edit superseded, plus the
# history a fork shares with the Session it was forked from.
VIEW_SQL = """
WITH target AS (
  SELECT session_key, generation_id, session_id FROM sessions
  WHERE project_id = ? AND agent_id = ? AND state = 'live' {session_filter}
), view AS (
  SELECT t.generation_id, t.session_id, e.entry_key, e.seq, e.entry_id, e.role, e.created_at
  FROM target AS t JOIN entries AS e ON e.session_key = t.session_key
  WHERE e.superseded_at_seq IS NULL
  UNION ALL
  SELECT t.generation_id, t.session_id, e.entry_key, e.seq, e.entry_id, e.role, e.created_at
  FROM target AS t JOIN session_lineage AS l ON l.session_key = t.session_key
  JOIN entries AS e ON e.session_key = l.ancestor_key
   AND e.seq >= l.from_seq AND e.seq < l.upto_seq
   AND (e.superseded_at_seq IS NULL OR e.superseded_at_seq >= l.as_of_seq)
)
"""

PREVIEW_BEFORE = 300
PREVIEW_LENGTH = 2000


class UsageError(Exception):
    """A wrong or missing input; the message names the corrected call."""


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        scope = _scope(arguments)
        database = _open(_data_dir(arguments.data_dir) / "sessions.db")
        try:
            lines = arguments.handler(database, scope, arguments)
            for line in lines:
                print(line)
        finally:
            database.close()
    except UsageError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 2
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def scoped(name: str, summary: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=summary, description=summary)
        command.add_argument(
            "--agent",
            help="Agent id, or agent@project for a Project Agent; default: the Agent of this Run",
        )
        command.add_argument(
            "--session", help="Session id; default: the Session of this Run (VBOT_RUN_SESSION_ID)"
        )
        command.add_argument(
            "--data-dir", help="vBot data directory; default: VBOT_DATA_DIR, else `vbot home`"
        )
        return command

    transcript = scoped(
        "transcript", "Print the User and Assistant Messages of a Session in order."
    )
    transcript.add_argument(
        "--after-seq", type=int, default=-1, help="Continue after this seq (from the last line)"
    )
    transcript.add_argument(
        "--limit", type=int, default=200, help="Messages per page (default 200)"
    )
    transcript.set_defaults(handler=_transcript)

    search = scoped("search", "Find exact text in Messages and Tool Results, ignoring case.")
    search.add_argument("text", help="Text to find")
    search.add_argument(
        "--all-sessions", action="store_true", help="Search every live Session of the Agent"
    )
    search.add_argument(
        "--role",
        action="append",
        choices=("user", "assistant", "tool"),
        help="Search only these roles (repeatable); default: all three",
    )
    search.add_argument("--limit", type=int, default=50, help="Matches to print (default 50)")
    search.set_defaults(handler=_search)

    result = scoped("result", "Print one exact Tool Result by its seq and generation.")
    result.add_argument("--seq", type=int, required=True, help="seq of the Tool Result entry")
    result.add_argument(
        "--generation", required=True, help="generation_id printed by search or transcript"
    )
    result.set_defaults(handler=_result)
    return parser


def _scope(arguments: argparse.Namespace) -> dict[str, str | None]:
    all_sessions = getattr(arguments, "all_sessions", False)
    session = arguments.session
    if arguments.agent:
        agent, _, project = arguments.agent.partition("@")
    else:
        agent = os.environ.get("VBOT_RUN_AGENT_ID", "")
        if not agent:
            raise UsageError("No Agent given outside a vBot Run: add --agent <agent-id>.")
        # An Identity Session's Project id is stored as the empty string, never NULL.
        project = os.environ.get("VBOT_RUN_PROJECT_ID", "")
        if session is None and not all_sessions:
            session = os.environ.get("VBOT_RUN_SESSION_ID")
    if session is None and not all_sessions:
        raise UsageError(
            "No Session given: add --session <session-id>; "
            f"`vbot session list {arguments.agent or agent} --all` lists them."
        )
    return {"project": project, "agent": agent, "session": session}


def _data_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    if os.environ.get("VBOT_DATA_DIR"):
        return Path(os.environ["VBOT_DATA_DIR"])
    command = shutil.which("vbot")
    if command is not None:
        completed = subprocess.run(
            [command, "home", "--output", "plain"], capture_output=True, text=True, check=False
        )
        for line in completed.stdout.splitlines():
            if line.startswith("data_dir:"):
                return Path(line.split(":", 1)[1].strip())
    raise UsageError("Cannot find the data directory: add --data-dir <data_dir from `vbot home`>.")


def _open(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise UsageError(
            f"No Session database at {path}: pass --data-dir with the data_dir `vbot home` "
            "prints on the server's computer."
        )
    database = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only = ON")
    return database


def _view(scope: dict[str, str | None]) -> tuple[str, list[Any]]:
    parameters: list[Any] = [scope["project"], scope["agent"]]
    session_filter = ""
    if scope["session"] is not None:
        session_filter = "AND session_id = ?"
        parameters.append(scope["session"])
    return VIEW_SQL.format(session_filter=session_filter), parameters


def _require_session(database: sqlite3.Connection, scope: dict[str, str | None]) -> None:
    if scope["session"] is None:
        return
    row = database.execute(
        "SELECT 1 FROM sessions WHERE project_id = ? AND agent_id = ? AND session_id = ?"
        " AND state = 'live'",
        (scope["project"], scope["agent"], scope["session"]),
    ).fetchone()
    if row is None:
        address = (scope["agent"] or "") + (f"@{scope['project']}" if scope["project"] else "")
        raise UsageError(
            f"No live Session {scope['session']} of {address}: "
            f"`vbot session list {address} --all` lists its Sessions."
        )


def _content(row: sqlite3.Row) -> Any:
    if row["blocks_json"] is not None:
        return json.loads(row["blocks_json"])
    return row["content"]


def _transcript(
    database: sqlite3.Connection, scope: dict[str, str | None], arguments: argparse.Namespace
) -> Iterator[str]:
    _require_session(database, scope)
    view, parameters = _view(scope)
    rows = database.execute(
        view
        + """
SELECT v.generation_id, v.seq, v.entry_id, v.role, v.created_at, x.content, x.blocks_json
FROM view AS v LEFT JOIN entry_text AS x ON x.entry_key = v.entry_key
WHERE v.role IN ('user', 'assistant') AND v.seq > ?
ORDER BY v.seq LIMIT ?
""",
        (*parameters, arguments.after_seq, arguments.limit + 1),
    ).fetchall()
    more = len(rows) > arguments.limit
    rows = rows[: arguments.limit]
    summary = f"transcript: {len(rows)} messages of Session {scope['session']}"
    if more:
        summary += f"; more after seq {rows[-1]['seq']}: add --after-seq {rows[-1]['seq']}"
    yield summary
    for row in rows:
        yield _json(
            {
                "generation_id": row["generation_id"],
                "seq": row["seq"],
                "message_id": row["entry_id"],
                "role": row["role"],
                "timestamp": row["created_at"],
                "content": _content(row),
            }
        )


def _search(
    database: sqlite3.Connection, scope: dict[str, str | None], arguments: argparse.Namespace
) -> Iterator[str]:
    _require_session(database, scope)
    roles = arguments.role or ["user", "assistant", "tool"]
    view, parameters = _view(scope)
    rows = database.execute(
        view
        + f"""
SELECT v.generation_id, v.session_id, v.seq, v.entry_id, v.role, v.created_at,
       COALESCE(x.content, x.search_text, '') AS text
FROM view AS v JOIN entry_text AS x ON x.entry_key = v.entry_key
WHERE v.role IN ({", ".join("?" for _ in roles)})
ORDER BY v.session_id, v.seq
""",
        (*parameters, *roles),
    )
    needle = arguments.text.casefold()
    hits: list[dict[str, Any]] = []
    more = False
    for row in rows:
        position = row["text"].casefold().find(needle)
        if position < 0:
            continue
        if len(hits) == arguments.limit:
            more = True
            break
        start = max(0, position - PREVIEW_BEFORE)
        hits.append(
            {
                "session_id": row["session_id"],
                "generation_id": row["generation_id"],
                "seq": row["seq"],
                "message_id": row["entry_id"],
                "role": row["role"],
                "timestamp": row["created_at"],
                "preview": row["text"][start : start + PREVIEW_LENGTH],
            }
        )
    summary = f"search: {len(hits)} matches"
    if more:
        summary += f"; more exist: narrow the text or raise --limit above {arguments.limit}"
    yield summary
    for hit in hits:
        yield _json(hit)


def _result(
    database: sqlite3.Connection, scope: dict[str, str | None], arguments: argparse.Namespace
) -> Iterator[str]:
    _require_session(database, scope)
    view, parameters = _view(scope)
    row = database.execute(
        view
        + """
SELECT v.seq, v.entry_id, c.name, x.content
FROM view AS v JOIN tool_calls AS c ON c.result_entry_key = v.entry_key
LEFT JOIN entry_text AS x ON x.entry_key = v.entry_key
WHERE v.generation_id = ? AND v.seq = ?
""",
        (*parameters, arguments.generation, arguments.seq),
    ).fetchone()
    if row is None:
        raise UsageError(
            f"No Tool Result at seq {arguments.seq} of generation {arguments.generation}: "
            "run search again and use the seq and generation_id it prints."
        )
    yield f"result: Tool {row['name']} at seq {row['seq']}"
    yield _json(
        {
            "seq": row["seq"],
            "message_id": row["entry_id"],
            "name": row["name"],
            "content": row["content"],
        }
    )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(main())
