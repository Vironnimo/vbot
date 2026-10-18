"""Tool-owned System Prompt block declarations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from core.utils.logging import get_logger

_LOGGER = get_logger("tools")


class ToolPromptBlockRegistry:
    """Collect tool-owned System Prompt block declarations (D6).

    The tool-side of the unified contributor path: a tool that wants prompt
    content declares a block here at its ``register_*`` step, and the runtime
    gathers :meth:`block_definitions` and hands them to the prompt manager. This
    keeps the prompt domain free of tool internals — it only ever consumes a list
    of ``core.prompts.BlockDefinition`` objects, never imports a tool class.

    A declared block is id ``tool:<name>`` and owner ``tool:<name>`` (so gate 2
    renders it only when ``<name>`` is on the agent's effective allowlist) unless
    the declaration names another owner, static
    (``default_text``) or dynamic (``render``) — the same split as a core or
    extension block. Project and Sub-Agent use this seam for dynamic catalogs and
    guidance. Collisions are resolved first-wins with a warning, like tool-name
    registration.
    """

    def __init__(self) -> None:
        self._declarations: dict[
            str, tuple[str | None, Callable[..., Any] | None, str | None, str | None]
        ] = {}

    def register(
        self,
        tool_name: str,
        *,
        default_text: str | None = None,
        render: Callable[..., Any] | None = None,
        owner: str | None = None,
        requires_generated: str | None = None,
    ) -> None:
        """Declare a prompt block for *tool_name* (exactly one text / render).

        *owner* gates the block on another owner than ``tool:<tool_name>``, such
        as ``builtin:<kind>`` for guidance only one built-in Agent gets.
        *requires_generated* names the ``{generated:NAME}`` producer a static
        block lists: the block renders nothing while that producer renders
        nothing (``BlockDefinition.requires_generated``).

        *render* returns the block's text, or a ``core.prompts.RenderedBlock``
        that also names the catalog the text lists. Chat pins a dynamic block's
        text per prompt epoch and tells the Model about later changes in a
        System Reminder. Passing both or neither raises ``ValueError`` at
        declaration. A second
        declaration for the same tool name is ignored with a warning (first wins),
        mirroring how a duplicate tool name is handled.
        """
        if not tool_name:
            raise ValueError("Tool prompt block requires a tool name")
        has_text = default_text is not None
        has_render = render is not None
        if has_text == has_render:
            raise ValueError("Tool prompt block requires exactly one of default_text / render")
        if tool_name in self._declarations:
            _LOGGER.warning(
                "Tool prompt block for %r already declared; ignoring the duplicate",
                tool_name,
            )
            return
        self._declarations[tool_name] = (default_text, render, owner, requires_generated)

    def block_definitions(self) -> list[Any]:
        """Return the declared blocks as ``core.prompts.BlockDefinition`` objects.

        Lazy ``core.prompts`` import so the tools domain carries no import-time
        dependency on the prompts domain (this runs at runtime collection, never at
        module load). Order is declaration order.
        """
        from core.prompts import BlockDefinition

        definitions: list[Any] = []
        for tool_name, (default_text, render, owner, required) in self._declarations.items():
            definitions.append(
                BlockDefinition(
                    id=f"tool:{tool_name}",
                    owner=owner or f"tool:{tool_name}",
                    default_text=default_text,
                    render=render,
                    requires_generated=required,
                )
            )
        return definitions
