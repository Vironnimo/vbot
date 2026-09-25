"""What a fresh Agent receives: System Prompt blocks and Tool definitions, with token costs.

The System Prompt is assembled by the production prompt manager for one Agent
of a fresh Runtime, and the Tool definitions are the ones that Agent's Provider
request carries. ``--all`` adds every registered Tool, including internal,
opt-in and Session-scoped ones, so a Tool outside the default allowlist can be
reviewed too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from core.projects.resolver import runtime_agent_body
from core.runtime.runtime import Runtime
from core.utils.tokens import estimate_json_tokens, estimate_tokens
from scripts.tool_lab._lab_runtime import lab_runtime


@dataclass(frozen=True, slots=True)
class PromptBlock:
    id: str
    text: str
    tokens: int


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    definition: dict[str, Any]
    tokens: int
    offered: bool


@dataclass(frozen=True, slots=True)
class AgentView:
    agent_id: str
    prompt_tokens: int
    blocks: tuple[PromptBlock, ...]
    tools: tuple[ToolDefinition, ...]


async def collect(agent_id: str, *, include_all: bool = False) -> AgentView:
    """Assemble one Agent's System Prompt and Tool definitions in a fresh Runtime."""
    async with lab_runtime() as (runtime, _root):
        agent = runtime.agent_resolver.resolve_agent(None, agent_id)
        details: list[dict[str, Any]] = []
        text = runtime.system_prompts.build_system_prompt(
            agent, agent_body=runtime_agent_body(agent), block_details=details
        )
        blocks = tuple(
            PromptBlock(detail["id"], detail["text"], estimate_tokens(detail["text"])[0])
            for detail in details
            if detail["included"]
        )
        offered, definitions = tool_definitions(runtime, agent_id, include_all=include_all)
        tools = tuple(
            ToolDefinition(
                name,
                definition,
                estimate_json_tokens(definition)[0],
                name in offered,
            )
            for name, definition in definitions.items()
        )
        return AgentView(agent_id, estimate_tokens(text)[0], blocks, tools)


def tool_definitions(
    runtime: Runtime, agent_id: str, *, include_all: bool
) -> tuple[frozenset[str], dict[str, dict[str, Any]]]:
    """The names the Agent is offered, and the definitions by name.

    With ``include_all`` the definitions also cover every registered Tool,
    including internal, opt-in and Session-scoped ones.
    """
    agent = runtime.agent_resolver.resolve_agent(None, agent_id)
    offered = runtime.system_prompts.provider_tool_definitions(agent)
    definitions = {str(definition["name"]): definition for definition in offered}
    if include_all:
        registry = runtime.tools
        names = [tool.name for tool in registry.list_tools(include_internal=True)]
        for definition in registry.provider_definitions(
            names, include_internal=True, session_grants=names, ready_only=False
        ):
            definitions.setdefault(str(definition["name"]), definition)
    return frozenset(str(definition["name"]) for definition in offered), definitions


def summary(view: AgentView) -> str:
    offered = [tool for tool in view.tools if tool.offered]
    tool_tokens = sum(tool.tokens for tool in offered)
    lines = [
        f"Agent {view.agent_id}: System Prompt {view.prompt_tokens} tokens, "
        f"{len(offered)} Tool definitions {tool_tokens} tokens, "
        f"together {view.prompt_tokens + tool_tokens} tokens (estimates)",
        "",
        "System Prompt blocks:",
    ]
    lines.extend(f"  {block.id:<28} {block.tokens:>6}" for block in view.blocks)
    lines.append("")
    lines.append("Tool definitions (* = not offered to this Agent by default):")
    for tool in sorted(view.tools, key=lambda item: (not item.offered, item.name)):
        marker = " " if tool.offered else "*"
        parameters = tool.definition.get("parameters") or {}
        names = ", ".join((parameters.get("properties") or {}).keys())
        lines.append(f" {marker}{tool.name:<22} {tool.tokens:>6}  {names}")
    return "\n".join(lines)


def show(view: AgentView, name: str) -> str:
    """One block's text, or one Tool definition in readable form and as sent."""
    for block in view.blocks:
        if block.id == name:
            return f"== block {block.id} ({block.tokens} tokens)\n{block.text}"
    for tool in view.tools:
        if tool.name == name:
            sent = json.dumps(tool.definition, ensure_ascii=False, indent=2)
            return (
                f"== Tool {tool.name} ({tool.tokens} tokens)\n{readable(tool.definition)}\n\n"
                f"-- as sent\n{sent}"
            )
    known = [block.id for block in view.blocks] + [tool.name for tool in view.tools]
    return f"No block or Tool named {name!r}. Known: {', '.join(known)}"


def readable(definition: dict[str, Any]) -> str:
    """Render a Tool definition as an Agent reads it: description, then parameters."""
    lines = [str(definition.get("description", "")).strip(), ""]
    _schema_lines(definition.get("parameters") or {}, lines, indent="")
    return "\n".join(lines).rstrip()


def _schema_lines(schema: dict[str, Any], lines: list[str], *, indent: str) -> None:
    required = set(schema.get("required") or ())
    for name, prop in (schema.get("properties") or {}).items():
        lines.append(f"{indent}- {name} ({_facts(prop, name in required)})")
        description = prop.get("description")
        if description:
            lines.extend(f"{indent}    {line}" for line in str(description).splitlines())
        items = prop.get("items")
        if isinstance(items, dict) and items.get("properties"):
            _schema_lines(items, lines, indent=indent + "    ")
        elif prop.get("properties"):
            _schema_lines(prop, lines, indent=indent + "    ")
    for keyword in ("oneOf", "anyOf"):
        for index, branch in enumerate(schema.get(keyword) or (), start=1):
            if isinstance(branch, dict):
                lines.append(f"{indent}{keyword} branch {index}:")
                _schema_lines(branch, lines, indent=indent + "    ")


def _facts(prop: dict[str, Any], required: bool) -> str:
    kind = prop.get("type")
    if isinstance(kind, list):
        kind = "|".join(str(item) for item in kind)
    if kind == "array" and isinstance(prop.get("items"), dict):
        kind = f"array of {prop['items'].get('type', 'item')}"
    facts = [str(kind or "any"), "required" if required else "optional"]
    if "enum" in prop:
        facts.append("one of " + ", ".join(json.dumps(value) for value in prop["enum"]))
    if "default" in prop:
        facts.append(f"default {json.dumps(prop['default'])}")
    for bound in ("minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"):
        if bound in prop:
            facts.append(f"{bound} {prop[bound]}")
    return ", ".join(facts)
