"""The refusal type of Tool contracts, apart so argument repair can raise it.

``core.tools.contracts`` exports it; import it from there.
"""

from __future__ import annotations

import json
from typing import Any

from core.tools.model_names import model_tool_name


class ToolContractError(ValueError):
    """A Tool definition or call violates its contract; nothing was run or changed.

    It is the one refusal type of a Tool call. Dispatch reports it as
    ``invalid_arguments``, whether argument repair, schema validation or the
    handler raised it. A handler raises it only before the call has any effect:
    every other exception escaping a handler reports that the call failed while
    running, with its effects unknown. Registration raises it for an invalid
    definition.
    """

    @classmethod
    def conflicting_values(
        cls,
        field: str,
        first: tuple[str, Any],
        second: tuple[str, Any],
        *,
        tool_name: str | None = None,
    ) -> ToolContractError:
        """Name both supplied instructions so the caller can choose one."""
        prefix = f"{model_tool_name(tool_name)} was not run: " if tool_name else "Nothing was run. "
        return cls(
            f"{prefix}Conflicting values for {field}: {first[0]} is {_shown(first[1])} and "
            f"{second[0]} is {_shown(second[1])}. Send only the intended one."
        )


def _shown(value: Any) -> str:
    """Quote a supplied value without copying a whole content payload into an error."""
    try:
        text = json.dumps(value, ensure_ascii=False)
    except TypeError, ValueError:
        text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


__all__ = ["ToolContractError"]
