"""Store values."""

# ruff: noqa: E501
# mypy: disable-error-code=no-any-return

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from core.sessions import DeliveryReceipt, SessionAddress
from core.settings.agent_defaults import validate_fallback_chain
from core.settings.settings import (
    SettingsValidationError,
    validate_temperature,
    validate_thinking_effort,
)
from core.tools.availability import normalize_tool_access
from core.utils.ids import new_id

Json = dict[str, Any]

_PROFILE_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")

_MAX_LIMIT = 100

_DELIVERY_ROUTES = frozenset({"main", "discussion", "ping"})

_DELIVERY_MODES = frozenset({"all", "idle", "pull"})

_SWARM_STATES = frozenset(
    {
        "preparing",
        "running",
        "idle",
        "needs_attention",
        "stopping",
        "stopped",
        "deleting",
        "cancelled",
        "interrupted",
    }
)

_MUTABLE_SWARM_STATES = frozenset({"preparing", "running", "idle", "needs_attention"})

_PARTICIPANT_STATES = frozenset({"idle", "running", "failed", "cancelled", "interrupted"})

_DELIVERY_DEFAULTS: Json = {
    "main": {"mode": "all", "wake_idle": True},
    "discussion": {"mode": "all", "wake_idle": True},
    "ping": {"mode": "all", "wake_idle": True},
    "coalesce_ms": 250,
    "batch_messages": 20,
    "batch_chars": 24_000,
}

DeliveryReceiptLookup = Callable[[SessionAddress, str, str, str], Awaitable[DeliveryReceipt | None]]


class SwarmStoreError(Exception):
    """A stable structured persistence error with no runtime guidance text."""

    def __init__(self, code: str, *, field: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.field = field


@dataclass(frozen=True)
class Page:
    entries: tuple[Json, ...]
    has_more: bool
    cursor: str | None


def _validate_profile(value: Mapping[str, Any]) -> Json:
    profile = _json_object(value)
    allowed = {
        "schema_version",
        "id",
        "revision",
        "slug",
        "name",
        "participants",
        "working_directory",
        "tool_access",
        "tools",
        "allowed_skills",
        "instructions",
        "prompt_blocks",
        "reminders",
        "delivery",
    }
    if set(profile) - allowed:
        raise SwarmStoreError("invalid_arguments")
    if type(profile.get("schema_version")) is not int or profile["schema_version"] != 1:
        raise SwarmStoreError("invalid_arguments", field="schema_version")
    if "id" not in profile:
        profile["id"] = new_id("prf")
    if not isinstance(profile["id"], str) or not profile["id"]:
        raise SwarmStoreError("invalid_arguments", field="id")
    profile.setdefault("slug", profile["id"])
    if not isinstance(profile.get("slug"), str) or _PROFILE_SLUG.fullmatch(profile["slug"]) is None:
        raise SwarmStoreError("invalid_arguments", field="slug")
    _text(profile.get("name"), "name", 120)
    participants = profile.get("participants")
    if not isinstance(participants, list) or not participants:
        raise SwarmStoreError("invalid_arguments", field="participants")
    normalized_participants: list[Json] = []
    for item in participants:
        normalized_participants.append(_formation(item))
    profile["participants"] = normalized_participants
    cwd = profile.get("working_directory")
    if not isinstance(cwd, dict) or cwd.get("kind") not in {"project", "directory"}:
        raise SwarmStoreError("invalid_arguments", field="working_directory")
    cwd_key = "project_id" if cwd["kind"] == "project" else "path"
    if set(cwd) != {"kind", cwd_key} or not isinstance(cwd.get(cwd_key), str) or not cwd[cwd_key]:
        raise SwarmStoreError("invalid_arguments", field="working_directory")
    access = profile.get("tool_access")
    try:
        normalized_access = normalize_tool_access(access)
    except ValueError as error:
        raise SwarmStoreError("invalid_arguments", field="tool_access") from error
    if normalized_access.mode != "selected":
        raise SwarmStoreError("invalid_arguments", field="tool_access")
    profile["tool_access"] = normalized_access.to_dict()
    tools = profile.get("tools", {})
    if not isinstance(tools, dict) or any(
        not isinstance(name, str) or not name or not isinstance(settings, dict)
        for name, settings in tools.items()
    ):
        raise SwarmStoreError("invalid_arguments", field="tools")
    allowed_skills = profile.get("allowed_skills", ["*"])
    if not isinstance(allowed_skills, list) or any(
        not isinstance(skill, str) or not skill for skill in allowed_skills
    ):
        raise SwarmStoreError("invalid_arguments", field="allowed_skills")
    instructions = profile.get("instructions", "")
    if not isinstance(instructions, str):
        raise SwarmStoreError("invalid_arguments", field="instructions")
    from .agent_text import DEFAULT_PROMPT_BLOCKS, DEFAULT_REMINDERS

    prompt_blocks = profile.get("prompt_blocks", DEFAULT_PROMPT_BLOCKS)
    if (
        not isinstance(prompt_blocks, list)
        or any(
            not isinstance(item, str) or ":" not in item or item == "core:agent_body"
            for item in prompt_blocks
        )
        or len(set(prompt_blocks)) != len(prompt_blocks)
    ):
        raise SwarmStoreError("invalid_arguments", field="prompt_blocks")
    reminders = profile.get("reminders", DEFAULT_REMINDERS)
    if (
        not isinstance(reminders, dict)
        or set(reminders) != set(DEFAULT_REMINDERS)
        or any(type(value) is not bool for value in reminders.values())
    ):
        raise SwarmStoreError("invalid_arguments", field="reminders")
    profile["prompt_blocks"] = list(prompt_blocks)
    profile["reminders"] = dict(reminders)
    profile["delivery"] = _delivery(profile.get("delivery", {}))
    profile.setdefault("tools", {})
    profile.setdefault("allowed_skills", ["*"])
    profile.setdefault("instructions", "")
    if "revision" in profile:
        _revision(profile["revision"], "revision", allow_zero=True)
    else:
        profile["revision"] = 0
    return profile


def _formation(value: object) -> Json:
    if not isinstance(value, dict):
        raise SwarmStoreError("invalid_arguments", field="participants")
    allowed = {"model", "count", "thinking_effort", "temperature", "fallback_models"}
    if set(value) - allowed or set(value) < {"model", "count"}:
        raise SwarmStoreError("invalid_arguments", field="participants")
    model = value.get("model")
    count = value.get("count")
    if not isinstance(model, str) or not model.strip() or type(count) is not int or count < 1:
        raise SwarmStoreError("invalid_arguments", field="participants")
    formation: Json = {"model": model, "count": count}
    if "thinking_effort" in value:
        try:
            formation["thinking_effort"] = validate_thinking_effort(
                value["thinking_effort"], label="thinking_effort", allow_none=True
            )
        except SettingsValidationError as error:
            raise SwarmStoreError("invalid_arguments", field="participants") from error
    if "temperature" in value:
        try:
            formation["temperature"] = validate_temperature(
                value["temperature"], label="temperature", allow_none=True
            )
        except SettingsValidationError as error:
            raise SwarmStoreError("invalid_arguments", field="participants") from error
    if "fallback_models" in value:
        try:
            formation["fallback_models"] = validate_fallback_chain(value["fallback_models"])
        except ValueError as error:
            raise SwarmStoreError("invalid_arguments", field="participants") from error
    return formation


def _delivery(value: object) -> Json:
    if not isinstance(value, dict):
        raise SwarmStoreError("invalid_arguments", field="delivery")
    allowed = _DELIVERY_ROUTES | {"coalesce_ms", "batch_messages", "batch_chars"}
    if set(value) - allowed:
        raise SwarmStoreError("invalid_arguments", field="delivery")
    delivery = _copy(_DELIVERY_DEFAULTS)
    for route in _DELIVERY_ROUTES:
        route_value = value.get(route, {})
        if not isinstance(route_value, dict) or set(route_value) - {"mode", "wake_idle"}:
            raise SwarmStoreError("invalid_arguments", field="delivery")
        mode = route_value.get("mode", delivery[route]["mode"])
        wake_idle = route_value.get("wake_idle", delivery[route]["wake_idle"])
        if not isinstance(mode, str) or mode not in _DELIVERY_MODES or type(wake_idle) is not bool:
            raise SwarmStoreError("invalid_arguments", field="delivery")
        delivery[route] = {"mode": mode, "wake_idle": wake_idle}
    for key, minimum, maximum in (
        ("coalesce_ms", 0, 5_000),
        ("batch_messages", 1, 100),
        ("batch_chars", 16_000, 128_000),
    ):
        item = value.get(key, delivery[key])
        if type(item) is not int or not minimum <= item <= maximum:
            raise SwarmStoreError("invalid_arguments", field="delivery")
        delivery[key] = item
    return delivery


def _revision(value: object, field: str, *, allow_zero: bool = False) -> None:
    if type(value) is not int or value < (0 if allow_zero else 1):
        raise SwarmStoreError("invalid_arguments", field=field)


def _json_object(value: Mapping[str, Any]) -> Json:
    if not isinstance(value, Mapping):
        raise SwarmStoreError("invalid_arguments")
    try:
        return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise SwarmStoreError("invalid_arguments") from error


def _text(value: object, field: str, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SwarmStoreError("invalid_arguments", field=field)


def _request_id(value: object) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise SwarmStoreError("invalid_arguments", field="request_id")


def _recipient_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or any(not isinstance(value, str) for value in values):
        raise SwarmStoreError("invalid_arguments", field="recipients")
    return tuple(sorted(set(values)))


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_LIMIT:
        raise SwarmStoreError("invalid_arguments", field="limit")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str) -> Json:
    return json.loads(value)


def _copy(value: Json) -> Json:
    return _load(_dump(value))


def _hash(value: Any) -> str:
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def _page(entries: list[Json], limit: int, cursor: str) -> Page:
    more = len(entries) > limit
    return Page(tuple(entries[:limit]), more, cursor if more else None)


def _post(row: sqlite3.Row) -> Json:
    value = {
        "id": row["id"],
        "sequence": row["sequence"],
        "created_at": row["created_at"],
        "discussion_id": row["discussion_id"],
        "author": {
            "kind": row["author_kind"],
            "id": row["author_id"],
            "name": row["author_name"],
        },
        "text": row["text"],
        "reply_to": row["reply_to"],
        "recipients": _load(row["recipients_json"]),
    }
    # sqlite3.Row membership checks values, not column names.
    columns = row.keys()
    if "route_class" in columns and row["route_class"] is not None:
        value["route_class"] = row["route_class"]
    if "discussion_title" in columns:
        value["discussion_title"] = row["discussion_title"]
    return value
