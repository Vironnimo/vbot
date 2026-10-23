"""The shape of context commands, shared by ``project.json`` and ``agent.json``.

Context commands are shell lines a Project or an Identity Agent runs when a
Session starts; their output reaches the Agent before its first Model request
(``chat/context-commands.md``). This module only validates the stored list.
"""

from __future__ import annotations

from typing import Any

MAX_CONTEXT_COMMANDS = 10
MAX_CONTEXT_COMMAND_CHARS = 4000


def normalize_context_commands(value: Any) -> list[str]:
    """Return the commands of *value* stripped and once each, in order.

    ``None`` is no command. Raises ``ValueError`` naming what is wrong, without
    the field name and without a final period.
    """
    problem = context_commands_problem(value)
    if problem is not None:
        raise ValueError(problem)
    if value is None:
        return []
    return list(dict.fromkeys(item.strip() for item in value))


def context_commands_problem(value: Any) -> str | None:
    """Why *value* is no valid command list, or ``None``."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return "must be a list of command strings"
    commands = [item.strip() for item in value]
    if any(not command for command in commands):
        return "must not contain an empty command"
    if any(len(command) > MAX_CONTEXT_COMMAND_CHARS for command in commands):
        return f"commands must have at most {MAX_CONTEXT_COMMAND_CHARS} characters"
    if len(dict.fromkeys(commands)) > MAX_CONTEXT_COMMANDS:
        return f"must hold at most {MAX_CONTEXT_COMMANDS} commands"
    return None
