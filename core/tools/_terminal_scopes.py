"""Terminal admission and lifecycle-scope fences."""

from __future__ import annotations

import asyncio
from collections.abc import (
    Callable,
)

from core.runs import RunExecutionOwner
from core.utils.workers import settle_before_cancelling

from ._terminal_session import (
    TerminalSession,
)
from ._terminal_state import (
    TERMINAL_MAX_LIVE_GLOBAL,
    TERMINAL_MAX_LIVE_PER_SESSION,
    TerminalCapacityError,
    TerminalClosedError,
    TerminalLaunchError,
    TerminalOwner,
)


class TerminalScopes:
    """Own admission limits and fences for Session and Extension cleanup."""

    def __init__(
        self,
        sessions: dict[str, TerminalSession],
        pending_spawns: dict[asyncio.Task[TerminalSession], TerminalOwner | None],
        pending_execution_spawns: dict[asyncio.Task[TerminalSession], RunExecutionOwner],
    ) -> None:
        self._sessions = sessions
        self._pending_spawns = pending_spawns
        self._pending_execution_spawns = pending_execution_spawns
        self._closing_scopes: list[Callable[[TerminalOwner], bool]] = []
        self._closed_execution_groups: set[tuple[str, str, str]] = set()

    def closing(self, owner: TerminalOwner | None) -> bool:
        return owner is not None and any(matches(owner) for matches in self._closing_scopes)

    def execution_closed(self, owner: RunExecutionOwner) -> bool:
        return (owner.extension, owner.group_id, owner.epoch) in self._closed_execution_groups

    async def close_execution_group(self, extension: str, group_id: str, epoch: str) -> None:
        """Drain exact execution-owned terminals without changing attachment authority.

        Callers close a group only after every Run of the group has settled.
        Admission stays closed while pending launches drain and owned terminals
        are terminated; a launch racing the drain is rejected. After the drain
        the admission marker is released.
        """
        key = (extension, group_id, epoch)
        self._closed_execution_groups.add(key)

        async def drain() -> None:
            pending = [
                task
                for task, owner in self._pending_execution_spawns.items()
                if (owner.extension, owner.group_id, owner.epoch) == key
            ]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            sessions = [
                session
                for session in self._sessions.values()
                if session.execution_owner is not None
                and (
                    session.execution_owner.extension,
                    session.execution_owner.group_id,
                    session.execution_owner.epoch,
                )
                == key
            ]
            results = await asyncio.gather(
                *(session.terminate(suppress_attention=True) for session in sessions),
                return_exceptions=True,
            )
            if failures := [result for result in results if isinstance(result, BaseException)]:
                raise failures[0]

        try:
            await settle_before_cancelling(drain())
        finally:
            # The group owner settles every Run of the group before closing its
            # resources, and each epoch key is used only once. With the launches
            # drained above, no launch for this key can arrive any more, so the
            # closed marker is released instead of accumulating for the server life.
            self._closed_execution_groups.discard(key)

    async def settle_transfers(self, matches: Callable[[TerminalOwner], bool]) -> None:
        while pending := [
            task
            for task, owner in self._pending_spawns.items()
            if not task.done() and owner is not None and matches(owner)
        ]:
            await asyncio.gather(
                *(asyncio.shield(task) for task in pending), return_exceptions=True
            )

    async def close_matching(self, matches: Callable[[TerminalOwner], bool]) -> int:
        """Fence matching launches, drain them and stop only matching lifecycle owners."""
        self._closing_scopes.append(matches)
        try:
            pending = [
                task
                for task, owner in self._pending_spawns.items()
                if not task.done() and owner is not None and matches(owner)
            ]
            terminating = [
                session
                for session in self._sessions.values()
                if session.lifecycle_owner is not None and matches(session.lifecycle_owner)
            ]
            stopped = len(pending) + sum(not session.finished for session in terminating)
            for session in self._sessions.values():
                attachment = session.attachment
                if session not in terminating and attachment is not None and matches(attachment):
                    stopped += not session.finished
                    session.detach()

            async def drain() -> None:
                # A native launch cannot be cancelled safely. Its publication
                # check sees the fence and disposes the child before returning.
                # Published terminals can stop while those launches are settling.
                results = await asyncio.gather(
                    asyncio.gather(*pending, return_exceptions=True),
                    *(session.terminate(suppress_attention=True) for session in terminating),
                    return_exceptions=True,
                )
                pending_results = results[0]
                failures = [result for result in results[1:] if isinstance(result, BaseException)]
                if isinstance(pending_results, list):
                    failures.extend(
                        result
                        for result in pending_results
                        if isinstance(result, BaseException)
                        and not isinstance(result, (TerminalClosedError, TerminalLaunchError))
                    )
                if failures:
                    raise failures[0]

            await settle_before_cancelling(drain())
            return stopped
        finally:
            self._closing_scopes.remove(matches)

    def enforce_capacity(self, owner: TerminalOwner | None) -> None:
        pending = [
            pending_owner for task, pending_owner in self._pending_spawns.items() if not task.done()
        ]
        live = [session for session in self._sessions.values() if not session.finished]
        # The live terminals the caller started: the ones it can stop.
        mine = tuple(
            session.info()
            for session in sorted(live, key=lambda item: item.started_at)
            if owner is not None and session.lifecycle_owner == owner
        )
        if len(live) + len(pending) >= TERMINAL_MAX_LIVE_GLOBAL:
            raise TerminalCapacityError(
                f"Live Terminal Session limit reached ({TERMINAL_MAX_LIVE_GLOBAL})",
                scope="global",
                limit=TERMINAL_MAX_LIVE_GLOBAL,
                terminals=mine,
            )
        owned = len(mine)
        owned += sum(1 for pending_owner in pending if owner is not None and pending_owner == owner)
        if owner is not None and owned >= TERMINAL_MAX_LIVE_PER_SESSION:
            raise TerminalCapacityError(
                "Live Terminal Session limit reached for this vBot Session "
                f"({TERMINAL_MAX_LIVE_PER_SESSION})",
                scope="session",
                limit=TERMINAL_MAX_LIVE_PER_SESSION,
                terminals=mine,
            )
