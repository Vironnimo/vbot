"""A fixed Model-facing entry point into the Run's ordinary Tool dispatch."""

from __future__ import annotations

from core.tools.arguments import required_string
from core.tools.call_syntax import SpellingAliases, normalize_call_arguments
from core.tools.contracts import JsonObject, compile_tool_contract
from core.tools.on_demand import CALL_TOOL_NAME
from core.tools.tools import ToolContext, ToolDisplay, ToolRegistry, tool_failure

CALL_TOOL_DESCRIPTION = (
    "Call an available Tool using its name and arguments. Use this for Tools loaded "
    "with load_tools or announced in this Session."
)
CALL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The Tool name from its definition."},
        "arguments": {
            "type": "object",
            "description": "The arguments for that Tool, as an object.",
        },
    },
    "required": ["name", "arguments"],
}
_CONTRACT = compile_tool_contract(
    name=CALL_TOOL_NAME, input_schema=CALL_TOOL_PARAMETERS, require_closed_input=False
)
_ALIASES = SpellingAliases(
    {"name": ("tool", "tool_name"), "arguments": ("args", "parameters", "input")}
)


async def _call_tool(context: ToolContext, arguments: JsonObject) -> JsonObject:
    if context.programmatic or context.parent_tool_name == CALL_TOOL_NAME:
        return tool_failure(
            "proxy_recursion",
            "Nothing was run. Set name to the target Tool, not call_tool. "
            "Inside Python, call the target with tools.<name>(...).",
        )
    if not context.tool_calls_available:
        return tool_failure(
            "tool_call_unavailable",
            "Nothing was run. call_tool needs an active conversation Run.",
        )
    return await context.call_tool(
        required_string(arguments.get("name"), field_name="name"), arguments["arguments"]
    )


def register_call_tool(registry: ToolRegistry) -> None:
    registry.register(
        CALL_TOOL_NAME,
        CALL_TOOL_DESCRIPTION,
        CALL_TOOL_PARAMETERS,
        _call_tool,
        internal=True,
        parallel_safe=False,
        execution_slot_required=False,
        display=ToolDisplay(summary_builder=lambda arguments: str(arguments.get("name", ""))),
        open_input_schema=True,
        argument_normalizer=lambda arguments: normalize_call_arguments(
            _CONTRACT, arguments, field_aliases=_ALIASES
        ),
        result_schema={"type": "object"},
    )
