"""Settings-owned Compaction Policy normalization shared by defaults and explicit overrides."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from core.utils.errors import StorageError

COMPACTION_SETTING_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "trigger": {"type": "context_ratio", "threshold": 0.8},
    "strategy": {"type": "summary_tail", "tail_tokens": 15_000, "summary_model": None},
}


def normalize_compaction_settings(compaction: Any) -> dict[str, Any]:
    """Return the normalized global compaction Policy."""
    return normalize_compaction_policy(compaction, use_defaults=True)


def normalize_compaction_policy(policy: Any, *, use_defaults: bool = False) -> dict[str, Any]:
    """Validate and normalize one complete Compaction Policy."""
    if policy is None:
        if use_defaults:
            return {
                "enabled": True,
                "trigger": {"type": "context_ratio", "threshold": 0.8},
                "strategy": {
                    "type": "summary_tail",
                    "tail_tokens": 15_000,
                    "summary_model": None,
                },
            }
        raise StorageError("Compaction Policy must be an object")
    section = _coerce_compaction_section(policy)
    unsupported = sorted(set(section) - {"enabled", "trigger", "strategy"})
    if unsupported:
        raise StorageError(f"Unsupported Compaction Policy fields: {', '.join(unsupported)}")
    if not use_defaults:
        missing = sorted({"enabled", "trigger", "strategy"} - set(section))
        if missing:
            raise StorageError(f"Missing Compaction Policy fields: {', '.join(missing)}")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise StorageError("Compaction Policy enabled must be a boolean")
    trigger = _normalize_compaction_trigger(section.get("trigger"))
    strategy = _normalize_compaction_strategy(section.get("strategy"))
    return {"enabled": enabled, "trigger": trigger, "strategy": strategy}


def _coerce_compaction_section(compaction: Any) -> dict[str, Any]:
    if compaction is None:
        return {}
    if not isinstance(compaction, Mapping):
        raise StorageError("Expected settings.compaction to be an object")
    return dict(compaction)


def _normalize_compaction_trigger(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "context_ratio", "threshold": 0.8}
    if not isinstance(value, Mapping):
        raise StorageError("Compaction Policy trigger must be an object")
    trigger = dict(value)
    trigger_type = trigger.get("type", "context_ratio")
    if trigger_type == "context_ratio":
        unsupported = sorted(set(trigger) - {"type", "threshold", "tokens"})
        if unsupported:
            raise StorageError(
                f"Unsupported context-ratio Trigger fields: {', '.join(unsupported)}"
            )
        normalized = {
            "type": trigger_type,
            "threshold": _normalize_compaction_threshold(trigger.get("threshold")),
        }
        if trigger.get("tokens") is not None:
            normalized["tokens"] = _normalize_compaction_positive_integer(
                trigger["tokens"], "tokens", 100_000
            )
        return normalized
    if trigger_type == "input_tokens":
        unsupported = sorted(set(trigger) - {"type", "tokens"})
        if unsupported:
            raise StorageError(f"Unsupported input-token Trigger fields: {', '.join(unsupported)}")
        return {
            "type": trigger_type,
            "tokens": _normalize_compaction_positive_integer(
                trigger.get("tokens"), "tokens", 100_000
            ),
        }
    raise StorageError("Compaction Policy trigger.type must be context_ratio or input_tokens")


def _normalize_compaction_threshold(value: Any) -> float:
    if value is None:
        return cast("float", COMPACTION_SETTING_DEFAULTS["trigger"]["threshold"])
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise StorageError("Compaction setting threshold must be a number")

    normalized_value = float(value)
    if normalized_value <= 0 or normalized_value > 1:
        raise StorageError("Compaction setting threshold must be in (0, 1]")
    return normalized_value


def _normalize_compaction_positive_integer(value: Any, field: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise StorageError(f"Compaction Policy {field} must be an integer")
    if value <= 0:
        raise StorageError(f"Compaction Policy {field} must be positive")
    return value


def _normalize_compaction_strategy(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "summary_tail", "tail_tokens": 15_000, "summary_model": None}
    if not isinstance(value, Mapping):
        raise StorageError("Compaction Policy strategy must be an object")
    strategy = dict(value)
    strategy_type = strategy.get("type", "summary_tail")
    if strategy_type == "summary_tail":
        unsupported = sorted(set(strategy) - {"type", "tail_tokens", "summary_model"})
        if unsupported:
            raise StorageError(
                f"Unsupported summary-tail Strategy fields: {', '.join(unsupported)}"
            )
        summary_model = strategy.get("summary_model")
        if summary_model is not None and not isinstance(summary_model, str):
            raise StorageError("Compaction Policy summary_model must be a string or null")
        return {
            "type": strategy_type,
            "tail_tokens": _normalize_compaction_positive_integer(
                strategy.get("tail_tokens"), "tail_tokens", 15_000
            ),
            "summary_model": summary_model,
        }
    if strategy_type == "continuation":
        unsupported = sorted(set(strategy) - {"type"})
        if unsupported:
            raise StorageError(
                f"Unsupported continuation Strategy fields: {', '.join(unsupported)}"
            )
        return {"type": strategy_type}
    raise StorageError("Compaction Policy strategy.type must be summary_tail or continuation")
