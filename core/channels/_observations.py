"""Durable admission and Session delivery of observed Channel context.

This is an inbox, not another transcript: rows leave once their complete Note batch
and its idempotency receipt reached canonical Session history. Rows do not reference
the Channel registry: disabling/deleting a transport cannot revoke accepted context.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, fields
from typing import TYPE_CHECKING, Any

from core.channels.adapter import ConversationFacts
from core.channels.config import ChannelConfig, ChannelError
from core.sessions.errors import SessionNotFoundError
from core.utils.logging import get_logger
from core.utils.timestamps import utc_now_timestamp

from ._conversation_routing import ChannelSessionRouting, _session_address

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from core.channels._conversation_work import _QueuedObservedMessage
    from core.channels.state import ChannelStateStore
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("channels.observations")


@dataclass(frozen=True)
class _Observation:
    id: str
    owner: str
    config: ChannelConfig
    conversation: ConversationFacts
    notes: tuple[str, ...]
    generation_id: str
    session_id: str
    routed: bool


class ChannelObservationInbox:
    """Own recoverable context admission beside the Channel's routing state."""

    def __init__(
        self,
        state: ChannelStateStore,
        sessions: ChatSessionManager,
        config_reader: Callable[[str], ChannelConfig] | None = None,
    ) -> None:
        self._state = state
        self._sessions = sessions
        self._config_reader = config_reader

    async def record(
        self,
        owner: str,
        config: ChannelConfig,
        routing: ChannelSessionRouting,
        observations: Sequence[_QueuedObservedMessage],
    ) -> None:
        """Persist admission before the adapter may acknowledge any observed context."""
        rows = []
        for observed in observations:
            # A fallback generation makes the inbox independent of later Channel deletion
            # or reassignment. FIFO processing may still follow an earlier /new.
            route, _ = await routing._prepare_inbound_route_async(observed.conversation)
            session = await self._sessions.get_async(
                _session_address(route.agent_id, route.session_id)
            )
            rows.append(
                (
                    observed.delivery_id,
                    owner,
                    config.id,
                    utc_now_timestamp(),
                    json.dumps(config.to_dict()),
                    json.dumps(asdict(observed.conversation)),
                    json.dumps([observed.note, *observed.following_notes]),
                    session.generation_id,
                    route.session_id,
                )
            )

        def insert(connection: Any) -> None:
            latest = connection.execute(
                "SELECT COALESCE(MAX(admission_order), 0) FROM channel_observation_inbox"
            ).fetchone()[0]
            connection.executemany(
                "INSERT INTO channel_observation_inbox "
                "(delivery_id, owner, channel_id, created_at, config_json, conversation_json, "
                "notes_json, generation_id, fallback_session_id, admission_order) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(*row, latest + index + 1) for index, row in enumerate(rows)],
            )

        await self._state.database.write_async(insert)

    async def _get(self, delivery_id: str) -> _Observation | None:
        rows = await self._state.database.read_async(
            lambda connection: connection.execute(
                "SELECT delivery_id, owner, config_json, conversation_json, notes_json, "
                "generation_id, "
                "fallback_session_id, routed "
                "FROM channel_observation_inbox WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchall()
        )
        return _decode(rows[0]) if rows else None

    async def prepare(self, delivery_id: str, routing: ChannelSessionRouting) -> None:
        """Freeze routing at the FIFO head, or at stop after navigation has settled."""
        observation = await self._get(delivery_id)
        if observation is None or observation.routed:
            return
        original = await self._sessions.run_async(
            self._sessions.generation_address, observation.generation_id
        )
        if original is None:
            # Explicit Session deletion has precedence over a previously accepted Note.
            await self._settle(delivery_id)
            return
        generation_id = observation.generation_id
        if (
            original.agent_id == observation.config.agent_id
            and original.session_id == observation.session_id
        ):
            route, _ = await routing._prepare_inbound_route_async(observation.conversation)
            session = await self._sessions.get_async(
                _session_address(route.agent_id, route.session_id)
            )
            assert session.generation_id is not None
            generation_id = session.generation_id
        # A moved/renamed Session keeps the accepted context; never recreate its old address.
        await self._state.database.write_async(
            lambda connection: connection.execute(
                "UPDATE channel_observation_inbox SET generation_id = ?, routed = 1 "
                "WHERE delivery_id = ? AND routed = 0",
                (generation_id, delivery_id),
            )
        )

    async def deliver(self, delivery_id: str) -> None:
        observation = await self._get(delivery_id)
        if observation is None:
            return
        while True:
            address = await self._sessions.run_async(
                self._sessions.generation_address, observation.generation_id
            )
            if address is None:
                await self._settle(delivery_id)
                return
            async with self._sessions.write_lock(address):
                try:
                    await self._sessions.run_async(
                        self._sessions.append_notes_once,
                        address,
                        delivery_id,
                        observation.notes,
                        generation_id=observation.generation_id,
                    )
                except SessionNotFoundError:
                    # A rename/move can win between the generation read and the lease.
                    continue
            await self._settle(delivery_id)
            return

    async def _settle(self, delivery_id: str) -> None:
        await self._state.database.write_async(
            lambda connection: connection.execute(
                "DELETE FROM channel_observation_inbox WHERE delivery_id = ?", (delivery_id,)
            )
        )

    async def ids(self, owner: str | None = None) -> list[str]:
        def select(connection: Any) -> list[str]:
            predicate, params = ("", ()) if owner is None else ("WHERE owner = ?", (owner,))
            return [
                str(row[0])
                for row in connection.execute(
                    f"SELECT delivery_id FROM channel_observation_inbox {predicate} "
                    "ORDER BY admission_order",
                    params,
                )
            ]

        return await self._state.database.read_async(select)

    async def freeze(self, owner: str, routing: ChannelSessionRouting) -> None:
        """Resolve the ended engine's remaining routes without waiting for open Runs."""
        for delivery_id in await self.ids(owner):
            await self.prepare(delivery_id, routing)

    async def recover(
        self,
        owner: str | None = None,
        *,
        exclude_owners: Callable[[], set[str]] | None = None,
    ) -> None:
        """Finish earlier admissions, including those of deleted/disabled Channels."""
        blocked: set[str] = set()
        for delivery_id in await self.ids(owner):
            observation = await self._get(delivery_id)
            if observation is None or observation.generation_id in blocked:
                continue
            if exclude_owners is not None and observation.owner in exclude_owners():
                continue
            try:
                if not observation.routed and await self._can_follow_pointer(observation):
                    routing = ChannelSessionRouting(observation.config, self._sessions, self._state)
                    await self.prepare(delivery_id, routing)
                await self.deliver(delivery_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                blocked.add(observation.generation_id)
                _LOGGER.error(
                    "Cannot persist observed Channel context (channel=%s error_type=%s)",
                    observation.config.id,
                    type(error).__name__,
                    exc_info=True,
                )

    async def _can_follow_pointer(self, observation: _Observation) -> bool:
        if self._config_reader is None:
            return True
        try:
            current = await self._state.run_async(self._config_reader, observation.config.id)
        except ChannelError:
            return False
        return (
            current.agent_id == observation.config.agent_id
            and current.platform == observation.config.platform
        )


def _decode(row: Any) -> _Observation:
    conversation = json.loads(row["conversation_json"])
    known = {field.name for field in fields(ConversationFacts)}
    return _Observation(
        str(row["delivery_id"]),
        str(row["owner"]),
        ChannelConfig.from_dict(json.loads(row["config_json"])),
        ConversationFacts(**{key: value for key, value in conversation.items() if key in known}),
        tuple(json.loads(row["notes_json"])),
        str(row["generation_id"]),
        str(row["fallback_session_id"]),
        bool(row["routed"]),
    )
