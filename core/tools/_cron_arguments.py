"""Read ``cron`` calls written in vBot's schema, its earlier schemas, or another harness's dialect.

Models schedule with the same intent under other names: vBot's earlier flat
schema (``agent_id``, ``schedule_type``, ``cron_expression``, ``run_at``,
``status``, ``timezone``, ``session_id``) and operation objects
(``{"create": {...}}``), Claude Code ``CronCreate``/``CronList``/``CronDelete``
(``cron``, ``recurring``, ``durable``), Hermes ``cronjob`` (``job_id``,
``pause``/``resume``/``remove``, ``deliver``), OpenClaw ``cron`` (a ``job``
with ``schedule`` and ``payload`` objects, ``jobId``), and scheduled-task Tools
(``cronExpression``, ``fireAt``, ``description``). This owner maps them onto
the canonical fields so a call whose intent is clear executes. What vBot cannot
honor as written (delivering the reply, choosing a Session, firing on demand,
several actions in one call, readings with different effects) fails before any
side effect with the corrected call.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from core.tools._argument_repair import normalize_call_arguments
from core.tools._call_vocabulary import (
    PLACEHOLDER_WORDS,
    SpellingAliases,
    is_placeholder,
    spelling,
)
from core.tools.contracts import ToolContract, ToolContractError

TIMEZONE_FIELD = "timezone"
ENABLED_FIELD = "enabled"
# Accepted so a requested time zone or paused state reaches the handler, which
# converts, applies or refuses it. Never advertised.
UNADVERTISED_PARAMETERS: dict[str, Any] = {
    TIMEZONE_FIELD: {"type": "string", "minLength": 1},
    ENABLED_FIELD: {"type": "boolean"},
}
OMIT = object()
"""``render_call`` override that removes a field from the rendered call."""


class CronCallRefusedError(ValueError):
    """A ``cron`` call was refused before any side effect; the message names the fix."""


_REFUSAL_PREFIX = "cron was not run: "
_CHANGE_FIELDS = ("target", "name", "prompt", "schedule", "repeat", ENABLED_FIELD)
_CALL_ORDER = ("action", "id", "target", "name", "prompt", "schedule", "repeat")
_LONG_PROMPT = 120
_LONG_PROMPT_STAND_IN = "<the prompt from this call>"
_TEMPLATE = re.compile(r"^\s*<[^<>]+>\s*$")

_FIELD_ALIASES = SpellingAliases(
    {
        "id": ("job_id", "cron_id", "task_id", "schedule_id"),
        "target": (
            "agent_id",
            "agent",
            "agent_name",
            "agent_address",
            "target_agent",
            "target_agent_id",
        ),
        "name": ("title", "label", "job_name", "display_name", "task_name"),
        "prompt": ("message", "instruction", "instructions", "task", "text", "content", "goal"),
        "repeat": (
            "repeats",
            "remaining_runs",
            "count",
            "times",
            "runs",
            "max_runs",
            "repeat_count",
            "run_count",
        ),
        TIMEZONE_FIELD: ("tz", "zone", "timezone_name", "iana_timezone"),
    }
)
_ACTION_SYNONYMS = SpellingAliases(
    {
        "create": (
            "add",
            "new",
            "schedule",
            "make",
            "register",
            "create_job",
            "add_job",
            "cron_create",
            "schedule_task",
            "create_task",
            "create_scheduled_task",
        ),
        "list": (
            "get",
            "show",
            "view",
            "ls",
            "read",
            "status",
            "info",
            "details",
            "inspect",
            "runs",
            "history",
            "cron_list",
            "list_jobs",
            "get_job",
            "list_scheduled_tasks",
        ),
        "update": (
            "edit",
            "modify",
            "change",
            "patch",
            "set",
            "reschedule",
            "update_job",
            "update_task",
            "update_scheduled_task",
        ),
        "delete": (
            "remove",
            "rm",
            "del",
            "destroy",
            "drop",
            "unschedule",
            "cron_delete",
            "delete_job",
            "remove_job",
            "delete_task",
            "delete_scheduled_task",
        ),
        "enable": ("resume", "unpause", "activate", "reactivate", "turn_on", "enable_job"),
        "disable": ("pause", "suspend", "deactivate", "turn_off", "disable_job", "pause_job"),
    }
)
# Firing on demand does not exist; "stop" and its kin can mean pausing or deleting.
_RUN_WORDS = frozenset(
    {"run", "runnow", "trigger", "fire", "execute", "invoke", "runjob", "runscheduledtask"}
)
_STOP_WORDS = frozenset({"cancel", "stop", "kill", "end", "halt", "abort", "terminate"})

_WRAPPER_KEYS = frozenset({"job", "data", "cronjob"})
_PAYLOAD_KEYS = frozenset({"payload"})
_AGENT_TURN_KINDS = frozenset({"agentturn", "agent", "prompt", "message"})

# Schedule spellings by what their value means.
_CRON_KEYS = frozenset({"cron", "cronexpression", "expression", "expr", "crontab"})
_AT_KEYS = frozenset(
    {
        "runat",
        "at",
        "atms",
        "fireat",
        "datetime",
        "when",
        "startat",
        "scheduledat",
        "scheduledtime",
        "triggerat",
        "timestamp",
    }
)
_EVERY_KEYS = frozenset(
    {
        "every",
        "everyms",
        "everyseconds",
        "everyminutes",
        "interval",
        "intervalms",
        "intervalseconds",
        "intervalminutes",
        "intervalhours",
    }
)
_IN_KEYS = frozenset(
    {"in", "delay", "delayms", "delayseconds", "delayminutes", "inseconds", "inminutes", "after"}
)
_TYPE_KEYS = frozenset({"scheduletype", "schedulekind", "kind", "type"})
_TYPE_WORDS = {
    "cron": frozenset({"cron", "crontab", "cronexpression", "expression", "expr"}),
    "once": frozenset(
        {"once", "onetime", "oneshot", "single", "at", "date", "datetime", "runat", "fireat"}
    ),
    "interval": frozenset({"interval", "every", "recurring", "repeating", "periodic"}),
}
_UNIT_BY_KEY_SUFFIX = (("ms", 0.001), ("seconds", 1), ("minutes", 60), ("hours", 3600))

_ENABLED_KEYS = frozenset({"enabled", "isenabled", "active", "isactive"})
_PAUSED_KEYS = frozenset({"paused", "ispaused", "disabled", "isdisabled", "inactive"})
_STATUS_KEYS = frozenset({"status", "state", "jobstatus"})
_ACTIVE_WORDS = frozenset({"active", "enabled", "enable", "on", "running", "scheduled", "live"})
_PAUSED_WORDS = frozenset(
    {"paused", "pause", "disabled", "disable", "inactive", "off", "stopped", "suspended"}
)
_RECURRING_KEYS = frozenset({"recurring", "isrecurring", "repeating", "recurrent"})
_ONE_SHOT_KEYS = frozenset(
    {"onetime", "oneshot", "once", "runonce", "deleteafterrun", "deleteafterfire"}
)
_SESSION_KEYS = frozenset({"sessionid", "session", "sessionkey", "sessiontarget"})
_FRESH_SESSION_WORDS = frozenset({"new", "fresh", "isolated", "auto", "default"})
_DELIVERY_KEYS = frozenset(
    {"deliver", "delivery", "deliverto", "notify", "notifyoncompletion", "notifyuser", "announce"}
)
_NO_DELIVERY_WORDS = frozenset({"local", "none", "off", "silent", "false", "no"})
_DESCRIPTION_KEYS = frozenset({"description", "desc", "summary", "details"})
# Accepted without effect: vBot jobs always persist, and list always shows every job.
_IGNORED_KEYS = frozenset({"durable", "persist", "persistent", "includedisabled", "reason"})
_LIST_ONLY_KEYS = frozenset({"limit", "offset", "all", "showall"})
_TARGET_PLACEHOLDERS = PLACEHOLDER_WORDS | {"self", "current", "default", "this"}
_REPEAT_UNLIMITED_WORDS = frozenset({"unlimited", "infinite", "infinity", "forever", "always"})
_BOOLEAN_WORDS = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}

_CRON_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
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
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _Problems:
    """Refusals collected while a call is read, reported together with one corrected call."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.choice: tuple[str, list[dict[str, Any]]] | None = None
        self.overrides: dict[str, Any] = {}

    def add(self, text: str, **overrides: Any) -> None:
        """Refuse the call; ``overrides`` replace refused values in the corrected call."""
        self.texts.append(text)
        self.overrides.update(overrides)

    def choose(self, text: str, alternatives: list[dict[str, Any]]) -> None:
        """Refuse a call with several plausible readings; each alternative overrides fields."""
        if self.choice is None:
            self.choice = (text, alternatives)
        else:
            self.texts.append(text)

    def raise_if_any(self, arguments: Mapping[str, Any]) -> None:
        call = {**arguments, **self.overrides}
        if self.choice is not None:
            text, alternatives = self.choice
            calls = " or ".join(render_call(call, **overrides) for overrides in alternatives)
            raise ToolContractError(_REFUSAL_PREFIX + " ".join([*self.texts, text, calls]))
        if self.texts:
            texts = " ".join(self.texts)
            raise ToolContractError(f"{_REFUSAL_PREFIX}{texts} Send: {render_call(call)}")


def normalize_cron_arguments(contract: ToolContract, arguments: Any) -> Any:
    """Return the canonical ``cron`` arguments for one Model call."""
    actions = tuple(contract.input_schema["properties"]["action"]["enum"])
    value = _json_object(arguments)
    if value is None:
        return normalize_call_arguments(contract, arguments)
    problems = _Problems()
    value = _single_operation(contract, value, actions)
    value = _unwrap_job(value, problems)
    normalized = normalize_call_arguments(
        contract,
        value,
        enum_fields=("action",),
        field_aliases=_FIELD_ALIASES,
        field_normalizers={"action": _action_word},
        empty_as_omitted=("id", "target", "name", "prompt", "schedule", TIMEZONE_FIELD),
    )
    if not isinstance(normalized, dict):
        return normalized
    _read_schedule(normalized, problems)
    _read_enabled(normalized, problems)
    _read_recurrence(normalized, problems)
    _read_session(normalized, problems)
    _read_delivery(normalized, problems)
    _read_extras(normalized)
    _omit_placeholders(normalized, problems)
    _read_action(normalized, problems)
    if problems.texts or problems.choice:
        # A corrected call must not silently drop what the call also asked for.
        known = set(contract.input_schema["properties"])
        for key in normalized:
            if key not in known:
                problems.add(f'"{key}" is not a parameter.')
    problems.raise_if_any(normalized)
    return normalized


def render_call(arguments: Mapping[str, Any], **overrides: Any) -> str:
    """Render a canonical ``cron`` call as compact JSON; ``OMIT`` removes a field."""
    call = {**arguments, **overrides}
    ordered: dict[str, Any] = {}
    for name in (*_CALL_ORDER, TIMEZONE_FIELD, ENABLED_FIELD):
        if name in call and call[name] is not OMIT:
            ordered[name] = call[name]
    prompt = ordered.get("prompt")
    if isinstance(prompt, str) and len(prompt) > _LONG_PROMPT:
        ordered["prompt"] = _LONG_PROMPT_STAND_IN
    return json.dumps(ordered, ensure_ascii=False, separators=(",", ":"))


def schedule_kind(schedule: str) -> str:
    """Classify a canonical schedule string: ``once``, ``interval`` or ``cron``."""
    text = schedule.strip()
    if text.startswith("every "):
        return "interval"
    if text.startswith("in "):
        return "once"
    if len(text.split()) == 5:
        return "cron"
    return "once"


def refusal(text: str, arguments: Mapping[str, Any], **overrides: Any) -> str:
    """Return a refusal message that ends with the corrected call."""
    return f"{_REFUSAL_PREFIX}{text} Send: {render_call(arguments, **overrides)}"


# -- operations and wrappers -------------------------------------------------------------


def _single_operation(
    contract: ToolContract, arguments: dict[str, Any], actions: tuple[str, ...]
) -> dict[str, Any]:
    """Keep one operation: drop inert extra ones, refuse several real ones."""
    operations: list[tuple[str | None, str, dict[str, Any]]] = []
    for name, item in arguments.items():
        found = next((action for action in actions if spelling(action) == spelling(name)), None)
        if found is None:
            continue
        if item is True:
            operations.append((name, found, {}))
            continue
        payload = _json_object(item)
        if payload is not None:
            operations.append((name, found, payload))
    if not operations:
        return arguments
    top_action = arguments.get("action")
    if isinstance(top_action, str) and top_action.strip():
        word = _action_word(top_action)
        same = [operation for operation in operations if spelling(operation[1]) == spelling(word)]
        if not same:
            rest = {
                k: v for k, v in arguments.items() if k not in {key for key, _, _ in operations}
            }
            operations.insert(0, (None, top_action, rest))
    if len(operations) < 2:
        key, action, _payload = operations[0]
        if key is not None and arguments.get(key) is True:
            result = {k: v for k, v in arguments.items() if k != key}
            result.setdefault("action", action)
            return result
        return arguments
    real = [operation for operation in operations if not _inert(operation)]
    if len(real) > 1:
        names = " and ".join(action for _key, action, _payload in real)
        calls = " ".join(_operation_call(contract, operation) for operation in real)
        raise ToolContractError(
            f"{_REFUSAL_PREFIX}one call performs one action, but this call asks for {names}. "
            f"Send them as separate calls: {calls}"
        )
    kept = {k: v for k, v in arguments.items() if k not in {key for key, _, _ in operations}}
    key, action, payload = real[0] if real else operations[0]
    if key is None:
        return kept
    kept[key] = payload
    return kept


def _inert(operation: tuple[str | None, str, dict[str, Any]]) -> bool:
    key, action, payload = operation
    if key is None or action == "list":
        return False
    return all(is_placeholder(item) or item in ({}, []) for item in payload.values())


def _operation_call(
    contract: ToolContract, operation: tuple[str | None, str, dict[str, Any]]
) -> str:
    _key, action, payload = operation
    call = {**payload, "action": action}
    try:
        normalized = normalize_cron_arguments(contract, call)
    except ValueError:
        return json.dumps(call, ensure_ascii=False, separators=(",", ":"))
    return render_call(normalized)


def _unwrap_job(arguments: dict[str, Any], problems: _Problems) -> dict[str, Any]:
    """Lift OpenClaw's ``job`` and ``payload`` objects into the call."""
    result = dict(arguments)
    for key in list(result):
        if spelling(key) in _WRAPPER_KEYS:
            inner = _json_object(result[key])
            if inner is not None:
                del result[key]
                _merge(result, inner)
    for key in list(result):
        if spelling(key) in _PAYLOAD_KEYS:
            inner = _json_object(result[key])
            if inner is None:
                continue
            del result[key]
            kind = next(
                (inner.pop(k) for k in list(inner) if spelling(k) in {"kind", "type"}), None
            )
            if kind is not None and spelling(str(kind)) not in _AGENT_TURN_KINDS:
                problems.add(
                    f'a job always runs its prompt as a new Agent Run; payload kind "{kind}" is '
                    "not available, so send the instruction as prompt."
                )
            _merge(result, inner)
    return result


def _merge(outer: dict[str, Any], inner: Mapping[str, Any]) -> None:
    for key, item in inner.items():
        if key in outer and outer[key] != item:
            raise ToolContractError(
                f"{_REFUSAL_PREFIX}it gives two different values for {key}; provide one."
            )
        outer[key] = item


def _action_word(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return _ACTION_SYNONYMS.get(value, value)


# -- schedule ----------------------------------------------------------------------------


def _read_schedule(arguments: dict[str, Any], problems: _Problems) -> None:
    """Resolve every schedule spelling into one canonical ``schedule`` string."""
    type_hint = _schedule_type(arguments, problems)
    candidates: list[tuple[str, str]] = []
    schedule = arguments.pop("schedule", None)
    if isinstance(schedule, dict):
        type_hint = _schedule_object(arguments, schedule, type_hint, candidates, problems)
    elif schedule is not None and not is_placeholder(schedule):
        candidates.append(("schedule", _schedule_text(schedule, type_hint, "schedule", problems)))
    for key in list(arguments):
        word = spelling(key)
        if word in _CRON_KEYS | _AT_KEYS | _EVERY_KEYS | _IN_KEYS:
            item = arguments.pop(key)
            if is_placeholder(item):
                continue
            text = _keyed_schedule(word, key, item, problems)
            if text is not None:
                candidates.append((key, text))
    texts = list(dict.fromkeys(text for _key, text in candidates if text))
    if not texts:
        return
    if len(texts) > 1 and type_hint is not None:
        typed = list(dict.fromkeys(text for text in texts if schedule_kind(text) == type_hint))
        if len(typed) == 1:
            texts = typed
    if len(texts) > 1:
        problems.choose(
            "it names more than one schedule ("
            + ", ".join(f'{key} "{text}"' for key, text in candidates)
            + "); send only the one that is meant:",
            [{"schedule": text} for text in texts],
        )
        arguments["schedule"] = texts[0]
        return
    text = texts[0]
    if type_hint is not None and schedule_kind(text) != type_hint:
        _schedule_type_mismatch(text, type_hint, problems)
    arguments["schedule"] = text


def _schedule_type(arguments: dict[str, Any], problems: _Problems) -> str | None:
    hints: list[str] = []
    for key in [key for key in arguments if spelling(key) in _TYPE_KEYS]:
        item = arguments.pop(key)
        if is_placeholder(item):
            continue
        word = spelling(str(item))
        kind = next((kind for kind, words in _TYPE_WORDS.items() if word in words), None)
        if kind is None and word in _AGENT_TURN_KINDS:
            # OpenClaw spells the payload kind at the top level too.
            continue
        if kind is None:
            problems.add(
                f'"{key}" "{item}" is not a schedule type; the schedule string alone decides it.'
            )
            continue
        hints.append(kind)
    if len(set(hints)) > 1:
        problems.add("it names conflicting schedule types; the schedule string alone decides it.")
        return None
    return hints[0] if hints else None


def _schedule_object(
    arguments: dict[str, Any],
    schedule: dict[str, Any],
    type_hint: str | None,
    candidates: list[tuple[str, str]],
    problems: _Problems,
) -> str | None:
    """Read an OpenClaw-style schedule object such as ``{"kind":"cron","expr":"0 8 * * *"}``."""
    inner = dict(schedule)
    object_hint = _schedule_type(inner, problems)
    if object_hint is not None:
        if type_hint is not None and type_hint != object_hint:
            problems.add(
                "it names conflicting schedule types; the schedule string alone decides it."
            )
        type_hint = object_hint
    for key in list(inner):
        word = spelling(key)
        item = inner.pop(key)
        if is_placeholder(item):
            continue
        if word in {"tz", "timezone", "timezonename"}:
            _set_field(arguments, TIMEZONE_FIELD, item, problems)
        elif word in {"stagger", "staggerms", "exact"}:
            continue
        elif word in _CRON_KEYS | _AT_KEYS | _EVERY_KEYS | _IN_KEYS:
            text = _keyed_schedule(word, key, item, problems)
            if text is not None:
                candidates.append((key, text))
        elif word == "schedule" or word == "value":
            candidates.append((key, _schedule_text(item, type_hint, key, problems)))
        else:
            problems.add(f'schedule field "{key}" is not available; send schedule as one string.')
    return type_hint


def _keyed_schedule(word: str, key: str, item: Any, problems: _Problems) -> str | None:
    if word in _CRON_KEYS:
        return _schedule_text(item, "cron", key, problems)
    if word in _AT_KEYS:
        if isinstance(item, int | float) and not isinstance(item, bool):
            return _epoch_text(item, milliseconds=word.endswith("ms"))
        if isinstance(item, str) and item.strip().isdigit():
            return _epoch_text(int(item.strip()), milliseconds=word.endswith("ms"))
        return _schedule_text(item, "once", key, problems)
    prefix = "every" if word in _EVERY_KEYS else "in"
    if isinstance(item, int | float) and not isinstance(item, bool):
        unit = next((size for suffix, size in _UNIT_BY_KEY_SUFFIX if word.endswith(suffix)), None)
        if unit is None:
            problems.add(
                f'"{key}" {item} has no unit; write it like "{prefix} 30m" or "{prefix} 2h".'
            )
            return None
        duration = _duration_from_seconds(item * unit)
        if duration is None:
            problems.add(f'"{key}" {item} is not a whole number of minutes.')
            return None
        return f"{prefix} {duration}"
    return _schedule_text(item, "interval" if prefix == "every" else "once", key, problems)


def _schedule_text(value: Any, hint: str | None, key: str, problems: _Problems) -> str:
    """Return the canonical schedule string for one supplied schedule value."""
    text = " ".join(str(value).split())
    lowered = text.casefold()
    if lowered in _CRON_MACROS:
        return _CRON_MACROS[lowered]
    prefix, _space, rest = lowered.partition(" ")
    if prefix in {"in", "every"} and rest:
        duration = _duration(rest)
        return f"{prefix} {duration}" if duration else text
    duration = _duration(lowered)
    if duration is not None:
        if hint == "once":
            return f"in {duration}"
        if hint == "interval":
            return f"every {duration}"
        problems.choose(
            f'"{key}" "{text}" can mean one fire after that time or a repeat at that interval:',
            [{"schedule": f"in {duration}"}, {"schedule": f"every {duration}"}],
        )
        return f"in {duration}"
    if _DATE_ONLY.match(text):
        problems.add(
            f'"{key}" "{text}" has no time of day; add one, as in "{text}T09:00".',
            schedule=f"<{text} with a time of day, as {text}THH:MM>",
        )
        return text
    fields = text.split()
    if len(fields) in {6, 7} and "T" not in text:
        problems.add(
            f'"{key}" "{text}" has {len(fields)} fields; cron here takes exactly five '
            "(minute hour day-of-month month day-of-week), without seconds or years.",
            schedule="<five cron fields: minute hour day-of-month month day-of-week>",
        )
    return text


def _schedule_type_mismatch(text: str, type_hint: str, problems: _Problems) -> None:
    kind = schedule_kind(text)
    if kind == "cron" and type_hint == "once":
        problems.choose(
            f'the schedule "{text}" repeats, but the call asks for a one-time job:',
            [{"schedule": text}, {"schedule": text, "repeat": 1}],
        )
    else:
        problems.add(
            f'the schedule "{text}" is not a {type_hint} schedule; the schedule string alone '
            "decides the type."
        )


def _duration(text: str) -> str | None:
    """Return ``30m``, ``2h`` or ``1d`` for a duration such as ``30 minutes`` or ``PT2H``."""
    text = text.strip().casefold()
    match = _DURATION.match(text)
    if match is not None:
        size = _DURATION_WORDS.get(match.group(2))
        if size is None:
            return None
        return _duration_from_seconds(int(match.group(1)) * size)
    iso = _ISO_DURATION.match(text)
    if iso is not None and any(iso.groups()):
        weeks, days, hours, minutes, seconds = (int(part or 0) for part in iso.groups())
        total = (((weeks * 7 + days) * 24 + hours) * 60 + minutes) * 60 + seconds
        return _duration_from_seconds(total)
    return None


def _duration_from_seconds(seconds: float) -> str | None:
    if seconds <= 0 or seconds % 60:
        return None
    minutes = int(seconds // 60)
    for size, unit in ((1440, "d"), (60, "h")):
        if minutes % size == 0:
            return f"{minutes // size}{unit}"
    return f"{minutes}m"


def _epoch_text(value: float, *, milliseconds: bool) -> str:
    seconds = value / 1000 if milliseconds or value > 100_000_000_000 else value
    return datetime.fromtimestamp(seconds, UTC).isoformat()


# -- state, recurrence, Session, delivery --------------------------------------------------


def _read_enabled(arguments: dict[str, Any], problems: _Problems) -> None:
    """Read status and enabled spellings into one ``enabled`` flag."""
    requested: list[bool] = []
    for key in list(arguments):
        word = spelling(key)
        if word not in _ENABLED_KEYS | _PAUSED_KEYS | _STATUS_KEYS:
            continue
        item = arguments.pop(key)
        if is_placeholder(item):
            continue
        if word in _STATUS_KEYS:
            state = spelling(str(item))
            if state in _ACTIVE_WORDS:
                requested.append(True)
            elif state in _PAUSED_WORDS:
                requested.append(False)
            else:
                problems.add(
                    f'status "{item}" cannot be set: a job is enabled or paused, and delete '
                    "removes it."
                )
            continue
        flag = _boolean(item)
        if not isinstance(flag, bool):
            problems.add(f'"{key}" must be true or false.')
            continue
        requested.append(flag if word in _ENABLED_KEYS else not flag)
    if len(set(requested)) > 1:
        problems.add("it asks for the job to be both enabled and paused; choose one.")
    elif requested:
        arguments[ENABLED_FIELD] = requested[0]


def _read_recurrence(arguments: dict[str, Any], problems: _Problems) -> None:
    """Read ``recurring: false`` and one-shot flags as a single remaining fire."""
    one_shot: list[bool] = []
    for key in list(arguments):
        word = spelling(key)
        if word not in _RECURRING_KEYS | _ONE_SHOT_KEYS:
            continue
        flag = _boolean(arguments.pop(key))
        if isinstance(flag, bool):
            one_shot.append(flag if word in _ONE_SHOT_KEYS else not flag)
    if len(set(one_shot)) > 1:
        problems.add("it asks for the job to repeat and not to repeat; choose one.")
        return
    if not one_shot:
        return
    schedule = arguments.get("schedule")
    kind = schedule_kind(schedule) if isinstance(schedule, str) else None
    if one_shot[0]:
        if kind == "interval" and isinstance(schedule, str):
            problems.choose(
                f'the schedule "{schedule}" repeats, but the call asks for a single fire:',
                [{"schedule": schedule}, {"schedule": "in " + schedule.split(" ", 1)[1]}],
            )
        elif kind != "once":
            if "repeat" in arguments and arguments["repeat"] != 1:
                problems.add("it asks for a single fire and for repeat; choose one.")
            arguments["repeat"] = 1
    elif kind == "once":
        problems.add(
            f'the schedule "{schedule}" fires once, but the call asks for a repeating job; '
            'use "every <duration>" or five cron fields.'
        )


def _read_session(arguments: dict[str, Any], problems: _Problems) -> None:
    for key in list(arguments):
        if spelling(key) not in _SESSION_KEYS:
            continue
        item = arguments.pop(key)
        if is_placeholder(item) or spelling(str(item)) in _FRESH_SESSION_WORDS:
            continue
        problems.add(
            f'every fire starts a fresh Session, so "{key}" cannot choose one; the prompt must '
            "carry everything the Run needs."
        )


def _read_delivery(arguments: dict[str, Any], problems: _Problems) -> None:
    for key in list(arguments):
        if spelling(key) not in _DELIVERY_KEYS:
            continue
        item = arguments.pop(key)
        if isinstance(item, dict):
            mode = next((v for k, v in item.items() if spelling(k) == "mode"), None)
            if len(item) == 1 and mode is not None and spelling(str(mode)) in _NO_DELIVERY_WORDS:
                continue
        elif (
            is_placeholder(item)
            or _boolean(item) is False
            or (isinstance(item, str) and spelling(item) in _NO_DELIVERY_WORDS)
        ):
            continue
        problems.add(
            f'a job cannot deliver its reply ("{key}"): each fire runs in a fresh Session and '
            "nobody is notified. Say in prompt how the result should reach the user, for "
            "example which Tool to send it with."
        )


def _read_extras(arguments: dict[str, Any]) -> None:
    """Fold descriptive fields into name and drop fields that request nothing."""
    for key in list(arguments):
        word = spelling(key)
        if word in _DESCRIPTION_KEYS:
            item = arguments.pop(key)
            if "name" not in arguments and isinstance(item, str) and item.strip():
                arguments["name"] = item.strip()
        elif word in _IGNORED_KEYS or (
            word in _LIST_ONLY_KEYS and arguments.get("action") in {None, "list"}
        ):
            del arguments[key]


def _omit_placeholders(arguments: dict[str, Any], problems: _Problems) -> None:
    prompt = arguments.get("prompt")
    if prompt is not None and _is_stand_in(prompt, PLACEHOLDER_WORDS) and "id" not in arguments:
        # A job would run the placeholder itself; a corrected call cannot supply the text.
        raise ToolContractError(
            f'{_REFUSAL_PREFIX}prompt "{prompt}" is a placeholder. Send the complete '
            "instruction the Agent should run at each fire."
        )
    if isinstance(prompt, str) and _TEMPLATE.match(prompt):
        # Dropped, the new instruction the update was meant to set would be lost.
        raise ToolContractError(
            f'{_REFUSAL_PREFIX}prompt "{prompt.strip()}" is a stand-in. Send the complete '
            "instruction the Agent should run at each fire in its place, or leave prompt out "
            "to keep the job's current one."
        )
    target = arguments.get("target")
    if isinstance(target, str) and _TEMPLATE.match(target):
        # Dropped, the job would run as the calling Agent instead of the one meant.
        default = (
            "keep the job's current target" if "id" in arguments else "run the job as yourself"
        )
        raise ToolContractError(
            f'{_REFUSAL_PREFIX}target "{target.strip()}" is a stand-in. Send an existing Agent '
            f"id, or agent@project for a Project member, in its place, or leave target out to "
            f"{default}."
        )
    for name in ("id", "name", "prompt", "schedule"):
        if name in arguments and _is_stand_in(arguments[name], PLACEHOLDER_WORDS):
            del arguments[name]
    if "target" in arguments and _is_stand_in(arguments["target"], _TARGET_PLACEHOLDERS):
        del arguments["target"]
    if TIMEZONE_FIELD in arguments and _is_stand_in(arguments[TIMEZONE_FIELD], PLACEHOLDER_WORDS):
        del arguments[TIMEZONE_FIELD]
    if "repeat" in arguments:
        repeat = arguments["repeat"]
        if isinstance(repeat, str) and spelling(repeat) in _REPEAT_UNLIMITED_WORDS:
            arguments["repeat"] = None
        elif isinstance(repeat, int) and not isinstance(repeat, bool) and repeat < 1:
            problems.add(
                "repeat counts the remaining fires and must be 1 or more; omit it for no limit "
                "(null on update removes a limit)."
            )
            del arguments["repeat"]


def _is_stand_in(value: Any, words: frozenset[str]) -> bool:
    return is_placeholder(value, words) or (isinstance(value, str) and bool(_TEMPLATE.match(value)))


# -- action --------------------------------------------------------------------------------


def _read_action(arguments: dict[str, Any], problems: _Problems) -> None:
    """Infer a missing action and settle calls whose fields do not fit their action."""
    action = arguments.get("action")
    if isinstance(action, str) and not action.strip():
        del arguments["action"]
        action = None
    job_id = arguments.get("id")
    changes = [name for name in _CHANGE_FIELDS if name in arguments]
    if action is None:
        if "prompt" in arguments or "schedule" in arguments:
            action = "update" if job_id is not None else "create"
        elif job_id is not None and changes:
            action = "update"
        elif job_id is not None:
            _refuse_bare_id(job_id, problems)
            return
        else:
            action = "list"
        arguments["action"] = action
    if not isinstance(action, str):
        return
    word = spelling(action)
    if word in _RUN_WORDS:
        problems.choose(
            "a job cannot be fired on demand. To run an instruction once right away, create a "
            "one-time job:",
            [
                {
                    "action": "create",
                    "id": OMIT,
                    "prompt": arguments.get("prompt", "<the job's prompt from list>"),
                    "schedule": "in 1m",
                    "repeat": OMIT,
                }
            ],
        )
        return
    if word in _STOP_WORDS:
        _refuse_stop(job_id, problems)
        return
    if action in {"enable", "disable"} and [name for name in changes if name != ENABLED_FIELD]:
        # Resume or pause while changing the job: one update does both.
        enabled = action == "enable"
        if arguments.get(ENABLED_FIELD, enabled) != enabled:
            problems.add(f"it asks to {action} the job and sets the opposite state; choose one.")
        arguments["action"] = "update"
        arguments[ENABLED_FIELD] = enabled
    elif action in {"enable", "disable"} and ENABLED_FIELD in arguments:
        if arguments.pop(ENABLED_FIELD) != (action == "enable"):
            problems.add(f"it asks to {action} the job and sets the opposite state; choose one.")
    elif action == "create" and arguments.get(ENABLED_FIELD) is True:
        # New jobs start enabled.
        del arguments[ENABLED_FIELD]
    if action == "create" and job_id is not None:
        problems.choose(
            "create makes a new job and assigns its id, so it takes no id. To change job "
            f'"{job_id}", use update; to make a new job, omit id:',
            [{"action": "update"}, {"id": OMIT}],
        )
    elif (
        action == "update" and job_id is None and "prompt" in arguments and "schedule" in arguments
    ):
        problems.choose(
            "update changes an existing job and needs its id from list; to make a new job, "
            "use create:",
            [{"action": "create"}],
        )
    elif action == "list":
        extra = [name for name in changes if name != ENABLED_FIELD]
        arguments.pop(ENABLED_FIELD, None)
        arguments.pop(TIMEZONE_FIELD, None)
        if extra:
            text = (
                "list only shows jobs (optionally one job by id); it takes no "
                + ", ".join(extra)
                + ". To show jobs, leave them out"
            )
            alternatives: list[dict[str, Any]] = [dict.fromkeys(extra, OMIT)]
            if job_id is not None:
                text += f'; to change job "{job_id}", use update'
                alternatives.append({"action": "update"})
            elif "prompt" in arguments:
                text += "; to make a job, use create"
                alternatives.append({"action": "create"})
            problems.choose(text + ":", alternatives)
    elif action == "delete":
        extra = [name for name in changes if name not in {ENABLED_FIELD}]
        arguments.pop(ENABLED_FIELD, None)
        arguments.pop(TIMEZONE_FIELD, None)
        if extra:
            problems.choose(
                "delete removes the job, so it takes no " + ", ".join(extra) + ". Either delete "
                "it or change it:",
                [dict.fromkeys(extra, OMIT), {"action": "update"}],
            )


def _refuse_bare_id(job_id: Any, problems: _Problems) -> None:
    problems.choose(
        f'it names job "{job_id}" but no action. Choose one:',
        [{"action": name} for name in ("delete", "disable", "enable", "list")],
    )


def _refuse_stop(job_id: Any, problems: _Problems) -> None:
    identifier = job_id if job_id is not None else "<job id from list>"
    problems.choose(
        "stopping a job can mean pausing it (enable resumes it) or deleting it:",
        [
            {"action": "disable", "id": identifier},
            {"action": "delete", "id": identifier},
        ],
    )


# -- helpers --------------------------------------------------------------------------------


def _set_field(arguments: dict[str, Any], name: str, item: Any, problems: _Problems) -> None:
    if name in arguments and arguments[name] != item:
        problems.add(f"it gives two different values for {name}; provide one.")
        return
    arguments[name] = item


def _json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            loaded = json.loads(value)
        except ValueError:
            return None
        return loaded if isinstance(loaded, dict) else None
    return None


def _boolean(value: Any) -> Any:
    if isinstance(value, str):
        return _BOOLEAN_WORDS.get(value.strip().casefold(), value)
    return value


__all__ = [
    "ENABLED_FIELD",
    "CronCallRefusedError",
    "OMIT",
    "TIMEZONE_FIELD",
    "UNADVERTISED_PARAMETERS",
    "normalize_cron_arguments",
    "refusal",
    "render_call",
    "schedule_kind",
]
