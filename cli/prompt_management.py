"""Prompt fragment RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Sequence

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def prompt_list(instance: ServerInstance) -> CommandResult:
    """Return prompt fragment metadata via `prompt.list` RPC."""

    payload = _rpc_call(instance, "prompt.list", {})
    if not payload.ok:
        return payload.to_command_result()
    fragments = payload.data.get("fragments")
    if not isinstance(fragments, list):
        return CommandResult(
            ok=False,
            message="RPC result missing prompt fragments list",
            instance=instance,
        )
    return CommandResult(ok=True, message=_format_prompt_rows(fragments), instance=instance)


def prompt_update(instance: ServerInstance, name: str, content: str) -> CommandResult:
    """Update one prompt fragment via `prompt.update` RPC."""

    payload = _rpc_call(instance, "prompt.update", {"name": name, "content": content})
    if not payload.ok:
        return payload.to_command_result()
    fragment_name = _string_or_default(payload.data.get("name"), name)
    return CommandResult(ok=True, message=f"updated {fragment_name}", instance=instance)


def prompt_reset(instance: ServerInstance, name: str) -> CommandResult:
    """Reset one prompt fragment via `prompt.reset` RPC."""

    payload = _rpc_call(instance, "prompt.reset", {"name": name})
    if not payload.ok:
        return payload.to_command_result()
    fragment_name = _string_or_default(payload.data.get("name"), name)
    return CommandResult(ok=True, message=f"reset {fragment_name}", instance=instance)


def prompt_preview(instance: ServerInstance, agent_id: str) -> CommandResult:
    """Render one agent's complete system prompt via `prompt.preview` RPC."""

    payload = _rpc_call(instance, "prompt.preview", {"agent_id": agent_id})
    if not payload.ok:
        return payload.to_command_result()
    text = payload.data.get("text")
    if not isinstance(text, str):
        return CommandResult(ok=False, message="RPC result missing prompt text", instance=instance)
    tokens = _value_text(payload.data.get("tokens"))
    estimated = _bool_text(payload.data.get("estimated"))
    return CommandResult(
        ok=True,
        message=f"tokens: {tokens} estimated={estimated}\n---\n{text}",
        instance=instance,
    )


def _format_prompt_rows(fragments: Sequence[object]) -> str:
    if not fragments:
        return "no prompt fragments"

    lines = ["prompts:"]
    for fragment in fragments:
        lines.append(_format_prompt_row(fragment))
    return "\n".join(lines)


def _format_prompt_row(fragment: object) -> str:
    if not isinstance(fragment, dict):
        return "- invalid prompt fragment"

    name = _string_or_default(fragment.get("name"), "?")
    modified = _bool_text(fragment.get("is_modified"))
    variables = _format_variables(fragment.get("variables"))
    return f"- {name} modified={modified} variables={variables}"


def _format_variables(value: object) -> str:
    if not isinstance(value, list) or not value:
        return "-"
    placeholders = []
    for item in value:
        if isinstance(item, dict):
            placeholder = item.get("placeholder")
            if isinstance(placeholder, str) and placeholder:
                placeholders.append(placeholder)
    return ",".join(placeholders) if placeholders else "-"


def _bool_text(value: object) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def _value_text(value: object) -> str:
    if value is None:
        return "-"
    return str(value)


def _string_or_default(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default
