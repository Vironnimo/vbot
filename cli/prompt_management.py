"""Prompt block RPC commands for the vBot CLI."""

from __future__ import annotations

from collections.abc import Sequence

from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def prompt_list(instance: ServerInstance) -> CommandResult:
    """Return System Prompt block metadata via `prompt.list` RPC."""

    payload = _rpc_call(instance, "prompt.list", {})
    if not payload.ok:
        return payload.to_command_result()
    blocks = payload.data.get("blocks")
    if not isinstance(blocks, list):
        return CommandResult(
            ok=False,
            message="RPC result missing prompt blocks list",
            instance=instance,
        )
    return CommandResult(ok=True, message=_format_prompt_rows(blocks), instance=instance)


def prompt_update(instance: ServerInstance, block_id: str, content: str) -> CommandResult:
    """Update one editable prompt block via `prompt.update` RPC."""

    payload = _rpc_call(instance, "prompt.update", {"id": block_id, "content": content})
    if not payload.ok:
        return payload.to_command_result()
    resolved_id = _string_or_default(payload.data.get("id"), block_id)
    return CommandResult(ok=True, message=f"updated {resolved_id}", instance=instance)


def prompt_reset(instance: ServerInstance, block_id: str) -> CommandResult:
    """Reset one editable prompt block via `prompt.reset` RPC."""

    payload = _rpc_call(instance, "prompt.reset", {"id": block_id})
    if not payload.ok:
        return payload.to_command_result()
    resolved_id = _string_or_default(payload.data.get("id"), block_id)
    return CommandResult(ok=True, message=f"reset {resolved_id}", instance=instance)


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


def _format_prompt_rows(blocks: Sequence[object]) -> str:
    if not blocks:
        return "no prompt blocks"

    lines = ["prompts:"]
    for block in blocks:
        lines.append(_format_prompt_row(block))
    return "\n".join(lines)


def _format_prompt_row(block: object) -> str:
    if not isinstance(block, dict):
        return "- invalid prompt block"

    block_id = _string_or_default(block.get("id"), "?")
    owner = _string_or_default(block.get("owner"), "?")
    kind = _string_or_default(block.get("kind"), "?")
    enabled = _bool_text(block.get("enabled"))
    editable = _bool_text(block.get("editable"))
    source = _string_or_default(block.get("source"), "?")
    modified = _modified_text(block)
    return (
        f"- {block_id} owner={owner} kind={kind} "
        f"enabled={enabled} editable={editable} source={source} modified={modified}"
    )


def _modified_text(block: dict[str, object]) -> str:
    if block.get("editable") is not True:
        return "-"
    return _bool_text(block.get("is_modified"))


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
