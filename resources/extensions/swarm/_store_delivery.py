"""Store delivery."""

# ruff: noqa: E501
# mypy: disable-error-code=no-any-return

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from core.sessions import DeliveryReceipt, SessionAddress
from core.utils.ids import new_id
from core.utils.timestamps import utc_now_timestamp

from ._board_view import delivered_chars
from ._store_database import (
    ALIASED_POST_COLUMNS,
    POST_CONTEXT_COLUMNS,
    SwarmDatabase,
)
from ._store_lifecycle import (
    _refresh_swarm_state,
)
from ._store_records import (
    _assert_epoch,
    _assert_mutable,
    _budgeted_rows,
    _participant,
    _pending_count,
    _pending_rows,
)
from ._store_values import (
    Json,
    Page,
    SwarmStoreError,
    _hash,
    _load,
    _page,
    _post,
)


def _delivery_rows(
    connection: sqlite3.Connection,
    swarm_id: str,
    participant_id: str,
    post_ids: Sequence[str],
) -> list[sqlite3.Row]:
    """Load full posts only after their delivery candidates have been selected.

    A post an automatic batch skipped counts as delivered but has not been shown,
    so a Board read that shows it still delivers it.
    """
    if not post_ids:
        return []
    placeholders = ",".join("?" for _ in post_ids)
    return connection.execute(
        f"SELECT {ALIASED_POST_COLUMNS},r.route_class,{POST_CONTEXT_COLUMNS} "
        "FROM recipients r JOIN posts p ON p.id=r.post_id "
        "JOIN discussions d ON d.id=p.discussion_id "
        f"WHERE p.swarm_id=? AND r.participant_id=? AND (r.delivered_at IS NULL OR r.skipped IS NOT NULL) AND p.id IN ({placeholders}) ORDER BY p.sequence",
        (swarm_id, participant_id, *post_ids),
    ).fetchall()


def _pending_outside(
    connection: sqlite3.Connection, swarm_id: str, participant_id: str, receipt_id: str
) -> int:
    """Count the participant's pending posts that the batch ``receipt_id`` leaves pending."""
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM recipients r JOIN posts p ON p.id=r.post_id "
            "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL "
            "AND r.post_id NOT IN (SELECT post_id FROM delivery_batch_entries WHERE receipt_id=?)",
            (swarm_id, participant_id, receipt_id),
        ).fetchone()[0]
    )


def _automatic_batch(
    connection: sqlite3.Connection,
    swarm_id: str,
    participant_id: str,
    candidates: Sequence[tuple[str, int, bool]],
    limit: int,
    batch_chars: int,
) -> tuple[list[sqlite3.Row], list[str]]:
    """Select one automatic batch from the eligible pending posts, oldest first.

    ``candidates`` are (post ID, sequence, kept) in sequence order; kept posts
    address or answer the reader, or come from the user. A backlog that fits one
    batch arrives whole. A larger one brings every kept post that fits and fills the
    rest with the newest other posts; the older other posts it passes over are
    returned as skipped, to count as delivered. Full posts are loaded for at most
    one batch, and a batch that cannot hold every kept post skips nothing.
    """
    loaded: dict[str, sqlite3.Row] = {}

    def load(post_ids: Sequence[str]) -> list[sqlite3.Row]:
        missing = [post_id for post_id in post_ids if post_id not in loaded]
        for row in _delivery_rows(connection, swarm_id, participant_id, missing):
            loaded[str(row["id"])] = row
        return [loaded[post_id] for post_id in post_ids if post_id in loaded]

    if len(candidates) <= limit:
        rows = load([post_id for post_id, _, _ in candidates])
        batch = _budgeted_rows(rows, limit, batch_chars)
        if len(batch) == len(rows):
            return batch, []
    kept_ids = [post_id for post_id, _, kept in candidates if kept]
    kept = _budgeted_rows(load(kept_ids[:limit]), limit, batch_chars)
    room = limit - len(kept)
    others = [post_id for post_id, _, kept in candidates if not kept]
    if len(kept) < len(kept_ids) or not room or not others:
        return kept, []
    chars = batch_chars - sum(delivered_chars(_post(row)) for row in kept)
    newest: list[sqlite3.Row] = []
    for row in reversed(load(others[-room:])):
        shown = delivered_chars(_post(row))
        if (kept or newest) and shown > chars:
            break
        newest.append(row)
        chars -= shown
    if not newest:
        return kept, []
    oldest = int(newest[-1]["sequence"])
    skipped = [post_id for post_id, sequence, kept in candidates if not kept and sequence < oldest]
    return sorted([*kept, *newest], key=lambda row: int(row["sequence"])), skipped


def _skipped_summary(
    connection: sqlite3.Connection, swarm_id: str, post_ids: Sequence[str]
) -> list[Json]:
    """Describe skipped posts per discussion, with what a read of them needs.

    ``before`` is the discussion's first post after its newest skipped one (None
    when there is none), and ``span`` counts the discussion's posts from its oldest
    skipped one up to ``before``, so one read page can hold them all.
    """
    if not post_ids:
        return []
    placeholders = ",".join("?" for _ in post_ids)
    groups = connection.execute(
        "SELECT p.discussion_id,d.sequence AS discussion_sequence,d.title AS discussion_title,"
        "COUNT(*) AS count,MIN(p.sequence) AS oldest,MAX(p.sequence) AS newest "
        "FROM posts p JOIN discussions d ON d.id=p.discussion_id "
        f"WHERE p.swarm_id=? AND p.id IN ({placeholders}) GROUP BY p.discussion_id ORDER BY MIN(p.sequence)",
        (swarm_id, *post_ids),
    ).fetchall()
    summary: list[Json] = []
    for group in groups:
        before = connection.execute(
            "SELECT MIN(sequence) FROM posts WHERE swarm_id=? AND discussion_id=? AND sequence>?",
            (swarm_id, group["discussion_id"], group["newest"]),
        ).fetchone()[0]
        span = connection.execute(
            "SELECT COUNT(*) FROM posts WHERE swarm_id=? AND discussion_id=? AND sequence>=? AND sequence<?",
            (
                swarm_id,
                group["discussion_id"],
                group["oldest"],
                int(group["newest"]) + 1 if before is None else before,
            ),
        ).fetchone()[0]
        summary.append(
            {
                "discussion_id": group["discussion_id"],
                "discussion_sequence": int(group["discussion_sequence"]),
                "discussion_title": group["discussion_title"],
                "count": int(group["count"]),
                "before": None if before is None else int(before),
                "span": int(span),
            }
        )
    return summary


def _prepare_delivery(
    db: SwarmDatabase,
    swarm_id: str,
    participant_id: str,
    post_ids: tuple[str, ...] | None,
    limit: int,
    effect_kind: str,
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _participant(connection, swarm_id, participant_id)
        binding = connection.execute(
            "SELECT project_id,agent_id,session_id,generation_id,owner_name FROM participant_sessions WHERE participant_id=?",
            (participant_id,),
        ).fetchone()
        if binding is None:
            raise SwarmStoreError("participant_unbound")
        settings = _load(
            connection.execute(
                "SELECT delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        )
        if post_ids is None:
            rows = _pending_rows(
                connection, swarm_id, participant_id, limit, settings["batch_chars"]
            )
            receipt_id = new_id("rcp")
        else:
            if not post_ids:
                return {"entries": [], "receipt_id": None, "pending_remaining": 0}
            rows = _delivery_rows(connection, swarm_id, participant_id, post_ids)
            receipt_id = new_id("rcp")
        entries = [_post(row) for row in rows]
        if not entries:
            return {"entries": [], "receipt_id": None, "pending_remaining": 0}
        content_hash = _hash({"effect_kind": effect_kind, "posts": entries})
        connection.execute(
            "INSERT INTO delivery_batches(receipt_id,participant_id,content_hash,effect_kind,created_at) VALUES(?,?,?,?,?)",
            (receipt_id, participant_id, content_hash, effect_kind, utc_now_timestamp()),
        )
        for entry in entries:
            connection.execute(
                "INSERT INTO delivery_batch_entries(receipt_id,post_id,participant_id) VALUES(?,?,?)",
                (receipt_id, entry["id"], participant_id),
            )
        return {
            "entries": entries,
            "receipt_id": receipt_id,
            "content_hash": content_hash,
            "effect_kind": effect_kind,
            "pending_remaining": _pending_outside(connection, swarm_id, participant_id, receipt_id),
        }

    return db._write(operation)


def _prepare_automatic_delivery(
    db: SwarmDatabase,
    swarm_id: str,
    participant_id: str,
    expected_epoch: int,
    boundary: int | None,
    wake_only: bool,
    announce: bool = True,
    wake_routes: frozenset[str] | None = None,
) -> Json:
    def wakes(settings: Json, route: str, author_kind: str) -> bool:
        # A narrowed wake still lets the user reach every idle participant at once.
        return bool(settings[route]["wake_idle"]) and (
            wake_routes is None or route in wake_routes or author_kind == "user"
        )

    def operation(connection: sqlite3.Connection) -> Json:
        _assert_epoch(connection, swarm_id, expected_epoch)
        participant = _participant(connection, swarm_id, participant_id)
        # Busy participants select their batch at the next request boundary;
        # freezing it in the wake scan could replay content read by a Tool.
        if wake_only and participant["state"] != "idle":
            return {
                "entries": [],
                "wake": False,
                "pending_remaining": _pending_count(connection, swarm_id, participant_id),
            }
        resolved_boundary = int(participant["idle_boundary"] or 0) if boundary is None else boundary
        if participant["state"] in {"failed", "cancelled", "interrupted"}:
            return {"entries": [], "wake": False, "pending_remaining": 0}
        settings_row = connection.execute(
            "SELECT revision,delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
        ).fetchone()
        settings = _load(settings_row["delivery_json"])
        prepared = connection.execute(
            "SELECT receipt_id,content_hash,effect_kind,settings_revision FROM delivery_batches "
            "WHERE participant_id=? AND effect_kind='swarm_automatic' AND acknowledged_at IS NULL "
            "ORDER BY created_at LIMIT 1",
            (participant_id,),
        ).fetchone()
        announced = int(participant["wake_announced_seq"] or 0)
        if prepared is not None:
            newest = int(
                connection.execute(
                    "SELECT COALESCE(MAX(p.sequence),0) FROM recipients r INDEXED BY recipients_pending_participant JOIN posts p ON p.id=r.post_id "
                    "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL",
                    (swarm_id, participant_id),
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"SELECT {ALIASED_POST_COLUMNS},r.route_class,{POST_CONTEXT_COLUMNS} FROM delivery_batch_entries e JOIN posts p ON p.id=e.post_id "
                "JOIN recipients r ON r.post_id=p.id AND r.participant_id=e.participant_id "
                "JOIN discussions d ON d.id=p.discussion_id "
                "WHERE e.receipt_id=? AND e.skipped IS NULL ORDER BY p.sequence",
                (prepared["receipt_id"],),
            ).fetchall()
            skipped = [
                str(row["post_id"])
                for row in connection.execute(
                    "SELECT post_id FROM delivery_batch_entries WHERE receipt_id=? AND skipped IS NOT NULL",
                    (prepared["receipt_id"],),
                )
            ]
            wake = (
                announce
                and newest > announced
                and not participant["wake_pending"]
                and participant["state"] in {"idle"}
                and any(
                    wakes(settings, row["route_class"], row["author_kind"])
                    for row in connection.execute(
                        "SELECT r.route_class,p.author_kind FROM recipients r JOIN posts p ON p.id=r.post_id "
                        "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL",
                        (swarm_id, participant_id),
                    )
                )
            )
            if wake:
                connection.execute(
                    "UPDATE participants SET wake_epoch=?,wake_pending=1,wake_pending_seq=? WHERE id=?",
                    (expected_epoch, _announced_through(rows, newest, announced), participant_id),
                )
            return {
                "entries": [_post(row) for row in rows],
                "skipped": _skipped_summary(connection, swarm_id, skipped),
                "receipt_id": prepared["receipt_id"],
                "content_hash": prepared["content_hash"],
                "effect_kind": prepared["effect_kind"],
                "wake": wake,
                "pending_remaining": _pending_outside(
                    connection, swarm_id, participant_id, prepared["receipt_id"]
                ),
                "settings_revision": int(prepared["settings_revision"]),
                "replayed": True,
            }
        # Scan only the facts needed for policy and wake decisions. Full posts and
        # their context are loaded for at most one batch, not the entire backlog.
        pending = connection.execute(
            "SELECT p.id,p.sequence,p.author_kind,r.route_class FROM recipients r INDEXED BY recipients_pending_participant JOIN posts p ON p.id=r.post_id "
            "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL ORDER BY p.sequence",
            (swarm_id, participant_id),
        )
        candidates: list[tuple[str, int, bool]] = []
        pending_count = 0
        newest = 0
        can_wake = False
        for row in pending:
            pending_count += 1
            newest = int(row["sequence"])
            can_wake = can_wake or wakes(settings, row["route_class"], row["author_kind"])
            policy = settings[row["route_class"]]
            if policy["mode"] == "pull" and not (
                participant["state"] == "idle" and policy["wake_idle"]
            ):
                continue
            if policy["mode"] == "idle" and participant["state"] != "idle":
                continue
            # A post that addresses or answers the reader, or the user's, is never skipped.
            kept = row["route_class"] == "ping" or row["author_kind"] == "user"
            candidates.append((str(row["id"]), int(row["sequence"]), kept))
        epoch = connection.execute(
            "SELECT epoch,is_open FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
        ).fetchone()
        if not epoch["is_open"] or int(epoch["epoch"]) != expected_epoch:
            raise SwarmStoreError("stale_epoch")
        epoch_value = int(epoch["epoch"])
        wake = bool(
            announce
            and newest > announced
            and not participant["wake_pending"]
            and can_wake
            and participant["state"] in {"idle"}
        )
        rows, skipped = (
            _automatic_batch(
                connection,
                swarm_id,
                participant_id,
                candidates,
                settings["batch_messages"],
                settings["batch_chars"],
            )
            if candidates and not (wake_routes is not None and not wake)
            else ([], [])
        )
        if wake:
            connection.execute(
                "UPDATE participants SET wake_epoch=?,wake_pending=1,wake_pending_seq=?,idle_boundary=? WHERE id=?",
                (
                    epoch_value,
                    _announced_through(rows, newest, announced),
                    resolved_boundary,
                    participant_id,
                ),
            )
        # A held wake freezes nothing, so the Run that eventually starts receives
        # the posts pending then rather than a batch selected while it waited.
        if not rows or (wake_routes is not None and not wake):
            return {
                "entries": [],
                "wake": wake,
                "pending_remaining": pending_count,
                "settings_revision": int(settings_row["revision"]),
                "admission_boundary": resolved_boundary,
            }
        receipt_id = new_id("rcp")
        entries = [_post(row) for row in rows]
        content: Json = {
            "effect_kind": "swarm_automatic",
            "posts": entries,
            "revision": int(settings_row["revision"]),
        }
        if skipped:
            content["skipped"] = skipped
        content_hash = _hash(content)
        connection.execute(
            "INSERT INTO delivery_batches(receipt_id,participant_id,content_hash,effect_kind,created_at,settings_revision) VALUES(?,?,?,?,?,?)",
            (
                receipt_id,
                participant_id,
                content_hash,
                "swarm_automatic",
                utc_now_timestamp(),
                int(settings_row["revision"]),
            ),
        )
        for entry in entries:
            connection.execute(
                "INSERT INTO delivery_batch_entries(receipt_id,post_id,participant_id) VALUES(?,?,?)",
                (receipt_id, entry["id"], participant_id),
            )
        for post_id in skipped:
            connection.execute(
                "INSERT INTO delivery_batch_entries(receipt_id,post_id,participant_id,skipped) VALUES(?,?,?,1)",
                (receipt_id, post_id, participant_id),
            )
        return {
            "entries": entries,
            "skipped": _skipped_summary(connection, swarm_id, skipped),
            "receipt_id": receipt_id,
            "content_hash": content_hash,
            "effect_kind": "swarm_automatic",
            "wake": wake,
            "pending_remaining": pending_count - len(entries) - len(skipped),
            "settings_revision": int(settings_row["revision"]),
            "admission_boundary": resolved_boundary,
        }

    return db._write(operation)


def _announced_through(rows: Sequence[sqlite3.Row], newest: int, announced: int) -> int:
    """Return the sequence a wake announces: through the batch its Run receives first.

    Posts beyond that batch stay unannounced, so if the Run ends before receiving
    them they start another wake. The fallback to ``newest`` keeps announcements
    strictly increasing, which the wake's admission key relies on.
    """

    delivered = max((int(row["sequence"]) for row in rows), default=0)
    return delivered if delivered > announced else newest


def _list_prepared_deliveries(db: SwarmDatabase, cursor: str | None, limit: int) -> Page:
    with db._read() as connection:
        high_water = int(
            connection.execute("SELECT COALESCE(MAX(rowid),0) FROM delivery_batches").fetchone()[0]
        )
        offset = 0
        if cursor:
            offset, frozen = db._cursor(cursor, "prepared", "all", high_water)
            if frozen is None:
                raise SwarmStoreError("invalid_cursor")
            high_water = frozen
        rows = connection.execute(
            "SELECT receipt_id,participant_id,content_hash,effect_kind,settings_revision FROM delivery_batches WHERE acknowledged_at IS NULL AND rowid<=? ORDER BY rowid LIMIT ? OFFSET ?",
            (high_water, limit + 1, offset),
        ).fetchall()
        return _page(
            [dict(row) for row in rows],
            limit,
            db._make_cursor("prepared", "all", high_water, offset + limit),
        )


def _claim_wake(db: SwarmDatabase, swarm_id: str, participant_id: str, expected_epoch: int) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _assert_epoch(connection, swarm_id, expected_epoch)
        participant = _participant(connection, swarm_id, participant_id)
        if not participant["wake_pending"] or int(participant["wake_epoch"]) != expected_epoch:
            return {"participant_id": participant_id, "pending": False}
        return {
            "participant_id": participant_id,
            "pending": True,
            "boundary": participant["idle_boundary"],
        }

    return db._write(operation)


def _list_wake_intents(db: SwarmDatabase, swarm_id: str, cursor: str | None, limit: int) -> Page:
    with db._read() as connection:
        _assert_mutable(connection, swarm_id)
        high_water = int(
            connection.execute(
                "SELECT COALESCE(MAX(ordinal),0) FROM participants WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        )
        offset = 0
        if cursor:
            offset, frozen = db._cursor(cursor, "wakes", swarm_id, high_water)
            if frozen is None:
                raise SwarmStoreError("invalid_cursor")
            high_water = frozen
        rows = connection.execute(
            "SELECT id,wake_epoch,wake_announced_seq,idle_boundary FROM participants WHERE swarm_id=? AND wake_pending=1 AND ordinal<=? ORDER BY ordinal LIMIT ? OFFSET ?",
            (swarm_id, high_water, limit + 1, offset),
        ).fetchall()
        entries = [
            {
                "participant_id": row["id"],
                "epoch": row["wake_epoch"],
                "boundary": row["idle_boundary"],
                "announced_sequence": row["wake_announced_seq"],
            }
            for row in rows
        ]
        return _page(entries, limit, db._make_cursor("wakes", swarm_id, high_water, offset + limit))


def _mark_wake_admitted(
    db: SwarmDatabase,
    swarm_id: str,
    participant_id: str,
    expected_epoch: int,
    run_id: str,
    boundary: int | None,
) -> Json:
    def operation(connection: sqlite3.Connection) -> Json:
        _assert_epoch(connection, swarm_id, expected_epoch)
        participant = _participant(connection, swarm_id, participant_id)
        if (
            not participant["wake_pending"]
            and participant["lifecycle_run_id"] == run_id
            and participant["idle_boundary"] == boundary
        ):
            return {
                "participant_id": participant_id,
                "run_id": run_id,
                "boundary": boundary,
                "admitted": True,
                "replayed": True,
                "swarm_state_changed": False,
            }
        if not participant["wake_pending"] or participant["idle_boundary"] != boundary:
            raise SwarmStoreError("wake_unavailable")
        state = participant["state"] if participant["lifecycle_run_id"] == run_id else "running"
        connection.execute(
            "UPDATE participants SET wake_pending=0,wake_announced_seq=wake_pending_seq,state=?,lifecycle_run_id=?,idle_boundary=? WHERE id=?",
            (state, run_id, boundary, participant_id),
        )
        return {
            "participant_id": participant_id,
            "run_id": run_id,
            "boundary": boundary,
            "admitted": True,
            "swarm_state_changed": _refresh_swarm_state(db, connection, swarm_id),
        }

    return db._write(operation)


def _prepared_delivery(db: SwarmDatabase, receipt_id: str) -> Json | None:
    with db._read() as connection:
        row = connection.execute(
            "SELECT b.participant_id,b.receipt_id,b.content_hash,b.effect_kind,b.acknowledged_at,s.project_id,s.agent_id,s.session_id,s.generation_id,s.owner_name "
            "FROM delivery_batches b JOIN participant_sessions s ON s.participant_id=b.participant_id "
            "WHERE b.receipt_id=?",
            (receipt_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "participant_id": str(row["participant_id"]),
        "receipt_id": str(row["receipt_id"]),
        "content_hash": str(row["content_hash"]),
        "effect_kind": str(row["effect_kind"]),
        "generation_id": str(row["generation_id"]),
        "owner_name": str(row["owner_name"]),
        "acknowledged": row["acknowledged_at"] is not None,
        "address": SessionAddress(row["project_id"], str(row["agent_id"]), str(row["session_id"])),
    }


def _acknowledge_delivery(
    db: SwarmDatabase, prepared: Json, receipt: DeliveryReceipt | None
) -> bool:
    if receipt is None:
        return False
    location = receipt.carrier_location
    if (
        receipt.receipt_id != prepared["receipt_id"]
        or receipt.content_hash != prepared["content_hash"]
        or receipt.effect_kind != prepared["effect_kind"]
        or not isinstance(location, dict)
        or not isinstance(location.get("kind"), str)
        or type(location.get("sequence")) is not int
        or location["sequence"] < 0
    ):
        raise SwarmStoreError("receipt_conflict")

    def operation(connection: sqlite3.Connection) -> bool:
        if prepared["acknowledged"]:
            return True
        connection.execute(
            "UPDATE recipients SET delivered_at=?,carrier_kind=?,carrier_sequence=?,"
            "skipped=(SELECT e.skipped FROM delivery_batch_entries e WHERE e.receipt_id=? "
            "AND e.post_id=recipients.post_id AND e.participant_id=recipients.participant_id) "
            "WHERE participant_id=? AND delivered_at IS NULL AND post_id IN "
            "(SELECT post_id FROM delivery_batch_entries WHERE receipt_id=? AND participant_id=?)",
            (
                utc_now_timestamp(),
                location["kind"],
                location["sequence"],
                receipt.receipt_id,
                prepared["participant_id"],
                receipt.receipt_id,
                prepared["participant_id"],
            ),
        )
        # A skipped post this batch shows has now reached its reader.
        connection.execute(
            "UPDATE recipients SET skipped=NULL WHERE participant_id=? AND skipped IS NOT NULL AND post_id IN "
            "(SELECT post_id FROM delivery_batch_entries WHERE receipt_id=? AND participant_id=? AND skipped IS NULL)",
            (prepared["participant_id"], receipt.receipt_id, prepared["participant_id"]),
        )
        connection.execute(
            "UPDATE delivery_batches SET acknowledged_at=?,carrier_kind=?,carrier_sequence=? WHERE receipt_id=?",
            (utc_now_timestamp(), location["kind"], location["sequence"], receipt.receipt_id),
        )
        return True

    return db._write(operation)
