"""Factory execution budgets: configuration, durable counters and stop reasons."""

from __future__ import annotations

import math
from typing import Any

from .store import FactoryError

type Json = dict[str, Any]

LIMIT_SCHEMA: Json = {
    "type": "object",
    "properties": {
        "runs": {"type": "integer", "minimum": 0},
        "tool_calls": {"type": "integer", "minimum": 0},
        "tokens": {"type": "integer", "minimum": 0},
        "cost_usd": {"type": "number", "minimum": 0},
    },
    "additionalProperties": False,
}


def normalize(value: Any) -> Json:
    """An omitted or zero limit is unlimited; reject invalid explicit values."""
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - set(LIMIT_SCHEMA["properties"]):
        raise FactoryError("invalid_limits", "Use runs, tool_calls, tokens or cost_usd as limits.")
    result: Json = {}
    for key, raw in value.items():
        try:
            number = float(raw)
        except TypeError, ValueError, OverflowError:
            number = -1
        if (
            isinstance(raw, bool)
            or not math.isfinite(number)
            or number < 0
            or (key != "cost_usd" and not number.is_integer())
        ):
            raise FactoryError(
                "invalid_limits",
                f"{key} needs a non-negative {'number' if key == 'cost_usd' else 'whole number'}. "
                "Use 0 or omit it for unlimited.",
            )
        if number:
            result[key] = number if key == "cost_usd" else int(number)
    return result


def observe_run(runtime: Json, run_id: str) -> None:
    budget = runtime.setdefault("budget", {})
    for key in ("runs", "step_runs"):
        runs = budget.setdefault(key, [])
        if run_id not in runs:
            runs.append(run_id)


def new_step(runtime: Json) -> None:
    budget = runtime.setdefault("budget", {})
    budget.update(step_runs=[], step_tool_calls=0)
    runtime.pop("step_limits", None)
    runtime.pop("limit_hit", None)
    for notice in runtime.get("budget_notices", {}).values():
        notice.pop("pending", None)


def configured(runtime: Json) -> tuple[Json, Json]:
    workflow = runtime.get("workflow", {})
    step: Json = next((s for s in workflow.get("steps", []) if s["id"] == runtime.get("step")), {})
    return runtime.get("card_limits", workflow.get("limits", {})) or {}, runtime.get(
        "step_limits", step.get("limits", {})
    ) or {}


def consumption(runtime: Json, scope: str, report: Json | None = None) -> Json:
    budget = runtime.get("budget", {})
    prefix = "step_" if scope == "step" else ""
    totals = report or {}
    return {
        "runs": len(budget.get(prefix + "runs", [])),
        "tool_calls": budget.get(prefix + "tool_calls", 0),
        "tokens": totals.get("input_tokens", 0) + totals.get("output_tokens", 0),
        "cost_usd": totals.get("cost_usd") if totals.get("calls") else 0,
        "unpriced_calls": totals.get("unpriced_calls", 0),
        "unreported_calls": totals.get("unreported_calls", 0),
    }


def reached(limits: Json, usage: Json, scope: str, *, check_runs: bool) -> Json | None:
    for metric in ("tool_calls", "tokens", "cost_usd", "runs"):
        maximum = limits.get(metric, 0)
        if not maximum or (metric == "runs" and not check_runs):
            continue
        unknown = (metric == "cost_usd" and usage["unpriced_calls"] > 0) or (
            metric == "tokens" and usage["unreported_calls"] > 0
        )
        used = usage[metric]
        if unknown or used is None or used >= maximum:
            return {
                "scope": scope,
                "metric": metric,
                "limit": maximum,
                "used": used,
                "reason": "usage_unavailable" if unknown or used is None else "limit_reached",
            }
    return None


def message(hit: Json) -> str:
    subject = "Card" if hit["scope"] == "card" else "Step"
    if hit["reason"] == "usage_unavailable":
        return f"{subject} paused: {hit['metric']} usage is unavailable for its configured limit."
    return f"{subject} limit reached: {hit['metric']} {hit['used']} / {hit['limit']}."


def warnings(runtime: Json, status: Json, seen: list[str]) -> list[Json]:
    """One notice per threshold and recipient; rework and raised limits are new budgets."""
    result = []
    for scope, item in status.items():
        for metric, maximum in item["limits"].items():
            used = item["usage"][metric]
            if not maximum or used is None:
                continue
            threshold = maximum * 0.8
            if metric != "cost_usd":
                threshold = maximum - max(1, math.ceil(maximum * 0.2))
            step = runtime.get("step", "") if scope == "step" else ""
            execution = runtime.get("passes", {}).get(step, 0) if step else 0
            key = f"{scope}:{step}:{execution}:{metric}:{maximum}"
            if used >= threshold and key not in seen:
                result.append(
                    {
                        "key": key,
                        "scope": scope,
                        "metric": metric,
                        "limit": maximum,
                        "used": used,
                        "remaining": max(0, maximum - used),
                    }
                )
    return result
