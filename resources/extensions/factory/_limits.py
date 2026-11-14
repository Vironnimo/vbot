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


def observe_run(runtime: Json, step_id: str, run_id: str) -> None:
    """Count a Run toward the Card and toward its step's current execution."""
    for budget in (
        runtime.setdefault("budget", {}),
        runtime["steps"][step_id].setdefault("budget", {}),
    ):
        runs = budget.setdefault("runs", [])
        if run_id not in runs:
            runs.append(run_id)


def count_tool_call(runtime: Json, step_id: str) -> None:
    for budget in (
        runtime.setdefault("budget", {}),
        runtime["steps"][step_id].setdefault("budget", {}),
    ):
        budget["tool_calls"] = budget.get("tool_calls", 0) + 1


def new_execution(state: Json) -> None:
    """A step that starts again gets a fresh step budget; a user's limits for it stay."""
    state["budget"] = {"runs": [], "tool_calls": 0}
    state.pop("limit_hit", None)


def configured(runtime: Json, step_id: str) -> tuple[Json, Json]:
    """(Card limits, step limits): a user's override, else the Workflow's."""
    workflow = runtime.get("workflow", {})
    step: Json = next((s for s in workflow.get("steps", []) if s["id"] == step_id), {})
    state = runtime.get("steps", {}).get(step_id, {})
    card = runtime.get("card_limits", workflow.get("limits", {})) or {}
    return card, state.get("limits", step.get("limits", {})) or {}


def consumption(runtime: Json, scope: str, step_id: str, report: Json | None = None) -> Json:
    budget = (
        runtime.get("steps", {}).get(step_id, {}).get("budget", {})
        if scope == "step"
        else runtime.get("budget", {})
    )
    totals = report or {}
    return {
        "runs": len(budget.get("runs", [])),
        "tool_calls": budget.get("tool_calls", 0),
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


def warnings(runtime: Json, step_id: str, status: Json, seen: list[str]) -> list[Json]:
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
            step = step_id if scope == "step" else ""
            execution = runtime.get("steps", {}).get(step, {}).get("passes", 0) if step else 0
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
