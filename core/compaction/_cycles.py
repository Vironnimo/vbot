"""Tool-cycle safety shared by canonical Tail and request-overflow scans."""

from collections.abc import Iterable
from typing import Any


class ToolCycleSuffix:
    """Validate one suffix while visiting its messages from newest to oldest.

    Results accumulate until the preceding Assistant declares exactly those
    Call ids. A repeated Result or incomplete cycle makes every earlier cut
    unsafe. Callers stop at the first refusal and own metadata filtering and
    which valid messages may start their Tail or User exchange.
    """

    def __init__(self) -> None:
        self._results: set[Any] = set()

    def accepts(
        self, role: str | None, *, call_ids: Iterable[Any] = (), result_id: Any = None
    ) -> bool:
        if role == "tool":
            if result_id in self._results:
                return False
            self._results.add(result_id)
            return True
        if role == "assistant":
            if set(call_ids) != self._results:
                return False
            self._results.clear()
            return True
        return not self._results
