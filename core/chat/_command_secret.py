"""``/secret``: the user gives the Agent a Secret it uses without seeing it.

The command parses the text and hands the value to :func:`give_secret`; it
starts no Run and leaves no message in the Session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.chat._command_types import CommandExecutionContext, CommandFeedback, CommandOutcome
from core.chat.secret_giving import give_secret
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

if TYPE_CHECKING:
    from core.secrets.agent_secrets import AgentSecrets
    from core.secrets.requests import SecretRequests
    from core.sessions import ChatSessionManager

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
    try:
        given = await give_secret(
            secrets=secrets,
            requests=requests,
            sessions=sessions,
            address=SessionAddress(context.project_id, context.agent_id, context.session_id),
            name=name,
            kind=kind,
            values=values,
            lifetime=parsed.lifetime,
            expires_at=expires_at,
            description=parsed.description,
        )
    except SecretRecordError as error:
        return _notice(f"Nothing was stored. {error}", delete_user_message=True)
    return _notice(
        _feedback(given.secret, parsed.value, replaced=given.replaced), delete_user_message=True
    )


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
