"""Read ``cron`` calls written in vBot's schema, its earlier schemas, or another harness's dialect.

Models schedule with the same intent under other names: vBot's earlier flat
schema (``agent_id``, ``schedule_type``, ``cron_expression``, ``run_at``,
``status``, ``timezone``, ``session_id``) and operation objects
(``{"create": {...}}``), Claude Code ``CronCreate``/``CronList``/``CronDelete``
(``cron``, ``recurring``, ``durable``), Hermes ``cronjob`` (``job_id``,
``pause``/``resume``/``remove``, ``deliver``), OpenClaw ``cron`` (a ``job``
with ``schedule`` and ``payload`` objects, ``jobId``), and scheduled-task Tools
(``cronExpression``, ``fireAt``, ``description``). With an ``event_id``, the
schedule is an event time, which Models also write as reminder offsets
(``minutes_before: 30``, ``-PT30M``, ``30 minutes before``, ``before_start``).
This owner maps them onto the canonical fields so a call whose intent is clear
executes. What vBot cannot honor as written (delivering to another chat, firing
on demand, several actions in one call, readings with different effects) fails
before any side effect with the corrected call.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from core.tools._cron_call import (
    ENABLED_FIELD,
    OMIT,
    REFUSAL_PREFIX,
    TIMEZONE_FIELD,
    CronCallRefusedError,
    _Problems,
    _set_field,
    refusal,
    render_call,
)
from core.tools._cron_schedule_arguments import (
    _AGENT_TURN_KINDS,
    CLOCK_SCHEDULE,
    EVENT_REPEAT_REFUSAL,
    EVENT_TIME_STAND_IN,
    _names_event_offset,
    _read_event_schedule,
    _read_schedule,
    ambiguous_event_time,
    event_time_readings,
    schedule_kind,
)
from core.tools._durations import (
    LATE_LIMITS_FIELD,
    LATE_LIMITS_PARAMETER,
    take_late_limits,
)
from core.tools.call_syntax import (
    PLACEHOLDER_WORDS,
    SpellingAliases,
    is_placeholder,
    normalize_call_arguments,
    spelling,
)
from core.tools.contracts import ToolContract, ToolContractError

CURRENT_SESSION = "current"
"""The ``session`` that names the calling Session; the handler resolves it."""
NEW_SESSION = "new"
"""The ``session`` that starts each run in a new Session."""
DELIVER_PLATFORM_FIELD = "deliver_platform"
"""The platform a ``deliver`` value named; "here" holds only in a chat on it."""
HERE = "here"
DELIVERY_CHOICES = (HERE, "app", "none")

# Accepted so a requested time zone, paused state or delivery platform reaches
# the handler, which converts, applies or refuses it, and so the handler reports
# ignored late-start limits. Never advertised.
UNADVERTISED_PARAMETERS: dict[str, Any] = {
    TIMEZONE_FIELD: {"type": "string", "minLength": 1},
    ENABLED_FIELD: {"type": "boolean"},
    LATE_LIMITS_FIELD: LATE_LIMITS_PARAMETER,
    DELIVER_PLATFORM_FIELD: {"type": "string", "minLength": 1},
}
_CHANGE_FIELDS = (
    "target",
    "name",
    "prompt",
    "schedule",
    "event_id",
    "repeat",
    "session",
    "deliver",
    ENABLED_FIELD,
)
_LATE_START = (
    "A fire that vBot missed, for example while the server was off, starts once, late, and "
    "its Run is told how late it is. To have a late Run skip the instruction, say so in "
    'prompt, for example "Skip this if it starts more than 30 minutes late."'
)
_TEMPLATE = re.compile(r"^\s*<[^<>]+>\s*$")

_FIELD_ALIASES = SpellingAliases(
    {
        "id": ("job_id", "cron_id", "task_id", "schedule_id"),
        "event_id": ("event", "calendar_event_id", "calendar_event"),
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
_FRESH_SESSION_WORDS = frozenset(
    {"new", "fresh", "isolated", "auto", "default", "newsession", "separate"}
)
# OpenClaw's "main" Session is the conversation the job was made in.
_CURRENT_SESSION_WORDS = frozenset(
    {
        "current",
        "this",
        "here",
        "same",
        "main",
        "self",
        "currentsession",
        "thissession",
        "conversation",
        "thisconversation",
    }
)
_DELIVERY_KEYS = frozenset(
    {"deliver", "delivery", "deliverto", "notify", "notifyoncompletion", "notifyuser", "announce"}
)
_HERE_WORDS = frozenset(
    {
        "here",
        "origin",
        "chat",
        "thischat",
        "this",
        "current",
        "same",
        "reply",
        "conversation",
        "source",
        "last",
        "announce",
        "user",
        "true",
        "yes",
        "on",
    }
)
_APP_WORDS = frozenset({"app", "vbot", "desktop", "notification", "webui", "inbox"})
_NO_DELIVERY_WORDS = frozenset({"none", "local", "off", "silent", "false", "no", "nowhere"})
_PLATFORM_WORDS = frozenset({"telegram", "discord", "slack", "mattermost", "whatsapp"})
_DELIVERY_FORMS = (
    "A job delivers each run's answer to this conversation (here), to vBot (app) or nowhere (none):"
)
_DESCRIPTION_KEYS = frozenset({"description", "desc", "summary", "details"})
# Accepted without effect: vBot jobs always persist, and list always shows every job.
_IGNORED_KEYS = frozenset({"durable", "persist", "persistent", "includedisabled", "reason"})
_LIST_ONLY_KEYS = frozenset({"limit", "offset", "all", "showall"})
SELF_TARGET = "self"
"""The target that names the calling Agent; the handler resolves it."""
_SELF_WORDS = frozenset({"self", "current", "default", "this", "me", "myself", "currentagent"})
_REPEAT_UNLIMITED_WORDS = frozenset({"unlimited", "infinite", "infinity", "forever", "always"})
_BOOLEAN_WORDS = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}


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
    if is_placeholder(normalized.get("event_id")):
        normalized.pop("event_id", None)
    if "event_id" in normalized:
        _read_event_schedule(normalized, problems)
    elif normalized.get("action") == "update" and _names_event_offset(normalized):
        # An update of a job at an event may send the new offset alone.
        _read_event_schedule(normalized, problems, bound=False)
    else:
        _read_schedule(normalized, problems)
    _read_enabled(normalized, problems)
    _read_recurrence(normalized, problems)
    _read_session(normalized, problems)
    _read_delivery(normalized, problems)
    late_limits = take_late_limits(normalized, _LATE_START)
    _read_extras(normalized)
    _omit_placeholders(normalized, problems)
    _read_action(normalized, problems)
    if problems.texts or problems.choice:
        # A corrected call must not silently drop what the call also asked for.
        known = set(contract.input_schema["properties"])
        for key in normalized:
            if key not in known:
                problems.add(f'"{key}" is not a parameter.')
        if late_limits:
            problems.add(late_limits)
    problems.raise_if_any(normalized)
    if late_limits:
        normalized[LATE_LIMITS_FIELD] = late_limits
    return normalized


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
            f"{REFUSAL_PREFIX}one call performs one action, but this call asks for {names}. "
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
                f"{REFUSAL_PREFIX}it gives two different values for {key}; provide one."
            )
        outer[key] = item


def _action_word(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return _ACTION_SYNONYMS.get(value, value)


# -- schedule ----------------------------------------------------------------------------


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
    """Read Session choices as ``current``, ``new`` or a Session id."""
    for key in list(arguments):
        if spelling(key) not in _SESSION_KEYS:
            continue
        item = arguments.pop(key)
        if isinstance(item, str) and spelling(item) in _CURRENT_SESSION_WORDS:
            value = CURRENT_SESSION
        elif is_placeholder(item):
            continue
        elif isinstance(item, str) and spelling(item) in _FRESH_SESSION_WORDS:
            value = NEW_SESSION
        elif isinstance(item, str) and not _TEMPLATE.match(item):
            value = item.strip()
        else:
            problems.add(
                f'"{key}" takes current for this conversation or a Session id; leave it out to '
                "start each run in a new Session.",
                session=OMIT,
            )
            continue
        _set_field(arguments, "session", value, problems)


def _read_delivery(arguments: dict[str, Any], problems: _Problems) -> None:
    """Read delivery requests as ``here``, ``app`` or ``none``."""
    for key in list(arguments):
        if spelling(key) not in _DELIVERY_KEYS:
            continue
        choice = _delivery_choice(arguments.pop(key), problems)
        if choice is None:
            continue
        word, platform = choice
        _set_field(arguments, "deliver", word, problems)
        if platform is not None:
            _set_field(arguments, DELIVER_PLATFORM_FIELD, platform, problems)


def _delivery_choice(item: Any, problems: _Problems) -> tuple[str, str | None] | None:
    """The delivery a value asks for, with the platform it names; None asks for nothing."""
    request = _json_object(item)
    if request is not None:
        # OpenClaw: {"mode": "announce" | "none", "channel": ..., "to": ...}.
        fields = {spelling(name): value for name, value in request.items()}
        mode = fields.get("mode")
        if isinstance(mode, str) and spelling(mode) in _NO_DELIVERY_WORDS:
            return "none", None
        chat = next((fields[name] for name in ("to", "target", "chatid") if name in fields), None)
        if not is_placeholder(chat):
            _refuse_other_chat(f"{fields.get('channel', '')}:{chat}".lstrip(":"), problems)
            return None
        channel = fields.get("channel")
        item = HERE if is_placeholder(channel) else channel
    if isinstance(item, bool):
        return (HERE if item else "none"), None
    if not isinstance(item, str):
        if item is not None:
            _refuse_delivery(str(item), problems)
        return None
    word = spelling(item)
    for choice, words in ((HERE, _HERE_WORDS), ("app", _APP_WORDS), ("none", _NO_DELIVERY_WORDS)):
        if word in words:
            return choice, None
    if word in _PLATFORM_WORDS:
        return HERE, word
    if is_placeholder(item):
        return None
    platform, separator, _chat = item.partition(":")
    if separator and spelling(platform) in _PLATFORM_WORDS:
        _refuse_other_chat(item, problems)
    else:
        _refuse_delivery(item, problems)
    return None


def _refuse_other_chat(chat: str, problems: _Problems) -> None:
    problems.choose(
        f'deliver "{chat}" names another chat. {_DELIVERY_FORMS}',
        [{"deliver": HERE}, {"deliver": "app"}],
    )


def _refuse_delivery(item: str, problems: _Problems) -> None:
    problems.choose(
        f'deliver "{item}" is not a delivery target. {_DELIVERY_FORMS}',
        [{"deliver": choice} for choice in DELIVERY_CHOICES],
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
            f'{REFUSAL_PREFIX}prompt "{prompt}" is a placeholder. Send the complete '
            "instruction the Agent should run at each fire."
        )
    if isinstance(prompt, str) and _TEMPLATE.match(prompt):
        # Dropped, the new instruction the update was meant to set would be lost.
        raise ToolContractError(
            f'{REFUSAL_PREFIX}prompt "{prompt.strip()}" is a stand-in. Send the complete '
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
            f'{REFUSAL_PREFIX}target "{target.strip()}" is a stand-in. Send an existing Agent '
            f"id, or agent@project for a Project member, in its place, or leave target out to "
            f"{default}."
        )
    for name in ("id", "name", "prompt", "schedule"):
        if name in arguments and _is_stand_in(arguments[name], PLACEHOLDER_WORDS):
            del arguments[name]
    target = arguments.get("target")
    if isinstance(target, str) and spelling(target) in _SELF_WORDS:
        # On update this moves the job to the calling Agent, so it is kept, not dropped.
        arguments["target"] = SELF_TARGET
    elif "target" in arguments and _is_stand_in(target, PLACEHOLDER_WORDS):
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
        extra = [name for name in changes if name not in {ENABLED_FIELD, "event_id"}]
        arguments.pop(ENABLED_FIELD, None)
        arguments.pop(TIMEZONE_FIELD, None)
        if extra:
            text = (
                "list only shows jobs (one job by id, or the jobs of an event_id); it takes no "
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
    "CLOCK_SCHEDULE",
    "CURRENT_SESSION",
    "DELIVERY_CHOICES",
    "DELIVER_PLATFORM_FIELD",
    "ENABLED_FIELD",
    "EVENT_REPEAT_REFUSAL",
    "EVENT_TIME_STAND_IN",
    "HERE",
    "NEW_SESSION",
    "SELF_TARGET",
    "CronCallRefusedError",
    "OMIT",
    "REFUSAL_PREFIX",
    "TIMEZONE_FIELD",
    "UNADVERTISED_PARAMETERS",
    "ambiguous_event_time",
    "event_time_readings",
    "normalize_cron_arguments",
    "refusal",
    "render_call",
    "schedule_kind",
]
