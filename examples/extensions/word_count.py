"""Example extension: a tiny ``word_count`` tool.

Copy this file into ``<data_dir>/extensions/`` (``~/.vbot/extensions/`` by
default) to enable it on the next server start.

It demonstrates ``api.register_tool``: extensions add their own agent tools
without forking vBot. The handler signature ``(context, arguments)`` and the
success/failure result envelope are identical to built-in tools — an
extension tool is a *normal* tool once registered. It appears in provider tool
definitions and is filtered by an agent's ``allowed_tools`` like any other.

(Tools are code that does one thing; for teaching the agent a *workflow*, write
a Skill instead — see ``.vorch/GLOSSARY.md`` Tool vs Skill.)
"""

from __future__ import annotations

from core.tools import tool_failure, tool_success

# JSON Schema for the tool's arguments — the same shape the model sees for
# built-in tools. Keep descriptions short: every tool enlarges the system
# prompt like any other.
_PARAMETERS = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "Text to count words in."},
    },
    "required": ["text"],
}


def _word_count(context, arguments):
    text = arguments.get("text")
    if not isinstance(text, str):
        # Expected bad input → a failure envelope, never a raised exception.
        return tool_failure("invalid_arguments", "`text` must be a string.")
    return tool_success({"word_count": len(text.split())})


def register(api):
    # Declares the tool; the runtime registers it into the live ToolRegistry
    # after the last built-in tool, right before the system prompt is built.
    api.register_tool(
        "word_count",
        "Count the number of whitespace-separated words in a piece of text.",
        _PARAMETERS,
        _word_count,
    )
