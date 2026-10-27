"""Select the measurements belonging to the last physical Model request."""

from collections.abc import Mapping
from typing import Any


def last_request_usage(usage: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """Use the final attempt when reported, including when its Usage is unknown.

    Legacy and ordinary requests have no separate attempt snapshot and use their
    top-level Usage. A malformed explicit snapshot is treated as unknown.
    """
    if usage is None:
        return {}
    if "last_request_usage" not in usage:
        return usage
    final = usage["last_request_usage"]
    return final if isinstance(final, Mapping) else {}
