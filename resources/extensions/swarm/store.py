"""Durable, extension-local persistence for Swarm profiles and Board state."""
# ruff: noqa: E501
# mypy: disable-error-code=no-any-return

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.sessions import DeliveryReceipt, SessionAddress, TemporarySessionBinding
from core.sessions.schema import required_journal_mode
from core.settings.agent_defaults import _fallback_chain_entries
from core.settings.settings import (
    SettingsValidationError,
    validate_temperature,
    validate_thinking_effort,
)
from core.tools.availability import normalize_tool_access
from core.utils.ids import new_id
from core.utils.workers import BoundedWorkerPool

Json = dict[str, Any]
_WORKERS = BoundedWorkerPool(name="swarm-store", max_workers=2)
_PROFILE_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_MAX_LIMIT = 100
_DELIVERY_ROUTES = frozenset({"main", "discussion", "ping"})
_DELIVERY_MODES = frozenset({"all", "idle", "pull"})
_SWARM_STATES = frozenset(
    {
        "preparing",
        "running",
        "idle",
        "needs_attention",
        "stopping",
        "stopped",
        "deleting",
        "cancelled",
        "interrupted",
    }
)
_MUTABLE_SWARM_STATES = frozenset({"preparing", "running", "idle", "needs_attention"})
_PARTICIPANT_STATES = frozenset({"idle", "running", "failed", "cancelled", "interrupted"})
_DELIVERY_DEFAULTS: Json = {
    "main": {"mode": "all", "wake_idle": True},
    "discussion": {"mode": "all", "wake_idle": True},
    "ping": {"mode": "all", "wake_idle": True},
    "coalesce_ms": 250,
    "batch_messages": 20,
    "batch_chars": 24_000,
}
DeliveryReceiptLookup = Callable[[SessionAddress, str, str, str], Awaitable[DeliveryReceipt | None]]


class SwarmStoreError(Exception):
    """A stable structured persistence error with no runtime guidance text."""

    def __init__(self, code: str, *, field: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field = field


@dataclass(frozen=True)
class Page:
    entries: tuple[Json, ...]
    has_more: bool
    cursor: str | None


class SwarmStore:
    """One deep SQLite owner for immutable Swarm configuration and Board facts."""

    def __init__(
        self,
        path: Path,
        *,
        lookup_delivery_receipt: DeliveryReceiptLookup | None = None,
        worker_pool: BoundedWorkerPool = _WORKERS,
    ) -> None:
        self._path = Path(path)
        self._workers = worker_pool
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._lookup_delivery_receipt = lookup_delivery_receipt

    async def open(self) -> None:
        await self._run(self._open)

    async def close(self) -> None:
        await self._run(self._close)

    async def save_profile(
        self, profile: Mapping[str, Any], *, expected_revision: int | None
    ) -> Json:
        normalized = _validate_profile(profile)
        return await self._run(
            self._save_profile, normalized, expected_revision, "slug" not in profile
        )

    async def get_profile(self, profile_id: str) -> Json:
        return await self._run(self._get_profile, profile_id)

    async def list_profiles(self, *, cursor: str | None = None, limit: int = 20) -> Page:
        return await self._run(self._list_profiles, cursor, _limit(limit))

    async def delete_profile(self, profile_id: str, *, expected_revision: int) -> None:
        _revision(expected_revision, "expected_revision")
        await self._run(self._delete_profile, profile_id, expected_revision)

    async def create_swarm(
        self,
        profile_id: str,
        prompt: str,
        effective_configuration: Mapping[str, Any],
        *,
        request_id: str,
        expected_profile_revision: int,
    ) -> Json:
        if not isinstance(prompt, str) or not prompt.strip():
            raise SwarmStoreError("invalid_arguments", field="prompt")
        _request_id(request_id)
        _revision(expected_profile_revision, "expected_profile_revision")
        return await self._run(
            self._create_swarm,
            profile_id,
            prompt,
            _json_object(effective_configuration),
            request_id,
            expected_profile_revision,
        )

    async def begin_delete(self, swarm_id: str) -> bool:
        return await self._run(self._begin_delete, swarm_id)

    async def delete_swarm(self, swarm_id: str) -> None:
        await self._run(self._delete_swarm, swarm_id)

    async def get_swarm(self, swarm_id: str) -> Json:
        return await self._run(self._get_swarm, swarm_id)

    async def apply_delivery_settings(
        self,
        swarm_id: str,
        delivery: Mapping[str, Any],
        *,
        expected_revision: int,
        request_id: str,
        actor: str,
    ) -> Json:
        _revision(expected_revision, "expected_revision")
        _request_id(request_id)
        if not isinstance(actor, str) or not actor:
            raise SwarmStoreError("invalid_arguments", field="actor")
        return await self._run(
            self._apply_delivery_settings,
            swarm_id,
            _delivery(delivery),
            expected_revision,
            request_id,
            actor,
        )

    async def set_swarm_state(self, swarm_id: str, state: str) -> Json:
        if state not in {"running", "idle", "needs_attention"}:
            raise SwarmStoreError("invalid_arguments", field="state")
        return await self._run(self._set_swarm_state, swarm_id, state)

    async def fail_startup(self, swarm_id: str, *, expected_epoch: int) -> Json:
        """Close a partially prepared epoch while preserving the Swarm for Resume."""
        if type(expected_epoch) is not int or expected_epoch < 0:
            raise SwarmStoreError("invalid_arguments", field="expected_epoch")
        return await self._run(self._fail_startup, swarm_id, expected_epoch)

    async def set_participant_state(
        self, swarm_id: str, participant_id: str, state: str, *, idle_boundary: int | None = None
    ) -> Json:
        if state not in _PARTICIPANT_STATES or (
            idle_boundary is not None and (type(idle_boundary) is not int or idle_boundary < 0)
        ):
            raise SwarmStoreError("invalid_arguments", field="state")
        return await self._run(
            self._set_participant_state, swarm_id, participant_id, state, idle_boundary
        )

    async def participant_status(
        self,
        swarm_id: str,
        participant_id: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> Json:
        return await self._run(
            self._participant_status,
            swarm_id,
            participant_id,
            cursor,
            _limit(limit),
        )

    async def record_run_started(
        self, swarm_id: str, participant_id: str, *, run_id: str, expected_epoch: int
    ) -> Json:
        return await self._run(
            self._record_run_started, swarm_id, participant_id, run_id, expected_epoch
        )

    async def reconcile_run_finished(
        self, swarm_id: str, participant_id: str, *, run_id: str, expected_epoch: int, outcome: str
    ) -> Json:
        if outcome not in {"completed", "failed", "cancelled", "interrupted"}:
            raise SwarmStoreError("invalid_arguments", field="outcome")
        return await self._run(
            self._reconcile_run_finished, swarm_id, participant_id, run_id, expected_epoch, outcome
        )

    async def recover_interrupted(self) -> list[Json]:
        """Close recoverable epochs without admitting or scheduling participant work."""
        return await self._run(self._recover_interrupted)

    async def begin_stop(self, swarm_id: str, *, request_id: str, actor: str) -> Json:
        _request_id(request_id)
        if not isinstance(actor, str) or not actor:
            raise SwarmStoreError("invalid_arguments", field="actor")
        return await self._run(self._begin_stop, swarm_id, request_id, actor)

    async def finish_stop(
        self, swarm_id: str, *, request_id: str, actor: str, drain_report: Mapping[str, Any]
    ) -> Json:
        _request_id(request_id)
        if not isinstance(actor, str) or not actor:
            raise SwarmStoreError("invalid_arguments", field="actor")
        return await self._run(
            self._finish_stop, swarm_id, request_id, actor, _json_object(drain_report)
        )

    async def begin_resume(
        self, swarm_id: str, *, request_id: str, actor: str, participant_id: str | None = None
    ) -> Json:
        _request_id(request_id)
        if not isinstance(actor, str) or not actor:
            raise SwarmStoreError("invalid_arguments", field="actor")
        return await self._run(self._begin_resume, swarm_id, request_id, actor, participant_id)

    async def list_swarms(self, *, cursor: str | None = None, limit: int = 20) -> Page:
        return await self._run(self._list_swarms, cursor, _limit(limit))

    async def list_events(
        self, swarm_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> Page:
        return await self._run(self._list_events, swarm_id, cursor, _limit(limit))

    async def bind_execution_epoch(
        self, swarm_id: str, *, expected_epoch: int, execution_epoch: str
    ) -> Json:
        if type(expected_epoch) is not int or expected_epoch < 0 or not execution_epoch:
            raise SwarmStoreError("invalid_arguments")
        return await self._run(
            self._bind_execution_epoch, swarm_id, expected_epoch, execution_epoch
        )

    async def bind_participant_session(self, binding: TemporarySessionBinding) -> None:
        await self._run(self._bind_participant_session, binding)

    async def prepare_inbox_delivery(
        self, swarm_id: str, participant_id: str, *, limit: int = 20
    ) -> Json:
        return await self._run(
            self._prepare_delivery, swarm_id, participant_id, None, _limit(limit), "swarm_inbox"
        )

    async def list_prepared_deliveries(self, *, cursor: str | None = None, limit: int = 20) -> Page:
        """Return bounded unacknowledged receipts for read-only startup reconciliation."""
        return await self._run(self._list_prepared_deliveries, cursor, _limit(limit))

    async def list_wake_intents(
        self, swarm_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> Page:
        return await self._run(self._list_wake_intents, swarm_id, cursor, _limit(limit))

    async def claim_wake(self, swarm_id: str, participant_id: str, *, expected_epoch: int) -> Json:
        return await self._run(self._claim_wake, swarm_id, participant_id, expected_epoch)

    async def mark_wake_admitted(
        self,
        swarm_id: str,
        participant_id: str,
        *,
        expected_epoch: int,
        run_id: str,
        boundary: int | None,
    ) -> Json:
        if (
            not isinstance(run_id, str)
            or not run_id
            or (boundary is not None and (type(boundary) is not int or boundary < 0))
        ):
            raise SwarmStoreError("invalid_arguments")
        return await self._run(
            self._mark_wake_admitted, swarm_id, participant_id, expected_epoch, run_id, boundary
        )

    async def prepare_board_read_delivery(
        self, swarm_id: str, participant_id: str, post_ids: Sequence[str]
    ) -> Json:
        if isinstance(post_ids, (str, bytes)) or not all(
            isinstance(post_id, str) for post_id in post_ids
        ):
            raise SwarmStoreError("invalid_arguments", field="post_ids")
        return await self._run(
            self._prepare_delivery,
            swarm_id,
            participant_id,
            tuple(dict.fromkeys(post_ids)),
            _MAX_LIMIT,
            "swarm_board_read",
        )

    async def prepare_automatic_delivery(
        self,
        swarm_id: str,
        participant_id: str,
        *,
        expected_epoch: int,
        admission_boundary: int | None = None,
    ) -> Json:
        if admission_boundary is not None and (
            type(admission_boundary) is not int or admission_boundary < 0
        ):
            raise SwarmStoreError("invalid_arguments", field="admission_boundary")
        if type(expected_epoch) is not int or expected_epoch < 0:
            raise SwarmStoreError("invalid_arguments", field="expected_epoch")
        return await self._run(
            self._prepare_automatic_delivery,
            swarm_id,
            participant_id,
            expected_epoch,
            admission_boundary,
        )

    async def prepare_wake(
        self, swarm_id: str, participant_id: str, *, expected_epoch: int
    ) -> Json:
        """Freeze one idle boundary and prepare its delivery before Run admission."""
        if type(expected_epoch) is not int or expected_epoch < 0:
            raise SwarmStoreError("invalid_arguments", field="expected_epoch")
        return await self._run(
            self._prepare_automatic_delivery, swarm_id, participant_id, expected_epoch, None
        )

    async def reconcile_delivery(self, receipt_id: str) -> bool:
        _request_id(receipt_id)
        prepared = await self._run(self._prepared_delivery, receipt_id)
        if prepared is None:
            return False
        if self._lookup_delivery_receipt is None:
            raise RuntimeError("SwarmStore has no delivery receipt lookup")
        receipt = await self._lookup_delivery_receipt(
            prepared["address"],
            prepared["generation_id"],
            prepared["owner_name"],
            receipt_id,
        )
        return await self._run(self._acknowledge_delivery, prepared, receipt)

    async def list_discussions(
        self, swarm_id: str, participant_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> Page:
        return await self._run(
            self._list_discussions, swarm_id, participant_id, cursor, _limit(limit)
        )

    async def list_human_discussions(
        self, swarm_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> Page:
        """List public Board discussions without creating participant delivery state."""
        return await self._run(self._list_human_discussions, swarm_id, cursor, _limit(limit))

    async def read_posts(
        self,
        swarm_id: str,
        participant_id: str,
        *,
        discussion_id: str | None = None,
        message_id: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> Page:
        if message_id is not None and (
            discussion_id is not None or cursor is not None or limit != 20
        ):
            raise SwarmStoreError("invalid_arguments", field="message_id")
        return await self._run(
            self._read_posts,
            swarm_id,
            participant_id,
            discussion_id,
            message_id,
            cursor,
            _limit(limit),
        )

    async def read_human_posts(
        self,
        swarm_id: str,
        *,
        discussion_id: str | None = None,
        message_id: str | None = None,
        cursor: str | None = None,
        limit: int = 20,
    ) -> Page:
        """Read public Board history without participant membership or receipts."""
        if message_id is not None and (
            discussion_id is not None or cursor is not None or limit != 20
        ):
            raise SwarmStoreError("invalid_arguments", field="message_id")
        return await self._run(
            self._read_human_posts,
            swarm_id,
            discussion_id,
            message_id,
            cursor,
            _limit(limit),
        )

    async def post(
        self,
        swarm_id: str,
        sender_id: str,
        *,
        text: str,
        request_id: str,
        discussion_id: str | None = None,
        reply_to: str | None = None,
        recipients: Sequence[str] = (),
        author_kind: str = "participant",
        author_name: str | None = None,
        expected_epoch: int | None = None,
    ) -> Json:
        _text(text, "text", 16_000)
        _request_id(request_id)
        if author_kind not in {"participant", "user"}:
            raise SwarmStoreError("invalid_arguments", field="author_kind")
        normalized_recipients = _recipient_ids(recipients)
        return await self._run(
            self._post,
            swarm_id,
            sender_id,
            text,
            request_id,
            discussion_id,
            reply_to,
            normalized_recipients,
            author_kind,
            author_name,
            expected_epoch,
        )

    async def post_human(
        self,
        swarm_id: str,
        *,
        text: str,
        request_id: str,
        discussion_id: str | None = None,
        reply_to: str | None = None,
        recipients: Sequence[str] = (),
        author_name: str = "User",
    ) -> Json:
        """Persist an idempotent user-attributed public Board post."""
        _text(text, "text", 16_000)
        _request_id(request_id)
        if not isinstance(author_name, str) or not author_name.strip() or len(author_name) > 120:
            raise SwarmStoreError("invalid_arguments", field="author_name")
        return await self._run(
            self._post,
            swarm_id,
            "user",
            text,
            request_id,
            discussion_id,
            reply_to,
            _recipient_ids(recipients),
            "user",
            author_name.strip(),
            None,
        )

    async def create_discussion(
        self,
        swarm_id: str,
        participant_id: str,
        *,
        title: str,
        text: str,
        request_id: str,
        recipients: Sequence[str] = (),
        expected_epoch: int | None = None,
    ) -> Json:
        _text(title, "title", 120)
        _text(text, "text", 16_000)
        _request_id(request_id)
        return await self._run(
            self._create_discussion,
            swarm_id,
            participant_id,
            title,
            text,
            request_id,
            _recipient_ids(recipients),
            expected_epoch,
        )

    async def join_discussion(
        self,
        swarm_id: str,
        participant_id: str,
        discussion_id: str,
        *,
        expected_epoch: int | None = None,
    ) -> Json:
        return await self._run(
            self._membership, swarm_id, participant_id, discussion_id, True, expected_epoch
        )

    async def leave_discussion(
        self,
        swarm_id: str,
        participant_id: str,
        discussion_id: str,
        *,
        expected_epoch: int | None = None,
    ) -> Json:
        return await self._run(
            self._membership, swarm_id, participant_id, discussion_id, False, expected_epoch
        )

    def _open(self) -> None:
        with self._lock:
            self._open_connection()

    async def _run(self, function: Callable[..., Any], *arguments: Any) -> Any:
        return await self._workers.run(self._locked_call, function, *arguments)

    def _locked_call(self, function: Callable[..., Any], *arguments: Any) -> Any:
        with self._lock:
            return function(*arguments)

    def _open_connection(self) -> None:
        if self._connection is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self._path, isolation_level=None, check_same_thread=False, timeout=1
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute(
            f"PRAGMA journal_mode={required_journal_mode(sqlite3.sqlite_version_info)}"
        )
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(_SCHEMA)
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT value FROM swarm_meta WHERE key = 'cursor_key'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO swarm_meta(key, value) VALUES ('cursor_key', ?)",
                    (secrets.token_hex(32),),
                )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            connection.close()
            raise
        self._connection = connection

    def _close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _write(self, function: Callable[[sqlite3.Connection], Any]) -> Any:
        with self._lock:
            return self._write_locked(function)

    def _write_locked(self, function: Callable[[sqlite3.Connection], Any]) -> Any:
        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            value = function(connection)
            connection.execute("COMMIT")
            return value
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SwarmStore is not open")
        return self._connection

    def _save_profile(
        self, profile: Json, expected_revision: int | None, automatic_slug: bool = False
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            existing = connection.execute(
                "SELECT revision, slug FROM profiles WHERE id = ?", (profile["id"],)
            ).fetchone()
            if existing is None:
                if expected_revision is not None:
                    raise SwarmStoreError("revision_conflict")
                revision = 1
            else:
                if expected_revision != int(existing["revision"]):
                    raise SwarmStoreError("revision_conflict")
                revision = int(existing["revision"]) + 1
            profile["revision"] = revision
            if automatic_slug:
                if existing is not None:
                    profile["slug"] = existing["slug"]
                else:
                    base = re.sub(r"[^a-z0-9]+", "-", profile["name"].lower()).strip("-") or "swarm"
                    candidate, suffix = base, 2
                    while connection.execute(
                        "SELECT 1 FROM profiles WHERE slug=?", (candidate,)
                    ).fetchone():
                        candidate, suffix = f"{base}-{suffix}", suffix + 1
                    profile["slug"] = candidate
            now = _now()
            try:
                connection.execute(
                    "INSERT INTO profiles(id, slug, name, revision, payload, created_at, updated_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET slug=excluded.slug,name=excluded.name,revision=excluded.revision,payload=excluded.payload,updated_at=excluded.updated_at",
                    (
                        profile["id"],
                        profile["slug"],
                        profile["name"],
                        revision,
                        _dump(profile),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise SwarmStoreError("slug_unavailable", field="slug") from error
            return _copy(profile)

        return self._write(operation)

    def _get_profile(self, profile_id: str) -> Json:
        row = (
            self._require_connection()
            .execute("SELECT payload FROM profiles WHERE id=?", (profile_id,))
            .fetchone()
        )
        if row is None:
            raise SwarmStoreError("profile_not_found")
        return _load(row["payload"])

    def _list_profiles(self, cursor: str | None, limit: int) -> Page:
        offset = self._cursor(cursor, "profiles", "all", None)[0] if cursor else 0
        rows = (
            self._require_connection()
            .execute(
                "SELECT payload FROM profiles ORDER BY slug LIMIT ? OFFSET ?", (limit + 1, offset)
            )
            .fetchall()
        )
        return _page(
            [_load(row["payload"]) for row in rows],
            limit,
            self._make_cursor("profiles", "all", None, offset + limit),
        )

    def _delete_profile(self, profile_id: str, expected_revision: int) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT revision FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
            if row is None:
                raise SwarmStoreError("profile_not_found")
            if int(row["revision"]) != expected_revision:
                raise SwarmStoreError("revision_conflict")
            connection.execute("DELETE FROM profiles WHERE id=?", (profile_id,))

        self._write(operation)

    def _create_swarm(
        self,
        profile_id: str,
        prompt: str,
        effective: Json,
        request_id: str,
        expected_profile_revision: int,
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            payload_hash = _hash(
                {
                    "profile_id": profile_id,
                    "prompt": prompt,
                    "effective": effective,
                    "expected_profile_revision": expected_profile_revision,
                }
            )
            replay = connection.execute(
                "SELECT payload_hash, outcome FROM requests WHERE scope='start' AND request_id=?",
                (request_id,),
            ).fetchone()
            if replay is not None:
                if replay["payload_hash"] != payload_hash:
                    raise SwarmStoreError("request_conflict")
                result = _load(replay["outcome"])
                result["replayed"] = True
                return result
            profile = connection.execute(
                "SELECT payload,revision FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
            if profile is None:
                raise SwarmStoreError("profile_not_found")
            if int(profile["revision"]) != expected_profile_revision:
                raise SwarmStoreError("revision_conflict")
            profile_snapshot = _load(profile["payload"])
            swarm_id = new_id("swr")
            now = _now()
            connection.execute(
                "INSERT INTO swarms(id,prompt,profile_snapshot,effective_configuration,state,created_at) VALUES(?,?,?,?,?,?)",
                (swarm_id, prompt, _dump(profile_snapshot), _dump(effective), "preparing", now),
            )
            self._refresh_swarm_state(connection, swarm_id)
            connection.execute(
                "INSERT INTO swarm_settings(swarm_id,revision,delivery_json) VALUES(?,?,?)",
                (swarm_id, 1, _dump(profile_snapshot["delivery"])),
            )
            connection.execute(
                "INSERT INTO swarm_epochs(swarm_id,epoch,is_open) VALUES(?,?,?)",
                (swarm_id, 0, 1),
            )
            main_id = new_id("dsc")
            connection.execute(
                "INSERT INTO discussions(id,swarm_id,title,sequence,is_main,created_at) VALUES(?,?,?,?,1,?)",
                (main_id, swarm_id, "Main", 1, now),
            )
            ordinal = 0
            for formation in profile_snapshot["participants"]:
                for _ in range(formation["count"]):
                    ordinal += 1
                    participant_id = new_id("prt")
                    name = f"Participant {ordinal}"
                    connection.execute(
                        "INSERT INTO participants(id,swarm_id,model,display_name,ordinal,state) VALUES(?,?,?,?,?,?)",
                        (participant_id, swarm_id, formation["model"], name, ordinal, "idle"),
                    )
                    connection.execute(
                        "INSERT INTO memberships(discussion_id,participant_id) VALUES(?,?)",
                        (main_id, participant_id),
                    )
            result = {"swarm_id": swarm_id, "main_discussion_id": main_id, "state": "preparing"}
            connection.execute(
                "INSERT INTO requests(scope,request_id,payload_hash,outcome) VALUES('start',?,?,?)",
                (request_id, payload_hash, _dump(result)),
            )
            return result

        return self._write(operation)

    def _begin_delete(self, swarm_id: str) -> bool:
        def operation(connection: sqlite3.Connection) -> bool:
            row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
            if row is None:
                return False
            epoch = connection.execute(
                "SELECT is_open FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
            ).fetchone()
            if (
                row["state"] not in {"stopped", "cancelled", "interrupted", "deleting"}
                or epoch["is_open"]
            ):
                raise SwarmStoreError("swarm_not_stopped")
            if connection.execute(
                "SELECT 1 FROM participants WHERE swarm_id=? AND state='running'", (swarm_id,)
            ).fetchone():
                raise SwarmStoreError("swarm_not_stopped")
            connection.execute("UPDATE swarms SET state='deleting' WHERE id=?", (swarm_id,))
            return True

        return self._write(operation)

    def _delete_swarm(self, swarm_id: str) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
            if row is None:
                return
            if row["state"] != "deleting":
                raise SwarmStoreError("invalid_lifecycle_state")
            for table in (
                "delivery_batch_entries",
                "delivery_batches",
                "recipients",
                "memberships",
                "participant_sessions",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE participant_id IN (SELECT id FROM participants WHERE swarm_id=?)",
                    (swarm_id,),
                )
            for table in (
                "posts",
                "discussions",
                "participants",
                "swarm_settings",
                "swarm_execution_epochs",
                "swarm_epochs",
                "swarm_events",
            ):
                connection.execute(f"DELETE FROM {table} WHERE swarm_id=?", (swarm_id,))
            connection.execute(
                "DELETE FROM requests WHERE scope IN (?,?,?,?) "
                "OR substr(scope,1,?)=? OR substr(scope,1,?)=? "
                "OR (scope='start' AND json_extract(outcome,'$.swarm_id')=?)",
                (
                    f"settings:{swarm_id}",
                    f"stop:{swarm_id}",
                    f"stop-finish:{swarm_id}",
                    f"resume:{swarm_id}",
                    len(f"post:{swarm_id}:"),
                    f"post:{swarm_id}:",
                    len(f"create:{swarm_id}:"),
                    f"create:{swarm_id}:",
                    swarm_id,
                ),
            )
            connection.execute("DELETE FROM swarms WHERE id=?", (swarm_id,))

        self._write(operation)

    def _get_swarm(self, swarm_id: str) -> Json:
        connection = self._require_connection()
        row = connection.execute("SELECT * FROM swarms WHERE id=?", (swarm_id,)).fetchone()
        if row is None:
            raise SwarmStoreError("swarm_not_found")
        participants = connection.execute(
            "SELECT p.id,p.model,p.display_name,p.ordinal,p.state,p.lifecycle_run_id,"
            "(SELECT COUNT(*) FROM recipients r JOIN posts ps ON ps.id=r.post_id "
            "WHERE r.participant_id=p.id AND r.delivered_at IS NULL AND ps.swarm_id=p.swarm_id) "
            "AS pending_count FROM participants p WHERE p.swarm_id=? ORDER BY p.ordinal",
            (swarm_id,),
        ).fetchall()
        main = connection.execute(
            "SELECT id FROM discussions WHERE swarm_id=? AND is_main=1", (swarm_id,)
        ).fetchone()
        return {
            "id": swarm_id,
            "prompt": row["prompt"],
            "profile_snapshot": _load(row["profile_snapshot"]),
            "effective_configuration": _load(row["effective_configuration"]),
            "state": row["state"],
            "delivery": _load(
                connection.execute(
                    "SELECT delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
                ).fetchone()[0]
            ),
            "settings_revision": int(
                connection.execute(
                    "SELECT revision FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
                ).fetchone()[0]
            ),
            "main_discussion_id": main["id"],
            "epoch": int(
                connection.execute(
                    "SELECT epoch FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
                ).fetchone()[0]
            ),
            "execution_epoch": (lambda value: value[0] if value else None)(
                connection.execute(
                    "SELECT execution_epoch FROM swarm_execution_epochs WHERE swarm_id=? ORDER BY epoch DESC LIMIT 1",
                    (swarm_id,),
                ).fetchone()
            ),
            "participants": [dict(value) for value in participants],
        }

    def _list_swarms(self, cursor: str | None, limit: int) -> Page:
        connection = self._require_connection()
        high_water = int(
            connection.execute("SELECT COALESCE(MAX(rowid),0) FROM swarms").fetchone()[0]
        )
        if cursor:
            offset, frozen_high_water = self._cursor(cursor, "swarms", "all", high_water)
            if frozen_high_water is None:
                raise SwarmStoreError("invalid_cursor")
            high_water = frozen_high_water
        else:
            offset = 0
        rows = connection.execute(
            "SELECT s.id,s.state,s.created_at,substr(s.prompt,1,120) AS title,COUNT(p.id) AS participant_count,"
            "ss.revision AS settings_revision "
            "FROM swarms s JOIN swarm_settings ss ON ss.swarm_id=s.id "
            "LEFT JOIN participants p ON p.swarm_id=s.id WHERE s.rowid<=? "
            "GROUP BY s.id ORDER BY s.rowid DESC LIMIT ? OFFSET ?",
            (high_water, limit + 1, offset),
        ).fetchall()
        entries = [
            {
                "id": str(row["id"]),
                "title": str(row["title"]).split("\n", 1)[0],
                "state": str(row["state"]),
                "created_at": str(row["created_at"]),
                "participant_count": int(row["participant_count"]),
                "settings_revision": int(row["settings_revision"]),
            }
            for row in rows
        ]
        return _page(
            entries,
            limit,
            self._make_cursor("swarms", "all", high_water, offset + limit),
        )

    def _list_events(self, swarm_id: str, cursor: str | None, limit: int) -> Page:
        connection = self._require_connection()
        if connection.execute("SELECT 1 FROM swarms WHERE id=?", (swarm_id,)).fetchone() is None:
            raise SwarmStoreError("swarm_not_found")
        high_water = int(
            connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM swarm_events WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        )
        if cursor:
            offset, frozen_high_water = self._cursor(cursor, "events", swarm_id, high_water)
            if frozen_high_water is None:
                raise SwarmStoreError("invalid_cursor")
            high_water = frozen_high_water
        else:
            offset = 0
        rows = connection.execute(
            "SELECT id,kind,actor,old_json,new_json,settings_revision,created_at FROM swarm_events "
            "WHERE swarm_id=? AND id<=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (swarm_id, high_water, limit + 1, offset),
        ).fetchall()
        entries = [
            {
                "id": int(row["id"]),
                "kind": str(row["kind"]),
                "actor": str(row["actor"]),
                "old": _load(row["old_json"]) if row["old_json"] is not None else None,
                "new": _load(row["new_json"]) if row["new_json"] is not None else None,
                "settings_revision": row["settings_revision"],
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]
        return _page(
            entries,
            limit,
            self._make_cursor("events", swarm_id, high_water, offset + limit),
        )

    def _apply_delivery_settings(
        self,
        swarm_id: str,
        delivery: Json,
        expected_revision: int,
        request_id: str,
        actor: str,
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            scope = f"settings:{swarm_id}"
            payload_hash = _hash({"delivery": delivery, "expected_revision": expected_revision})
            replay = connection.execute(
                "SELECT payload_hash,outcome FROM requests WHERE scope=? AND request_id=?",
                (scope, request_id),
            ).fetchone()
            if replay is not None:
                if replay["payload_hash"] != payload_hash:
                    raise SwarmStoreError("request_conflict")
                result = _load(replay["outcome"])
                result["replayed"] = True
                return result
            self._assert_mutable(connection, swarm_id)
            row = connection.execute(
                "SELECT revision,delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
            ).fetchone()
            if row is None:
                raise SwarmStoreError("swarm_not_found")
            if int(row["revision"]) != expected_revision:
                raise SwarmStoreError("revision_conflict")
            old = _load(row["delivery_json"])
            revision = expected_revision + 1
            connection.execute(
                "UPDATE swarm_settings SET revision=?,delivery_json=? WHERE swarm_id=?",
                (revision, _dump(delivery), swarm_id),
            )
            for participant in connection.execute(
                "SELECT id FROM participants WHERE swarm_id=? AND wake_pending=1", (swarm_id,)
            ):
                still_eligible = connection.execute(
                    "SELECT 1 FROM recipients r JOIN posts p ON p.id=r.post_id "
                    "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL "
                    "AND r.route_class IN (?,?,?) LIMIT 1",
                    (swarm_id, participant["id"], *_DELIVERY_ROUTES),
                ).fetchone()
                if still_eligible is None or not any(
                    delivery[route]["wake_idle"]
                    for route in _DELIVERY_ROUTES
                    if connection.execute(
                        "SELECT 1 FROM recipients r JOIN posts p ON p.id=r.post_id "
                        "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL "
                        "AND r.route_class=? LIMIT 1",
                        (swarm_id, participant["id"], route),
                    ).fetchone()
                    is not None
                ):
                    connection.execute(
                        "UPDATE participants SET wake_pending=0 WHERE id=?", (participant["id"],)
                    )
            newly_enabled = {
                route
                for route in _DELIVERY_ROUTES
                if not old[route]["wake_idle"] and delivery[route]["wake_idle"]
            }
            wake_intents: list[Json] = []
            if newly_enabled:
                for participant in connection.execute(
                    "SELECT id,state,wake_pending,wake_announced_seq FROM participants WHERE swarm_id=?",
                    (swarm_id,),
                ):
                    if participant["state"] not in {"idle"} or participant["wake_pending"]:
                        continue
                    placeholders = ",".join("?" for _ in newly_enabled)
                    pending = connection.execute(
                        "SELECT COUNT(*) AS count,MAX(p.sequence) AS newest FROM recipients r "
                        "JOIN posts p ON p.id=r.post_id WHERE p.swarm_id=? AND r.participant_id=? "
                        "AND r.delivered_at IS NULL AND r.route_class IN (" + placeholders + ")",
                        (swarm_id, participant["id"], *sorted(newly_enabled)),
                    ).fetchone()
                    if pending["newest"] is None or int(pending["newest"]) <= int(
                        participant["wake_announced_seq"]
                    ):
                        continue
                    connection.execute(
                        "UPDATE participants SET wake_pending=1 WHERE id=?", (participant["id"],)
                    )
                    wake_intents.append(
                        {
                            "participant_id": str(participant["id"]),
                            "pending_count": int(pending["count"]),
                            "settings_revision": revision,
                        }
                    )
            connection.execute(
                "INSERT INTO swarm_events(swarm_id,kind,actor,old_json,new_json,settings_revision,created_at) VALUES(?,?,?,?,?,?,?)",
                (swarm_id, "settings", actor, _dump(old), _dump(delivery), revision, _now()),
            )
            result = {
                "revision": revision,
                "old": old,
                "new": delivery,
                "effective": delivery,
                "wake_intents": wake_intents,
            }
            connection.execute(
                "INSERT INTO requests(scope,request_id,payload_hash,outcome) VALUES(?,?,?,?)",
                (scope, request_id, payload_hash, _dump(result)),
            )
            return result

        return self._write(operation)

    def _set_swarm_state(self, swarm_id: str, state: str) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
            if row is None:
                raise SwarmStoreError("swarm_not_found")
            if row["state"] not in _MUTABLE_SWARM_STATES:
                raise SwarmStoreError("invalid_lifecycle_state")
            connection.execute("UPDATE swarms SET state=? WHERE id=?", (state, swarm_id))
            return {"state": state}

        return self._write(operation)

    def _fail_startup(self, swarm_id: str, expected_epoch: int) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            self._assert_epoch(connection, swarm_id, expected_epoch)
            row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
            if row is None:
                raise SwarmStoreError("swarm_not_found")
            connection.execute("UPDATE swarm_epochs SET is_open=0 WHERE swarm_id=?", (swarm_id,))
            connection.execute("DELETE FROM swarm_execution_epochs WHERE swarm_id=?", (swarm_id,))
            connection.execute("UPDATE swarms SET state='needs_attention' WHERE id=?", (swarm_id,))
            return {"swarm_id": swarm_id, "state": "needs_attention"}

        return self._write(operation)

    def _refresh_swarm_state(self, connection: sqlite3.Connection, swarm_id: str) -> None:
        row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
        if row is None or row["state"] not in {"running", "idle", "needs_attention"}:
            return
        states = {
            str(item["state"])
            for item in connection.execute(
                "SELECT state FROM participants WHERE swarm_id=?", (swarm_id,)
            )
        }
        if "running" in states:
            state = "running"
        elif states & {"failed", "cancelled", "interrupted"}:
            state = "needs_attention"
        elif states and states <= {"idle"}:
            state = "idle"
        else:
            state = "running"
        connection.execute("UPDATE swarms SET state=? WHERE id=?", (state, swarm_id))

    def _set_participant_state(
        self, swarm_id: str, participant_id: str, state: str, idle_boundary: int | None
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            self._assert_mutable(connection, swarm_id)
            participant = self._participant(connection, swarm_id, participant_id)
            connection.execute(
                "UPDATE participants SET state=?,idle_boundary=?,wake_pending=? WHERE id=?",
                (
                    state,
                    idle_boundary,
                    0 if state == "running" else participant["wake_pending"],
                    participant_id,
                ),
            )
            self._refresh_swarm_state(connection, swarm_id)
            return {
                "participant_id": participant_id,
                "state": state,
                "idle_boundary": idle_boundary,
            }

        return self._write(operation)

    def _participant_status(
        self,
        swarm_id: str,
        participant_id: str,
        cursor: str | None,
        limit: int,
    ) -> Json:
        connection = self._require_connection()
        self._participant(connection, swarm_id, participant_id)
        high = int(
            connection.execute(
                "SELECT COALESCE(MAX(ordinal),0) FROM participants WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        )
        scope = f"{swarm_id}:{participant_id}:{limit}"
        offset = self._cursor(cursor, "status", scope, high)[0] if cursor else 0
        rows = connection.execute(
            "SELECT id,display_name,state FROM participants WHERE swarm_id=? ORDER BY ordinal LIMIT ? OFFSET ?",
            (swarm_id, limit + 1, offset),
        ).fetchall()

        self_row = connection.execute(
            "SELECT id,state FROM participants WHERE id=?",
            (participant_id,),
        ).fetchone()
        totals = {
            str(row["state"]): int(row["count"])
            for row in connection.execute(
                "SELECT state,COUNT(*) AS count FROM participants WHERE swarm_id=? GROUP BY state",
                (swarm_id,),
            )
        }
        settings = connection.execute(
            "SELECT delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
        ).fetchone()
        delivery = _load(settings["delivery_json"])
        receive = {
            label: [
                route for route in ("main", "discussion", "ping") if delivery[route]["mode"] == mode
            ]
            for label, mode in (("automatic", "all"), ("when_idle", "idle"), ("on_request", "pull"))
        }
        roster = [
            {"id": row["id"], "name": row["display_name"], "state": row["state"]}
            for row in rows[:limit]
        ]
        consumed = len(roster)
        has_more = consumed < len(rows)
        main = self._main(connection, swarm_id)
        return {
            "self": {"id": self_row["id"], "state": self_row["state"]},
            "main_discussion_id": main,
            "delivery": {label: routes for label, routes in receive.items() if routes},
            "wake_on_messages": [
                route for route in ("main", "discussion", "ping") if delivery[route]["wake_idle"]
            ],
            "pending_count": self._pending_count(connection, swarm_id, participant_id),
            "state_totals": totals,
            "roster": roster,
            "has_more": has_more,
            "cursor": self._make_cursor("status", scope, high, offset + consumed)
            if has_more
            else None,
        }

    def _record_run_started(
        self, swarm_id: str, participant_id: str, run_id: str, expected_epoch: int
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            self._assert_epoch(connection, swarm_id, expected_epoch)
            participant = self._participant(connection, swarm_id, participant_id)
            if participant["lifecycle_run_id"] == run_id:
                return {
                    "participant_id": participant_id,
                    "run_id": run_id,
                    "state": participant["state"],
                }
            connection.execute(
                "UPDATE participants SET state='running',lifecycle_run_id=? WHERE id=?",
                (run_id, participant_id),
            )
            self._refresh_swarm_state(connection, swarm_id)
            return {"participant_id": participant_id, "run_id": run_id, "state": "running"}

        return self._write(operation)

    def _reconcile_run_finished(
        self, swarm_id: str, participant_id: str, run_id: str, expected_epoch: int, outcome: str
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            self._assert_epoch(connection, swarm_id, expected_epoch)
            participant = self._participant(connection, swarm_id, participant_id)
            if participant["lifecycle_run_id"] != run_id:
                raise SwarmStoreError("stale_run")
            state = "idle" if outcome == "completed" else outcome
            connection.execute(
                "UPDATE participants SET state=? WHERE id=?", (state, participant_id)
            )
            self._refresh_swarm_state(connection, swarm_id)
            return {"participant_id": participant_id, "run_id": run_id, "state": state}

        return self._write(operation)

    def _recover_interrupted(self) -> list[Json]:
        def operation(connection: sqlite3.Connection) -> list[Json]:
            rows = connection.execute(
                "SELECT id,state FROM swarms WHERE state IN (?,?,?,?,?)",
                ("preparing", "running", "idle", "needs_attention", "stopping"),
            ).fetchall()
            result: list[Json] = []
            for row in rows:
                swarm_id = str(row["id"])
                connection.execute(
                    "UPDATE swarm_epochs SET is_open=0 WHERE swarm_id=?", (swarm_id,)
                )
                connection.execute("UPDATE swarms SET state='interrupted' WHERE id=?", (swarm_id,))
                connection.execute(
                    "UPDATE participants SET state=CASE WHEN state='running' THEN 'interrupted' ELSE state END,wake_pending=0 WHERE swarm_id=?",
                    (swarm_id,),
                )
                connection.execute(
                    "INSERT INTO swarm_events(swarm_id,kind,actor,old_json,new_json,settings_revision,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        swarm_id,
                        "recovered",
                        "system",
                        _dump({"state": row["state"]}),
                        _dump({"state": "interrupted"}),
                        None,
                        _now(),
                    ),
                )
                result.append({"swarm_id": swarm_id, "state": "interrupted"})
            return result

        return self._write(operation)

    def _begin_stop(self, swarm_id: str, request_id: str, actor: str) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            scope = f"stop:{swarm_id}"
            payload_hash = _hash({"action": "begin"})
            replay = self._request_replay(connection, scope, request_id, payload_hash)
            row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
            if row is None:
                raise SwarmStoreError("swarm_not_found")
            if row["state"] == "deleting":
                raise SwarmStoreError("swarm_closed")
            if replay is not None:
                return replay
            epoch = connection.execute(
                "SELECT epoch FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
            ).fetchone()
            if row["state"] in {"stopped", "cancelled"}:
                result = {
                    "swarm_id": swarm_id,
                    "state": str(row["state"]),
                    "epoch": int(epoch["epoch"]),
                }
                self._record_lifecycle_request(
                    connection,
                    scope,
                    request_id,
                    payload_hash,
                    swarm_id,
                    "stop_requested",
                    actor,
                    row["state"],
                    result,
                )
                return result
            connection.execute("UPDATE swarm_epochs SET is_open=0 WHERE swarm_id=?", (swarm_id,))
            connection.execute("UPDATE swarms SET state='stopping' WHERE id=?", (swarm_id,))
            result = {"swarm_id": swarm_id, "state": "stopping", "epoch": int(epoch["epoch"])}
            self._record_lifecycle_request(
                connection,
                scope,
                request_id,
                payload_hash,
                swarm_id,
                "stop_requested",
                actor,
                row["state"],
                result,
            )
            return result

        return self._write(operation)

    def _finish_stop(self, swarm_id: str, request_id: str, actor: str, drain_report: Json) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            scope = f"stop-finish:{swarm_id}"
            payload_hash = _hash({"drain_report": drain_report})
            replay = self._request_replay(connection, scope, request_id, payload_hash)
            if replay is not None:
                return replay
            row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
            if row is None:
                raise SwarmStoreError("swarm_not_found")
            if row["state"] != "stopping":
                raise SwarmStoreError("invalid_lifecycle_state")
            connection.execute(
                "UPDATE participants SET state=CASE WHEN state='running' THEN 'cancelled' ELSE state END,wake_pending=0 WHERE swarm_id=?",
                (swarm_id,),
            )
            connection.execute("UPDATE swarms SET state='cancelled' WHERE id=?", (swarm_id,))
            result = {"swarm_id": swarm_id, "state": "cancelled", "drain_report": drain_report}
            self._record_lifecycle_request(
                connection,
                scope,
                request_id,
                payload_hash,
                swarm_id,
                "stopped",
                actor,
                row["state"],
                result,
            )
            return result

        return self._write(operation)

    def _begin_resume(
        self, swarm_id: str, request_id: str, actor: str, participant_id: str | None
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            scope = f"resume:{swarm_id}"
            payload_hash = _hash({"action": "begin", "participant_id": participant_id})
            replay = self._request_replay(connection, scope, request_id, payload_hash)
            row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
            if row is None:
                raise SwarmStoreError("swarm_not_found")
            if row["state"] == "deleting":
                raise SwarmStoreError("swarm_closed")
            if replay is not None:
                return replay
            if row["state"] in {"stopping", "preparing"}:
                raise SwarmStoreError("swarm_closed")
            epoch = connection.execute(
                "SELECT epoch,is_open FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
            ).fetchone()
            if participant_id is not None:
                target = connection.execute(
                    "SELECT state FROM participants WHERE swarm_id=? AND id=?",
                    (swarm_id, participant_id),
                ).fetchone()
                if target is None:
                    raise SwarmStoreError("participant_not_found")
                eligible = {"idle", "failed", "cancelled", "interrupted"}
                if target["state"] not in eligible:
                    raise SwarmStoreError("participant_not_resumable")
            if epoch["is_open"]:
                participants = [
                    str(item["id"])
                    for item in connection.execute(
                        "SELECT id FROM participants WHERE swarm_id=? "
                        "AND state IN ('idle','failed','cancelled','interrupted') "
                        "ORDER BY ordinal",
                        (swarm_id,),
                    )
                ]
                if participant_id is not None:
                    participants = [participant_id]
                result = {
                    "swarm_id": swarm_id,
                    "state": str(row["state"]),
                    "epoch": int(epoch["epoch"]),
                    "participant_ids": participants,
                    "reused_epoch": True,
                }
                self._record_lifecycle_request(
                    connection,
                    scope,
                    request_id,
                    payload_hash,
                    swarm_id,
                    "resume_requested",
                    actor,
                    row["state"],
                    result,
                )
                return result
            next_epoch = int(epoch["epoch"]) + 1
            participants = [
                str(item["id"])
                for item in connection.execute(
                    "SELECT id FROM participants WHERE swarm_id=? ORDER BY ordinal",
                    (swarm_id,),
                )
            ]
            if participant_id is not None:
                participants = [participant_id]
            connection.executemany(
                "UPDATE participants SET state='idle',wake_pending=0 WHERE swarm_id=? AND id=?",
                [(swarm_id, peer_id) for peer_id in participants],
            )
            connection.execute(
                "UPDATE swarm_epochs SET epoch=?,is_open=1 WHERE swarm_id=?", (next_epoch, swarm_id)
            )
            connection.execute("DELETE FROM swarm_execution_epochs WHERE swarm_id=?", (swarm_id,))
            connection.execute("UPDATE swarms SET state='preparing' WHERE id=?", (swarm_id,))
            result = {
                "swarm_id": swarm_id,
                "state": "preparing",
                "epoch": next_epoch,
                "participant_ids": participants,
            }
            self._record_lifecycle_request(
                connection,
                scope,
                request_id,
                payload_hash,
                swarm_id,
                "resume_requested",
                actor,
                row["state"],
                result,
            )
            return result

        return self._write(operation)

    @staticmethod
    def _request_replay(
        connection: sqlite3.Connection, scope: str, request_id: str, payload_hash: str
    ) -> Json | None:
        row = connection.execute(
            "SELECT payload_hash,outcome FROM requests WHERE scope=? AND request_id=?",
            (scope, request_id),
        ).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != payload_hash:
            raise SwarmStoreError("request_conflict")
        value = _load(row["outcome"])
        value["replayed"] = True
        return value

    @staticmethod
    def _record_lifecycle_request(
        connection: sqlite3.Connection,
        scope: str,
        request_id: str,
        payload_hash: str,
        swarm_id: str,
        kind: str,
        actor: str,
        old_state: str,
        result: Json,
    ) -> None:
        connection.execute(
            "INSERT INTO swarm_events(swarm_id,kind,actor,old_json,new_json,settings_revision,created_at) VALUES(?,?,?,?,?,?,?)",
            (swarm_id, kind, actor, _dump({"state": old_state}), _dump(result), None, _now()),
        )
        connection.execute(
            "INSERT INTO requests(scope,request_id,payload_hash,outcome) VALUES(?,?,?,?)",
            (scope, request_id, payload_hash, _dump(result)),
        )

    def _bind_execution_epoch(
        self, swarm_id: str, expected_epoch: int, execution_epoch: str
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            self._assert_epoch(connection, swarm_id, expected_epoch)
            row = connection.execute(
                "SELECT execution_epoch FROM swarm_execution_epochs WHERE swarm_id=? AND epoch=?",
                (swarm_id, expected_epoch),
            ).fetchone()
            if row is not None:
                if row["execution_epoch"] != execution_epoch:
                    raise SwarmStoreError("execution_epoch_conflict")
                return {
                    "swarm_id": swarm_id,
                    "epoch": expected_epoch,
                    "execution_epoch": execution_epoch,
                    "replayed": True,
                }
            connection.execute(
                "INSERT INTO swarm_execution_epochs(swarm_id,epoch,execution_epoch) VALUES(?,?,?)",
                (swarm_id, expected_epoch, execution_epoch),
            )
            return {
                "swarm_id": swarm_id,
                "epoch": expected_epoch,
                "execution_epoch": execution_epoch,
            }

        return self._write(operation)

    def _bind_participant_session(self, binding: TemporarySessionBinding) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            participant = connection.execute(
                "SELECT swarm_id FROM participants WHERE id=?", (binding.participant_id,)
            ).fetchone()
            if participant is None or str(participant["swarm_id"]) != binding.group_id:
                raise SwarmStoreError("participant_not_found")
            connection.execute(
                "INSERT INTO participant_sessions(participant_id,project_id,agent_id,session_id,generation_id,owner_name) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(participant_id) DO NOTHING",
                (
                    binding.participant_id,
                    binding.address.project_id,
                    binding.address.agent_id,
                    binding.address.session_id,
                    binding.generation_id,
                    binding.owner_name,
                ),
            )
            existing = connection.execute(
                "SELECT project_id,agent_id,session_id,generation_id,owner_name FROM participant_sessions WHERE participant_id=?",
                (binding.participant_id,),
            ).fetchone()
            expected = (
                binding.address.project_id,
                binding.address.agent_id,
                binding.address.session_id,
                binding.generation_id,
                binding.owner_name,
            )
            if existing is None or tuple(existing) != expected:
                raise SwarmStoreError("participant_binding_conflict")

        self._write(operation)

    def _prepare_delivery(
        self,
        swarm_id: str,
        participant_id: str,
        post_ids: tuple[str, ...] | None,
        limit: int,
        effect_kind: str,
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            self._participant(connection, swarm_id, participant_id)
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
                rows = self._pending_rows(
                    connection, swarm_id, participant_id, limit, settings["batch_chars"]
                )
                receipt_id = new_id("rcp")
            else:
                if not post_ids:
                    return {"entries": [], "receipt_id": None, "pending_remaining": 0}
                placeholders = ",".join("?" for _ in post_ids)
                rows = connection.execute(
                    "SELECT p.*,r.route_class,d.title AS discussion_title "
                    "FROM recipients r JOIN posts p ON p.id=r.post_id "
                    "JOIN discussions d ON d.id=p.discussion_id "
                    f"WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL AND p.id IN ({placeholders}) ORDER BY p.sequence",
                    (swarm_id, participant_id, *post_ids),
                ).fetchall()
                receipt_id = new_id("rcp")
            entries = [_post(row) for row in rows]
            if not entries:
                return {"entries": [], "receipt_id": None, "pending_remaining": 0}
            content_hash = _hash({"effect_kind": effect_kind, "posts": entries})
            connection.execute(
                "INSERT INTO delivery_batches(receipt_id,participant_id,content_hash,effect_kind,created_at) VALUES(?,?,?,?,?)",
                (receipt_id, participant_id, content_hash, effect_kind, _now()),
            )
            for entry in entries:
                connection.execute(
                    "INSERT INTO delivery_batch_entries(receipt_id,post_id,participant_id) VALUES(?,?,?)",
                    (receipt_id, entry["id"], participant_id),
                )
            pending_remaining = int(
                connection.execute(
                    "SELECT COUNT(*) FROM recipients r JOIN posts p ON p.id=r.post_id WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL",
                    (swarm_id, participant_id),
                ).fetchone()[0]
            ) - len(entries)
            return {
                "entries": entries,
                "receipt_id": receipt_id,
                "content_hash": content_hash,
                "effect_kind": effect_kind,
                "pending_remaining": max(0, pending_remaining),
            }

        return self._write(operation)

    def _prepare_automatic_delivery(
        self, swarm_id: str, participant_id: str, expected_epoch: int, boundary: int | None
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            self._assert_epoch(connection, swarm_id, expected_epoch)
            participant = self._participant(connection, swarm_id, participant_id)
            resolved_boundary = (
                int(participant["idle_boundary"] or 0) if boundary is None else boundary
            )
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
            if prepared is not None:
                newest = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(p.sequence),0) FROM recipients r JOIN posts p ON p.id=r.post_id "
                        "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL",
                        (swarm_id, participant_id),
                    ).fetchone()[0]
                )
                wake = (
                    newest > int(participant["wake_announced_seq"] or 0)
                    and not participant["wake_pending"]
                    and participant["state"] in {"idle"}
                    and any(
                        settings[row["route_class"]]["wake_idle"]
                        for row in connection.execute(
                            "SELECT r.route_class FROM recipients r JOIN posts p ON p.id=r.post_id "
                            "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL",
                            (swarm_id, participant_id),
                        )
                    )
                )
                if wake:
                    connection.execute(
                        "UPDATE participants SET wake_epoch=?,wake_pending=1,wake_pending_seq=? WHERE id=?",
                        (expected_epoch, newest, participant_id),
                    )
                rows = connection.execute(
                    "SELECT p.*,r.route_class,d.title AS discussion_title FROM delivery_batch_entries e JOIN posts p ON p.id=e.post_id "
                    "JOIN recipients r ON r.post_id=p.id AND r.participant_id=e.participant_id "
                    "JOIN discussions d ON d.id=p.discussion_id "
                    "WHERE e.receipt_id=? ORDER BY p.sequence",
                    (prepared["receipt_id"],),
                ).fetchall()
                return {
                    "entries": [_post(row) for row in rows],
                    "receipt_id": prepared["receipt_id"],
                    "content_hash": prepared["content_hash"],
                    "effect_kind": prepared["effect_kind"],
                    "wake": wake,
                    "pending_remaining": self._pending_count(connection, swarm_id, participant_id)
                    - len(rows),
                    "settings_revision": int(prepared["settings_revision"]),
                    "replayed": True,
                }
            pending = connection.execute(
                "SELECT p.*,r.route_class,d.title AS discussion_title FROM recipients r JOIN posts p ON p.id=r.post_id JOIN discussions d ON d.id=p.discussion_id "
                "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL ORDER BY p.sequence",
                (swarm_id, participant_id),
            ).fetchall()
            eligible = []
            for row in pending:
                policy = settings[row["route_class"]]
                if policy["mode"] == "pull" and not (
                    participant["state"] == "idle" and policy["wake_idle"]
                ):
                    continue
                if policy["mode"] == "idle" and (
                    participant["state"] not in {"idle"}
                    and resolved_boundary != participant["idle_boundary"]
                ):
                    continue
                eligible.append(row)
            newest = max((int(row["sequence"]) for row in pending), default=0)
            epoch = connection.execute(
                "SELECT epoch,is_open FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
            ).fetchone()
            if not epoch["is_open"] or int(epoch["epoch"]) != expected_epoch:
                raise SwarmStoreError("stale_epoch")
            epoch_value = int(epoch["epoch"])
            announced = int(participant["wake_announced_seq"] or 0)
            wake = bool(
                newest > announced
                and not participant["wake_pending"]
                and any(settings[row["route_class"]]["wake_idle"] for row in pending)
                and participant["state"] in {"idle"}
            )
            if wake:
                connection.execute(
                    "UPDATE participants SET wake_epoch=?,wake_pending=1,wake_pending_seq=?,idle_boundary=? WHERE id=?",
                    (epoch_value, newest, resolved_boundary, participant_id),
                )
            if not eligible:
                return {
                    "entries": [],
                    "wake": wake,
                    "pending_remaining": len(pending),
                    "settings_revision": int(settings_row["revision"]),
                    "admission_boundary": resolved_boundary,
                }
            rows = self._pending_rows_from_rows(
                eligible, settings["batch_messages"], settings["batch_chars"]
            )
            receipt_id = new_id("rcp")
            entries = [_post(row) for row in rows]
            content_hash = _hash(
                {
                    "effect_kind": "swarm_automatic",
                    "posts": entries,
                    "revision": int(settings_row["revision"]),
                }
            )
            connection.execute(
                "INSERT INTO delivery_batches(receipt_id,participant_id,content_hash,effect_kind,created_at,settings_revision) VALUES(?,?,?,?,?,?)",
                (
                    receipt_id,
                    participant_id,
                    content_hash,
                    "swarm_automatic",
                    _now(),
                    int(settings_row["revision"]),
                ),
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
                "effect_kind": "swarm_automatic",
                "wake": wake,
                "pending_remaining": len(pending) - len(entries),
                "settings_revision": int(settings_row["revision"]),
                "admission_boundary": resolved_boundary,
            }

        return self._write(operation)

    def _list_prepared_deliveries(self, cursor: str | None, limit: int) -> Page:
        connection = self._require_connection()
        high_water = int(
            connection.execute("SELECT COALESCE(MAX(rowid),0) FROM delivery_batches").fetchone()[0]
        )
        offset = 0
        if cursor:
            offset, frozen = self._cursor(cursor, "prepared", "all", high_water)
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
            self._make_cursor("prepared", "all", high_water, offset + limit),
        )

    def _claim_wake(self, swarm_id: str, participant_id: str, expected_epoch: int) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            self._assert_epoch(connection, swarm_id, expected_epoch)
            participant = self._participant(connection, swarm_id, participant_id)
            if not participant["wake_pending"] or int(participant["wake_epoch"]) != expected_epoch:
                return {"participant_id": participant_id, "pending": False}
            return {
                "participant_id": participant_id,
                "pending": True,
                "boundary": participant["idle_boundary"],
            }

        return self._write(operation)

    def _list_wake_intents(self, swarm_id: str, cursor: str | None, limit: int) -> Page:
        connection = self._require_connection()
        self._assert_mutable(connection, swarm_id)
        high_water = int(
            connection.execute(
                "SELECT COALESCE(MAX(ordinal),0) FROM participants WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        )
        offset = 0
        if cursor:
            offset, frozen = self._cursor(cursor, "wakes", swarm_id, high_water)
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
        return _page(
            entries, limit, self._make_cursor("wakes", swarm_id, high_water, offset + limit)
        )

    def _mark_wake_admitted(
        self,
        swarm_id: str,
        participant_id: str,
        expected_epoch: int,
        run_id: str,
        boundary: int | None,
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            self._assert_epoch(connection, swarm_id, expected_epoch)
            participant = self._participant(connection, swarm_id, participant_id)
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
                }
            if not participant["wake_pending"] or participant["idle_boundary"] != boundary:
                raise SwarmStoreError("wake_unavailable")
            state = participant["state"] if participant["lifecycle_run_id"] == run_id else "running"
            connection.execute(
                "UPDATE participants SET wake_pending=0,wake_announced_seq=wake_pending_seq,state=?,lifecycle_run_id=?,idle_boundary=? WHERE id=?",
                (state, run_id, boundary, participant_id),
            )
            self._refresh_swarm_state(connection, swarm_id)
            return {
                "participant_id": participant_id,
                "run_id": run_id,
                "boundary": boundary,
                "admitted": True,
            }

        return self._write(operation)

    @staticmethod
    def _pending_rows_from_rows(
        rows: list[sqlite3.Row], limit: int, batch_chars: int
    ) -> list[sqlite3.Row]:
        selected: list[sqlite3.Row] = []
        chars = 0
        for row in rows[:limit]:
            size = len(str(row["text"]))
            if selected and chars + size > batch_chars:
                break
            selected.append(row)
            chars += size
        return selected

    @staticmethod
    def _pending_rows(
        connection: sqlite3.Connection,
        swarm_id: str,
        participant_id: str,
        limit: int,
        batch_chars: int,
    ) -> list[sqlite3.Row]:
        candidates = connection.execute(
            "SELECT p.*,r.route_class,d.title AS discussion_title "
            "FROM recipients r JOIN posts p ON p.id=r.post_id "
            "JOIN discussions d ON d.id=p.discussion_id "
            "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL ORDER BY p.sequence LIMIT ?",
            (swarm_id, participant_id, limit),
        ).fetchall()
        selected: list[sqlite3.Row] = []
        char_count = 0
        for row in candidates:
            text_size = len(str(row["text"]))
            if selected and char_count + text_size > batch_chars:
                break
            selected.append(row)
            char_count += text_size
        return selected

    def _prepared_delivery(self, receipt_id: str) -> Json | None:
        row = (
            self._require_connection()
            .execute(
                "SELECT b.participant_id,b.receipt_id,b.content_hash,b.effect_kind,b.acknowledged_at,s.project_id,s.agent_id,s.session_id,s.generation_id,s.owner_name "
                "FROM delivery_batches b JOIN participant_sessions s ON s.participant_id=b.participant_id "
                "WHERE b.receipt_id=?",
                (receipt_id,),
            )
            .fetchone()
        )
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
            "address": SessionAddress(
                row["project_id"], str(row["agent_id"]), str(row["session_id"])
            ),
        }

    def _acknowledge_delivery(self, prepared: Json, receipt: DeliveryReceipt | None) -> bool:
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
                "UPDATE recipients SET delivered_at=?,carrier_kind=?,carrier_sequence=? "
                "WHERE participant_id=? AND delivered_at IS NULL AND post_id IN "
                "(SELECT post_id FROM delivery_batch_entries WHERE receipt_id=? AND participant_id=?)",
                (
                    _now(),
                    location["kind"],
                    location["sequence"],
                    prepared["participant_id"],
                    receipt.receipt_id,
                    prepared["participant_id"],
                ),
            )
            connection.execute(
                "UPDATE delivery_batches SET acknowledged_at=?,carrier_kind=?,carrier_sequence=? WHERE receipt_id=?",
                (_now(), location["kind"], location["sequence"], receipt.receipt_id),
            )
            return True

        return self._write(operation)

    def _list_discussions(
        self, swarm_id: str, participant_id: str, cursor: str | None, limit: int
    ) -> Page:
        connection = self._require_connection()
        self._participant(connection, swarm_id, participant_id)
        high_water = self._discussion_high_water(connection, swarm_id)
        if cursor:
            offset, frozen_high_water = self._cursor(
                cursor, "discussions", f"{swarm_id}:{participant_id}", high_water
            )
            if frozen_high_water is None:
                raise SwarmStoreError("invalid_cursor")
            high_water = frozen_high_water
        else:
            offset = 0
        rows = connection.execute(
            "SELECT d.*, EXISTS(SELECT 1 FROM memberships m WHERE m.discussion_id=d.id AND m.participant_id=?) AS joined, (SELECT COUNT(*) FROM memberships m WHERE m.discussion_id=d.id) AS member_count, "
            "(SELECT COUNT(*) FROM recipients r JOIN posts p ON p.id=r.post_id WHERE p.discussion_id=d.id AND r.participant_id=? AND r.delivered_at IS NULL) AS pending_count "
            "FROM discussions d WHERE d.swarm_id=? AND d.sequence<=? ORDER BY d.is_main DESC,d.sequence LIMIT ? OFFSET ?",
            (participant_id, participant_id, swarm_id, high_water, limit + 1, offset),
        ).fetchall()
        entries = [
            {
                "id": r["id"],
                "title": r["title"],
                "joined": bool(r["joined"]),
                "member_count": r["member_count"],
                "pending_count": r["pending_count"],
            }
            for r in rows
        ]
        return _page(
            entries,
            limit,
            self._make_cursor(
                "discussions", f"{swarm_id}:{participant_id}", high_water, offset + limit
            ),
        )

    def _list_human_discussions(self, swarm_id: str, cursor: str | None, limit: int) -> Page:
        connection = self._require_connection()
        if connection.execute("SELECT 1 FROM swarms WHERE id=?", (swarm_id,)).fetchone() is None:
            raise SwarmStoreError("swarm_not_found")
        high_water = self._discussion_high_water(connection, swarm_id)
        scope = f"human:{swarm_id}"
        if cursor:
            offset, frozen_high_water = self._cursor(cursor, "human_discussions", scope, high_water)
            if frozen_high_water is None:
                raise SwarmStoreError("invalid_cursor")
            high_water = frozen_high_water
        else:
            offset = 0
        rows = connection.execute(
            "SELECT d.*, (SELECT COUNT(*) FROM memberships m WHERE m.discussion_id=d.id) AS member_count "
            "FROM discussions d WHERE d.swarm_id=? AND d.sequence<=? "
            "ORDER BY d.is_main DESC,d.sequence LIMIT ? OFFSET ?",
            (swarm_id, high_water, limit + 1, offset),
        ).fetchall()
        return _page(
            [
                {
                    "id": row["id"],
                    "title": row["title"],
                    "member_count": row["member_count"],
                }
                for row in rows
            ],
            limit,
            self._make_cursor("human_discussions", scope, high_water, offset + limit),
        )

    def _read_posts(
        self,
        swarm_id: str,
        participant_id: str,
        discussion_id: str | None,
        message_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> Page:
        connection = self._require_connection()
        self._participant(connection, swarm_id, participant_id)
        if message_id is not None:
            row = connection.execute(
                "SELECT p.*,d.title AS discussion_title,r.route_class FROM posts p "
                "JOIN discussions d ON d.id=p.discussion_id "
                "LEFT JOIN recipients r ON r.post_id=p.id AND r.participant_id=? "
                "WHERE p.id=? AND p.swarm_id=?",
                (participant_id, message_id, swarm_id),
            ).fetchone()
            if row is None:
                raise SwarmStoreError("message_not_found")
            return Page((_post(row),), False, None)
        discussion_id = discussion_id or self._main(connection, swarm_id)
        self._discussion(connection, swarm_id, discussion_id)
        return self._post_page(connection, swarm_id, participant_id, discussion_id, cursor, limit)

    def _read_human_posts(
        self,
        swarm_id: str,
        discussion_id: str | None,
        message_id: str | None,
        cursor: str | None,
        limit: int,
    ) -> Page:
        connection = self._require_connection()
        if connection.execute("SELECT 1 FROM swarms WHERE id=?", (swarm_id,)).fetchone() is None:
            raise SwarmStoreError("swarm_not_found")
        if message_id is not None:
            row = connection.execute(
                "SELECT * FROM posts WHERE id=? AND swarm_id=?", (message_id, swarm_id)
            ).fetchone()
            if row is None:
                raise SwarmStoreError("message_not_found")
            return Page((_post(row),), False, None)
        discussion_id = discussion_id or self._main(connection, swarm_id)
        self._discussion(connection, swarm_id, discussion_id)
        return self._human_post_page(connection, swarm_id, discussion_id, cursor, limit)

    def _post_page(
        self,
        connection: sqlite3.Connection,
        swarm_id: str,
        participant_id: str,
        discussion_id: str,
        cursor: str | None,
        limit: int,
    ) -> Page:
        high_water = self._post_high_water(connection, swarm_id, discussion_id)
        scope = f"{swarm_id}:{participant_id}:{discussion_id}"
        if cursor:
            offset, frozen_high_water = self._cursor(cursor, "posts", scope, high_water)
            if frozen_high_water is None:
                raise SwarmStoreError("invalid_cursor")
            high_water = frozen_high_water
        else:
            offset = 0
        batch_chars = _load(
            connection.execute(
                "SELECT delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        )["batch_chars"]
        rows = connection.execute(
            "SELECT p.*,d.title AS discussion_title,r.route_class FROM posts p "
            "JOIN discussions d ON d.id=p.discussion_id "
            "LEFT JOIN recipients r ON r.post_id=p.id AND r.participant_id=? "
            "WHERE p.swarm_id=? AND p.discussion_id=? AND p.sequence<=? "
            "ORDER BY p.sequence DESC LIMIT ? OFFSET ?",
            (participant_id, swarm_id, discussion_id, high_water, limit + 1, offset),
        ).fetchall()
        selected = self._pending_rows_from_rows(rows, limit, batch_chars)
        values = [_post(row) for row in reversed(selected)]
        more = len(rows) > len(selected)
        return Page(
            tuple(values),
            more,
            self._make_cursor("posts", scope, high_water, offset + len(selected)) if more else None,
        )

    def _human_post_page(
        self,
        connection: sqlite3.Connection,
        swarm_id: str,
        discussion_id: str,
        cursor: str | None,
        limit: int,
    ) -> Page:
        high_water = self._post_high_water(connection, swarm_id, discussion_id)
        scope = f"human:{swarm_id}:{discussion_id}"
        if cursor:
            offset, frozen_high_water = self._cursor(cursor, "human_posts", scope, high_water)
            if frozen_high_water is None:
                raise SwarmStoreError("invalid_cursor")
            high_water = frozen_high_water
        else:
            offset = 0
        batch_chars = _load(
            connection.execute(
                "SELECT delivery_json FROM swarm_settings WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        )["batch_chars"]
        rows = connection.execute(
            "SELECT * FROM posts WHERE swarm_id=? AND discussion_id=? AND sequence<=? "
            "ORDER BY sequence DESC LIMIT ? OFFSET ?",
            (swarm_id, discussion_id, high_water, limit + 1, offset),
        ).fetchall()
        selected = self._pending_rows_from_rows(rows, limit, batch_chars)
        values = [_post(row) for row in reversed(selected)]
        more = len(rows) > len(selected)
        return Page(
            tuple(values),
            more,
            self._make_cursor("human_posts", scope, high_water, offset + len(selected))
            if more
            else None,
        )

    def _post(
        self,
        swarm_id: str,
        sender_id: str,
        text: str,
        request_id: str,
        discussion_id: str | None,
        reply_to: str | None,
        recipients: tuple[str, ...],
        author_kind: str,
        author_name: str | None,
        expected_epoch: int | None,
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            self._assert_mutable(connection, swarm_id)
            if expected_epoch is not None:
                self._assert_epoch(connection, swarm_id, expected_epoch)
            sender = (
                self._participant(connection, swarm_id, sender_id)
                if author_kind == "participant"
                else None
            )
            target = None
            if reply_to is not None:
                target = connection.execute(
                    "SELECT discussion_id FROM posts WHERE id=? AND swarm_id=?",
                    (reply_to, swarm_id),
                ).fetchone()
                if target is None:
                    raise SwarmStoreError("message_not_found")
            discussion_id_value = discussion_id or (
                target["discussion_id"] if target is not None else self._main(connection, swarm_id)
            )
            self._discussion(connection, swarm_id, discussion_id_value)
            payload = {
                "discussion_id": discussion_id_value,
                "text": text,
                "reply_to": reply_to,
                "recipients": recipients,
                "author_kind": author_kind,
            }
            scope = f"post:{swarm_id}:{sender_id}"
            replay = connection.execute(
                "SELECT payload_hash,outcome FROM requests WHERE scope=? AND request_id=?",
                (scope, request_id),
            ).fetchone()
            if replay is not None:
                if replay["payload_hash"] != _hash(payload):
                    raise SwarmStoreError("request_conflict")
                outcome = _load(replay["outcome"])
                outcome["replayed"] = True
                return outcome
            if target is not None and target["discussion_id"] != discussion_id_value:
                raise SwarmStoreError("reply_discussion_mismatch")
            members = connection.execute(
                "SELECT participant_id FROM memberships WHERE discussion_id=?",
                (discussion_id_value,),
            ).fetchall()
            audience = {
                str(row["participant_id"]): "main"
                if discussion_id_value == self._main(connection, swarm_id)
                else "discussion"
                for row in members
            }
            for recipient in recipients:
                if (
                    connection.execute(
                        "SELECT 1 FROM participants WHERE swarm_id=? AND id=?",
                        (swarm_id, recipient),
                    ).fetchone()
                    is None
                ):
                    raise SwarmStoreError("invalid_recipient")
                audience[recipient] = "ping"
            audience.pop(sender_id, None)
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM posts WHERE swarm_id=?", (swarm_id,)
                ).fetchone()[0]
            )
            post_id = new_id("pst")
            name = author_name or (sender["display_name"] if sender is not None else "User")
            connection.execute(
                "INSERT INTO posts(id,swarm_id,discussion_id,sequence,author_kind,author_id,author_name,text,reply_to,recipients_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    post_id,
                    swarm_id,
                    discussion_id_value,
                    sequence,
                    author_kind,
                    sender_id,
                    name,
                    text,
                    reply_to,
                    _dump(list(recipients)),
                    _now(),
                ),
            )
            for recipient, route in audience.items():
                connection.execute(
                    "INSERT INTO recipients(post_id,participant_id,route_class) VALUES(?,?,?)",
                    (post_id, recipient, route),
                )
            result = {
                "post_id": post_id,
                "sequence": sequence,
                "discussion_id": discussion_id_value,
                "routes": {
                    route: sum(1 for value in audience.values() if value == route)
                    for route in ("ping", "discussion", "main")
                },
            }
            connection.execute(
                "INSERT INTO requests(scope,request_id,payload_hash,outcome) VALUES(?,?,?,?)",
                (scope, request_id, _hash(payload), _dump(result)),
            )
            return result

        return self._write(operation)

    def _create_discussion(
        self,
        swarm_id: str,
        participant_id: str,
        title: str,
        text: str,
        request_id: str,
        recipients: tuple[str, ...],
        expected_epoch: int | None,
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            self._assert_mutable(connection, swarm_id)
            if expected_epoch is not None:
                self._assert_epoch(connection, swarm_id, expected_epoch)
            self._participant(connection, swarm_id, participant_id)
            payload = {"title": title, "text": text, "recipients": recipients}
            scope = f"create:{swarm_id}:{participant_id}"
            replay = connection.execute(
                "SELECT payload_hash,outcome FROM requests WHERE scope=? AND request_id=?",
                (scope, request_id),
            ).fetchone()
            if replay is not None:
                if replay["payload_hash"] != _hash(payload):
                    raise SwarmStoreError("request_conflict")
                value = _load(replay["outcome"])
                value["replayed"] = True
                return value
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM discussions WHERE swarm_id=?",
                    (swarm_id,),
                ).fetchone()[0]
            )
            for recipient in recipients:
                if (
                    connection.execute(
                        "SELECT 1 FROM participants WHERE swarm_id=? AND id=?",
                        (swarm_id, recipient),
                    ).fetchone()
                    is None
                ):
                    raise SwarmStoreError("invalid_recipient")
            discussion_id = new_id("dsc")
            connection.execute(
                "INSERT INTO discussions(id,swarm_id,title,sequence,is_main,created_at) VALUES(?,?,?,?,0,?)",
                (discussion_id, swarm_id, title, sequence, _now()),
            )
            connection.execute(
                "INSERT INTO memberships(discussion_id,participant_id) VALUES(?,?)",
                (discussion_id, participant_id),
            )
            opening_id = self._insert_post(
                connection,
                swarm_id,
                participant_id,
                discussion_id,
                text,
                None,
                recipients,
                "participant",
                None,
            )
            announcement_id = self._insert_post(
                connection,
                swarm_id,
                participant_id,
                self._main(connection, swarm_id),
                _dump(
                    {"discussion_id": discussion_id, "title": title, "opening_post_id": opening_id}
                ),
                None,
                (),
                "participant",
                None,
            )
            result = {
                "discussion_id": discussion_id,
                "opening_post_id": opening_id,
                "main_announcement_id": announcement_id,
                "joined": True,
            }
            connection.execute(
                "INSERT INTO requests(scope,request_id,payload_hash,outcome) VALUES(?,?,?,?)",
                (scope, request_id, _hash(payload), _dump(result)),
            )
            return result

        return self._write(operation)

    def _insert_post(
        self,
        connection: sqlite3.Connection,
        swarm_id: str,
        sender_id: str,
        discussion_id: str,
        text: str,
        reply_to: str | None,
        recipients: tuple[str, ...],
        author_kind: str,
        author_name: str | None,
    ) -> str:
        sender = self._participant(connection, swarm_id, sender_id)
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM posts WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        )
        post_id = new_id("pst")
        connection.execute(
            "INSERT INTO posts(id,swarm_id,discussion_id,sequence,author_kind,author_id,author_name,text,reply_to,recipients_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                post_id,
                swarm_id,
                discussion_id,
                sequence,
                author_kind,
                sender_id,
                author_name or sender["display_name"],
                text,
                reply_to,
                _dump(list(recipients)),
                _now(),
            ),
        )
        audience = {
            row["participant_id"]: (
                "main" if discussion_id == self._main(connection, swarm_id) else "discussion"
            )
            for row in connection.execute(
                "SELECT participant_id FROM memberships WHERE discussion_id=?", (discussion_id,)
            )
        }
        audience.update(dict.fromkeys(recipients, "ping"))
        audience.pop(sender_id, None)
        for recipient, route in audience.items():
            connection.execute(
                "INSERT INTO recipients(post_id,participant_id,route_class) VALUES(?,?,?)",
                (post_id, recipient, route),
            )
        return post_id

    def _membership(
        self,
        swarm_id: str,
        participant_id: str,
        discussion_id: str,
        joining: bool,
        expected_epoch: int | None,
    ) -> Json:
        def operation(connection: sqlite3.Connection) -> Json:
            self._assert_mutable(connection, swarm_id)
            if expected_epoch is not None:
                self._assert_epoch(connection, swarm_id, expected_epoch)
            self._participant(connection, swarm_id, participant_id)
            discussion = self._discussion(connection, swarm_id, discussion_id)
            if not joining and discussion["is_main"]:
                raise SwarmStoreError("main_membership_required")
            if joining:
                connection.execute(
                    "INSERT OR IGNORE INTO memberships(discussion_id,participant_id) VALUES(?,?)",
                    (discussion_id, participant_id),
                )
            else:
                connection.execute(
                    "DELETE FROM memberships WHERE discussion_id=? AND participant_id=?",
                    (discussion_id, participant_id),
                )
            if not joining:
                return {
                    "discussion_id": discussion_id,
                    "joined": False,
                    "pending_count": self._pending_count(connection, swarm_id, participant_id),
                }
            recent = self._post_page(connection, swarm_id, participant_id, discussion_id, None, 20)
            return {
                "discussion_id": discussion_id,
                "joined": True,
                "recent": {
                    "entries": list(recent.entries),
                    "has_more": recent.has_more,
                    "cursor": recent.cursor,
                },
            }

        return self._write(operation)

    @staticmethod
    def _participant(
        connection: sqlite3.Connection, swarm_id: str, participant_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM participants WHERE swarm_id=? AND id=?", (swarm_id, participant_id)
        ).fetchone()
        if row is None:
            raise SwarmStoreError("participant_not_found")
        return row

    @staticmethod
    def _pending_count(connection: sqlite3.Connection, swarm_id: str, participant_id: str) -> int:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM recipients r JOIN posts p ON p.id=r.post_id "
                "WHERE p.swarm_id=? AND r.participant_id=? AND r.delivered_at IS NULL",
                (swarm_id, participant_id),
            ).fetchone()[0]
        )

    @staticmethod
    def _assert_mutable(connection: sqlite3.Connection, swarm_id: str) -> None:
        row = connection.execute("SELECT state FROM swarms WHERE id=?", (swarm_id,)).fetchone()
        if row is None:
            raise SwarmStoreError("swarm_not_found")
        epoch = connection.execute(
            "SELECT is_open FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
        ).fetchone()
        if row["state"] not in _MUTABLE_SWARM_STATES or epoch is None or not epoch["is_open"]:
            raise SwarmStoreError("swarm_closed")

    @staticmethod
    def _assert_epoch(connection: sqlite3.Connection, swarm_id: str, expected_epoch: int) -> None:
        SwarmStore._assert_mutable(connection, swarm_id)
        row = connection.execute(
            "SELECT epoch FROM swarm_epochs WHERE swarm_id=?", (swarm_id,)
        ).fetchone()
        if row is None or int(row["epoch"]) != expected_epoch:
            raise SwarmStoreError("stale_epoch")

    @staticmethod
    def _discussion(
        connection: sqlite3.Connection, swarm_id: str, discussion_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM discussions WHERE swarm_id=? AND id=?", (swarm_id, discussion_id)
        ).fetchone()
        if row is None:
            raise SwarmStoreError("discussion_not_found")
        return row

    @staticmethod
    def _main(connection: sqlite3.Connection, swarm_id: str) -> str:
        return str(
            connection.execute(
                "SELECT id FROM discussions WHERE swarm_id=? AND is_main=1", (swarm_id,)
            ).fetchone()[0]
        )

    @staticmethod
    def _discussion_high_water(connection: sqlite3.Connection, swarm_id: str) -> int:
        return int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM discussions WHERE swarm_id=?", (swarm_id,)
            ).fetchone()[0]
        )

    @staticmethod
    def _post_high_water(connection: sqlite3.Connection, swarm_id: str, discussion_id: str) -> int:
        return int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM posts WHERE swarm_id=? AND discussion_id=?",
                (swarm_id, discussion_id),
            ).fetchone()[0]
        )

    def _make_cursor(self, kind: str, scope: str, high_water: int | None, offset: int) -> str:
        data = _dump({"k": kind, "s": scope, "h": high_water, "o": offset})
        key = (
            self._require_connection()
            .execute("SELECT value FROM swarm_meta WHERE key='cursor_key'")
            .fetchone()[0]
        )
        mac = hmac.new(str(key).encode(), data.encode(), hashlib.sha256).digest()
        return (
            base64.urlsafe_b64encode(data.encode()).decode()
            + "."
            + base64.urlsafe_b64encode(mac).decode()
        )

    def _cursor(
        self, cursor: str, kind: str, scope: str, high_water: int | None
    ) -> tuple[int, int | None]:
        try:
            encoded_data, encoded_mac = cursor.split(".", 1)
            data = base64.urlsafe_b64decode(encoded_data.encode())
            mac = base64.urlsafe_b64decode(encoded_mac.encode())
            key = (
                self._require_connection()
                .execute("SELECT value FROM swarm_meta WHERE key='cursor_key'")
                .fetchone()[0]
            )
            if not hmac.compare_digest(
                mac, hmac.new(str(key).encode(), data, hashlib.sha256).digest()
            ):
                raise ValueError
            value = _load(data.decode())
            if (
                value.get("k") != kind
                or value.get("s") != scope
                or not isinstance(value.get("o"), int)
                or value["o"] < 0
                or (value.get("h") is not None and not isinstance(value["h"], int))
            ):
                raise ValueError
            return value["o"], value["h"]
        except (
            ValueError,
            KeyError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            binascii.Error,
        ) as error:
            raise SwarmStoreError("invalid_cursor") from error


def _validate_profile(value: Mapping[str, Any]) -> Json:
    profile = _json_object(value)
    allowed = {
        "schema_version",
        "id",
        "revision",
        "slug",
        "name",
        "participants",
        "working_directory",
        "tool_access",
        "tools",
        "allowed_skills",
        "instructions",
        "prompt_blocks",
        "reminders",
        "delivery",
    }
    if set(profile) - allowed:
        raise SwarmStoreError("invalid_arguments")
    if type(profile.get("schema_version")) is not int or profile["schema_version"] != 1:
        raise SwarmStoreError("invalid_arguments", field="schema_version")
    if "id" not in profile:
        profile["id"] = new_id("prf")
    if not isinstance(profile["id"], str) or not profile["id"]:
        raise SwarmStoreError("invalid_arguments", field="id")
    profile.setdefault("slug", profile["id"])
    if not isinstance(profile.get("slug"), str) or _PROFILE_SLUG.fullmatch(profile["slug"]) is None:
        raise SwarmStoreError("invalid_arguments", field="slug")
    _text(profile.get("name"), "name", 120)
    participants = profile.get("participants")
    if not isinstance(participants, list) or not participants:
        raise SwarmStoreError("invalid_arguments", field="participants")
    normalized_participants: list[Json] = []
    for item in participants:
        normalized_participants.append(_formation(item))
    profile["participants"] = normalized_participants
    cwd = profile.get("working_directory")
    if not isinstance(cwd, dict) or cwd.get("kind") not in {"project", "directory"}:
        raise SwarmStoreError("invalid_arguments", field="working_directory")
    cwd_key = "project_id" if cwd["kind"] == "project" else "path"
    if set(cwd) != {"kind", cwd_key} or not isinstance(cwd.get(cwd_key), str) or not cwd[cwd_key]:
        raise SwarmStoreError("invalid_arguments", field="working_directory")
    access = profile.get("tool_access")
    try:
        normalized_access = normalize_tool_access(access)
    except ValueError as error:
        raise SwarmStoreError("invalid_arguments", field="tool_access") from error
    if normalized_access.mode != "selected":
        raise SwarmStoreError("invalid_arguments", field="tool_access")
    profile["tool_access"] = normalized_access.to_dict()
    tools = profile.get("tools", {})
    if not isinstance(tools, dict) or any(
        not isinstance(name, str) or not name or not isinstance(settings, dict)
        for name, settings in tools.items()
    ):
        raise SwarmStoreError("invalid_arguments", field="tools")
    allowed_skills = profile.get("allowed_skills", ["*"])
    if not isinstance(allowed_skills, list) or any(
        not isinstance(skill, str) or not skill for skill in allowed_skills
    ):
        raise SwarmStoreError("invalid_arguments", field="allowed_skills")
    instructions = profile.get("instructions", "")
    if not isinstance(instructions, str):
        raise SwarmStoreError("invalid_arguments", field="instructions")
    from .agent_text import DEFAULT_PROMPT_BLOCKS, DEFAULT_REMINDERS

    prompt_blocks = profile.get("prompt_blocks", DEFAULT_PROMPT_BLOCKS)
    if (
        not isinstance(prompt_blocks, list)
        or any(
            not isinstance(item, str) or ":" not in item or item == "core:agent_body"
            for item in prompt_blocks
        )
        or len(set(prompt_blocks)) != len(prompt_blocks)
    ):
        raise SwarmStoreError("invalid_arguments", field="prompt_blocks")
    reminders = profile.get("reminders", DEFAULT_REMINDERS)
    if (
        not isinstance(reminders, dict)
        or set(reminders) != set(DEFAULT_REMINDERS)
        or any(type(value) is not bool for value in reminders.values())
    ):
        raise SwarmStoreError("invalid_arguments", field="reminders")
    profile["prompt_blocks"] = list(prompt_blocks)
    profile["reminders"] = dict(reminders)
    profile["delivery"] = _delivery(profile.get("delivery", {}))
    profile.setdefault("tools", {})
    profile.setdefault("allowed_skills", ["*"])
    profile.setdefault("instructions", "")
    if "revision" in profile:
        _revision(profile["revision"], "revision", allow_zero=True)
    else:
        profile["revision"] = 0
    return profile


def _formation(value: object) -> Json:
    if not isinstance(value, dict):
        raise SwarmStoreError("invalid_arguments", field="participants")
    allowed = {"model", "count", "thinking_effort", "temperature", "fallback_models"}
    if set(value) - allowed or set(value) < {"model", "count"}:
        raise SwarmStoreError("invalid_arguments", field="participants")
    model = value.get("model")
    count = value.get("count")
    if not isinstance(model, str) or not model.strip() or type(count) is not int or count < 1:
        raise SwarmStoreError("invalid_arguments", field="participants")
    formation: Json = {"model": model, "count": count}
    if "thinking_effort" in value:
        try:
            formation["thinking_effort"] = validate_thinking_effort(
                value["thinking_effort"], label="thinking_effort", allow_none=True
            )
        except SettingsValidationError as error:
            raise SwarmStoreError("invalid_arguments", field="participants") from error
    if "temperature" in value:
        try:
            formation["temperature"] = validate_temperature(
                value["temperature"], label="temperature", allow_none=True
            )
        except SettingsValidationError as error:
            raise SwarmStoreError("invalid_arguments", field="participants") from error
    if "fallback_models" in value:
        try:
            formation["fallback_models"] = _fallback_chain_entries(value["fallback_models"])
        except ValueError as error:
            raise SwarmStoreError("invalid_arguments", field="participants") from error
    return formation


def _delivery(value: object) -> Json:
    if not isinstance(value, dict):
        raise SwarmStoreError("invalid_arguments", field="delivery")
    allowed = _DELIVERY_ROUTES | {"coalesce_ms", "batch_messages", "batch_chars"}
    if set(value) - allowed:
        raise SwarmStoreError("invalid_arguments", field="delivery")
    delivery = _copy(_DELIVERY_DEFAULTS)
    for route in _DELIVERY_ROUTES:
        route_value = value.get(route, {})
        if not isinstance(route_value, dict) or set(route_value) - {"mode", "wake_idle"}:
            raise SwarmStoreError("invalid_arguments", field="delivery")
        mode = route_value.get("mode", delivery[route]["mode"])
        wake_idle = route_value.get("wake_idle", delivery[route]["wake_idle"])
        if not isinstance(mode, str) or mode not in _DELIVERY_MODES or type(wake_idle) is not bool:
            raise SwarmStoreError("invalid_arguments", field="delivery")
        delivery[route] = {"mode": mode, "wake_idle": wake_idle}
    for key, minimum, maximum in (
        ("coalesce_ms", 0, 5_000),
        ("batch_messages", 1, 100),
        ("batch_chars", 16_000, 128_000),
    ):
        item = value.get(key, delivery[key])
        if type(item) is not int or not minimum <= item <= maximum:
            raise SwarmStoreError("invalid_arguments", field="delivery")
        delivery[key] = item
    return delivery


def _revision(value: object, field: str, *, allow_zero: bool = False) -> None:
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise SwarmStoreError("invalid_arguments", field=field)


def _json_object(value: Mapping[str, Any]) -> Json:
    if not isinstance(value, Mapping):
        raise SwarmStoreError("invalid_arguments")
    try:
        return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise SwarmStoreError("invalid_arguments") from error


def _text(value: object, field: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SwarmStoreError("invalid_arguments", field=field)


def _request_id(value: object) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise SwarmStoreError("invalid_arguments", field="request_id")


def _recipient_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or any(not isinstance(value, str) for value in values):
        raise SwarmStoreError("invalid_arguments", field="recipients")
    return tuple(sorted(set(values)))


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_LIMIT:
        raise SwarmStoreError("invalid_arguments", field="limit")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str) -> Json:
    return json.loads(value)


def _copy(value: Json) -> Json:
    return _load(_dump(value))


def _hash(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def _page(entries: list[Json], limit: int, cursor: str) -> Page:
    more = len(entries) > limit
    return Page(tuple(entries[:limit]), more, cursor if more else None)


def _post(row: sqlite3.Row) -> Json:
    value = {
        "id": row["id"],
        "sequence": row["sequence"],
        "created_at": row["created_at"],
        "discussion_id": row["discussion_id"],
        "author": {
            "kind": row["author_kind"],
            "id": row["author_id"],
            "name": row["author_name"],
        },
        "text": row["text"],
        "reply_to": row["reply_to"],
        "recipients": _load(row["recipients_json"]),
    }
    # sqlite3.Row membership checks values, not column names.
    columns = row.keys()
    if "route_class" in columns and row["route_class"] is not None:
        value["route_class"] = row["route_class"]
    if "discussion_title" in columns:
        value["discussion_title"] = row["discussion_title"]
    return value


_SCHEMA = """
CREATE TABLE IF NOT EXISTS swarm_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY,slug TEXT NOT NULL UNIQUE,name TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS swarms(id TEXT PRIMARY KEY,prompt TEXT NOT NULL,profile_snapshot TEXT NOT NULL,effective_configuration TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS participants(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),model TEXT NOT NULL,display_name TEXT NOT NULL,ordinal INTEGER NOT NULL,state TEXT NOT NULL,idle_boundary INTEGER,wake_announced_seq INTEGER NOT NULL DEFAULT 0,wake_epoch INTEGER NOT NULL DEFAULT 0,wake_pending INTEGER NOT NULL DEFAULT 0,wake_pending_seq INTEGER NOT NULL DEFAULT 0,lifecycle_run_id TEXT,UNIQUE(swarm_id,ordinal),UNIQUE(swarm_id,display_name COLLATE NOCASE)) STRICT;
CREATE TABLE IF NOT EXISTS participant_sessions(participant_id TEXT PRIMARY KEY REFERENCES participants(id),project_id TEXT,agent_id TEXT NOT NULL,session_id TEXT NOT NULL,generation_id TEXT NOT NULL,owner_name TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS swarm_settings(swarm_id TEXT PRIMARY KEY REFERENCES swarms(id),revision INTEGER NOT NULL,delivery_json TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS swarm_epochs(swarm_id TEXT PRIMARY KEY REFERENCES swarms(id),epoch INTEGER NOT NULL,is_open INTEGER NOT NULL CHECK(is_open IN(0,1))) STRICT;
CREATE TABLE IF NOT EXISTS swarm_execution_epochs(swarm_id TEXT NOT NULL REFERENCES swarms(id),epoch INTEGER NOT NULL,execution_epoch TEXT NOT NULL,PRIMARY KEY(swarm_id,epoch)) STRICT;
CREATE TABLE IF NOT EXISTS swarm_events(id INTEGER PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),kind TEXT NOT NULL,actor TEXT NOT NULL,old_json TEXT,new_json TEXT,settings_revision INTEGER,created_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS discussions(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),title TEXT NOT NULL,sequence INTEGER NOT NULL,is_main INTEGER NOT NULL CHECK(is_main IN(0,1)),created_at TEXT NOT NULL,UNIQUE(swarm_id,sequence)) STRICT;
CREATE TABLE IF NOT EXISTS memberships(discussion_id TEXT NOT NULL REFERENCES discussions(id),participant_id TEXT NOT NULL REFERENCES participants(id),PRIMARY KEY(discussion_id,participant_id)) STRICT;
CREATE TABLE IF NOT EXISTS posts(id TEXT PRIMARY KEY,swarm_id TEXT NOT NULL REFERENCES swarms(id),discussion_id TEXT NOT NULL REFERENCES discussions(id),sequence INTEGER NOT NULL,author_kind TEXT NOT NULL,author_id TEXT NOT NULL,author_name TEXT NOT NULL,text TEXT NOT NULL,reply_to TEXT,recipients_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(swarm_id,sequence)) STRICT;
CREATE TABLE IF NOT EXISTS recipients(post_id TEXT NOT NULL REFERENCES posts(id),participant_id TEXT NOT NULL REFERENCES participants(id),route_class TEXT NOT NULL,delivered_at TEXT,receipt_id TEXT,content_hash TEXT,effect_kind TEXT,carrier_kind TEXT,carrier_sequence INTEGER,prepared_at TEXT,PRIMARY KEY(post_id,participant_id)) STRICT;
CREATE TABLE IF NOT EXISTS delivery_batches(receipt_id TEXT PRIMARY KEY,participant_id TEXT NOT NULL REFERENCES participants(id),content_hash TEXT NOT NULL,effect_kind TEXT NOT NULL,created_at TEXT NOT NULL,acknowledged_at TEXT,carrier_kind TEXT,carrier_sequence INTEGER,settings_revision INTEGER) STRICT;
CREATE TABLE IF NOT EXISTS delivery_batch_entries(receipt_id TEXT NOT NULL REFERENCES delivery_batches(receipt_id),post_id TEXT NOT NULL REFERENCES posts(id),participant_id TEXT NOT NULL REFERENCES participants(id),PRIMARY KEY(receipt_id,post_id,participant_id)) STRICT;
CREATE TABLE IF NOT EXISTS requests(scope TEXT NOT NULL,request_id TEXT NOT NULL,payload_hash TEXT NOT NULL,outcome TEXT NOT NULL,PRIMARY KEY(scope,request_id)) STRICT;
CREATE INDEX IF NOT EXISTS posts_discussion_page ON posts(swarm_id,discussion_id,sequence DESC);
CREATE INDEX IF NOT EXISTS discussions_page ON discussions(swarm_id,is_main DESC,sequence);
CREATE UNIQUE INDEX IF NOT EXISTS discussions_one_main ON discussions(swarm_id) WHERE is_main=1;
"""
