"""Wake pacing: participants with nothing to do are not woken for every post.

A completed Run in which the participant acted on nothing shows that the posts
it received needed nothing from it. Using no Tool, or Tools only to read the
Board, the Wiki or the Swarm state, is not acting: a delivered opening invites a
read, and reading it may show there is nothing to do. Until its quiet period
ends, only posts that address it and posts by the user wake it; each further
such Run doubles the period up to its maximum, and a Run that acts ends pacing.
Pacing lives in memory: after a restart every participant starts unpaced.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass

# Seconds of quiet after the first, second, third, and every later Run without acting.
QUIET_SECONDS = (30.0, 60.0, 120.0, 240.0)
# Routes that still wake a quiet participant; the Store adds posts by the user.
ADDRESSED_ROUTES = frozenset({"ping"})


@dataclass
class _Quiet:
    level: int
    timer: asyncio.TimerHandle | None


class WakePacing:
    """Track quiet periods per participant and report when one ends."""

    def __init__(self, quiet_ended: Callable[[str], None]) -> None:
        self._quiet_ended = quiet_ended
        self._quiet: dict[tuple[str, str], _Quiet] = {}
        self._acting: dict[tuple[str, str], str] = {}
        self._reading: dict[tuple[str, str], set[str]] = {}

    def read_only(self, swarm_id: str, participant_id: str, call_id: str) -> None:
        """Record a Tool call that only read; its batch is reported to ``tools_used``."""

        self._reading.setdefault((swarm_id, participant_id), set()).add(call_id)

    def tools_used(
        self, swarm_id: str, participant_id: str, run_id: str, call_ids: Iterable[str]
    ) -> None:
        """Record a finished Tool batch; any call not recorded as read-only is acting."""

        key = (swarm_id, participant_id)
        reading = self._reading.pop(key, set())
        if any(call_id not in reading for call_id in call_ids):
            self._acting[key] = run_id

    def run_finished(
        self, swarm_id: str, participant_id: str, run_id: str, *, completed: bool
    ) -> None:
        """Start or extend a quiet period after a completed Run that did not act."""

        key = (swarm_id, participant_id)
        acted = self._acting.pop(key, None) == run_id
        self._reading.pop(key, None)
        previous = self._quiet.pop(key, None)
        if previous is not None and previous.timer is not None:
            previous.timer.cancel()
        if acted or not completed:
            return
        level = min((previous.level if previous is not None else 0) + 1, len(QUIET_SECONDS))
        state = _Quiet(level, None)
        state.timer = asyncio.get_running_loop().call_later(
            QUIET_SECONDS[level - 1], self._end, key, state
        )
        self._quiet[key] = state

    def wake_routes(self, swarm_id: str, participant_id: str) -> frozenset[str] | None:
        """Return the routes that may wake the participant now; ``None`` means all."""

        state = self._quiet.get((swarm_id, participant_id))
        return ADDRESSED_ROUTES if state is not None and state.timer is not None else None

    def forget(self, swarm_id: str) -> None:
        for key in [key for key in self._quiet if key[0] == swarm_id]:
            timer = self._quiet.pop(key).timer
            if timer is not None:
                timer.cancel()
        for table in (self._acting, self._reading):
            for key in [key for key in table if key[0] == swarm_id]:
                del table[key]

    def close(self) -> None:
        for state in self._quiet.values():
            if state.timer is not None:
                state.timer.cancel()
        self._quiet.clear()
        self._acting.clear()
        self._reading.clear()

    def _end(self, key: tuple[str, str], state: _Quiet) -> None:
        # The level stays, so the next Run without acting waits longer.
        if self._quiet.get(key) is state:
            state.timer = None
            self._quiet_ended(key[0])


__all__ = ["ADDRESSED_ROUTES", "QUIET_SECONDS", "WakePacing"]
