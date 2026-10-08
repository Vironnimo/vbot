"""Read durations written by Models, and the late-start limits ``cron`` and ``calendar`` ignore.

Models limit how late a missed start can still happen under the names of
schedulers they know (``max_delay``, APScheduler ``misfire_grace_time``,
Kubernetes ``startingDeadlineSeconds``, Hermes ``catch_up_missed``, OpenClaw
``skipMissedJobs``). Neither Tool takes such a limit: a missed start happens
late and its Run is told how late it is. A call that sends one still executes,
and its result says that the field has no effect.
"""

from __future__ import annotations

import re
from typing import Any

from core.tools.call_syntax import is_placeholder, spelling
from core.tools.contracts import JsonObject

LATE_LIMITS_FIELD = "late_limits_note"
"""Unadvertised field that carries the note on ignored late-start limits to the handler."""
LATE_LIMITS_PARAMETER: dict[str, Any] = {"type": "string", "minLength": 1}

_DURATION_WORDS = {
    "s": 1,
    "sec": 1,
    "secs": 1,
    "second": 1,
    "seconds": 1,
    "m": 60,
    "min": 60,
    "mins": 60,
    "minute": 60,
    "minutes": 60,
    "h": 3600,
    "hr": 3600,
    "hrs": 3600,
    "hour": 3600,
    "hours": 3600,
    "d": 86400,
    "day": 86400,
    "days": 86400,
    "w": 604800,
    "wk": 604800,
    "week": 604800,
    "weeks": 604800,
}
_DURATION = re.compile(r"^(\d+)\s*([a-z]+)$")
_ISO_DURATION = re.compile(
    r"^p(?:(\d+)w)?(?:(\d+)d)?(?:t(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?)?$", re.IGNORECASE
)
# Spellings of a limit on how late a missed start can still happen.
_LATE_LIMIT_KEYS = frozenset(
    {
        "maxdelay",
        "maxlateness",
        "maxlate",
        "latelimit",
        "catchupwindow",
        "catchupwithin",
        "misfiregrace",
        "gracetime",
        "graceperiod",
        "startingdeadline",
        "maxdelayseconds",
        "misfiregracetime",
        "misfiregraceseconds",
        "startingdeadlineseconds",
        "gracetimeseconds",
        "graceperiodseconds",
        # Hermes ``catch_up_missed`` and OpenClaw ``skipMissedJobs`` flags.
        "catchup",
        "catchupmissed",
        "runmissed",
        "startwhenavailable",
        "skipmissed",
        "skipmissedjobs",
        "skipmissedruns",
    }
)


def duration_text(text: str) -> str | None:
    """Return ``30m``, ``2h`` or ``1d`` for a duration such as ``30 minutes`` or ``PT2H``."""
    text = text.strip().casefold()
    match = _DURATION.match(text)
    if match is not None:
        size = _DURATION_WORDS.get(match.group(2))
        if size is None:
            return None
        return duration_from_seconds(int(match.group(1)) * size)
    iso = _ISO_DURATION.match(text)
    if iso is not None and any(iso.groups()):
        weeks, days, hours, minutes, seconds = (int(part or 0) for part in iso.groups())
        total = (((weeks * 7 + days) * 24 + hours) * 60 + minutes) * 60 + seconds
        return duration_from_seconds(total)
    return None


def duration_from_seconds(seconds: float) -> str | None:
    """Return ``30m``, ``2h`` or ``1d`` for a positive whole number of minutes in seconds."""
    if seconds <= 0 or seconds % 60:
        return None
    minutes = int(seconds // 60)
    for size, unit in ((1440, "d"), (60, "h")):
        if minutes % size == 0:
            return f"{minutes // size}{unit}"
    return f"{minutes}m"


def take_late_limits(arguments: dict[str, Any], behavior: str) -> str | None:
    """Remove every late-start limit from a call; return the note for the result, if any.

    ``behavior`` says what happens to a missed start instead. Empty values and
    placeholders request nothing and leave no note.
    """
    sent: list[str] = []
    for key in list(arguments):
        if spelling(key) in _LATE_LIMIT_KEYS and not is_placeholder(arguments.pop(key)):
            sent.append(f'"{key}"')
    if not sent:
        return None
    names = sent[0] if len(sent) == 1 else f"{', '.join(sent[:-1])} and {sent[-1]}"
    return f"{names} {'has' if len(sent) == 1 else 'have'} no effect. {behavior}"


def with_late_limits_note(result: JsonObject, note: str, refusal_prefix: str) -> JsonObject:
    """Add the note to a Tool result; a refusal keeps its cause first and its call last."""
    if result.get("ok"):
        data = result["data"]
        data["note"] = f"{data['note']} {note}" if data.get("note") else note
        return result
    error = result["error"]
    message = str(error["message"])
    head, separator, call = message.rpartition(" Send: ")
    if separator:
        error["message"] = f"{head} {note}{separator}{call}"
    elif message.startswith(refusal_prefix) and message.endswith("}"):
        # A choice ends with its calls; the note goes before its cause.
        error["message"] = f"{refusal_prefix}{note} {message.removeprefix(refusal_prefix)}"
    else:
        error["message"] = f"{message} {note}"
    return result


__all__ = [
    "LATE_LIMITS_FIELD",
    "LATE_LIMITS_PARAMETER",
    "duration_from_seconds",
    "duration_text",
    "take_late_limits",
    "with_late_limits_note",
]
