"""Atomic temporary Session bindings, delivery receipts and Run attribution."""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from core.chat.errors import ChatSessionError
from core.runs import RunExecutionOwner
from core.sessions import _store_codec, _store_fts, _store_queries, _store_values
from core.sessions._metadata import _decode_state_object
from core.sessions._types import (
    DeliveryReceipt,
    JsonObject,
    OwnedRunRecord,
    OwnedSessionSummary,
    RunStartBoundary,
    TemporarySessionBinding,
)
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


def _temporary_binding(row: sqlite3.Row, address: SessionAddress) -> TemporarySessionBinding:
    return TemporarySessionBinding(
        address,
        str(row["generation_id"]),
        str(row["owner_name"]),
        str(row["group_id"]),
        str(row["participant_id"]),
        _decode_state_object(str(row["config_json"]), "temporary Session config"),
    )


def temporary_binding(
    connection: sqlite3.Connection, address: SessionAddress
) -> Callable[[], TemporarySessionBinding | None]:
    """Select the binding of one live Session; the decoder yields ``None`` for none."""
    state = _store_values._require_live(connection, address)
    row = connection.execute(
        "SELECT * FROM temporary_session_bindings WHERE session_key = ?",
        (state["session_key"],),
    ).fetchone()
    return lambda: None if row is None else _temporary_binding(row, address)


def temporary_binding_by_participant(
    connection: sqlite3.Connection, *, owner_name: str, group_id: str, participant_id: str
) -> Callable[[], TemporarySessionBinding | None]:
    """Select one participant's live binding; the decoder yields ``None`` for none."""
    row = connection.execute(
        "SELECT s.project_id, s.agent_id, s.session_id, b.* FROM temporary_session_bindings AS b "
        "JOIN sessions AS s ON s.session_key = b.session_key "
        "WHERE b.owner_name = ? AND b.group_id = ? AND b.participant_id = ? AND s.status = 'live'",
        (owner_name, group_id, participant_id),
    ).fetchone()
    return lambda: None if row is None else _temporary_binding(row, _store_values._address(row))


def delete_temporary_group(
    connection: sqlite3.Connection, *, owner_name: str, group_id: str
) -> int:
    """Delete only this owner's bound participant generations in one transaction."""

    def operation(connection: sqlite3.Connection) -> int:
        for row in connection.execute(
            "SELECT session_key FROM temporary_session_bindings WHERE owner_name=? AND group_id=?",
            (owner_name, group_id),
        ).fetchall():
            _store_fts._delete_fts_session(connection, int(row[0]))
        connection.execute(
            "DELETE FROM temporary_group_titles WHERE owner_name=? AND group_id=?",
            (owner_name, group_id),
        )
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
) -> Callable[[], list[TemporarySessionBinding]]:
    """Page one group's live bindings by participant id after ``after``."""
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("invalid temporary Session page bounds")
    rows = connection.execute(
        "SELECT s.project_id, s.agent_id, s.session_id, b.* FROM temporary_session_bindings b "
        "JOIN sessions s ON s.session_key = b.session_key "
        "WHERE b.owner_name = ? AND b.group_id = ? AND b.participant_id > ? "
        "AND s.status = 'live' ORDER BY b.participant_id LIMIT ?",
        (owner_name, group_id, after, limit),
    ).fetchall()
    return lambda: [_temporary_binding(row, _store_values._address(row)) for row in rows]


TEMPORARY_GROUP_TITLE_MAX_CHARACTERS = 120


def set_temporary_group_title(
    connection: sqlite3.Connection, *, owner_name: str, group_id: str, title: str
) -> None:
    """Replace one owner group's display title; it describes, never authorizes."""
    if not all(isinstance(value, str) and value for value in (owner_name, group_id)):
        raise ChatSessionError("temporary group identity is invalid")
    if (
        not isinstance(title, str)
        or not title.strip()
        or title != title.strip()
        or "\n" in title
        or "\r" in title
        or len(title) > TEMPORARY_GROUP_TITLE_MAX_CHARACTERS
    ):
        raise ChatSessionError("temporary group title is invalid")
    connection.execute(
        "INSERT INTO temporary_group_titles (owner_name, group_id, title) VALUES (?, ?, ?) "
        "ON CONFLICT (owner_name, group_id) DO UPDATE SET title = excluded.title",
        (owner_name, group_id, title),
    )


def temporary_group_titles(
    connection: sqlite3.Connection, *, owner_name: str, group_ids: Sequence[str]
) -> dict[str, str]:
    unique = tuple(dict.fromkeys(group_ids))
    if len(unique) > 1000 or not all(isinstance(value, str) and value for value in unique):
        raise ValueError("invalid temporary group title lookup")
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    rows = connection.execute(
        "SELECT group_id, title FROM temporary_group_titles "
        f"WHERE owner_name = ? AND group_id IN ({placeholders})",
        (owner_name, *unique),
    ).fetchall()
    return {str(row["group_id"]): str(row["title"]) for row in rows}


def owned_session_summaries(
    connection: sqlite3.Connection,
    *,
    owner_name: str | None = None,
    group_id: str | None = None,
    metadata_keys: Sequence[str] = (),
) -> Callable[[], list[OwnedSessionSummary]]:
    """Read live owner-managed Sessions in creation order with their labels.

    Only display labels and the configured Model leave the protected binding,
    never its complete configuration. The returned decoder builds the summaries
    after the read transaction.
    """
    if group_id is not None and owner_name is None:
        raise ValueError("a temporary group filter requires its owner")
    selected_keys, metadata_columns = _store_queries._summary_metadata_columns(metadata_keys)
    rows = connection.execute(
        f"SELECT {_store_values._SESSION_LIST_COLUMNS}{metadata_columns}, "
        "b.owner_name, b.group_id, b.participant_id, "
        "json_extract(b.config_json, '$.name') AS participant_name, "
        "json_extract(b.config_json, '$.model') AS participant_model, "
        "(SELECT g.title FROM temporary_group_titles AS g "
        "WHERE g.owner_name = b.owner_name AND g.group_id = b.group_id) AS group_title "
        "FROM temporary_session_bindings AS b "
        "JOIN sessions ON sessions.session_key = b.session_key "
        "WHERE sessions.status = 'live' "
        "AND (? IS NULL OR b.owner_name = ?) AND (? IS NULL OR b.group_id = ?) "
        "ORDER BY b.owner_name, b.group_id, sessions.session_key",
        (owner_name, owner_name, group_id, group_id),
    ).fetchall()
    return lambda: [_owned_session_summary(row, selected_keys) for row in rows]


def _owned_session_summary(row: sqlite3.Row, metadata_keys: Sequence[str]) -> OwnedSessionSummary:
    participant_name, model = row["participant_name"], row["participant_model"]
    group_title = row["group_title"]
    return OwnedSessionSummary(
        address=_store_values._address(row),
        owner_name=str(row["owner_name"]),
        group_id=str(row["group_id"]),
        group_title=group_title if isinstance(group_title, str) else None,
        participant_id=str(row["participant_id"]),
        participant_name=participant_name if isinstance(participant_name, str) else None,
        model=model if isinstance(model, str) else None,
        summary=_store_queries._summary(row, metadata_keys),
    )


def append_messages_with_receipts(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    generation_id: str,
    owner_name: str,
    messages: Sequence[ChatMessage],
    receipts: Sequence[tuple[int, str, str, str, str]],
    deduplicate_carrier: bool = False,
    run_id: str | None = None,
    assistant_message_id: str | None = None,
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
                connection,
                int(state["session_key"]),
                next_seq + index,
                message,
                run_id=run_id,
                assistant_message_id=assistant_message_id,
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
) -> DeliveryReceipt | None:
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
    if row is None:
        return None
    return DeliveryReceipt(
        str(row["receipt_id"]),
        str(row["content_hash"]),
        str(row["effect_kind"]),
        {"kind": str(row["carrier_kind"]), "sequence": int(row["carrier_sequence"])},
    )


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


_OWNED_RUN_COLUMNS = (
    "SELECT o.*, s.project_id, s.agent_id, s.session_id, "
    "CASE WHEN r.status='running' THEN NULL ELSE r.status END AS terminal_status, "
    "r.terminal_sequence AS terminal_sequence "
    "FROM run_execution_owners o JOIN sessions s ON s.session_key=o.session_key "
    "AND s.generation_id=o.generation_id JOIN runs r "
    "ON r.session_key=o.session_key AND r.run_id=o.run_id WHERE "
)
# One statement probes at most this many exact Run ids.
_OWNED_RUN_LOOKUP_BATCH = 100


def _owned_run_record(row: sqlite3.Row) -> OwnedRunRecord:
    return OwnedRunRecord(
        record_key=int(row["record_key"]),
        address=_store_values._address(row),
        generation_id=str(row["generation_id"]),
        run_id=str(row["run_id"]),
        owner=RunExecutionOwner(
            str(row["owner_name"]),
            str(row["group_id"]),
            str(row["participant_id"]),
            str(row["participant_generation_id"]),
            str(row["epoch"]),
        ),
        start_sequence=int(row["start_sequence"]),
        terminal_status=row["terminal_status"],
        terminal_sequence=row["terminal_sequence"],
        input_id=row["input_id"],
    )


def owned_runs(
    connection: sqlite3.Connection,
    *,
    owner_name: str,
    group_id: str,
    participant_id: str | None = None,
    after: int = 0,
    limit: int = 100,
) -> list[OwnedRunRecord]:
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
        _OWNED_RUN_COLUMNS + " AND ".join(clauses) + " ORDER BY o.record_key LIMIT ?",
        values,
    ).fetchall()
    return [_owned_run_record(row) for row in rows]


def owned_runs_by_id(
    connection: sqlite3.Connection,
    *,
    owner_name: str,
    group_id: str,
    run_ids: Sequence[str],
) -> dict[str, OwnedRunRecord]:
    """Read the exact execution records of Run ids in one group, keyed by Run id.

    ``run_execution_owners_group_run`` makes each id one index probe, so the cost
    does not grow with the group's retained Run history. Ids without a record in
    that group, including Runs of deleted Sessions, are absent.
    """
    unique = tuple(dict.fromkeys(run_ids))
    if not all(isinstance(run_id, str) and run_id for run_id in unique):
        raise ValueError("invalid owned Run ids")
    records: dict[str, OwnedRunRecord] = {}
    for start in range(0, len(unique), _OWNED_RUN_LOOKUP_BATCH):
        batch = unique[start : start + _OWNED_RUN_LOOKUP_BATCH]
        placeholders = ", ".join("?" * len(batch))
        rows = connection.execute(
            _OWNED_RUN_COLUMNS
            + f"o.owner_name = ? AND o.group_id = ? AND o.run_id IN ({placeholders})",
            (owner_name, group_id, *batch),
        ).fetchall()
        records.update((str(row["run_id"]), _owned_run_record(row)) for row in rows)
    return records


def owned_run_by_input(
    connection: sqlite3.Connection, address: SessionAddress, input_id: str
) -> OwnedRunRecord | None:
    """Read the execution record admitted for one input of a live Session.

    ``UNIQUE (session_key, input_id)`` serves the probe; a Session that is not
    live has no admissible input and yields ``None``.
    """
    if not isinstance(input_id, str) or not input_id:
        raise ValueError("invalid owned Run input id")
    row = connection.execute(
        _OWNED_RUN_COLUMNS
        + "o.session_key = (SELECT session_key FROM sessions WHERE project_id = ? "
        "AND agent_id = ? AND session_id = ? AND status = 'live') AND o.input_id = ?",
        (*_store_values._scope(address), input_id),
    ).fetchone()
    return None if row is None else _owned_run_record(row)


def run_start_boundaries(
    connection: sqlite3.Connection, addresses: Sequence[SessionAddress]
) -> list[RunStartBoundary]:
    """Read every Run start, owned or ordinary, of up to 100 addresses.

    Each address contributes its live generation and any archived generations;
    rows carry ``generation_id`` so callers can tell them apart, ordered by
    address, then start sequence.
    """
    unique = tuple(dict.fromkeys(addresses))
    if len(unique) > 100:
        raise ValueError("too many Run boundary addresses")
    if not unique:
        return []
    addresses_sql = ", ".join("(?, ?, ?)" for _ in unique)
    values: list[Any] = []
    for address in unique:
        values.extend((address.project_id or "", address.agent_id, address.session_id))
    # Each address index is partial on one status, so every generation is found
    # through one probe per status.
    generations = " UNION ALL ".join(
        "SELECT session_key FROM sessions WHERE status = '"
        + status
        + f"' AND (project_id, agent_id, session_id) IN (VALUES {addresses_sql})"
        for status in ("live", "archived")
    )
    rows = connection.execute(
        "SELECT s.project_id,s.agent_id,s.session_id,s.generation_id,r.run_id,r.start_sequence "
        "FROM runs r JOIN sessions s ON s.session_key=r.session_key "
        f"WHERE s.session_key IN ({generations}) "
        "ORDER BY s.project_id,s.agent_id,s.session_id,r.start_sequence,r.run_key",
        (*values, *values),
    ).fetchall()
    return [
        RunStartBoundary(
            _store_values._address(row),
            str(row["generation_id"]),
            str(row["run_id"]),
            int(row["start_sequence"]),
        )
        for row in rows
    ]


def _record_run_start(
    connection: sqlite3.Connection, state: sqlite3.Row, run_id: str, error: str
) -> None:
    existing = connection.execute(
        "SELECT start_sequence,status,origin_generation_id FROM runs WHERE session_key = ? AND run_id = ?",
        (state["session_key"], run_id),
    ).fetchone()
    if existing is not None:
        if existing["status"] != "running" or existing["origin_generation_id"] is not None:
            raise ChatSessionError("A settled or inherited Run cannot be admitted again")
        return
    from datetime import UTC, datetime

    connection.execute(
        "INSERT INTO runs(session_key, run_id, start_sequence, status, started_at) "
        "VALUES (?, ?, ?, 'running', ?)",
        (state["session_key"], run_id, state["message_count"], datetime.now(UTC).isoformat()),
    )
