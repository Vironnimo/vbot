"""Voice command calls on top of the Desktop speech server client.

:class:`VoiceServerClient` extends :class:`desktop.speech.server_client.SpeechServerClient`
(readiness, preparation, transcription, upload budget) with the operations a
spoken command needs after transcription: Agent lookup, Session resolution and
sending the command text. Failures raise the speech client's typed errors with
these codes:

- ``target_agent_unavailable``: an unknown Agent or any other ``agent.get``
  refusal; any RPC refusal with ``agent_not_found`` maps to it as well.
- ``session_resolution_failed``: ``session.list`` or ``session.create`` failed.
- ``send_failed``: ``chat.stream`` refused the command.

Retries: ``agent.get`` and ``session.list`` are idempotent reads and make up to
:data:`MAX_ATTEMPTS` attempts. Mutations (``session.create``, ``chat.stream``)
make exactly one attempt: after a lost response the server may already have
committed a Session or Run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, override

from desktop.speech.server_client import (
    MAX_ATTEMPTS,
    SpeechServerClient,
    SpeechServerInvalidResponse,
    SpeechServerRejected,
)
from desktop.wakeword.config import SESSION_BEHAVIOR_NEW, SESSION_BEHAVIORS

ERROR_TARGET_AGENT_UNAVAILABLE = "target_agent_unavailable"
ERROR_SESSION_RESOLUTION_FAILED = "session_resolution_failed"
ERROR_SEND_FAILED = "send_failed"

_RPC_AGENT_NOT_FOUND = "agent_not_found"
_SPEECH_INPUT_ORIGIN = "speech_transcription"


class VoiceServerClient(SpeechServerClient):
    """Typed, retrying access to one vBot server for Voice commands."""

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        """Return the server's ``agent.get`` result for one Agent.

        An unknown Agent or any other refusal raises
        :class:`SpeechServerRejected` with ``target_agent_unavailable``.
        """
        return self._rpc(
            "agent.get",
            {"id": agent_id},
            error_code=ERROR_TARGET_AGENT_UNAVAILABLE,
            attempts=MAX_ATTEMPTS,
        )

    def resolve_session(self, agent_id: str, session_behavior: str) -> str:
        """Return the Session a command for ``agent_id`` is sent to.

        ``new`` creates a Session and makes it the Agent's current one.
        ``active`` uses the Agent's current Session; an Agent without one uses
        its most recently active conversation Session (subagent, reflection
        and scheduled Sessions excluded), and a new Session when it has none.
        """
        if session_behavior not in SESSION_BEHAVIORS:
            raise ValueError(f"Unknown Session behavior: {session_behavior}")
        if session_behavior == SESSION_BEHAVIOR_NEW:
            return self._create_session(agent_id)
        current_session_id = _non_empty_string(self.get_agent(agent_id).get("current_session_id"))
        if current_session_id is not None:
            return current_session_id
        listed = self._rpc(
            "session.list",
            {
                "agent_id": agent_id,
                "limit": 1,
                "include_subagents": False,
                "include_memory_reflections": False,
                "include_skill_reflections": False,
                "include_cron": False,
                "include_channels": False,
            },
            error_code=ERROR_SESSION_RESOLUTION_FAILED,
            attempts=MAX_ATTEMPTS,
        )
        sessions = listed.get("sessions")
        if not isinstance(sessions, list):
            raise SpeechServerInvalidResponse(
                ERROR_SESSION_RESOLUTION_FAILED, "session.list returned no Session list"
            )
        newest_session_id = _newest_session_id(sessions)
        if newest_session_id is not None:
            return newest_session_id
        return self._create_session(agent_id)

    def send_command(self, agent_id: str, session_id: str, text: str) -> None:
        """Send the command text to the Session as a spoken Chat message (one attempt)."""
        self._rpc(
            "chat.stream",
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "content": text,
                "input_origin": _SPEECH_INPUT_ORIGIN,
            },
            error_code=ERROR_SEND_FAILED,
            attempts=1,
        )

    @override
    def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        error_code: str,
        attempts: int,
    ) -> dict[str, Any]:
        try:
            return super()._rpc(method, params, error_code=error_code, attempts=attempts)
        except SpeechServerRejected as exc:
            if exc.rpc_code != _RPC_AGENT_NOT_FOUND:
                raise
            if exc.error_code == ERROR_TARGET_AGENT_UNAVAILABLE:
                raise
            raise SpeechServerRejected(
                ERROR_TARGET_AGENT_UNAVAILABLE,
                str(exc),
                status_code=exc.status_code,
                rpc_code=exc.rpc_code,
            ) from exc

    def _create_session(self, agent_id: str) -> str:
        result = self._rpc(
            "session.create",
            {"agent_id": agent_id, "make_current": True},
            error_code=ERROR_SESSION_RESOLUTION_FAILED,
            attempts=1,
        )
        session_id = _non_empty_string(result.get("session_id"))
        if session_id is None:
            raise SpeechServerInvalidResponse(
                ERROR_SESSION_RESOLUTION_FAILED, "session.create returned no Session id"
            )
        return session_id


def _newest_session_id(sessions: list[Any]) -> str | None:
    """Return the id of the most recently active listed Session."""
    candidates = [
        (str(session.get("last_active_at") or ""), session_id)
        for session in sessions
        if isinstance(session, Mapping)
        and (session_id := _non_empty_string(session.get("id"))) is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def _non_empty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
