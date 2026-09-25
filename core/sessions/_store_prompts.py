"""Prompt state of a Session: prompt-epoch pins, seen Skills and cache affinity.

Pins are content-addressed: each Session slot references one ``prompt_blobs``
row, so forks share pinned values instead of copying them. Every write that
drops a reference deletes blobs nothing references any more, in the same
transaction.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING

from core.chat.errors import ChatSessionError
from core.sessions import _store_values
from core.sessions._metadata import (
    _default_prompt_cache_affinity_id,
    _is_prompt_cache_affinity_id,
    _new_prompt_cache_affinity_id,
)
from core.sessions._types import AGENT_BOUND_PROMPT_PIN_SLOTS, JsonObject, SeenSkillsUpdate

if TYPE_CHECKING:
    from core.sessions._types import SessionAddress


def _require_slot(slot: str) -> None:
    if not isinstance(slot, str) or not slot.startswith(_store_values._PROMPT_PIN_KEY_PREFIX):
        raise ChatSessionError(f"invalid prompt pin slot: {slot!r}")


def _pin_row(connection: sqlite3.Connection, session_key: int, slot: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = connection.execute(
        "SELECT p.blob_key, b.value_json FROM session_prompt_pins AS p "
        "JOIN prompt_blobs AS b ON b.blob_key = p.blob_key "
        "WHERE p.session_key = ? AND p.slot = ?",
        (session_key, slot),
    ).fetchone()
    return row


def _pin_value(row: sqlite3.Row | None) -> JsonObject | None:
    if row is None:
        return None
    return _store_values._json_from_payload(str(row["value_json"]), "prompt pin")


def prompt_pin(
    connection: sqlite3.Connection, address: SessionAddress, slot: str
) -> JsonObject | None:
    """Return one live Session's pinned value in *slot*, or ``None``."""
    _require_slot(slot)
    state = _store_values._require_live(connection, address)
    return _pin_value(_pin_row(connection, int(state["session_key"]), slot))


def _blob_key(connection: sqlite3.Connection, value: JsonObject) -> int:
    payload = _store_values._json_object(value, "prompt pin")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    connection.execute(
        "INSERT INTO prompt_blobs (sha256, value_json) VALUES (?, ?) "
        "ON CONFLICT (sha256) DO NOTHING",
        (digest, payload),
    )
    row = connection.execute(
        "SELECT blob_key FROM prompt_blobs WHERE sha256 = ?", (digest,)
    ).fetchone()
    return int(row[0])


def delete_unreferenced_blobs(connection: sqlite3.Connection, blob_keys: Iterable[int]) -> None:
    """Delete the given blobs that no Session pin references any more."""
    connection.executemany(
        "DELETE FROM prompt_blobs WHERE blob_key = ? "
        "AND NOT EXISTS (SELECT 1 FROM session_prompt_pins WHERE blob_key = ?)",
        [(key, key) for key in set(blob_keys)],
    )


def session_blob_keys(connection: sqlite3.Connection, session_key: int) -> list[int]:
    return [
        int(row[0])
        for row in connection.execute(
            "SELECT blob_key FROM session_prompt_pins WHERE session_key = ?", (session_key,)
        )
    ]


def _set_pin(
    connection: sqlite3.Connection, session_key: int, slot: str, value: JsonObject | None
) -> None:
    previous = _pin_row(connection, session_key, slot)
    if value is None:
        if previous is None:
            return
        connection.execute(
            "DELETE FROM session_prompt_pins WHERE session_key = ? AND slot = ?",
            (session_key, slot),
        )
    else:
        blob_key = _blob_key(connection, value)
        if previous is not None and int(previous["blob_key"]) == blob_key:
            return
        connection.execute(
            "INSERT INTO session_prompt_pins (session_key, slot, blob_key) VALUES (?, ?, ?) "
            "ON CONFLICT (session_key, slot) DO UPDATE SET blob_key = excluded.blob_key",
            (session_key, slot, blob_key),
        )
    if previous is not None:
        delete_unreferenced_blobs(connection, (int(previous["blob_key"]),))


def ensure_prompt_pin(
    connection: sqlite3.Connection,
    address: SessionAddress,
    slot: str,
    value: JsonObject,
    accept: Callable[[JsonObject], bool],
) -> JsonObject:
    """Keep an acceptable pinned value in *slot*, else pin *value*; return the pin.

    The check and the write share one transaction, so concurrent first builds
    settle on one value.
    """
    _require_slot(slot)
    state = _store_values._require_live(connection, address)
    session_key = int(state["session_key"])
    current = _pin_value(_pin_row(connection, session_key, slot))
    if current is not None and accept(current):
        return current
    _set_pin(connection, session_key, slot, value)
    return value


def replace_pins(
    connection: sqlite3.Connection, session_key: int, pins: Mapping[str, JsonObject | None]
) -> None:
    """Set every slot in *pins*; a ``None`` value removes that pin."""
    for slot, value in pins.items():
        _require_slot(slot)
        _set_pin(connection, session_key, slot, value)


def seen_skills(connection: sqlite3.Connection, address: SessionAddress) -> frozenset[str] | None:
    """Return the Skills this Session has seen, or ``None`` before its first record."""
    state = _store_values._require_live(connection, address)
    if not state["seen_skills_initialized"]:
        return None
    return frozenset(
        str(row[0])
        for row in connection.execute(
            "SELECT skill_name FROM session_seen_skills WHERE session_key = ?",
            (state["session_key"],),
        )
    )


def _require_skill_names(names: Iterable[str]) -> tuple[str, ...]:
    values = tuple(names)
    if not all(isinstance(name, str) and name for name in values):
        raise ChatSessionError("seen Skill names must be non-empty strings")
    return values


def replace_seen_skills(
    connection: sqlite3.Connection, session_key: int, names: Iterable[str]
) -> None:
    """Start the seen-Skill set over from *names*."""
    values = _require_skill_names(names)
    connection.execute("DELETE FROM session_seen_skills WHERE session_key = ?", (session_key,))
    connection.executemany(
        "INSERT OR IGNORE INTO session_seen_skills (session_key, skill_name) VALUES (?, ?)",
        [(session_key, name) for name in values],
    )
    connection.execute(
        "UPDATE sessions SET seen_skills_initialized = 1, state_revision = state_revision + 1 "
        "WHERE session_key = ?",
        (session_key,),
    )


def record_seen_skills(
    connection: sqlite3.Connection, session_key: int, update: SeenSkillsUpdate
) -> None:
    """Seed the set from ``baseline`` on first use; afterwards add ``added``.

    Adding only Skills the set already holds writes nothing.
    """
    initialized = connection.execute(
        "SELECT seen_skills_initialized FROM sessions WHERE session_key = ?", (session_key,)
    ).fetchone()[0]
    if not initialized:
        replace_seen_skills(connection, session_key, update.baseline)
        return
    added = _require_skill_names(update.added)
    if not added:
        return
    changes = connection.total_changes
    connection.executemany(
        "INSERT OR IGNORE INTO session_seen_skills (session_key, skill_name) VALUES (?, ?)",
        [(session_key, name) for name in added],
    )
    if connection.total_changes == changes:
        return
    connection.execute(
        "UPDATE sessions SET state_revision = state_revision + 1 WHERE session_key = ?",
        (session_key,),
    )


def affinity_of(state: sqlite3.Row) -> str:
    """Return a Session row's prompt-cache affinity id, defaulting by address."""
    value = state["prompt_cache_affinity_id"]
    if value is None:
        return _default_prompt_cache_affinity_id(_store_values._address(state))
    if not _is_prompt_cache_affinity_id(value):
        raise ChatSessionError(
            f"invalid prompt cache affinity id for session: {state['session_id']}"
        )
    return str(value)


def prompt_cache_affinity_id(connection: sqlite3.Connection, address: SessionAddress) -> str:
    return affinity_of(_store_values._require_live(connection, address))


def rotate_affinity(connection: sqlite3.Connection, session_key: int) -> str:
    """Start a new prompt-cache lineage and return its affinity id."""
    value = _new_prompt_cache_affinity_id()
    connection.execute(
        "UPDATE sessions SET prompt_cache_affinity_id = ?, state_revision = state_revision + 1 "
        "WHERE session_key = ?",
        (value, session_key),
    )
    return value


def carry_prompt_state(
    connection: sqlite3.Connection,
    *,
    source: sqlite3.Row,
    target_key: int,
    same_scope: bool,
) -> None:
    """Give *target_key* the prompt state a fork or move of *source* keeps.

    Within one scope everything carries over, including the effective affinity
    id. Into another scope, Agent-bound pins and the seen Skills stay behind
    and the target starts a new prompt-cache lineage, so its own Agent pins its
    own Skill catalog, SOUL block and pinned memory.
    """
    source_key = int(source["session_key"])
    if source_key != target_key:
        connection.execute(
            "INSERT INTO session_prompt_pins (session_key, slot, blob_key) "
            "SELECT ?, slot, blob_key FROM session_prompt_pins WHERE session_key = ?",
            (target_key, source_key),
        )
        if same_scope and source["seen_skills_initialized"]:
            connection.execute(
                "INSERT INTO session_seen_skills (session_key, skill_name) "
                "SELECT ?, skill_name FROM session_seen_skills WHERE session_key = ?",
                (target_key, source_key),
            )
    if same_scope:
        connection.execute(
            "UPDATE sessions SET prompt_cache_affinity_id = ?, seen_skills_initialized = ? "
            "WHERE session_key = ?",
            (affinity_of(source), int(source["seen_skills_initialized"]), target_key),
        )
        return
    dropped = [
        int(row[0])
        for row in connection.execute(
            "SELECT blob_key FROM session_prompt_pins WHERE session_key = ? AND slot IN "
            f"({', '.join('?' for _ in AGENT_BOUND_PROMPT_PIN_SLOTS)})",
            (target_key, *sorted(AGENT_BOUND_PROMPT_PIN_SLOTS)),
        )
    ]
    connection.execute(
        "DELETE FROM session_prompt_pins WHERE session_key = ? AND slot IN "
        f"({', '.join('?' for _ in AGENT_BOUND_PROMPT_PIN_SLOTS)})",
        (target_key, *sorted(AGENT_BOUND_PROMPT_PIN_SLOTS)),
    )
    delete_unreferenced_blobs(connection, dropped)
    connection.execute("DELETE FROM session_seen_skills WHERE session_key = ?", (target_key,))
    connection.execute(
        "UPDATE sessions SET prompt_cache_affinity_id = ?, seen_skills_initialized = 0 "
        "WHERE session_key = ?",
        (_new_prompt_cache_affinity_id(), target_key),
    )
