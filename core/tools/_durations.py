"""Read durations written by Models, and the ``max_delay`` that ``cron`` and ``calendar`` share.

Both Tools take ``max_delay``: how late a start that vBot missed, for example
while the server was off, can still happen. Models write it under the names of
schedulers they know (APScheduler ``misfire_grace_time``, Kubernetes
``startingDeadlineSeconds``, Hermes ``catch_up_missed``, OpenClaw
``skipMissedJobs``) and as words, numbers or ISO 8601 durations. This module
reads every spelling into one canonical value and converts it to seconds.
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.tools.call_syntax import is_placeholder, spelling

MAX_DELAY_UNLIMITED = "unlimited"
"""The canonical ``max_delay`` that removes a limit."""
MAX_DELAY_STAND_IN = "<duration such as 2h>"
"""The ``max_delay`` a corrected call shows in place of a value it could not read."""

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
_CANONICAL = re.compile(r"^(\d+)([mhd])$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}
_UNLIMITED_WORDS = frozenset({"unlimited", "infinite", "infinity", "forever", "always"})
# Spellings of the latest start of a missed start: durations, and seconds as numbers.
_MAX_DELAY_KEYS = frozenset(
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
    }
)
_MAX_DELAY_SECONDS_KEYS = frozenset(
    {
        "maxdelayseconds",
        "misfiregracetime",
        "misfiregraceseconds",
        "startingdeadlineseconds",
        "gracetimeseconds",
        "graceperiodseconds",
    }
)
# Hermes ``catch_up_missed`` and OpenClaw ``skipMissedJobs`` flags.
_CATCH_UP_KEYS = frozenset({"catchup", "catchupmissed", "runmissed", "startwhenavailable"})
_SKIP_MISSED_KEYS = frozenset({"skipmissed", "skipmissedjobs", "skipmissedruns"})
_ZERO_DURATION = re.compile(r"^(?:0+\s*[a-z]*|pt?0+[a-z]?)$")
_BOOLEAN_WORDS = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}


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


def read_max_delay(arguments: dict[str, Any], *, missed: str) -> list[str]:
    """Read every spelling of ``max_delay`` into one canonical value; return refusal texts.

    Canonical values are ``30m``, ``2h`` or ``1d``, ``0m`` (missed starts never
    happen) and ``unlimited``. A number names seconds only under a key that says
    so. ``missed`` names what ``0m`` skips in the refusal texts, such as
    ``missed fire``.
    """
    texts: list[str] = []
    readings: list[str] = []
    keys = _MAX_DELAY_KEYS | _MAX_DELAY_SECONDS_KEYS | _CATCH_UP_KEYS | _SKIP_MISSED_KEYS
    for key in list(arguments):
        word = spelling(key)
        if word not in keys:
            continue
        item = arguments.pop(key)
        if item is None:
            readings.append(MAX_DELAY_UNLIMITED)
            continue
        if is_placeholder(item):
            continue
        if word in _CATCH_UP_KEYS | _SKIP_MISSED_KEYS:
            flag = _BOOLEAN_WORDS.get(item.strip().casefold()) if isinstance(item, str) else item
            if not isinstance(flag, bool):
                texts.append(f'"{key}" must be true or false.')
            elif flag == (word in _CATCH_UP_KEYS):
                readings.append(MAX_DELAY_UNLIMITED)
            else:
                readings.append("0m")
            continue
        reading = _max_delay_text(item, seconds=word in _MAX_DELAY_SECONDS_KEYS)
        if reading is None:
            value = json.dumps(item, ensure_ascii=False)
            texts.append(
                f'"{key}" {value} is not a duration. max_delay takes a duration such as "30m", '
                f'"2h" or "1d", "0m" to skip every {missed}, or "unlimited".'
            )
            continue
        readings.append(reading)
    if len(set(readings)) > 1:
        texts.append("it sets more than one max_delay; choose one.")
    elif readings:
        arguments["max_delay"] = readings[0]
    return texts


def max_delay_seconds(value: Any, *, missed: str) -> int | None:
    """Return the seconds of a canonical ``max_delay``; ``None`` is no limit.

    Raises ``ValueError`` with the refusal text for any other value.
    """
    if value == MAX_DELAY_UNLIMITED:
        return None
    match = _CANONICAL.fullmatch(str(value))
    if match is None:
        raise ValueError(
            f'max_delay "{value}" is not a duration. Use "30m", "2h" or "1d", "0m" to skip '
            f'every {missed}, or "unlimited".'
        )
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def max_delay_text(seconds: int) -> str:
    """Spell a limit in seconds as the canonical ``max_delay``, such as ``2h`` or ``0m``."""
    return duration_from_seconds(seconds) or "0m"


def _max_delay_text(value: Any, *, seconds: bool) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if value == 0:
            return "0m"
        # A bare number names no unit.
        return duration_from_seconds(value) if seconds else None
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    if spelling(text) in _UNLIMITED_WORDS:
        return MAX_DELAY_UNLIMITED
    if text.isdigit():
        return _max_delay_text(int(text), seconds=seconds)
    if _ZERO_DURATION.match(text.replace(" ", "")):
        return "0m"
    return duration_text(text)


__all__ = [
    "MAX_DELAY_STAND_IN",
    "MAX_DELAY_UNLIMITED",
    "duration_from_seconds",
    "duration_text",
    "max_delay_seconds",
    "max_delay_text",
    "read_max_delay",
]
