"""The operator's bounded, persisted choice of execution computers for a Session."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from core.utils.ids import is_safe_id

DEFAULT_EXECUTION = MappingProxyType({"computer_id": "server", "allowed_computer_ids": ("server",)})


def normalize_execution(value: Any) -> dict[str, Any]:
    """Return a fresh policy; never turn a malformed explicit target into the server."""
    if value is None:
        return {"computer_id": "server", "allowed_computer_ids": ["server"]}
    if not isinstance(value, Mapping):
        raise ValueError("execution needs computer_id and allowed_computer_ids")
    computer_id = value.get("computer_id")
    allowed = value.get("allowed_computer_ids", [computer_id])
    if not is_safe_id(computer_id):
        raise ValueError("computer_id must identify one computer")
    if not isinstance(allowed, (list, tuple)) or len(allowed) > 100:
        raise ValueError("allowed_computer_ids must contain at most 100 computer ids")
    if any(not is_safe_id(item) for item in allowed):
        raise ValueError("allowed_computer_ids contains an invalid computer id")
    return {"computer_id": computer_id, "allowed_computer_ids": list(dict.fromkeys(allowed))}
