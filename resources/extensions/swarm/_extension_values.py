"""Extension values."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.agents.temporary import TemporaryAgentConfig
from core.tools import tool_failure
from core.tools.availability import normalize_tool_access
from core.tools.tools import run_tool_worker

from .agent_text import (
    ERRORS,
    REMINDER_TEXTS,
)
from .store import Page, SwarmStoreError

Json = dict[str, Any]


def _board_page(page: Page, arguments: Json) -> Json:
    result: Json = {"entries": list(page.entries), "has_more": page.has_more}
    if page.has_more:
        result["next_call"] = {
            "tool": "swarm_board",
            "arguments": {**arguments, "cursor": page.cursor},
        }
    return result


def _management_page(page: Page) -> Json:
    result: Json = {"entries": list(page.entries), "has_more": page.has_more}
    if page.has_more:
        result["cursor"] = page.cursor
    return result


def _exact(arguments: Json, allowed: set[str], *, required: set[str] | None = None) -> None:
    if set(arguments) - allowed or not (required or set()).issubset(arguments):
        raise SwarmStoreError("invalid_arguments")


def _string(arguments: Json, key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise SwarmStoreError("invalid_arguments", field=key)
    return value


def _integer(arguments: Json, key: str, *, minimum: int) -> int:
    value = arguments.get(key)
    if type(value) is not int or value < minimum:
        raise SwarmStoreError("invalid_arguments", field=key)
    return value


def _page_arguments(arguments: Json) -> Json:
    _exact(arguments, {"cursor", "limit"})
    result: Json = {}
    if "cursor" in arguments:
        result["cursor"] = _string(arguments, "cursor")
    if "limit" in arguments:
        result["limit"] = _integer(arguments, "limit", minimum=1)
    return result


def _pagination_arguments(arguments: Json) -> Json:
    return _page_arguments(
        {key: value for key, value in arguments.items() if key in {"cursor", "limit"}}
    )


async def _profile_cwd(profile: Json, catalog: Json) -> tuple[Path, str | None]:
    working = profile["working_directory"]
    if working["kind"] == "directory":
        path = Path(working["path"])
        if not path.is_absolute() or not await run_tool_worker(lambda: path.is_dir()):
            raise SwarmStoreError("invalid_arguments", field="working_directory")
        return path, None
    project = next(
        (item for item in catalog.get("projects", []) if item.get("id") == working["project_id"]),
        None,
    )
    if project is None:
        raise SwarmStoreError("invalid_arguments", field="working_directory")
    path = Path(project["cwd"])
    if not await run_tool_worker(lambda: path.is_dir()):
        raise SwarmStoreError("invalid_arguments", field="working_directory")
    return path, working["project_id"]


def _swarm_projection(swarm: Json) -> Json:
    result = dict(swarm)
    result.pop("execution_epoch", None)
    return result


def _validate_profile_catalog(profile: Json, catalog: Json) -> None:
    known_models = {item.get("id") for item in catalog.get("models", [])}
    if any(item["model"] not in known_models for item in profile["participants"]):
        raise SwarmStoreError("invalid_arguments", field="participants")
    known_tools = {item.get("name") for item in catalog.get("tools", [])}
    allowed = profile["tool_access"].get("allowed", [])
    if any(item not in known_tools for item in allowed):
        raise SwarmStoreError("invalid_arguments", field="tool_access")
    project = next(
        (
            item
            for item in catalog.get("projects", [])
            if item.get("id") == profile["working_directory"].get("project_id")
        ),
        None,
    )
    known_skills = {item.get("name") for item in catalog.get("skills", [])}
    known_skills.update((project or {}).get("allowed_skills", []))
    if any(item != "*" and item not in known_skills for item in profile["allowed_skills"]):
        raise SwarmStoreError("invalid_arguments", field="allowed_skills")


def _participant_config(profile: Json, participant: Json, cwd: Path) -> TemporaryAgentConfig:
    ordinal = participant["ordinal"]
    formation = next(
        item for item in profile["participants"] if (ordinal := ordinal - item["count"]) <= 0
    )
    return TemporaryAgentConfig(
        model=participant["model"],
        cwd=cwd,
        tool_access=normalize_tool_access(profile["tool_access"]),
        allowed_skills=profile["allowed_skills"],
        tools=profile["tools"],
        name=participant["display_name"],
        temperature=formation.get("temperature"),
        thinking_effort=formation.get("thinking_effort"),
        fallback_models=formation.get("fallback_models", []),
        instructions=profile["instructions"],
        prompt_blocks=["core:agent_body", *profile["prompt_blocks"]],
    )


def _reminder(swarm: Json, event: str) -> str:
    enabled = swarm["profile_snapshot"]["reminders"][event]
    return REMINDER_TEXTS[event] if enabled else ""


def _swarm_command_argument(argument: str) -> tuple[str, str]:
    profile_id, separator, remainder = argument.strip().partition(" ")
    prompt = remainder.strip()
    if not profile_id or not separator or not prompt:
        raise SwarmStoreError("invalid_arguments", field="argument")
    if prompt.startswith('"'):
        try:
            value = json.loads(prompt)
        except json.JSONDecodeError as error:
            raise SwarmStoreError("invalid_arguments", field="argument") from error
        if not isinstance(value, str) or not value.strip():
            raise SwarmStoreError("invalid_arguments", field="argument")
        prompt = value
    return profile_id, prompt


def _failure(
    error: SwarmStoreError, arguments: Json | None = None, parameters: Json | None = None
) -> Json:
    code = error.code
    if code in {"stale_epoch", "swarm_not_found"}:
        code = "swarm_closed"
    elif code == "participant_not_found":
        code = "participant_inactive"
    guidance = ERRORS.get(code, ERRORS["invalid_arguments"])
    if code == "exact_message_arguments":
        code = "invalid_arguments"
    if error.field:
        field = error.field
        properties = (parameters or {}).get("properties", {})
        if code == "inapplicable_field":
            code = "invalid_arguments"
            action = (arguments or {}).get("action")
            guidance = (
                f"Field '{field}' is not accepted for action '{action}'. "
                "Omit it. No change was applied."
            )
        elif field in properties:
            specification = properties[field]
            correction = specification["description"]
            if "enum" in specification:
                correction += " Choose one of: " + ", ".join(specification["enum"]) + "."
            guidance = f"{field}: {correction} No change was applied."
        elif parameters is not None:
            action = (arguments or {}).get("action")
            guidance = (
                f"Field '{field}' is not accepted"
                + (f" for action '{action}'" if isinstance(action, str) else "")
                + ". Omit it. No change was applied."
            )
        else:
            guidance = f"{field}: {ERRORS['invalid_value']}"
    return tool_failure(code, guidance)


def _validate_board(arguments: Json) -> str:
    fields = {
        "list": {"cursor", "limit"},
        "read": {"discussion_id", "message_id", "cursor", "limit"},
        "post": {"discussion_id", "text", "reply_to", "recipients", "request_id"},
        "create": {"title", "text", "request_id", "recipients"},
        "join": {"discussion_id"},
        "leave": {"discussion_id"},
    }
    action = arguments.get("action")
    if not isinstance(action, str) or action not in fields:
        raise SwarmStoreError("invalid_arguments", field="action")
    unexpected = sorted(set(arguments) - {"action", *fields[action]})
    if unexpected:
        raise SwarmStoreError("inapplicable_field", field=unexpected[0])
    required = {
        "post": {"text", "request_id"},
        "create": {"title", "text", "request_id"},
        "join": {"discussion_id"},
        "leave": {"discussion_id"},
    }
    for key in required.get(action, set()):
        if key not in arguments:
            raise SwarmStoreError("invalid_arguments", field=key)
    for key, value in arguments.items():
        if key == "limit":
            valid = type(value) is int and 1 <= value <= 100
        elif key == "recipients":
            valid = isinstance(value, list) and all(
                isinstance(item, str) and item for item in value
            )
        else:
            maximum = {"text": 16000, "title": 120, "request_id": 128}.get(key)
            valid = (
                isinstance(value, str)
                and bool(value.strip())
                and (maximum is None or len(value) <= maximum)
            )
        if not valid:
            raise SwarmStoreError("invalid_arguments", field=key)
    if "message_id" in arguments and {"discussion_id", "cursor", "limit"} & arguments.keys():
        raise SwarmStoreError("exact_message_arguments")
    return action


def _validate_state(arguments: Json) -> None:
    unexpected = sorted(set(arguments) - {"cursor", "limit"})
    if unexpected:
        raise SwarmStoreError("inapplicable_field", field=unexpected[0])
    for key, value in arguments.items():
        valid = (key == "limit" and type(value) is int and 1 <= value <= 100) or (
            key == "cursor" and isinstance(value, str) and bool(value.strip())
        )
        if not valid:
            raise SwarmStoreError("invalid_arguments", field=key)
