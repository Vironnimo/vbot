"""The user gives the Agent of one Session a Secret: ``/secret`` and its WebUI form.

A pending ``secrets`` request of the same name in the Session is answered, and
its Tool result tells the Agent. Otherwise the Secret is replaced or created,
and a note tells the Agent of a Session Secret or a replaced value; the System
Prompt block tells it of a new Secret granted to the Agent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from core.secrets.agent_secrets import AgentSecret, Lifetime, SecretKind, normalize_secret_name
from core.sessions import SessionAddress
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.secrets.agent_secrets import AgentSecrets
    from core.secrets.requests import SecretRequests
    from core.sessions import ChatSessionManager

__all__ = ["GivenSecret", "give_secret"]

_LOGGER = get_logger("chat.secrets")


@dataclass(frozen=True)
class GivenSecret:
    secret: AgentSecret
    replaced: bool
    answered_request: bool


async def give_secret(
    *,
    secrets: AgentSecrets,
    requests: SecretRequests,
    sessions: ChatSessionManager | None,
    address: SessionAddress,
    name: str,
    kind: SecretKind,
    values: Mapping[str, str],
    lifetime: Lifetime = "session",
    expires_at: datetime | None = None,
    description: str = "",
) -> GivenSecret:
    """Store the value; :class:`SecretRecordError` when nothing was stored."""
    secret_name = normalize_secret_name(name)
    pending = requests.pending_for(address.session_id, secret_name)
    if pending is not None:
        secret = await requests.answer(pending.id, values, lifetime=lifetime, expires_at=expires_at)
        return GivenSecret(secret, replaced=pending.replaces is not None, answered_request=True)
    existing = secrets.lookup(address.agent_id, address.session_id, secret_name)
    if existing is not None and existing.source is None:
        secret = await asyncio.to_thread(secrets.update, existing.id, actor="user", values=values)
        given = GivenSecret(secret, replaced=True, answered_request=False)
    else:
        secret = await asyncio.to_thread(
            secrets.create,
            name=secret_name,
            kind=kind,
            values=values,
            actor="user",
            lifetime=lifetime,
            session_id=address.session_id,
            expires_at=expires_at,
            agents=[address.agent_id],
            description=description,
        )
        given = GivenSecret(secret, replaced=False, answered_request=False)
    await _tell_agent(sessions, address, given)
    return given


async def _tell_agent(
    sessions: ChatSessionManager | None, address: SessionAddress, given: GivenSecret
) -> None:
    from core.tools.secrets import secret_line

    secret = given.secret
    if given.replaced:
        note = f"The user replaced the value of the Secret {secret.name}."
    elif secret.session_id is not None:
        note = (
            f"The user gave you a Secret for this session: {secret_line(secret)[2:]} "
            "Write its reference where the value belongs; vBot inserts the value there."
        )
    else:
        return
    if sessions is None:
        return
    try:
        async with sessions.write_lock(address):
            session = await sessions.get_async(address)
            await session.add_note_async(note)
    except Exception:
        _LOGGER.warning(
            "Could not tell the Agent about Secret %s (agent=%s session=%s)",
            secret.name,
            address.agent_id,
            address.session_id,
            exc_info=True,
        )
