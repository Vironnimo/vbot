"""Durable, extension-local persistence for Swarm profiles and Board state."""

# ruff: noqa: E501
# mypy: disable-error-code=no-any-return

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from core.sessions import TemporarySessionBinding
from core.utils.workers import BoundedWorkerPool

from ._store_board import (
    _create_discussion,
    _membership,
    _post_message,
)
from ._store_database import (
    SwarmDatabase,
)
from ._store_delivery import (
    _acknowledge_delivery,
    _claim_wake,
    _list_prepared_deliveries,
    _list_wake_intents,
    _mark_wake_admitted,
    _prepare_automatic_delivery,
    _prepare_delivery,
    _prepared_delivery,
)
from ._store_lifecycle import (
    _begin_resume,
    _begin_stop,
    _bind_execution_epoch,
    _bind_participant_session,
    _fail_startup,
    _finish_admission,
    _finish_stop,
    _participant_status,
    _reconcile_run_finished,
    _record_run_started,
    _recover_interrupted,
    _set_participant_state,
    _set_swarm_state,
)
from ._store_profiles import (
    _apply_delivery_settings,
    _begin_delete,
    _create_swarm,
    _delete_profile,
    _delete_swarm,
    _get_profile,
    _get_swarm,
    _list_events,
    _list_profiles,
    _list_swarms,
    _save_profile,
)
from ._store_reads import (
    _list_discussions,
    _list_human_discussions,
    _read_human_posts,
    _read_posts,
)
from ._store_values import (
    _MAX_LIMIT,
    _PARTICIPANT_STATES,
    DeliveryReceiptLookup,
    Json,
    Page,
    SwarmStoreError,
    _delivery,
    _json_object,
    _limit,
    _recipient_ids,
    _request_id,
    _revision,
    _text,
    _validate_profile,
)

__all__ = [
    "DeliveryReceiptLookup",
    "Json",
    "Page",
    "SwarmDatabase",
    "SwarmStore",
    "SwarmStoreError",
]

_WORKERS = BoundedWorkerPool(name="swarm-store", max_workers=2)


class SwarmStore:
    """Public asynchronous persistence API for Swarm profiles and Board facts."""

    def __init__(
        self,
        path: Path,
        *,
        lookup_delivery_receipt: DeliveryReceiptLookup | None = None,
        worker_pool: BoundedWorkerPool = _WORKERS,
    ) -> None:
        self._database = SwarmDatabase(path)
        self._workers = worker_pool
        self._lookup_delivery_receipt = lookup_delivery_receipt

    async def open(self) -> None:
        await self._run(SwarmDatabase._open)

    async def close(self) -> None:
        await self._run(SwarmDatabase._close)

    async def save_profile(
        self, profile: Mapping[str, Any], *, expected_revision: int | None
    ) -> Json:
        normalized = _validate_profile(profile)
        return await self._run(_save_profile, normalized, expected_revision, "slug" not in profile)

    async def get_profile(self, profile_id: str) -> Json:
        return await self._run(_get_profile, profile_id)

    async def list_profiles(self, *, cursor: str | None = None, limit: int = 20) -> Page:
        return await self._run(_list_profiles, cursor, _limit(limit))

    async def delete_profile(self, profile_id: str, *, expected_revision: int) -> None:
        _revision(expected_revision, "expected_revision")
        await self._run(_delete_profile, profile_id, expected_revision)

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
            _create_swarm,
            profile_id,
            prompt,
            _json_object(effective_configuration),
            request_id,
            expected_profile_revision,
        )

    async def begin_delete(self, swarm_id: str) -> bool:
        return await self._run(_begin_delete, swarm_id)

    async def delete_swarm(self, swarm_id: str) -> None:
        await self._run(_delete_swarm, swarm_id)

    async def get_swarm(self, swarm_id: str) -> Json:
        return await self._run(_get_swarm, swarm_id)

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
            _apply_delivery_settings,
            swarm_id,
            _delivery(delivery),
            expected_revision,
            request_id,
            actor,
        )

    async def set_swarm_state(self, swarm_id: str, state: str) -> Json:
        if state not in {"running", "idle", "needs_attention"}:
            raise SwarmStoreError("invalid_arguments", field="state")
        return await self._run(_set_swarm_state, swarm_id, state)

    async def fail_startup(self, swarm_id: str, *, expected_epoch: int) -> Json:
        """Close a partially prepared epoch while preserving the Swarm for Resume."""
        if type(expected_epoch) is not int or expected_epoch < 0:
            raise SwarmStoreError("invalid_arguments", field="expected_epoch")
        return await self._run(_fail_startup, swarm_id, expected_epoch)

    async def set_participant_state(
        self, swarm_id: str, participant_id: str, state: str, *, idle_boundary: int | None = None
    ) -> Json:
        if state not in _PARTICIPANT_STATES or (
            idle_boundary is not None and (type(idle_boundary) is not int or idle_boundary < 0)
        ):
            raise SwarmStoreError("invalid_arguments", field="state")
        return await self._run(
            _set_participant_state, swarm_id, participant_id, state, idle_boundary
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
            _participant_status,
            swarm_id,
            participant_id,
            cursor,
            _limit(limit),
        )

    async def record_run_started(
        self, swarm_id: str, participant_id: str, *, run_id: str, expected_epoch: int
    ) -> Json:
        return await self._run(
            _record_run_started, swarm_id, participant_id, run_id, expected_epoch
        )

    async def reconcile_run_finished(
        self, swarm_id: str, participant_id: str, *, run_id: str, expected_epoch: int, outcome: str
    ) -> Json:
        if outcome not in {"completed", "failed", "cancelled", "interrupted"}:
            raise SwarmStoreError("invalid_arguments", field="outcome")
        return await self._run(
            _reconcile_run_finished, swarm_id, participant_id, run_id, expected_epoch, outcome
        )

    async def recover_interrupted(self) -> list[Json]:
        """Close recoverable epochs without admitting or scheduling participant work."""
        return await self._run(_recover_interrupted)

    async def begin_stop(self, swarm_id: str, *, request_id: str, actor: str) -> Json:
        _request_id(request_id)
        if not isinstance(actor, str) or not actor:
            raise SwarmStoreError("invalid_arguments", field="actor")
        return await self._run(_begin_stop, swarm_id, request_id, actor)

    async def finish_stop(
        self, swarm_id: str, *, request_id: str, actor: str, drain_report: Mapping[str, Any]
    ) -> Json:
        _request_id(request_id)
        if not isinstance(actor, str) or not actor:
            raise SwarmStoreError("invalid_arguments", field="actor")
        return await self._run(
            _finish_stop, swarm_id, request_id, actor, _json_object(drain_report)
        )

    async def begin_resume(
        self, swarm_id: str, *, request_id: str, actor: str, participant_id: str | None = None
    ) -> Json:
        _request_id(request_id)
        if not isinstance(actor, str) or not actor:
            raise SwarmStoreError("invalid_arguments", field="actor")
        return await self._run(_begin_resume, swarm_id, request_id, actor, participant_id)

    async def finish_admission(
        self,
        swarm_id: str,
        *,
        request_id: str,
        kind: Literal["start", "resume"],
        runs: Sequence[Json],
    ) -> Json:
        """Retain the result of one admission request without replaying its effects."""
        return await self._run(_finish_admission, swarm_id, request_id, kind, list(runs))

    async def list_swarms(self, *, cursor: str | None = None, limit: int = 20) -> Page:
        return await self._run(_list_swarms, cursor, _limit(limit))

    async def list_events(
        self, swarm_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> Page:
        return await self._run(_list_events, swarm_id, cursor, _limit(limit))

    async def bind_execution_epoch(
        self, swarm_id: str, *, expected_epoch: int, execution_epoch: str
    ) -> Json:
        if type(expected_epoch) is not int or expected_epoch < 0 or not execution_epoch:
            raise SwarmStoreError("invalid_arguments")
        return await self._run(_bind_execution_epoch, swarm_id, expected_epoch, execution_epoch)

    async def bind_participant_session(self, binding: TemporarySessionBinding) -> None:
        await self._run(_bind_participant_session, binding)

    async def prepare_inbox_delivery(
        self, swarm_id: str, participant_id: str, *, limit: int = 20
    ) -> Json:
        return await self._run(
            _prepare_delivery, swarm_id, participant_id, None, _limit(limit), "swarm_inbox"
        )

    async def list_prepared_deliveries(self, *, cursor: str | None = None, limit: int = 20) -> Page:
        """Return bounded unacknowledged receipts for read-only startup reconciliation."""
        return await self._run(_list_prepared_deliveries, cursor, _limit(limit))

    async def list_wake_intents(
        self, swarm_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> Page:
        return await self._run(_list_wake_intents, swarm_id, cursor, _limit(limit))

    async def claim_wake(self, swarm_id: str, participant_id: str, *, expected_epoch: int) -> Json:
        return await self._run(_claim_wake, swarm_id, participant_id, expected_epoch)

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
            _mark_wake_admitted, swarm_id, participant_id, expected_epoch, run_id, boundary
        )

    async def prepare_board_read_delivery(
        self, swarm_id: str, participant_id: str, post_ids: Sequence[str]
    ) -> Json:
        if isinstance(post_ids, (str, bytes)) or not all(
            isinstance(post_id, str) for post_id in post_ids
        ):
            raise SwarmStoreError("invalid_arguments", field="post_ids")
        return await self._run(
            _prepare_delivery,
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
            _prepare_automatic_delivery,
            swarm_id,
            participant_id,
            expected_epoch,
            admission_boundary,
            False,
        )

    async def prepare_wake(
        self, swarm_id: str, participant_id: str, *, expected_epoch: int
    ) -> Json:
        """Freeze one idle boundary and prepare its delivery before Run admission."""
        if type(expected_epoch) is not int or expected_epoch < 0:
            raise SwarmStoreError("invalid_arguments", field="expected_epoch")
        return await self._run(
            _prepare_automatic_delivery, swarm_id, participant_id, expected_epoch, None, True
        )

    async def reconcile_delivery(self, receipt_id: str) -> bool:
        _request_id(receipt_id)
        prepared = await self._run(_prepared_delivery, receipt_id)
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
        return await self._run(_acknowledge_delivery, prepared, receipt)

    async def list_discussions(
        self, swarm_id: str, participant_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> Page:
        return await self._run(_list_discussions, swarm_id, participant_id, cursor, _limit(limit))

    async def list_human_discussions(
        self, swarm_id: str, *, cursor: str | None = None, limit: int = 20
    ) -> Page:
        """List public Board discussions without creating participant delivery state."""
        return await self._run(_list_human_discussions, swarm_id, cursor, _limit(limit))

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
            _read_posts,
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
            _read_human_posts,
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
            _post_message,
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
            _post_message,
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
            _create_discussion,
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
            _membership, swarm_id, participant_id, discussion_id, True, expected_epoch
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
            _membership, swarm_id, participant_id, discussion_id, False, expected_epoch
        )

    async def _run(self, function: Callable[..., Any], *arguments: Any) -> Any:
        return await self._workers.run(self._database.call, function, *arguments)
