"""``/secret``: the user gives the Agent a Secret it uses without seeing it.

The command parses the text and hands the value to :func:`give_secret`; it
starts no Run and leaves no message in the Session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.chat._command_types import CommandExecutionContext, CommandFeedback, CommandOutcome
from core.chat.secret_giving import give_secret
from core.secrets.agent_secrets import (
    AgentSecret,
    Lifetime,
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


def _notice(text: str) -> CommandOutcome:
    return CommandOutcome(command="secret", feedback=CommandFeedback(kind="notice", text=text))


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
    try:
        name = normalize_secret_name(parsed.name)
    except SecretRecordError as error:
        return _notice(str(error))
    kind = parsed.kind or ("text" if "\n" in parsed.value else "token")
    # The value goes to the main field of the Secret it replaces or creates.
    values = {"username": parsed.username} if parsed.username else {}
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
            value=parsed.value,
            lifetime=parsed.lifetime,
            expires_at=expires_at,
            description=parsed.description,
        )
    except SecretRecordError as error:
        return _notice(f"Nothing was stored. {error}")
    return _notice(
        _feedback(
            given.secret, parsed.value, replaced=given.replaced, previous=given.previous_lifetime
        )
    )


_LIFETIME_WORDS = {"permanent": "permanently", "session": "for this session", "once": "for one use"}


def _lifetime_words(secret: AgentSecret) -> str:
    if secret.lifetime == "until" and secret.expires_at is not None:
        return f"until {secret.expires_at.isoformat(timespec='minutes')}"
    return _LIFETIME_WORDS.get(secret.lifetime, "")


def _feedback(
    secret: AgentSecret, value: str, *, replaced: bool, previous: Lifetime | None = None
) -> str:
    lifetime = _lifetime_words(secret)
    if not replaced:
        text = f"Stored {secret.name} {lifetime}. The agent sees only {secret.reference()}."
    elif previous is None:
        text = f"Replaced the value of {secret.name}."
    else:
        before = "for a limited time" if previous == "until" else _LIFETIME_WORDS[previous]
        text = (
            f"Replaced the value of {secret.name}. It is now kept {lifetime} instead of {before}."
        )
    if len(value) < _SHORT_VALUE:
        text += (
            f" The value has fewer than {_SHORT_VALUE} characters, so vBot cannot recognize it "
            "if a program prints it."
        )
    return text
