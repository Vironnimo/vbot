"""Atomic temporary Session bindings, delivery receipts and Run attribution."""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from core.chat.errors import ChatSessionError
from core.sessions import _store_codec, _store_fts, _store_values
from core.sessions._types import JsonObject
from core.sessions.errors import SessionStoreCorruptError

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.runs import RunExecutionOwner
    from core.sessions._types import SessionAddress


def create_bound_temporary_session(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    owner_name: str,
    group_id: str,
    participant_id: str,
    config: JsonObject,
) -> str:
    """Create or reconcile one owner-managed Session in one transaction."""
    if not all(
        isinstance(value, str) and value for value in (owner_name, group_id, participant_id)
    ):
        raise ChatSessionError("temporary Session binding identity is invalid")
    config_payload = _store_values._json_object(config, "temporary Session config")
    generation_id: str | None = None

    def _fn(connection: sqlite3.Connection) -> None:
        nonlocal generation_id
        claimed = connection.execute(
            "SELECT s.project_id, s.agent_id, s.session_id, s.status, b.generation_id, b.config_json "
            "FROM temporary_session_bindings AS b JOIN sessions AS s ON s.session_key = b.session_key "
            "WHERE b.owner_name = ? AND b.group_id = ? AND b.participant_id = ?",
            (owner_name, group_id, participant_id),
        ).fetchone()
        if claimed is not None:
            if str(claimed["status"]) != "live":
                raise ChatSessionError(
                    "temporary participant binding belongs to an archived Session"
                )
            if _store_values._canonical_json_payload(
                str(claimed["config_json"])
            ) != _store_values._canonical_json_payload(config_payload):
                raise ChatSessionError(
                    "temporary participant binding conflicts with its existing configuration"
                )
            generation_id = str(claimed["generation_id"])
            return
        existing_state = _store_values._find_live(connection, address)
        if existing_state is not None:
            raise ChatSessionError("temporary Session address is already in use")
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        cursor = connection.execute(
            "INSERT INTO sessions (generation_id, project_id, agent_id, session_id, created_at, active_sort, list_visibility_mask) "
            "VALUES (?, ?, ?, ?, ?, COALESCE(julianday(?), 0.0), ?)",
            (
                uuid.uuid4().hex,
                *_store_values._scope(address),
                timestamp,
                timestamp,
                _store_values._LIST_VISIBILITY_OWNER_MANAGED,
            ),
        )
        session_key = cursor.lastrowid
        if session_key is None:
            raise SessionStoreCorruptError("SQLite did not return a temporary Session key")
        state = connection.execute(
            "SELECT * FROM sessions WHERE session_key = ?", (session_key,)
        ).fetchone()
        assert state is not None
        generation_id = str(state["generation_id"])
        connection.execute(
            "INSERT INTO temporary_session_bindings "
            "(session_key, generation_id, owner_name, group_id, participant_id, config_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_key, generation_id, owner_name, group_id, participant_id, config_payload),
        )

    _fn(connection)
    assert generation_id is not None
    return generation_id


def temporary_binding(
    connection: sqlite3.Connection, address: SessionAddress
) -> sqlite3.Row | None:
    state = _store_values._require_live(connection, address)
    row = connection.execute(
        "SELECT * FROM temporary_session_bindings WHERE session_key = ?",
        (state["session_key"],),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def temporary_binding_by_participant(
    connection: sqlite3.Connection, *, owner_name: str, group_id: str, participant_id: str
) -> tuple[SessionAddress, sqlite3.Row] | None:
    row = connection.execute(
        "SELECT s.project_id, s.agent_id, s.session_id, b.* FROM temporary_session_bindings AS b "
        "JOIN sessions AS s ON s.session_key = b.session_key "
        "WHERE b.owner_name = ? AND b.group_id = ? AND b.participant_id = ? AND s.status = 'live'",
        (owner_name, group_id, participant_id),
    ).fetchone()
    return None if row is None else (_store_values._address(row), cast(sqlite3.Row, row))


def delete_temporary_group(
    connection: sqlite3.Connection, *, owner_name: str, group_id: str
) -> int:
    """Delete only this owner's bound participant generations in one transaction."""

    def operation(connection: sqlite3.Connection) -> int:
        return connection.execute(
            "DELETE FROM sessions WHERE session_key IN ("
            "SELECT session_key FROM temporary_session_bindings "
            "WHERE owner_name=? AND group_id=?)",
            (owner_name, group_id),
        ).rowcount

    return cast(int, operation(connection))


def temporary_bindings(
    connection: sqlite3.Connection,
    *,
    owner_name: str,
    group_id: str,
    after: str = "",
    limit: int = 100,
) -> list[tuple[SessionAddress, sqlite3.Row]]:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("invalid temporary Session page bounds")
    rows = connection.execute(
        "SELECT s.project_id, s.agent_id, s.session_id, b.* FROM temporary_session_bindings b "
        "JOIN sessions s ON s.session_key = b.session_key "
        "WHERE b.owner_name = ? AND b.group_id = ? AND b.participant_id > ? "
        "AND s.status = 'live' ORDER BY b.participant_id LIMIT ?",
        (owner_name, group_id, after, limit),
    ).fetchall()
    return [(_store_values._address(row), row) for row in rows]


def append_messages_with_receipts(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    generation_id: str,
    owner_name: str,
    messages: Sequence[ChatMessage],
    receipts: Sequence[tuple[int, str, str, str, str]],
    deduplicate_carrier: bool = False,
) -> None:
    if not messages or type(deduplicate_carrier) is not bool:
        raise ChatSessionError("delivery receipt carriers are invalid")
    for message in messages:
        _store_codec._message_base_row(message)

    def _fn(connection: sqlite3.Connection) -> None:
        state = _store_values._require_live(connection, address)
        if str(state["generation_id"]) != generation_id:
            raise ChatSessionError("delivery receipt Session generation is stale")
        binding = connection.execute(
            "SELECT 1 FROM temporary_session_bindings WHERE session_key = ? AND generation_id = ? AND owner_name = ?",
            (state["session_key"], generation_id, owner_name),
        ).fetchone()
        if binding is None:
            raise ChatSessionError("delivery receipt owner is not bound to this Session")
        indexed_receipts: list[tuple[int, str, str, str, str]] = []
        seen_receipts: dict[str, tuple[int, str, str, str]] = {}
        for receipt in receipts:
            if not isinstance(receipt, tuple) or len(receipt) != 5:
                raise ChatSessionError("delivery receipt is invalid")
            carrier_index, receipt_id, content_hash, effect_kind, carrier_kind = receipt
            if (
                type(carrier_index) is not int
                or not 0 <= carrier_index < len(messages)
                or not all(
                    isinstance(value, str) and value
                    for value in (receipt_id, content_hash, effect_kind, carrier_kind)
                )
                or messages[carrier_index].role != carrier_kind
            ):
                raise ChatSessionError("delivery receipt is invalid")
            desired = (carrier_index, content_hash, effect_kind, carrier_kind)
            previous = seen_receipts.get(receipt_id)
            if previous is not None:
                if previous != desired:
                    raise ChatSessionError("delivery receipt conflicts within its batch")
                continue
            seen_receipts[receipt_id] = desired
            indexed_receipts.append(receipt)
        if deduplicate_carrier and (
            len(messages) != 1
            or len(indexed_receipts) != 1
            or indexed_receipts[0][0] != 0
            or messages[0].role not in {"user", "note"}
        ):
            raise ChatSessionError("delivery receipt carrier cannot be deduplicated")
        next_seq = int(state["message_count"])
        new_receipts: list[tuple[int, str, str, str, str]] = []
        existing_matching_receipt = False
        for (
            carrier_index,
            receipt_id,
            content_hash,
            effect_kind,
            carrier_kind,
        ) in indexed_receipts:
            receipt_effect = (content_hash, effect_kind, carrier_kind)
            existing = connection.execute(
                "SELECT content_hash, effect_kind, carrier_kind FROM session_delivery_receipts WHERE generation_id = ? AND owner_name = ? AND receipt_id = ?",
                (generation_id, owner_name, receipt_id),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != receipt_effect:
                    raise ChatSessionError("delivery receipt conflicts with its prior effect")
                existing_matching_receipt = True
                continue
            new_receipts.append(
                (carrier_index, receipt_id, content_hash, effect_kind, carrier_kind)
            )
        new_messages = [] if deduplicate_carrier and existing_matching_receipt else list(messages)
        for index, message in enumerate(new_messages):
            _store_codec._insert_message(
                connection, int(state["session_key"]), next_seq + index, message
            )
        for carrier_index, receipt_id, content_hash, effect_kind, carrier_kind in new_receipts:
            connection.execute(
                "INSERT INTO session_delivery_receipts (session_key, generation_id, owner_name, receipt_id, content_hash, effect_kind, carrier_kind, carrier_sequence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    state["session_key"],
                    generation_id,
                    owner_name,
                    receipt_id,
                    content_hash,
                    effect_kind,
                    carrier_kind,
                    next_seq + carrier_index,
                ),
            )
        if new_messages:
            last = new_messages[-1]
            connection.execute(
                "UPDATE sessions SET message_count = message_count + ?, last_message_at = ?, active_sort = COALESCE(julianday(?), 0.0), last_message_id = ?, history_revision = history_revision + 1, state_revision = state_revision + 1 WHERE session_key = ?",
                (
                    len(new_messages),
                    last.timestamp,
                    last.timestamp,
                    last.id,
                    state["session_key"],
                ),
            )
            _store_fts._mark_fts_write(connection)

    _fn(connection)


def delivery_receipt(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    generation_id: str,
    owner_name: str,
    receipt_id: str,
) -> sqlite3.Row | None:
    state = _store_values._find_live(connection, address)
    if state is None:
        return None
    if str(state["generation_id"]) != generation_id:
        return None
    row = connection.execute(
        "SELECT receipt_id, content_hash, effect_kind, carrier_kind, carrier_sequence "
        "FROM session_delivery_receipts WHERE generation_id = ? AND owner_name = ? AND receipt_id = ?",
        (generation_id, owner_name, receipt_id),
    ).fetchone()
    return cast(sqlite3.Row | None, row)


def record_run_owner(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    run_id: str,
    owner: RunExecutionOwner,
    input_id: str | None = None,
) -> None:
    """Claim one exact Run before execution without granting Session authority."""
    error = (
        "This Session no longer matches this Run. Ask the user to resume it through its Extension."
    )
    fields = (
        owner.extension,
        owner.group_id,
        owner.participant_id,
        owner.generation_id,
        owner.epoch,
    )
    if not all(isinstance(value, str) and value for value in (run_id, *fields)):
        raise ChatSessionError(error)
    if input_id is not None and (not isinstance(input_id, str) or not input_id):
        raise ChatSessionError(error)

    def write(connection: sqlite3.Connection) -> None:
        state = _store_values._require_live(connection, address)
        binding = connection.execute(
            "SELECT 1 FROM temporary_session_bindings b JOIN sessions s "
            "ON s.session_key = b.session_key WHERE b.owner_name = ? "
            "AND b.group_id = ? AND b.participant_id = ? AND b.generation_id = ? "
            "AND s.generation_id = b.generation_id AND s.status = 'live'",
            fields[:4],
        ).fetchone()
        if binding is None:
            raise ChatSessionError(error)
        existing = connection.execute(
            "SELECT owner_name, group_id, participant_id, participant_generation_id, epoch "
            ", input_id FROM run_execution_owners WHERE session_key = ? AND run_id = ?",
            (state["session_key"], run_id),
        ).fetchone()
        if existing is not None:
            if tuple(existing) != (*fields, input_id):
                raise ChatSessionError(error)
            return
        _record_run_start(connection, state, run_id, error)
        connection.execute(
            "INSERT INTO run_execution_owners (session_key, generation_id, run_id, "
            "owner_name, group_id, participant_id, participant_generation_id, epoch, "
            "start_sequence, input_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                state["session_key"],
                state["generation_id"],
                run_id,
                *fields,
                state["message_count"],
                input_id,
            ),
        )

    write(connection)


def record_run_start(
    connection: sqlite3.Connection, address: SessionAddress, *, run_id: str
) -> None:
    """Record an idempotent canonical Run admission boundary."""
    if not isinstance(run_id, str) or not run_id:
        raise ChatSessionError("invalid Run start")

    def write(connection: sqlite3.Connection) -> None:
        state = _store_values._require_live(connection, address)
        _record_run_start(connection, state, run_id, "This Session no longer matches this Run.")

    write(connection)


def owned_runs(
    connection: sqlite3.Connection,
    *,
    owner_name: str,
    group_id: str,
    participant_id: str | None = None,
    after: int = 0,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Page canonical execution records, including retained archived history."""
    if type(limit) is not int or not 1 <= limit <= 1000 or type(after) is not int or after < 0:
        raise ValueError("invalid owned Run page bounds")
    clauses = ["o.owner_name = ?", "o.group_id = ?", "o.record_key > ?"]
    values: list[Any] = [owner_name, group_id, after]
    if participant_id is not None:
        clauses.append("o.participant_id = ?")
        values.append(participant_id)
    values.append(limit)
    rows = connection.execute(
        "SELECT o.*, s.project_id, s.agent_id, s.session_id, "
        "(SELECT rs.status FROM run_summaries rs JOIN messages m "
        "ON m.message_key = rs.message_key WHERE m.session_key = o.session_key "
        "AND rs.run_id = o.run_id AND m.seq >= o.start_sequence "
        "ORDER BY m.seq DESC LIMIT 1) AS terminal_status, "
        "(SELECT m.seq FROM run_summaries rs JOIN messages m "
        "ON m.message_key = rs.message_key WHERE m.session_key = o.session_key "
        "AND rs.run_id = o.run_id AND m.seq >= o.start_sequence "
        "ORDER BY m.seq DESC LIMIT 1) AS terminal_sequence "
        "FROM run_execution_owners o JOIN sessions s ON s.session_key = o.session_key "
        "AND s.generation_id = o.generation_id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY o.record_key LIMIT ?",
        values,
    ).fetchall()
    return cast(list[sqlite3.Row], rows)


def run_start_boundaries(
    connection: sqlite3.Connection, addresses: Sequence[SessionAddress]
) -> list[sqlite3.Row]:
    """Read exact owner-backed Run starts for a bounded set of live addresses."""
    unique = tuple(dict.fromkeys(addresses))
    if len(unique) > 100:
        raise ValueError("too many Run boundary addresses")
    if not unique:
        return []
    clauses = " OR ".join("(s.project_id=? AND s.agent_id=? AND s.session_id=?)" for _ in unique)
    values: list[Any] = []
    for address in unique:
        values.extend((address.project_id or "", address.agent_id, address.session_id))
    rows = connection.execute(
        "SELECT s.project_id,s.agent_id,s.session_id,r.generation_id,r.run_id,r.start_sequence "
        "FROM run_execution_starts r JOIN sessions s ON s.session_key=r.session_key "
        "AND s.generation_id=r.generation_id WHERE " + clauses + " "
        "ORDER BY s.project_id,s.agent_id,s.session_id,r.start_sequence,r.record_key",
        values,
    ).fetchall()
    return cast(list[sqlite3.Row], rows)


def _record_run_start(
    connection: sqlite3.Connection, state: sqlite3.Row, run_id: str, error: str
) -> None:
    existing = connection.execute(
        "SELECT generation_id,start_sequence FROM run_execution_starts "
        "WHERE session_key=? AND run_id=?",
        (state["session_key"], run_id),
    ).fetchone()
    expected = (state["generation_id"], state["message_count"])
    if existing is not None:
        if tuple(existing) != expected:
            raise ChatSessionError(error)
        return
    connection.execute(
        "INSERT INTO run_execution_starts(session_key,generation_id,run_id,start_sequence) "
        "VALUES(?,?,?,?)",
        (state["session_key"], state["generation_id"], run_id, state["message_count"]),
    )
