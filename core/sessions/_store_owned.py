"""Owner-managed Sessions: temporary bindings, delivery receipts and owned Runs."""
# ruff: noqa: E501

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from core.chat.errors import ChatSessionError
from core.runs import RunExecutionOwner
from core.sessions import _store_codec, _store_mutations, _store_queries, _store_values
from core.sessions._metadata import _decode_state_object
from core.sessions._types import (
    DeliveryReceipt,
    JsonObject,
    OwnedRunRecord,
    OwnedSessionSummary,
    TemporarySessionBinding,
    ToolResultFacts,
)
from core.utils.timestamps import utc_now_timestamp

if TYPE_CHECKING:
    from core.chat.messages import ChatMessage
    from core.sessions._types import SessionAddress

_BINDING_COLUMNS = (
    "s.project_id, s.agent_id, s.session_id, s.generation_id, "
    "b.owner_name, b.group_id, b.participant_id, b.config_json"
)


def create_bound_temporary_session(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    owner_name: str,
    group_id: str,
    participant_id: str,
    config: JsonObject,
) -> str:
    """Create or reconcile one owner-managed Session; return its generation id."""
    if not all(
        isinstance(value, str) and value for value in (owner_name, group_id, participant_id)
    ):
        raise ChatSessionError("temporary Session binding identity is invalid")
    config_payload = _store_values._json_object(config, "temporary Session config")
    claimed = connection.execute(
        "SELECT s.generation_id, s.state, b.config_json FROM temporary_session_bindings AS b "
        "JOIN sessions AS s ON s.session_key = b.session_key "
        "WHERE b.owner_name = ? AND b.group_id = ? AND b.participant_id = ?",
        (owner_name, group_id, participant_id),
    ).fetchone()
    if claimed is not None:
        if str(claimed["state"]) != "live":
            raise ChatSessionError("temporary participant binding belongs to an archived Session")
        if _store_values._canonical_json_payload(
            str(claimed["config_json"])
        ) != _store_values._canonical_json_payload(config_payload):
            raise ChatSessionError(
                "temporary participant binding conflicts with its existing configuration"
            )
        return str(claimed["generation_id"])
    if _store_values._find_live(connection, address) is not None:
        raise ChatSessionError("temporary Session address is already in use")
    session_key = _store_mutations._insert_session(connection, address, utc_now_timestamp())
    connection.execute(
        "INSERT INTO temporary_session_bindings "
        "(session_key, owner_name, group_id, participant_id, config_json) VALUES (?, ?, ?, ?, ?)",
        (session_key, owner_name, group_id, participant_id, config_payload),
    )
    return str(_store_values._state_by_key(connection, session_key)["generation_id"])


def _temporary_binding(row: sqlite3.Row) -> TemporarySessionBinding:
    return TemporarySessionBinding(
        _store_values._address(row),
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
        f"SELECT {_BINDING_COLUMNS} FROM temporary_session_bindings AS b "
        "JOIN sessions AS s ON s.session_key = b.session_key WHERE b.session_key = ?",
        (state["session_key"],),
    ).fetchone()
    return lambda: None if row is None else _temporary_binding(row)


def temporary_binding_by_participant(
    connection: sqlite3.Connection, *, owner_name: str, group_id: str, participant_id: str
) -> Callable[[], TemporarySessionBinding | None]:
    """Select one participant's live binding; the decoder yields ``None`` for none."""
    row = connection.execute(
        f"SELECT {_BINDING_COLUMNS} FROM temporary_session_bindings AS b "
        "JOIN sessions AS s ON s.session_key = b.session_key "
        "WHERE b.owner_name = ? AND b.group_id = ? AND b.participant_id = ? AND s.state = 'live'",
        (owner_name, group_id, participant_id),
    ).fetchone()
    return lambda: None if row is None else _temporary_binding(row)


def delete_temporary_group(
    connection: sqlite3.Connection, *, owner_name: str, group_id: str
) -> int:
    """Delete only this owner's bound participant generations; return how many."""
    keys = [
        int(row[0])
        for row in connection.execute(
            "SELECT session_key FROM temporary_session_bindings WHERE owner_name = ? "
            "AND group_id = ? ORDER BY session_key",
            (owner_name, group_id),
        )
    ]
    for session_key in keys:
        _store_mutations.delete_session(connection, session_key)
    connection.execute(
        "DELETE FROM temporary_group_titles WHERE owner_name = ? AND group_id = ?",
        (owner_name, group_id),
    )
    return len(keys)


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
        f"SELECT {_BINDING_COLUMNS} FROM temporary_session_bindings AS b "
        "JOIN sessions AS s ON s.session_key = b.session_key "
        "WHERE b.owner_name = ? AND b.group_id = ? AND b.participant_id > ? "
        "AND s.state = 'live' ORDER BY b.participant_id LIMIT ?",
        (owner_name, group_id, after, limit),
    ).fetchall()
    return lambda: [_temporary_binding(row) for row in rows]


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
    rows = connection.execute(
        "SELECT group_id, title FROM temporary_group_titles "
        "WHERE owner_name = ? AND group_id IN (SELECT value FROM json_each(?))",
        (owner_name, _store_values._json_list(unique)),
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
        "g.title AS group_title "
        "FROM temporary_session_bindings AS b "
        "JOIN sessions AS s ON s.session_key = b.session_key "
        f"{_store_values._DERIVED_METADATA_JOIN} "
        "LEFT JOIN temporary_group_titles AS g "
        "ON g.owner_name = b.owner_name AND g.group_id = b.group_id "
        "WHERE s.state = 'live' "
        "AND (? IS NULL OR b.owner_name = ?) AND (? IS NULL OR b.group_id = ?) "
        "ORDER BY b.owner_name, b.group_id, s.session_key",
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


# -- Delivery receipts ---------------------------------------------------------------

_Receipt = tuple[int, str, str, str, str]


def _unique_receipts(
    messages: Sequence[ChatMessage], receipts: Sequence[_Receipt]
) -> list[_Receipt]:
    """Validate *receipts* against their carriers; drop exact repeats in the batch."""
    unique: list[_Receipt] = []
    seen: dict[str, tuple[int, str, str, str]] = {}
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
        previous = seen.get(receipt_id)
        if previous is not None:
            if previous != desired:
                raise ChatSessionError("delivery receipt conflicts within its batch")
            continue
        seen[receipt_id] = desired
        unique.append(receipt)
    return unique


def append_messages_with_receipts(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    generation_id: str,
    owner_name: str,
    messages: Sequence[ChatMessage],
    receipts: Sequence[_Receipt],
    deduplicate_carrier: bool = False,
    run_id: str | None = None,
    assistant_message_id: str | None = None,
    tool_results: Mapping[str, ToolResultFacts] | None = None,
) -> None:
    """Append owner deliveries with their receipts, each receipt's effect once.

    A receipt already recorded must describe the same effect. With
    *deduplicate_carrier*, a single carrier whose receipt exists is not
    appended again.
    """
    if not messages or type(deduplicate_carrier) is not bool:
        raise ChatSessionError("delivery receipt carriers are invalid")
    for message in messages:
        _store_codec.validate_appendable(message)
    unique = _unique_receipts(messages, receipts)
    if deduplicate_carrier and (
        len(messages) != 1
        or len(unique) != 1
        or unique[0][0] != 0
        or messages[0].role not in {"user", "note"}
    ):
        raise ChatSessionError("delivery receipt carrier cannot be deduplicated")
    state = _store_values._require_live(connection, address)
    session_key = int(state["session_key"])
    if str(state["generation_id"]) != generation_id:
        raise ChatSessionError("delivery receipt Session generation is stale")
    binding = connection.execute(
        "SELECT 1 FROM temporary_session_bindings WHERE session_key = ? AND owner_name = ?",
        (session_key, owner_name),
    ).fetchone()
    if binding is None:
        raise ChatSessionError("delivery receipt owner is not bound to this Session")
    new_receipts: list[_Receipt] = []
    recorded = False
    for receipt in unique:
        _index, receipt_id, content_hash, effect_kind, carrier_kind = receipt
        existing = connection.execute(
            "SELECT content_hash, effect_kind, carrier_kind FROM session_delivery_receipts "
            "WHERE session_key = ? AND owner_name = ? AND receipt_id = ?",
            (session_key, owner_name, receipt_id),
        ).fetchone()
        if existing is None:
            new_receipts.append(receipt)
        elif tuple(existing) != (content_hash, effect_kind, carrier_kind):
            raise ChatSessionError("delivery receipt conflicts with its prior effect")
        else:
            recorded = True
    if deduplicate_carrier and recorded:
        return
    first_seq = int(state["next_seq"])
    _store_mutations.append_messages(
        connection,
        address,
        messages,
        run_id=run_id,
        assistant_message_id=assistant_message_id,
        tool_results=tool_results,
    )
    connection.executemany(
        "INSERT INTO session_delivery_receipts (session_key, owner_name, receipt_id, "
        "content_hash, effect_kind, carrier_kind, carrier_sequence) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (
                session_key,
                owner_name,
                receipt_id,
                content_hash,
                effect_kind,
                carrier_kind,
                first_seq + index,
            )
            for index, receipt_id, content_hash, effect_kind, carrier_kind in new_receipts
        ],
    )


def delivery_receipt(
    connection: sqlite3.Connection,
    address: SessionAddress,
    *,
    generation_id: str,
    owner_name: str,
    receipt_id: str,
) -> DeliveryReceipt | None:
    state = _store_values._find_live(connection, address)
    if state is None or str(state["generation_id"]) != generation_id:
        return None
    row = connection.execute(
        "SELECT receipt_id, content_hash, effect_kind, carrier_kind, carrier_sequence "
        "FROM session_delivery_receipts WHERE session_key = ? AND owner_name = ? AND receipt_id = ?",
        (state["session_key"], owner_name, receipt_id),
    ).fetchone()
    if row is None:
        return None
    return DeliveryReceipt(
        str(row["receipt_id"]),
        str(row["content_hash"]),
        str(row["effect_kind"]),
        {"kind": str(row["carrier_kind"]), "sequence": int(row["carrier_sequence"])},
    )


# -- Owned Runs ------------------------------------------------------------------------

_OWNED_RUN_SQL = (
    "SELECT o.record_key, o.run_id, o.input_id, o.owner_name, o.group_id, o.participant_id, "
    "o.participant_generation_id, o.epoch, s.project_id, s.agent_id, s.session_id, "
    "s.generation_id, r.start_seq, "
    "CASE WHEN r.status = 'running' THEN NULL ELSE r.status END AS terminal_status, "
    "t.seq AS terminal_seq "
    "FROM run_execution_owners AS o JOIN runs AS r ON r.run_key = o.run_key "
    "JOIN sessions AS s ON s.session_key = o.session_key "
    "LEFT JOIN entries AS t ON t.entry_key = r.end_entry_key WHERE "
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
        start_sequence=int(row["start_seq"]),
        terminal_status=row["terminal_status"],
        terminal_sequence=row["terminal_seq"],
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
    """Page execution records in admission order, archived Sessions included."""
    if type(limit) is not int or not 1 <= limit <= 1000 or type(after) is not int or after < 0:
        raise ValueError("invalid owned Run page bounds")
    clauses = ["o.owner_name = ?", "o.group_id = ?", "o.record_key > ?"]
    values: list[Any] = [owner_name, group_id, after]
    if participant_id is not None:
        clauses.append("o.participant_id = ?")
        values.append(participant_id)
    values.append(limit)
    rows = connection.execute(
        _OWNED_RUN_SQL + " AND ".join(clauses) + " ORDER BY o.record_key LIMIT ?",
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
    """Read the execution records of exact Run ids in one group, keyed by Run id.

    ``run_execution_owners_group_run`` makes each id one index probe, so the
    cost does not grow with the group's Run history. Ids without a record in
    that group, including Runs of deleted Sessions, are absent.
    """
    unique = tuple(dict.fromkeys(run_ids))
    if not all(isinstance(run_id, str) and run_id for run_id in unique):
        raise ValueError("invalid owned Run ids")
    records: dict[str, OwnedRunRecord] = {}
    for start in range(0, len(unique), _OWNED_RUN_LOOKUP_BATCH):
        batch = unique[start : start + _OWNED_RUN_LOOKUP_BATCH]
        rows = connection.execute(
            _OWNED_RUN_SQL + "o.owner_name = ? AND o.group_id = ? "
            "AND o.run_id IN (SELECT value FROM json_each(?))",
            (owner_name, group_id, _store_values._json_list(batch)),
        ).fetchall()
        records.update((str(row["run_id"]), _owned_run_record(row)) for row in rows)
    return records


def owned_run_by_input(
    connection: sqlite3.Connection, address: SessionAddress, input_id: str
) -> OwnedRunRecord | None:
    """Read the execution record admitted for one input of a live Session."""
    if not isinstance(input_id, str) or not input_id:
        raise ValueError("invalid owned Run input id")
    state = _store_values._find_live(connection, address)
    if state is None:
        return None
    row = connection.execute(
        _OWNED_RUN_SQL + "o.session_key = ? AND o.input_id = ?",
        (state["session_key"], input_id),
    ).fetchone()
    return None if row is None else _owned_run_record(row)
