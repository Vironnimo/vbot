"""Prompt block RPC commands for the vBot CLI."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from cli.formatting import bool_text as _bool_text
from cli.formatting import string_or_default as _string_or_default
from cli.formatting import value_text as _value_text
from cli.rpc_client import httpx as httpx
from cli.rpc_client import rpc_call as _rpc_call
from cli.server_management import CommandResult, ServerInstance


def prompt_list(instance: ServerInstance, scope: str = "default") -> CommandResult:
    """Return System Prompt block metadata via `prompt.list` RPC."""

    try:
        params = _scope_params(scope)
    except ValueError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    payload = _rpc_call(instance, "prompt.list", params)
    if not payload.ok:
        return payload.to_command_result()
    blocks = payload.data.get("blocks")
    if not isinstance(blocks, list):
        return CommandResult(
            ok=False,
            message="RPC result missing prompt blocks list",
            instance=instance,
        )
    scopes = payload.data.get("scopes")
    message = _format_prompt_rows(blocks)
    if isinstance(scopes, list) and scopes:
        message = f"scope: {scope}\navailable_scopes: {_scope_list_text(scopes)}\n{message}"
    return CommandResult(ok=True, message=message, instance=instance)


def prompt_update(
    instance: ServerInstance, block_id: str, content: str, scope: str = "default"
) -> CommandResult:
    """Update one editable prompt block via `prompt.update` RPC."""

    try:
        params = {"id": block_id, "content": content, **_scope_params(scope)}
    except ValueError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    payload = _rpc_call(instance, "prompt.update", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_prompt_mutation("updated", payload.data, block_id),
        instance=instance,
    )


def prompt_reset(instance: ServerInstance, block_id: str, scope: str = "default") -> CommandResult:
    """Reset one editable prompt block via `prompt.reset` RPC."""

    try:
        params = {"id": block_id, **_scope_params(scope)}
    except ValueError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    payload = _rpc_call(instance, "prompt.reset", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True, message=_format_prompt_mutation("reset", payload.data, block_id), instance=instance
    )


def prompt_create(
    instance: ServerInstance,
    slug: str,
    content: str | None,
    position: int | None,
    scope: str = "default",
) -> CommandResult:
    """Create a custom prompt block via ``prompt.create_block`` RPC."""

    try:
        params: dict[str, Any] = {"slug": slug, **_scope_params(scope)}
    except ValueError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    if content is not None:
        params["content"] = content
    if position is not None:
        params["position"] = position
    payload = _rpc_call(instance, "prompt.create_block", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=_format_prompt_mutation("created", payload.data, f"user:{slug}"),
        instance=instance,
    )


def prompt_remove(instance: ServerInstance, block_id: str, scope: str = "default") -> CommandResult:
    """Remove a custom prompt block via ``prompt.remove_block`` RPC."""

    try:
        params = {"id": block_id, **_scope_params(scope)}
    except ValueError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    payload = _rpc_call(instance, "prompt.remove_block", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=f"removed {block_id}\n{_format_layout(payload.data.get('layout'))}",
        instance=instance,
    )


def prompt_set_layout(
    instance: ServerInstance, layout: list[object], scope: str = "default"
) -> CommandResult:
    """Replace one prompt scope's layout via ``prompt.set_layout`` RPC."""

    try:
        params = {"layout": layout, **_scope_params(scope)}
    except ValueError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    payload = _rpc_call(instance, "prompt.set_layout", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=f"updated prompt layout for {scope}\n{_format_layout(payload.data.get('layout'))}",
        instance=instance,
    )


def prompt_reset_layout(instance: ServerInstance, scope: str = "default") -> CommandResult:
    """Reset one prompt scope's layout via ``prompt.reset_layout`` RPC."""

    try:
        params = _scope_params(scope)
    except ValueError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    payload = _rpc_call(instance, "prompt.reset_layout", params)
    if not payload.ok:
        return payload.to_command_result()
    return CommandResult(
        ok=True,
        message=f"reset prompt layout for {scope}\n{_format_layout(payload.data.get('layout'))}",
        instance=instance,
    )


def prompt_preview(
    instance: ServerInstance, agent_id: str, scope: str = "default"
) -> CommandResult:
    """Render one agent's complete system prompt via `prompt.preview` RPC."""

    try:
        params = {"agent_id": agent_id, **_scope_params(scope)}
    except ValueError as exc:
        return CommandResult(ok=False, message=str(exc), instance=instance)
    payload = _rpc_call(instance, "prompt.preview", params)
    if not payload.ok:
        return payload.to_command_result()
    text = payload.data.get("text")
    if not isinstance(text, str):
        return CommandResult(ok=False, message="RPC result missing prompt text", instance=instance)
    tokens = _value_text(payload.data.get("tokens"))
    estimated = _bool_text(payload.data.get("estimated"))
    tool_text = ""
    if "tool_tokens" in payload.data or "tool_count" in payload.data:
        tool_text = (
            f" tool_tokens={_value_text(payload.data.get('tool_tokens'))}"
            f" tool_count={_value_text(payload.data.get('tool_count'))}"
        )
    return CommandResult(
        ok=True,
        message=(f"tokens: {tokens}{tool_text} estimated={estimated}\n---\n{text}"),
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


def _prompt_scope(raw: str) -> dict[str, str]:
    if raw == "default":
        return {"type": "default"}
    if raw.startswith("agent:") and raw.removeprefix("agent:"):
        return {"type": "agent", "agent_id": raw.removeprefix("agent:")}
    raise ValueError("invalid prompt scope; use 'default' or 'agent:<id>'")


def _scope_params(raw: str) -> dict[str, object]:
    scope = _prompt_scope(raw)
    return {} if scope["type"] == "default" else {"scope": scope}


def _format_prompt_mutation(action: str, data: Mapping[str, Any], fallback_id: str) -> str:
    resolved_id = _string_or_default(data.get("id"), fallback_id)
    if not {"owner", "kind", "enabled", "editable", "source"}.issubset(data):
        return f"{action} {resolved_id}"
    return f"{action} {resolved_id}\n{_format_prompt_row(data)}"


def _format_layout(value: object) -> str:
    if not isinstance(value, list):
        return "layout: -"
    return "layout: " + json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _scope_list_text(scopes: Sequence[object]) -> str:
    rendered: list[str] = []
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        scope_type = scope.get("type")
        if scope_type == "default":
            rendered.append("default")
        elif scope_type == "agent" and isinstance(scope.get("agent_id"), str):
            rendered.append(f"agent:{scope['agent_id']}")
    return ",".join(rendered) if rendered else "-"
