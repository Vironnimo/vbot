"""Extension values."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.agents.temporary import TemporaryAgentConfig
from core.tools import ToolContext, tool_failure
from core.tools.availability import normalize_tool_access
from core.tools.tools import run_tool_worker

from ._store_values import _hash
from .agent_text import (
    ERRORS,
    LIMIT_CLAMPED,
    REMINDER_TEXTS,
)
from .store import Page, SwarmStoreError

Json = dict[str, Any]


class AgentCallError(Exception):
    """A Session Tool call that failed before any effect, with its Agent-facing explanation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _call_request_id(context: ToolContext) -> str:
    """Derive a mutation's idempotency key from the Tool Call that requested it."""

    return _hash(
        [context.session_id, context.run_id, context.iteration_number, context.tool_call_id]
    )


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
        # Profiles and snapshots saved before the optional field inherit.
        compaction_policy=profile.get("compaction_policy"),
    )


def _reminder(swarm: Json, event: str) -> str:
    enabled = swarm["profile_snapshot"]["reminders"][event]
    return REMINDER_TEXTS[event] if enabled else ""


def _tool_available(swarm: Json, name: str) -> bool:
    return name not in swarm["profile_snapshot"]["tool_access"].get("denied", [])


def _clamped_limit(arguments: Json, notes: list[str], maximum: int = 100) -> int:
    """Return the requested page size, lowered to ``maximum`` with a note."""

    limit = arguments.get("limit", 20)
    if type(limit) is not int or limit < 1:
        raise SwarmStoreError("invalid_arguments", field="limit")
    if limit > maximum:
        notes.append(LIMIT_CLAMPED.format(requested=limit, maximum=maximum))
        arguments["limit"] = limit = maximum
    return limit


def _initial_message(swarm: Json) -> str:
    from .agent_text import INITIAL_MESSAGE

    denied = swarm["profile_snapshot"]["tool_access"].get("denied", [])
    if "swarm_board" not in denied:
        return INITIAL_MESSAGE.format(goal_post_id=swarm["goal_post_id"])
    guidance = (
        "Use your available shared collaboration Tools to discuss and examine the request "
        "with your peers before implementation. Decide together when you are ready to work."
        if "swarm_wiki" not in denied
        else (
            "Work toward the request with the Tools available to you. Other "
            "participants may be working in parallel."
        )
    )
    return f"{guidance}\n\nUser request:\n{swarm['prompt']}"


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
    if code == "scope_mismatch":
        return tool_failure(
            "invalid_arguments",
            f"{error.field} does not match this Session's Swarm or participant. "
            "Omit it only if you intend to use this Session's bound identity. "
            "No change was applied.",
        )
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


def _validate_state(arguments: Json) -> None:
    unexpected = sorted(set(arguments) - {"cursor", "limit"})
    if unexpected:
        raise SwarmStoreError("inapplicable_field", field=unexpected[0])
    cursor = arguments.get("cursor")
    if cursor is not None and not (isinstance(cursor, str) and cursor.strip()):
        raise SwarmStoreError("invalid_arguments", field="cursor")
