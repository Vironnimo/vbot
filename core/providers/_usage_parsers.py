"""Usage parsers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from core.providers._usage_types import (
    _DAY_SECONDS,
    _EPOCH_MILLISECONDS_THRESHOLD,
    _FIVE_HOUR_SECONDS,
    _MINIMAX_CHAT_MODEL_PREFIX,
    _MINIMAX_PLAN_KEYS,
    _MINIMAX_REMAINING_KEYS,
    _MINIMAX_RESET_KEYS,
    _MINIMAX_TOTAL_KEYS,
    _PRIMARY_FALLBACK_LABEL,
    _RATIO_PERCENT_DECIMAL_PLACES,
    _SECONDARY_FALLBACK_LABEL,
    _WEEK_SECONDS,
    ProviderUsageSnapshot,
    UsageCredits,
    UsageFetchError,
    UsageWindow,
)
from core.providers.accounts import (
    DEFAULT_ACCOUNT_ID,
)


def _parse_openai_usage(
    connection_id: str,
    display_name: str,
    body: Any,
    *,
    account: str = DEFAULT_ACCOUNT_ID,
) -> ProviderUsageSnapshot:
    rate_limit = body.get("rate_limit") if isinstance(body, Mapping) else None
    windows: list[UsageWindow] = []
    if isinstance(rate_limit, Mapping):
        primary = _openai_window(rate_limit.get("primary_window"), _primary_window_label)
        if primary is not None:
            windows.append(primary)
        secondary = _openai_window(rate_limit.get("secondary_window"), _secondary_window_label)
        if secondary is not None:
            windows.append(secondary)
    return ProviderUsageSnapshot(
        connection=connection_id,
        account=account,
        display_name=display_name,
        plan=_first_string(body, ("plan_type",)) if isinstance(body, Mapping) else None,
        windows=windows,
        credits=_openai_credits(body),
    )


def _openai_window(raw: Any, label_for: Callable[[Any], str]) -> UsageWindow | None:
    if not isinstance(raw, Mapping):
        return None
    used_percent = _as_number(raw.get("used_percent"))
    if used_percent is None:
        return None
    return UsageWindow(
        label=label_for(raw.get("limit_window_seconds")),
        used_percent=clamp_percent(used_percent),
        reset_at=_epoch_to_iso(raw.get("reset_at")),
        window_seconds=_positive_int(raw.get("limit_window_seconds")),
    )


def _openai_credits(body: Any) -> UsageCredits | None:
    """Keep OpenAI credit state structured instead of merging it into plan."""

    if not isinstance(body, Mapping):
        return None
    credits = body.get("credits")
    if not isinstance(credits, Mapping) or not isinstance(credits.get("has_credits"), bool):
        return None
    return UsageCredits(
        enabled=credits["has_credits"],
        balance=_coerce_number(credits.get("balance")),
    )


def _primary_window_label(seconds: Any) -> str:
    if not _is_positive_number(seconds):
        return _PRIMARY_FALLBACK_LABEL
    return f"{seconds / 3600:g}h"


def _secondary_window_label(seconds: Any) -> str:
    if not _is_positive_number(seconds):
        return _SECONDARY_FALLBACK_LABEL
    if seconds >= _WEEK_SECONDS:
        return "Week"
    if seconds >= _DAY_SECONDS:
        return "Day"
    return f"{round(seconds / 3600)}h"


def _parse_copilot_usage(
    connection_id: str,
    display_name: str,
    body: Any,
    *,
    account: str = DEFAULT_ACCOUNT_ID,
) -> ProviderUsageSnapshot:
    quota_snapshots = body.get("quota_snapshots") if isinstance(body, Mapping) else None
    reset_at = _date_to_iso(body.get("quota_reset_date")) if isinstance(body, Mapping) else None
    windows: list[UsageWindow] = []
    if isinstance(quota_snapshots, Mapping):
        for snapshot_key, label in (("premium_interactions", "Premium"), ("chat", "Chat")):
            window = _copilot_window(quota_snapshots.get(snapshot_key), label, reset_at)
            if window is not None:
                windows.append(window)
    return ProviderUsageSnapshot(
        connection=connection_id,
        account=account,
        display_name=display_name,
        plan=_copilot_plan(body),
        windows=windows,
    )


def _copilot_window(raw: Any, label: str, reset_at: str | None) -> UsageWindow | None:
    if not isinstance(raw, Mapping):
        return None
    percent_remaining = _as_number(raw.get("percent_remaining"))
    if percent_remaining is None:
        return None
    remaining = _as_number(raw.get("remaining"))
    total = _as_number(raw.get("entitlement"))
    return UsageWindow(
        label=label,
        used_percent=clamp_percent(100.0 - percent_remaining),
        reset_at=reset_at,
        used_units=(total - remaining) if total is not None and remaining is not None else None,
        remaining_units=remaining,
        total_units=total,
        unit="interactions" if total is not None or remaining is not None else None,
        unlimited=raw.get("unlimited") if isinstance(raw.get("unlimited"), bool) else None,
    )


def _copilot_plan(body: Any) -> str | None:
    if not isinstance(body, Mapping):
        return None
    plan = body.get("copilot_plan")
    return plan.strip() if isinstance(plan, str) and plan.strip() else None


def _parse_ollama_balance(
    connection_id: str,
    display_name: str,
    body: Any,
    *,
    account: str = DEFAULT_ACCOUNT_ID,
) -> ProviderUsageSnapshot:
    """Project Ollama's documented ``/api/balance`` answer.

    ``included`` holds either the older plans' session and weekly limits
    (remaining percentages with reset times) or the monthly included credits
    of the current plans; the shape, not a plan name, selects the branch.
    """

    included = body.get("included") if isinstance(body, Mapping) else None
    if not isinstance(included, Mapping):
        raise UsageFetchError("Unsupported response shape")

    windows: list[UsageWindow] = []
    if "session" in included or "weekly" in included:
        for key, label, window_seconds in (
            ("session", "5h", _FIVE_HOUR_SECONDS),
            ("weekly", "Week", _WEEK_SECONDS),
        ):
            raw = included.get(key)
            if raw is None:
                continue
            window = _ollama_limit_window(raw, label, window_seconds)
            if window is None:
                raise UsageFetchError("Unsupported response shape")
            windows.append(window)
    else:
        window = _ollama_credit_window(included)
        if window is None:
            raise UsageFetchError("Unsupported response shape")
        windows.append(window)

    if not windows:
        raise UsageFetchError("Unsupported response shape")
    return ProviderUsageSnapshot(
        connection=connection_id,
        account=account,
        display_name=display_name,
        windows=windows,
        credits=_ollama_purchased_credits(body),
    )


def _ollama_limit_window(raw: Any, label: str, window_seconds: int) -> UsageWindow | None:
    if not isinstance(raw, Mapping):
        return None
    remaining_percent = _as_number(raw.get("remaining_percent"))
    if remaining_percent is None:
        return None
    return UsageWindow(
        label=label,
        used_percent=clamp_percent(round(100.0 - remaining_percent, _RATIO_PERCENT_DECIMAL_PLACES)),
        reset_at=_date_to_iso(raw.get("resets_at")),
        window_seconds=window_seconds,
    )


def _ollama_credit_window(included: Mapping[str, Any]) -> UsageWindow | None:
    balance = _as_number(included.get("balance_usd"))
    allowance = _as_number(included.get("allowance_usd"))
    if balance is None or allowance is None or allowance <= 0:
        return None
    period = included.get("period")
    used = max(0.0, allowance - balance)
    # The period follows the subscription's monthly schedule, so its length
    # varies; a fixed duration would split the history series every month.
    return UsageWindow(
        label="Month",
        used_percent=clamp_percent(round(used / allowance * 100.0, _RATIO_PERCENT_DECIMAL_PLACES)),
        reset_at=_date_to_iso(period.get("until")) if isinstance(period, Mapping) else None,
        used_units=used,
        remaining_units=max(0.0, balance),
        total_units=allowance,
        unit="USD",
    )


def _ollama_purchased_credits(body: Mapping[str, Any]) -> UsageCredits | None:
    purchased = body.get("purchased")
    balance = _as_number(purchased.get("balance_usd")) if isinstance(purchased, Mapping) else None
    if balance is None:
        return None
    return UsageCredits(enabled=balance > 0, balance=balance, unit="USD")


def _parse_opencode_go_usage(
    connection_id: str,
    display_name: str,
    body: Any,
    *,
    account: str = DEFAULT_ACCOUNT_ID,
) -> ProviderUsageSnapshot:
    """Project OpenCode Go's ``/usage`` answer (undocumented; strict).

    Each window's ``percent`` is cost-weighted across Models, so it carries no
    request or money units. The month follows the subscription anniversary.
    """

    usage = body.get("usage") if isinstance(body, Mapping) else None
    if not isinstance(usage, Mapping):
        raise UsageFetchError("Unsupported response shape")

    windows: list[UsageWindow] = []
    for key, label, window_seconds in (
        ("rolling", "5h", _FIVE_HOUR_SECONDS),
        ("weekly", "Week", _WEEK_SECONDS),
        ("monthly", "Month", None),
    ):
        raw = usage.get(key)
        if raw is None:
            continue
        window = _opencode_go_window(raw, label, window_seconds)
        if window is None:
            raise UsageFetchError("Unsupported response shape")
        windows.append(window)

    if not windows:
        raise UsageFetchError("Unsupported response shape")
    return ProviderUsageSnapshot(
        connection=connection_id,
        account=account,
        display_name=display_name,
        windows=windows,
    )


def _opencode_go_window(raw: Any, label: str, window_seconds: int | None) -> UsageWindow | None:
    if not isinstance(raw, Mapping):
        return None
    percent = _as_number(raw.get("percent"))
    reset_at = _date_to_iso(raw.get("resetsAt"))
    if percent is None or reset_at is None:
        return None
    if raw.get("status") == "rate-limited":
        percent = 100.0
    return UsageWindow(
        label=label,
        used_percent=clamp_percent(percent),
        reset_at=reset_at,
        window_seconds=window_seconds,
    )


def _parse_minimax_usage(
    connection_id: str,
    display_name: str,
    body: Any,
    *,
    account: str = DEFAULT_ACCOUNT_ID,
) -> ProviderUsageSnapshot:
    model_remains = body.get("model_remains") if isinstance(body, Mapping) else None
    if not isinstance(model_remains, list):
        raise UsageFetchError("Unsupported response shape")
    entry = _pick_minimax_model(model_remains)
    if entry is None:
        raise UsageFetchError("Unsupported response shape")

    total = _first_number(entry, _MINIMAX_TOTAL_KEYS)
    remaining = _first_number(entry, _MINIMAX_REMAINING_KEYS)
    if total is None or total <= 0 or remaining is None:
        raise UsageFetchError("Unsupported response shape")

    window = UsageWindow(
        label=_minimax_window_label(entry),
        used_percent=clamp_percent((total - remaining) / total * 100.0),
        reset_at=_minimax_reset_at(entry),
        window_seconds=_minimax_window_seconds(entry),
        used_units=total - remaining,
        remaining_units=remaining,
        total_units=total,
        unit="requests",
    )
    return ProviderUsageSnapshot(
        connection=connection_id,
        account=account,
        display_name=display_name,
        plan=_first_string(body, _MINIMAX_PLAN_KEYS) if isinstance(body, Mapping) else None,
        windows=[window],
    )


def _pick_minimax_model(model_remains: list[Any]) -> Mapping[str, Any] | None:
    """Return the chat-model entry (``MiniMax-M*``) with a non-zero total."""

    for entry in model_remains:
        if not isinstance(entry, Mapping):
            continue
        model_name = entry.get("model_name")
        if not isinstance(model_name, str):
            continue
        if not model_name.lower().startswith(_MINIMAX_CHAT_MODEL_PREFIX):
            continue
        total = _first_number(entry, _MINIMAX_TOTAL_KEYS)
        if total is not None and total > 0:
            return entry
    return None


def _minimax_window_label(entry: Mapping[str, Any]) -> str:
    minutes = _as_number(entry.get("current_interval_minutes"))
    if minutes is not None and minutes > 0:
        if minutes >= 60:
            return f"{minutes / 60:g}h"
        return f"{round(minutes)}m"
    model_name = entry.get("model_name")
    return model_name if isinstance(model_name, str) and model_name else "Plan"


def _minimax_window_seconds(entry: Mapping[str, Any]) -> int | None:
    minutes = _as_number(entry.get("current_interval_minutes"))
    if minutes is None or minutes <= 0:
        return None
    return round(minutes * 60)


def _minimax_reset_at(entry: Mapping[str, Any]) -> str | None:
    for key in _MINIMAX_RESET_KEYS:
        reset_at = _date_to_iso(entry.get(key))
        if reset_at is not None:
            return reset_at
    return None


def _parse_openrouter_usage(
    connection_id: str,
    display_name: str,
    credits_body: Any,
    key_body: Any,
    *,
    account: str = DEFAULT_ACCOUNT_ID,
) -> ProviderUsageSnapshot:
    credits_data = credits_body.get("data") if isinstance(credits_body, Mapping) else None
    total_credits = (
        _as_number(credits_data.get("total_credits")) if isinstance(credits_data, Mapping) else None
    )
    total_usage = (
        _as_number(credits_data.get("total_usage")) if isinstance(credits_data, Mapping) else None
    )
    credits = (
        UsageCredits(enabled=True, balance=total_credits - total_usage, unit="USD")
        if total_credits is not None and total_usage is not None
        else None
    )

    key_data = key_body.get("data") if isinstance(key_body, Mapping) else None
    windows: list[UsageWindow] = []
    if isinstance(key_data, Mapping):
        window = _openrouter_window(key_data)
        if window is not None:
            windows.append(window)
    return ProviderUsageSnapshot(
        connection=connection_id,
        account=account,
        display_name=display_name,
        windows=windows,
        credits=credits,
    )


def _openrouter_window(key_data: Mapping[str, Any]) -> UsageWindow | None:
    limit = _as_number(key_data.get("limit"))
    remaining = _as_number(key_data.get("limit_remaining"))
    # remaining > limit means OpenRouter rolled the cap over (credits added
    # mid-period); the ratio is then no longer a meaningful quota window.
    if limit is None or limit <= 0 or remaining is None or not 0 <= remaining <= limit:
        return None
    return UsageWindow(
        label="API key spending cap",
        used_percent=clamp_percent((limit - remaining) / limit * 100.0),
        reset_at=_date_to_iso(key_data.get("limit_reset")),
        used_units=limit - remaining,
        remaining_units=remaining,
        total_units=limit,
        unit="USD",
    )


def clamp_percent(value: Any) -> float:
    """Clamp a raw percentage to the inclusive 0–100 range as a float."""

    number = _as_number(value)
    if number is None:
        return 0.0
    return max(0.0, min(100.0, number))


def _epoch_to_iso(value: Any) -> str | None:
    seconds = _as_number(value)
    if seconds is None:
        return None
    if seconds > _EPOCH_MILLISECONDS_THRESHOLD:
        seconds /= 1000.0
    try:
        return datetime.fromtimestamp(seconds, UTC).isoformat()
    except OverflowError, OSError, ValueError:
        return None


def _date_to_iso(value: Any) -> str | None:
    """Normalize an epoch number or an ISO date/datetime string to ISO-8601 UTC."""

    if _as_number(value) is not None:
        return _epoch_to_iso(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _first_number(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        number = _as_number(mapping.get(key))
        if number is not None:
            return number
    return None


def _first_string(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _as_number(value: Any) -> float | None:
    """Return *value* as a float, or ``None`` when it is not a real number."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _coerce_number(value: Any) -> float | None:
    """Like :func:`_as_number` but also parse a numeric string (e.g. ``"1234"``)."""

    number = _as_number(value)
    if number is not None:
        return number
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if isfinite(number) else None
    return None


def _is_positive_number(value: Any) -> bool:
    number = _as_number(value)
    return number is not None and number > 0


def _positive_int(value: Any) -> int | None:
    number = _as_number(value)
    if number is None or number <= 0:
        return None
    return round(number)
