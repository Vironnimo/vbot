"""``/secret``: the user gives the Agent a Secret it uses without seeing it.

The value goes straight into Agent Secrets. The command starts no Run and
leaves no message in the Session; the Agent learns of a Session Secret from a
note that names it without its value. A pending ``secrets`` request of the same
name in the same Session is answered instead, and its Tool result tells the
Agent.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from core.chat._command_types import CommandExecutionContext, CommandFeedback, CommandOutcome
from core.secrets.agent_secrets import (
    AgentSecret,
    SecretRecordError,
    normalize_secret_name,
)
from core.secrets.commands import (
    SECRET_COMMAND_USAGE,
    SecretCommandError,
    parse_secret_command,
)
from core.sessions import SessionAddress
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.secrets.agent_secrets import AgentSecrets
    from core.secrets.requests import SecretRequests
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("chat.commands")

# Shorter values are not recognized in output (``MIN_MASKED_LENGTH``).
_SHORT_VALUE = 8


def _notice(text: str, **facts: Any) -> CommandOutcome:
    return CommandOutcome(
        command="secret", feedback=CommandFeedback(kind="notice", text=text), facts=facts
    )


async def execute_secret(
    context: CommandExecutionContext,
    argument: str | None,
    *,
    secrets: AgentSecrets | None,
    requests: SecretRequests | None,
    sessions: ChatSessionManager | None,
) -> CommandOutcome:
    if secrets is None or requests is None:
        return _notice("Secrets are not available in this vBot.")
    try:
        parsed = parse_secret_command(argument or "")
    except SecretCommandError as error:
        return _notice(str(error))
    if parsed.name is None or not parsed.value:
        form = "" if context.reply_surface.kind == "channel" else " /secret alone opens a form."
        return _notice(f"{SECRET_COMMAND_USAGE}{form}")
    # From here on the message holds a value: every reply asks a Channel to remove it.
    try:
        name = normalize_secret_name(parsed.name)
    except SecretRecordError as error:
        return _notice(str(error), delete_user_message=True)
    kind = parsed.kind or ("text" if "\n" in parsed.value else "token")
    values = (
        {"password": parsed.value, **({"username": parsed.username} if parsed.username else {})}
        if kind == "login"
        else {"value": parsed.value}
    )
    now = secrets.now()
    expires_at = now + parsed.duration if parsed.duration is not None else None
    pending = requests.pending_for(context.session_id, name)
    replaced = False
    try:
        if pending is not None:
            secret = await requests.answer(
                pending.id, values, lifetime=parsed.lifetime, expires_at=expires_at
            )
        else:
            existing = secrets.lookup(context.agent_id, context.session_id, name)
            if existing is not None and existing.source is None:
                secret = await asyncio.to_thread(
                    secrets.update, existing.id, actor="user", values=values
                )
                replaced = True
            else:
                secret = await asyncio.to_thread(
                    secrets.create,
                    name=name,
                    kind=kind,
                    values=values,
                    actor="user",
                    lifetime=parsed.lifetime,
                    session_id=context.session_id,
                    expires_at=expires_at,
                    agents=[context.agent_id],
                    description=parsed.description,
                )
    except SecretRecordError as error:
        return _notice(f"Nothing was stored. {error}", delete_user_message=True)
    if pending is None:
        await _tell_agent(context, sessions, secret, replaced=replaced)
    return _notice(_feedback(secret, parsed.value, replaced=replaced), delete_user_message=True)


def _feedback(secret: AgentSecret, value: str, *, replaced: bool) -> str:
    lifetime = {
        "permanent": "permanently",
        "session": "for this session",
        "once": "for one use",
    }.get(secret.lifetime, "")
    if secret.lifetime == "until" and secret.expires_at is not None:
        lifetime = f"until {secret.expires_at.isoformat(timespec='minutes')}"
    text = (
        f"Replaced the value of {secret.name}."
        if replaced
        else f"Stored {secret.name} {lifetime}. The agent sees only {secret.reference()}."
    )
    if len(value) < _SHORT_VALUE:
        text += (
            f" The value has fewer than {_SHORT_VALUE} characters, so vBot cannot recognize it "
            "if a program prints it."
        )
    return text


async def _tell_agent(
    context: CommandExecutionContext,
    sessions: ChatSessionManager | None,
    secret: AgentSecret,
    *,
    replaced: bool,
) -> None:
    """A note for a Session Secret; the System Prompt block tells of the Agent's own."""
    from core.tools.secrets import secret_line

    if replaced:
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
    address = SessionAddress(context.project_id, context.agent_id, context.session_id)
    try:
        async with sessions.write_lock(address):
            session = await sessions.get_async(address)
            await session.add_note_async(note)
    except Exception:
        _LOGGER.warning(
            "Could not tell the Agent about Secret %s (agent=%s session=%s)",
            secret.name,
            context.agent_id,
            context.session_id,
            exc_info=True,
        )
