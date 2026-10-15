"""Canonical ownership of deferred Extension Tool outcomes.

An opaque receipt binds one Extension to one original invocation. Recovery follows
that Session's live generation after moves, never a reused address or a fork.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from core.sessions import _store_external_notes, _store_values
from core.sessions._types import SessionAddress
from core.utils.ids import new_id


def capture_tool_receipt(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    owner_name: str,
    run_id: str,
    tool_call_id: str,
    tool_name: str,
    execution_owner: dict[str, Any] | None,
) -> str | None:
    state = _store_values._find_live(connection, address)
    if state is None:
        return None
    row = connection.execute(
        "SELECT c.call_key FROM tool_calls c JOIN entries e ON e.entry_key=c.entry_key "
        "JOIN runs r ON r.run_key=e.run_key WHERE e.session_key=? AND r.run_id=? "
        "AND r.inherited=0 AND r.status='running' AND c.call_id=? AND c.name=? "
        "AND e.superseded_at_seq IS NULL",
        (state["session_key"], run_id, tool_call_id, tool_name),
    ).fetchone()
    if row is None:
        return None
    existing = connection.execute(
        "SELECT receipt_id FROM extension_tool_receipts WHERE owner_name=? AND call_key=?",
        (owner_name, row["call_key"]),
    ).fetchone()
    if existing is not None:
        return str(existing["receipt_id"])
    receipt = new_id("etr")
    connection.execute(
        "INSERT INTO extension_tool_receipts(receipt_id, owner_name, call_key, "
        "execution_owner_json) VALUES(?,?,?,?)",
        (receipt, owner_name, row["call_key"], json.dumps(execution_owner)),
    )
    return receipt


def resolve_tool_receipt(
    connection: sqlite3.Connection, receipt_id: str, *, owner_name: str
) -> dict[str, Any] | None:
    row = connection.execute(
        "SELECT s.project_id,s.agent_id,s.session_id,s.generation_id,r.run_id,r.status,"
        "c.call_id,c.name,c.result_entry_key,t.execution_owner_json "
        "FROM extension_tool_receipts t JOIN tool_calls c ON c.call_key=t.call_key "
        "JOIN entries e ON e.entry_key=c.entry_key JOIN runs r ON r.run_key=e.run_key "
        "JOIN sessions s ON s.session_key=e.session_key WHERE t.receipt_id=? AND t.owner_name=? "
        "AND s.state='live' AND e.superseded_at_seq IS NULL AND r.inherited=0",
        (receipt_id, owner_name),
    ).fetchone()
    if row is None or row["result_entry_key"] is not None:
        return None
    return {
        "project_id": row["project_id"] or None,
        "agent_id": row["agent_id"],
        "session_id": row["session_id"],
        "generation_id": row["generation_id"],
        "run_id": row["run_id"],
        "tool_call_id": row["call_id"],
        "tool_name": row["name"],
        "execution_owner": json.loads(row["execution_owner_json"]),
    }


def deliver_tool_outcome(
    connection: sqlite3.Connection, receipt_id: str, content: str, *, owner_name: str
) -> bool:
    context = resolve_tool_receipt(connection, receipt_id, owner_name=owner_name)
    if context is None:
        return False
    address = SessionAddress(context["project_id"], context["agent_id"], context["session_id"])
    _store_external_notes.append_notes_once(
        connection,
        address,
        f"extension:{owner_name}/task:{receipt_id}",
        [content],
        generation_id=context["generation_id"],
    )
    return True
