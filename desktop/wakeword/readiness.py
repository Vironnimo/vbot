"""Repeatable, cancellation-aware checks for command phrase readiness.

A listener owns one worker. Routing requests replace its next snapshot; network
work happens outside its lock, and results return through the controller's
generation admission before becoming visible status. A result with a transient
problem (the server unreachable, the readiness call failing) is checked again
after :data:`RECHECK_SECONDS`, so it clears by itself once the server is back.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from desktop.speech.server_client import (
    ERROR_SERVER_UNREACHABLE,
    ERROR_SPEECH_TO_TEXT_READINESS_FAILED,
    SpeechRequestCancelled,
    SpeechServerError,
)
from desktop.wakeword.config import CommandAction
from desktop.wakeword.server_client import VoiceServerClient

logger = logging.getLogger("vbot.desktop.wakeword.controller")

RECHECK_SECONDS = 30.0
_TRANSIENT_PROBLEMS = frozenset({ERROR_SERVER_UNREACHABLE, ERROR_SPEECH_TO_TEXT_READINESS_FAILED})


@dataclass(frozen=True)
class ReadinessResult:
    speech_problem: str | None = None
    agent_problems: dict[str, str | None] = field(default_factory=dict)
    budget_bytes: int | None = None

    @property
    def transient(self) -> bool:
        """Whether a problem may clear by itself (an unreachable server, a failed check)."""
        return any(
            problem in _TRANSIENT_PROBLEMS
            for problem in (self.speech_problem, *self.agent_problems.values())
        )


class ReadinessWorker:
    """Serialize checks, repeating with the newest routing after an in-flight check.

    While the last result has a transient problem, the check repeats every
    ``recheck_seconds`` until it clears, the routing changes or the stop event is set.
    """

    def __init__(
        self,
        client: VoiceServerClient,
        stop: threading.Event,
        publish: Callable[[ReadinessResult], None],
        *,
        recheck_seconds: float = RECHECK_SECONDS,
    ) -> None:
        self._client = client
        self._stop = stop
        self._publish = publish
        self._recheck_seconds = recheck_seconds
        self._condition = threading.Condition()
        self._actions: tuple[CommandAction, ...] = ()
        self._version = 0
        self._running = False
        self._thread: threading.Thread | None = None

    def request(self, actions: Sequence[CommandAction]) -> None:
        with self._condition:
            if self._stop.is_set():
                return
            self._actions = tuple(actions)
            self._version += 1
            if self._running:
                self._condition.notify_all()  # ends a wait for a recheck
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._run, name="vbot-voice-readiness", daemon=True
            )
            self._thread.start()

    def join(self, timeout: float) -> bool:
        """Wait for the worker after the stop event; ``True`` once it ended."""
        with self._condition:
            self._condition.notify_all()  # ends a wait for a recheck
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _run(self) -> None:
        while True:
            with self._condition:
                version, actions = self._version, self._actions
            try:
                speech_problem = self._client.speech_readiness() if actions else None
                agent_ids = sorted({action.agent_id for action in actions if action.agent_id})
                agent_problems = {
                    agent_id: _agent_problem(self._client, agent_id) for agent_id in agent_ids
                }
                budget = self._client.upload_budget_bytes() if actions else None
            except SpeechRequestCancelled:
                return
            except Exception:
                if not self._stop.is_set():
                    logger.exception("Voice readiness check failed")
                with self._condition:
                    self._running = False
                return
            result = ReadinessResult(speech_problem, agent_problems, budget)
            self._publish(result)
            with self._condition:
                if version == self._version and result.transient and not self._stop.is_set():
                    # A new request or join() ends the wait early.
                    self._condition.wait(self._recheck_seconds)
                if self._stop.is_set() or (version == self._version and not result.transient):
                    self._running = False
                    return


def _agent_problem(client: VoiceServerClient, agent_id: str) -> str | None:
    try:
        client.get_agent(agent_id)
    except SpeechServerError as exc:
        return exc.error_code
    return None
