"""The ``secrets`` Tool: an Agent lists, requests and generates Secrets.

Values never reach the Agent. ``request`` asks the user through
:class:`core.secrets.requests.SecretRequests` and waits; ``generate`` stores a
random password the Agent then uses by reference.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Final

from core.projects.address import format_agent_address
from core.runs import RunKind
from core.secrets.agent_secrets import (
    DEFAULT_PASSWORD_SYMBOLS,
    AgentSecret,
    SecretField,
    SecretRecordError,
    normalize_secret_name,
)
from core.secrets.commands import secret_command_example
from core.secrets.requests import REQUEST_TIMEOUT_SECONDS
from core.sessions import SessionAddress
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.secrets.agent_secrets import AgentSecrets, Lifetime, SecretKind
    from core.secrets.requests import SecretRequests
    from core.tools.tools import ToolPromptBlockRegistry

_LOGGER = get_logger("tools.secrets")

SECRETS_TOOL_NAME: Final = "secrets"
# Reasons for every sentence: ``.vorch/domain-maps/secrets.md`` -> Agent-facing text.
SECRETS_TOOL_DESCRIPTION: Final = (
    "Manage Secrets: passwords, API keys and other values you use by reference without "
    "seeing them. When a task needs such a value you do not have, request it here instead "
    "of asking the user to write it in the chat."
)
SECRETS_TOOL_PARAMETERS: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "request", "generate"],
            "description": (
                "request shows the user a form and waits up to 10 minutes for the answer; "
                "requesting a name you have replaces its value. generate stores a new random "
                "password, for registering an account or changing a password."
            ),
        },
        "name": {
            "type": "string",
            "description": "Required for request and generate, such as GITHUB or STRIPE_API_KEY.",
        },
        "kind": {
            "type": "string",
            "enum": ["token", "login", "text"],
            "description": (
                "For request: token for one key or code, login for a username and password, "
                "text for multi-line values such as an SSH key. Omit for token."
            ),
        },
        "description": {
            "type": "string",
            "description": (
                "For request and generate: what the Secret is for, shown to the user, such as "
                "GitHub account for pushing to vbot."
            ),
        },
        "lifetime": {
            "type": "string",
            "enum": ["session", "permanent", "once"],
            "description": (
                "For request: the lifetime you suggest; the user decides. session ends with "
                "this session, once ends after the first use (one-time codes). Omit for session."
            ),
        },
        "username": {
            "type": "string",
            "description": "For generate: the account's username or email. Omit if it has none.",
        },
        "length": {"type": "integer", "default": 24, "description": "For generate."},
        "symbols": {
            "type": "string",
            "description": (
                'For generate: the symbols the password can contain; "" for letters and digits '
                "only. Omit to allow common ASCII symbols."
            ),
        },
    },
    "required": ["action"],
}
SECRETS_TOOL_SUMMARY: Final = "Use passwords and API keys without seeing them."

_ACTIONS: Final = {
    "list": "list",
    "show": "list",
    "get": "list",
    "request": "request",
    "ask": "request",
    "generate": "generate",
}
_KINDS: Final[dict[str, SecretKind]] = {
    "token": "token",
    "key": "token",
    "api_key": "token",
    "apikey": "token",
    "code": "token",
    "login": "login",
    "password": "login",
    "account": "login",
    "credentials": "login",
    "text": "text",
    "multiline": "text",
    "ssh_key": "text",
    "note": "text",
}
_LIFETIMES: Final[dict[str, Lifetime]] = {
    "session": "session",
    "temporary": "session",
    "permanent": "permanent",
    "persistent": "permanent",
    "forever": "permanent",
    "once": "once",
    "one_time": "once",
    "onetime": "once",
    "single_use": "once",
}
_ALIASES: Final = {
    "secret": "name",
    "secret_name": "name",
    "key": "name",
    "type": "kind",
    "reason": "description",
    "purpose": "description",
    "user": "username",
    "email": "username",
    "login": "username",
}
_SINK_TEXT: Final = {
    "environment": "shell env_keys",
    "terminal": "terminal input",
    "files": "file contents",
    "typing": "typed text",
    "external_tools": "external Tool arguments",
}
_LIFETIME_TEXT: Final = {
    "session": "this session",
    "once": "one use, this session",
}
_USE_HINT: Final = (
    "Write a reference where the value belongs; vBot puts the value there, and output shows "
    "it to you as the reference."
)
_ATTENDED_RUN_KINDS: Final = frozenset({RunKind.USER, RunKind.LIVE})
# Reasons for every sentence: ``.vorch/domain-maps/secrets.md`` -> Agent-facing text.
SECRETS_PROMPT_BLOCK_HEAD: Final = (
    "## Secrets\n\n"
    "You can use these Secrets without seeing their values. Write a reference such as "
    "{{secret:NAME.field}} where the value belongs, in the places listed for each Secret; "
    "vBot inserts the value there. Tool output shows values as their references. "
    "To write the text {{secret:NAME}} itself, such as in documentation, write "
    "{{!secret:NAME}}."
)
SECRETS_CATALOG_TITLE: Final = "Secrets"


def register_secrets_tool(
    registry: ToolRegistry,
    secrets: AgentSecrets,
    requests: SecretRequests,
    prompt_blocks: ToolPromptBlockRegistry | None = None,
) -> None:
    """Register ``secrets`` and the ``## Secrets`` block that lists an Agent's Secrets."""
    if prompt_blocks is not None:
        prompt_blocks.register(
            SECRETS_TOOL_NAME, render=lambda context: _render_prompt_block(context, secrets)
        )

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await _handle(secrets, requests, context, arguments)

    registry.register(
        SECRETS_TOOL_NAME,
        SECRETS_TOOL_DESCRIPTION,
        SECRETS_TOOL_PARAMETERS,
        handler,
        summary=SECRETS_TOOL_SUMMARY,
        open_input_schema=True,
        handler_validates_arguments=True,
        execution_slot_required=False,
        result_schema={"type": "object"},
        display=ToolDisplay(summary_builder=_summary),
    )


def _render_prompt_block(context: Any, secrets: AgentSecrets) -> Any:
    """The Agent's own Secrets as a catalog; Session Secrets reach it as notes instead."""
    from core.prompts import BlockCatalog, RenderedBlock

    # A Sub-Agent Session has only the Secrets passed to it; its Tool results list them.
    if context.subagent_session:
        return ""
    agent_id = format_agent_address(context.agent.id, context.agent_project_id)
    own = [
        secret
        for secret in secrets.all()
        if secret.session_id is None and agent_id in secret.agents
    ]
    if not own:
        return ""
    entries = tuple((secret.name, secret_line(secret)) for secret in own)
    text = "\n".join([SECRETS_PROMPT_BLOCK_HEAD, *(line for _, line in entries)])
    return RenderedBlock(
        text=text,
        catalog=BlockCatalog(
            title=SECRETS_CATALOG_TITLE, entries=entries, frame=SECRETS_PROMPT_BLOCK_HEAD
        ),
    )


def secret_line(secret: AgentSecret) -> str:
    """One Secret on one line, as results, the System Prompt block and notes show it."""
    fields = ", ".join(f"{item.name} {_shown(secret, item)}" for item in secret.fields)
    head = f"- {secret.name} ({secret.kind}"
    if secret.lifetime == "until" and secret.expires_at is not None:
        head += f", until {secret.expires_at.isoformat(timespec='minutes')}"
    elif secret.lifetime != "permanent":
        head += f", {_LIFETIME_TEXT[secret.lifetime]}"
    head += ")"
    if secret.description:
        head += f": {secret.description}"
    return f"{head}. {fields}. Usable in: {_usable_in(secret)}."


def _shown(secret: AgentSecret, item: SecretField) -> str:
    """A visible field's value, or a concealed field's reference."""
    if not item.concealed and item.value is not None:
        return item.value
    return secret.reference(item.name)


def _usable_in(secret: AgentSecret) -> str:
    places = []
    for sink in sorted(secret.sinks):
        if sink == "environment":
            variables = ", ".join(secret.environment_names().values())
            places.append(f'shell env_keys ["{secret.name}"] ({variables})')
        else:
            places.append(_SINK_TEXT[sink])
    return ", ".join(places) if places else "nowhere until the user allows a use in Settings"


def _summary(arguments: JsonObject) -> str | None:
    action = _ACTIONS.get(str(arguments.get("action", "")).strip().lower())
    name = arguments.get("name")
    if action is None:
        return None
    if action != "list" and isinstance(name, str) and name.strip():
        return f"{action} {name.strip()}"
    return action


async def _handle(
    secrets: AgentSecrets, requests: SecretRequests, context: ToolContext, raw: JsonObject
) -> JsonObject:
    arguments, unknown = _canonical(raw)
    action_text = str(arguments.get("action") or "").strip()
    action = _ACTIONS.get(action_text.lower())
    if action is None:
        received = f'Unknown action "{action_text}". ' if action_text else "action is missing. "
        return tool_failure("invalid_arguments", f"{received}Use one of: list, request, generate.")
    try:
        if action == "list":
            result = tool_success({"content": _list_text(secrets, context)})
        elif action == "request":
            result = await _request(secrets, requests, context, arguments)
        else:
            result = await _generate(secrets, context, arguments)
        return _with_note(result, unknown)
    except Exception:
        _LOGGER.error(
            "secrets %s failed (agent=%s session=%s)",
            action,
            context.agent_id,
            context.session_id,
            exc_info=True,
        )
        return tool_failure(
            "secrets_failed",
            f"The {action} call failed inside vBot, and nothing was stored. Try it once more; "
            "if it fails again, tell the user.",
        )


def _canonical(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """The arguments under their canonical names, and the fields without effect."""
    if not isinstance(raw, dict):
        return {}, []
    properties = SECRETS_TOOL_PARAMETERS["properties"]
    arguments: dict[str, Any] = {}
    unknown: list[str] = []
    for key, value in sorted(raw.items(), key=lambda item: item[0] not in properties):
        field = key if key in properties else _ALIASES.get(key)
        if field is None:
            if value not in (None, ""):
                unknown.append(key)
            continue
        if value is None or (value == "" and field not in {"symbols", "description"}):
            continue
        arguments.setdefault(field, value)
    return arguments, unknown


def _with_note(result: JsonObject, unknown: list[str]) -> JsonObject:
    if not unknown or not result.get("ok"):
        return result
    data = result["data"]
    note = f"{', '.join(unknown)} had no effect."
    if any(key.lower() in {"value", "password", "secret_value", "token"} for key in unknown):
        note += " Secrets get their values only from the user (request) or from generate."
    data["content"] = f"{data['content']}\n{note}"
    return result


def _list_text(secrets: AgentSecrets, context: ToolContext) -> str:
    usable = secrets.usable(
        SessionAddress(context.project_id, context.agent_id, context.session_id),
        passed_only=context.subagent_session,
    )
    if not usable:
        return "You have no Secrets. Use action request to ask the user for one."
    lines = ["Your Secrets:"]
    for secret in usable:
        lines.extend(_secret_lines(secrets, secret))
    lines.append(_USE_HINT)
    return "\n".join(lines)


def _secret_lines(secrets: AgentSecrets, secret: AgentSecret) -> list[str]:
    lines = [secret_line(secret)]
    if secret.source is not None:
        status = secrets.source_status(secret.source.source)
        if status.state != "available":
            lines.append(
                f"  It comes from {_source_label(secrets, secret.source.source)}, which is "
                f"{status.state}: ask the user "
                "to unlock it in Settings -> Secrets."
            )
    return lines


def _source_label(secrets: AgentSecrets, name: str) -> str:
    source = secrets.source(name)
    return source.label if source is not None else name


def _name(arguments: dict[str, Any], action: str) -> str | JsonObject:
    raw = arguments.get("name")
    if not isinstance(raw, str) or not raw.strip():
        return tool_failure(
            "invalid_arguments",
            f"Nothing was {'requested' if action == 'request' else 'generated'}. {action} needs "
            "name, the Secret name such as GITHUB.",
        )
    try:
        return normalize_secret_name(raw)
    except SecretRecordError as error:
        return tool_failure("invalid_arguments", f"Nothing was changed. {error}")


def _choice[T](
    arguments: dict[str, Any], field: str, choices: dict[str, T], default: T
) -> T | JsonObject:
    raw = arguments.get(field)
    if raw is None:
        return default
    chosen = choices.get(str(raw).strip().lower().replace("-", "_").replace(" ", "_"))
    if chosen is None:
        options = ", ".join(dict.fromkeys(str(item) for item in choices.values()))
        return tool_failure(
            "invalid_arguments",
            f'Nothing was changed. Unknown {field} "{raw}". Use one of: {options}.',
        )
    return chosen


async def _request(
    secrets: AgentSecrets,
    requests: SecretRequests,
    context: ToolContext,
    arguments: dict[str, Any],
) -> JsonObject:
    name = _name(arguments, "request")
    if isinstance(name, dict):
        return name
    kind = _choice(arguments, "kind", _KINDS, "token")
    if isinstance(kind, dict):
        return kind
    lifetime = _choice(arguments, "lifetime", _LIFETIMES, "session")
    if isinstance(lifetime, dict):
        return lifetime
    description = str(arguments.get("description") or "").strip()
    existing = secrets.lookup(
        SessionAddress(context.project_id, context.agent_id, context.session_id),
        name,
        passed_only=context.subagent_session,
    )
    if existing is not None and existing.source is not None:
        return tool_failure(
            "secret_from_source",
            f"Nothing was requested. {name} comes from "
            f"{_source_label(secrets, existing.source.source)}; ask the user "
            "in your reply to correct it there or to unlock it in Settings -> Secrets.",
        )
    surface = context.reply_surface
    example = secret_command_example(name, existing.kind if existing else kind, lifetime)
    if surface is not None and surface.kind == "channel":
        return tool_success(
            {
                "content": (
                    f"Nothing was requested yet: the user is on {surface.platform_display_name}, "
                    f"where vBot cannot show a form. Ask them in your reply to send {example} "
                    "as a message of its own. vBot stores it without showing you the value; you "
                    "can use it from your next turn."
                )
            }
        )
    attended = (surface is not None and surface.kind in {"webui", "tui"}) or (
        surface is None and context.run_kind in _ATTENDED_RUN_KINDS
    )
    if not attended:
        return tool_failure(
            "no_user",
            f"Nothing was requested: nobody can answer in this run. Say in your reply or report "
            f"that you need {name} and what for; the user can add it in Settings -> Secrets or "
            f"send {example} in a chat with you.",
        )
    outcome = await requests.request(
        agent_id=context.agent_id,
        session_id=context.session_id,
        project_id=context.project_id,
        name=name,
        kind=kind,
        description=description,
        lifetime=lifetime,
        passed_only=context.subagent_session,
    )
    if outcome.status == "stored" and outcome.secret is not None:
        verb = "replaced the value of" if outcome.replaced else "stored"
        lines = [f"The user {verb} {outcome.secret.name}:"]
        lines.extend(_secret_lines(secrets, outcome.secret))
        lines.append(_USE_HINT)
        return tool_success({"content": "\n".join(lines)})
    if outcome.status == "declined":
        return tool_success(
            {
                "content": (
                    f"The user declined to give {name}. Continue without it, or ask the user in "
                    "your reply how to proceed."
                )
            }
        )
    minutes = int(REQUEST_TIMEOUT_SECONDS // 60)
    return tool_success(
        {
            "content": (
                f"Nobody answered the request for {name} within {minutes} minutes, so it was "
                f"withdrawn. Ask the user in your reply; they can send {example}."
            )
        }
    )


async def _generate(
    secrets: AgentSecrets, context: ToolContext, arguments: dict[str, Any]
) -> JsonObject:
    name = _name(arguments, "generate")
    if isinstance(name, dict):
        return name
    length = arguments.get("length", 24)
    if isinstance(length, str) and length.strip().isdigit():
        length = int(length.strip())
    if isinstance(length, float) and length.is_integer():
        length = int(length)
    if isinstance(length, bool) or not isinstance(length, int) or not 8 <= length <= 128:
        return tool_failure(
            "invalid_arguments",
            f"Nothing was generated. length must be a whole number from 8 to 128; got {length}.",
        )
    symbols = arguments.get("symbols")
    symbols = DEFAULT_PASSWORD_SYMBOLS if symbols is None else str(symbols)
    if (
        secrets.lookup(
            SessionAddress(context.project_id, context.agent_id, context.session_id),
            name,
            passed_only=context.subagent_session,
        )
        is not None
    ):
        return tool_failure(
            "secret_exists",
            f"Nothing was generated. {name} already exists, and a new password would lose the "
            f"stored one. Use another name, such as {name}_NEW, or ask the user to delete {name} "
            "in Settings -> Secrets.",
        )
    username = str(arguments.get("username") or "").strip()
    values = {"password": secrets.generate_password(length, symbols)}
    if username:
        values["username"] = username
    try:
        secret = await asyncio.to_thread(
            secrets.create,
            name=name,
            kind="login",
            values=values,
            actor=f"agent:{context.agent_id}",
            session_id=context.session_id,
            agents=[format_agent_address(context.agent_id, context.project_id)],
            description=str(arguments.get("description") or "").strip(),
        )
    except SecretRecordError as error:
        return tool_failure("invalid_arguments", f"Nothing was generated. {error}")
    lines = [f"Generated a {length}-character password and stored it as {secret.name}:"]
    lines.extend(_secret_lines(secrets, secret))
    lines.append(f"{_USE_HINT} The user can see the password in Settings -> Secrets.")
    return tool_success({"content": "\n".join(lines)})


__all__ = [
    "SECRETS_TOOL_DESCRIPTION",
    "SECRETS_TOOL_NAME",
    "SECRETS_TOOL_PARAMETERS",
    "register_secrets_tool",
]
