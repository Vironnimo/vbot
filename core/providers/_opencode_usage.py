"""OpenCode Console billing extension shared by its native inference wires."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.pricing import billing_details, nonnegative_amount


def opencode_usage(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Read the namespaced extension; a bare cost has provider-specific semantics."""
    extension = raw.get("opencode")
    if not isinstance(extension, Mapping) or isinstance(raw.get("cost"), Mapping):
        return {}
    amount = nonnegative_amount(raw.get("cost"))
    details = billing_details(
        {
            "billing_source": extension.get("billing_source"),
            "list_cost_usd": extension.get("list_cost"),
            "allowance_usd": extension.get("allowance"),
            "estimated": extension.get("estimated"),
        }
    )
    if amount is None:
        return {"cost": {"amount_usd": None, "source": "unknown", **details}} if details else {}
    return {
        "reported_cost_usd": amount,
        "cost": {"amount_usd": amount, "source": "provider", **details},
    }
