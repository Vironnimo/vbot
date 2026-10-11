"""The refusal type of Tool contracts, apart so argument repair can raise it.

``core.tools.contracts`` exports it; import it from there.
"""

from __future__ import annotations


class ToolContractError(ValueError):
    """A Tool definition or call violates its contract; nothing was run or changed.

    It is the one refusal type of a Tool call. Dispatch reports it as
    ``invalid_arguments``, whether argument repair, schema validation or the
    handler raised it. A handler raises it only before the call has any effect:
    every other exception escaping a handler reports that the call failed while
    running, with its effects unknown. Registration raises it for an invalid
    definition.
    """


__all__ = ["ToolContractError"]
