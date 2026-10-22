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

from core.chat._run_state import is_subagent_session
from core.projects.address import format_agent_address
from core.secrets.agent_secrets import (
    AgentSecret,
    Lifetime,
    SecretKind,
    SecretRecordError,
    main_field_name,
    normalize_secret_name,
)
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
    # The lifetime the replaced Secret had, when the replacement changed it.
    previous_lifetime: Lifetime | None = None


async def give_secret(
    *,
    secrets: AgentSecrets,
    requests: SecretRequests,
    sessions: ChatSessionManager | None,
    address: SessionAddress,
    name: str,
    kind: SecretKind,
    values: Mapping[str, str],
    lifetime: Lifetime | None = None,
    expires_at: datetime | None = None,
    description: str = "",
    value: str | None = None,
) -> GivenSecret:
    """Store the values; :class:`SecretRecordError` when nothing was stored.

    ``value`` is one value given without a field name (``/secret``): it goes to
    the main field of the Secret it replaces, or of a new Secret of ``kind``.
    Without ``lifetime`` and ``expires_at``, a replaced Secret keeps its
    lifetime and grants, and a new one lasts for the Session (user decision
    2026-10-22).
    """
    secret_name = normalize_secret_name(name)
    if lifetime is None and expires_at is not None:
        lifetime = "until"
    pending = requests.pending_for(address, secret_name)
    if pending is not None:
        current = secrets.get(pending.replaces) if pending.replaces else None
        secret = await requests.answer(
            pending.id,
            _with_value(values, value, current, pending.kind),
            lifetime=lifetime,
            expires_at=expires_at,
        )
        return GivenSecret(
            secret,
            replaced=pending.replaces is not None,
            answered_request=True,
            previous_lifetime=_changed_lifetime(current, secret),
        )
    passed_only = sessions is not None and await asyncio.to_thread(
        is_subagent_session, sessions, address
    )
    existing = secrets.lookup(address, secret_name, passed_only=passed_only)
    if existing is not None and existing.source is None:
        secret = await asyncio.to_thread(
            secrets.update,
            existing.id,
            actor="user",
            values=_with_value(values, value, existing, kind),
            lifetime=lifetime,
            given_to=address if lifetime is not None else None,
            expires_at=expires_at,
        )
        given = GivenSecret(
            secret,
            replaced=True,
            answered_request=False,
            previous_lifetime=_changed_lifetime(existing, secret),
        )
    else:
        secret = await asyncio.to_thread(
            secrets.create,
            name=secret_name,
            kind=kind,
            values=_with_value(values, value, None, kind),
            actor="user",
            lifetime=lifetime or "session",
            session_id=address.session_id,
            expires_at=expires_at,
            agents=[format_agent_address(address.agent_id, address.project_id)],
            description=description,
        )
        given = GivenSecret(secret, replaced=False, answered_request=False)
    await _tell_agent(sessions, address, given, passed_only=passed_only)
    return given


def _changed_lifetime(before: AgentSecret | None, after: AgentSecret) -> Lifetime | None:
    if before is None or before.lifetime == after.lifetime:
        return None
    return before.lifetime


def _with_value(
    values: Mapping[str, str], value: str | None, secret: AgentSecret | None, kind: SecretKind
) -> Mapping[str, str]:
    """``values`` plus the unnamed ``value`` in the main field of ``secret``, else of ``kind``."""
    if value is None:
        return values
    if secret is None:
        return {**values, main_field_name(kind): value}
    main = secret.default_field()
    if main is None:
        raise SecretRecordError(
            f"{secret.name} has several fields; give each value by its field name in the form."
        )
    return {**values, main.name: value}


async def _tell_agent(
    sessions: ChatSessionManager | None,
    address: SessionAddress,
    given: GivenSecret,
    *,
    passed_only: bool,
) -> None:
    from core.tools.secrets import secret_line

    secret = given.secret
    if given.replaced:
        note = f"The user replaced the Secret: {secret_line(secret)[2:]}"
    # A Sub-Agent Session has no System Prompt block that would list it.
    elif secret.session_id is not None or passed_only:
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
