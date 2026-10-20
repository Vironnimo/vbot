"""Secret requests: an Agent asks the user for a Secret and waits for the answer.

A request is pending while its Tool call waits. The WebUI and TUI show it as a
masked form; the user's answer is stored straight into :class:`AgentSecrets`,
so the value never passes through the Agent, the Run or the Session history.
``/secret NAME value`` answers a pending request of the same name in the same
Session the same way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

from core.secrets._agent_secret_model import (
    KIND_FIELDS,
    OPTIONAL_KIND_FIELDS,
    AgentSecret,
    Lifetime,
    SecretKind,
    SecretRecordError,
    normalize_secret_name,
)
from core.secrets.agent_secrets import AgentSecrets
from core.utils.ids import new_id
from core.utils.logging import get_logger

__all__ = [
    "REQUEST_TIMEOUT_SECONDS",
    "PendingSecretRequest",
    "SecretRequestOutcome",
    "SecretRequests",
]

_LOGGER = get_logger("secrets.requests")

#: How long a request waits for the user before it is withdrawn.
REQUEST_TIMEOUT_SECONDS: Final = 600.0

RequestStatus = Literal["stored", "declined", "expired"]


@dataclass(frozen=True)
class SecretRequestOutcome:
    status: RequestStatus
    secret: AgentSecret | None = None
    replaced: bool = False


@dataclass
class PendingSecretRequest:
    id: str
    agent_id: str
    session_id: str
    project_id: str | None
    name: str
    kind: SecretKind
    description: str
    lifetime: Lifetime
    created_at: datetime
    expires_at: datetime
    # The Secret an answer replaces the values of, when the name exists already.
    replaces: str | None
    outcome: asyncio.Future[SecretRequestOutcome] = field(repr=False)

    def describe(self) -> dict[str, Any]:
        """The form the user answers: never a value."""
        optional = OPTIONAL_KIND_FIELDS[self.kind]
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "lifetime": self.lifetime,
            "replaces": self.replaces is not None,
            "fields": [
                {"name": name, "concealed": concealed, "optional": name in optional}
                for name, concealed in KIND_FIELDS[self.kind]
            ],
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }


class SecretRequests:
    """The pending Secret requests of one process.

    ``on_change`` receives the id of each request that appears or goes away.
    Requests live only in memory: a restart ends the Tool calls that wait.
    """

    def __init__(
        self,
        secrets: AgentSecrets,
        *,
        clock: Callable[[], datetime],
        on_change: Callable[[str], None] | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._secrets = secrets
        self._clock = clock
        self._on_change = on_change
        self._timeout = timeout
        self._pending: dict[str, PendingSecretRequest] = {}

    def set_change_listener(self, on_change: Callable[[str], None] | None) -> None:
        self._on_change = on_change

    async def request(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None,
        name: str,
        kind: SecretKind,
        description: str,
        lifetime: Lifetime,
    ) -> SecretRequestOutcome:
        """Wait for the user's answer; ``expired`` after the timeout.

        A second request for the same name in the same Session replaces the
        first, whose caller then gets ``declined``.
        """
        secret_name = normalize_secret_name(name)
        existing = self._secrets.lookup(agent_id, session_id, secret_name)
        now = self._clock()
        pending = PendingSecretRequest(
            id=new_id("sreq", claim=lambda item: item not in self._pending),
            agent_id=agent_id,
            session_id=session_id,
            project_id=project_id,
            name=secret_name,
            kind=existing.kind if existing is not None else kind,
            description=description or (existing.description if existing is not None else ""),
            lifetime=lifetime,
            created_at=now,
            expires_at=now + timedelta(seconds=self._timeout),
            replaces=existing.id if existing is not None else None,
            outcome=asyncio.get_running_loop().create_future(),
        )
        for other in list(self._pending.values()):
            if other.session_id == session_id and other.name == secret_name:
                self._finish(other, SecretRequestOutcome("declined"))
        self._pending[pending.id] = pending
        self._changed(pending.id)
        _LOGGER.info("Secret %s requested (agent=%s session=%s)", secret_name, agent_id, session_id)
        try:
            async with asyncio.timeout(self._timeout):
                return await asyncio.shield(pending.outcome)
        except TimeoutError:
            _LOGGER.info("Secret request for %s expired unanswered", secret_name)
            return SecretRequestOutcome("expired")
        finally:
            if self._pending.pop(pending.id, None) is not None:
                self._changed(pending.id)

    def list(self, session_id: str | None = None) -> list[dict[str, Any]]:
        return [
            item.describe()
            for item in self._pending.values()
            if session_id is None or item.session_id == session_id
        ]

    def pending_for(self, session_id: str, name: str) -> PendingSecretRequest | None:
        try:
            secret_name = normalize_secret_name(name)
        except SecretRecordError:
            return None
        return next(
            (
                item
                for item in self._pending.values()
                if item.session_id == session_id and item.name == secret_name
            ),
            None,
        )

    async def answer(
        self,
        request_id: str,
        values: Mapping[str, str],
        *,
        lifetime: Lifetime | None = None,
        expires_at: datetime | None = None,
    ) -> AgentSecret:
        """Store the user's answer; :class:`SecretRecordError` leaves the request pending."""
        pending = self._pending.get(request_id)
        if pending is None or pending.outcome.done():
            raise SecretRecordError("This request is no longer waiting; the Agent stopped asking.")
        chosen = lifetime or pending.lifetime
        if expires_at is not None and chosen == "permanent":
            chosen = "until"
        secret, replaced = await asyncio.to_thread(
            self._store, pending, dict(values), chosen, expires_at
        )
        self._finish(pending, SecretRequestOutcome("stored", secret, replaced))
        return secret

    def decline(self, request_id: str) -> None:
        pending = self._pending.get(request_id)
        if pending is None:
            raise SecretRecordError("This request is no longer waiting; the Agent stopped asking.")
        _LOGGER.info("Secret request for %s declined", pending.name)
        self._finish(pending, SecretRequestOutcome("declined"))

    def _store(
        self,
        pending: PendingSecretRequest,
        values: dict[str, str],
        lifetime: Lifetime,
        expires_at: datetime | None,
    ) -> tuple[AgentSecret, bool]:
        current = self._secrets.get(pending.replaces) if pending.replaces else None
        if current is not None and current.source is None:
            return self._secrets.update(current.id, actor="user", values=values), True
        return (
            self._secrets.create(
                name=pending.name,
                kind=pending.kind,
                values=values,
                actor="user",
                lifetime=lifetime,
                session_id=pending.session_id,
                expires_at=expires_at,
                agents=[pending.agent_id],
                description=pending.description,
            ),
            False,
        )

    def _finish(self, pending: PendingSecretRequest, outcome: SecretRequestOutcome) -> None:
        if not pending.outcome.done():
            pending.outcome.set_result(outcome)
        if self._pending.pop(pending.id, None) is not None:
            self._changed(pending.id)

    def _changed(self, request_id: str) -> None:
        if self._on_change is None:
            return
        try:
            self._on_change(request_id)
        except Exception:
            _LOGGER.exception("Secret request change listener failed")
