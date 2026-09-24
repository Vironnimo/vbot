"""Failure guidance for the shared CLI output owner; never executes recovery steps."""

from __future__ import annotations

import argparse
import os
import re
import shlex
from dataclasses import dataclass

from cli._server_target import CommandResult


@dataclass(frozen=True)
class RecoveryGuidance:
    explanation: str
    commands: tuple[tuple[str, ...], ...]


_INSPECTIONS = {
    "agent": ("agent", "list"),
    "project": ("project", "list"),
    "channel": ("channel", "list"),
    "provider": ("provider", "list"),
    "model": ("model", "list"),
    "tool": ("tool", "list"),
    "prompt": ("prompt", "list"),
    "skill": ("skill", "inventory"),
    "task-model": ("task-model", "list"),
    "extensions": ("extensions", "list"),
    "cron": ("cron", "list"),
    "bootstrap": ("bootstrap", "list"),
    "config": ("config", "list"),
    "log": ("log", "list"),
    "debug": ("debug", "status"),
    "performance": ("performance", "status"),
    "statistics": ("log", "list"),
    "data-store": ("data-store", "status"),
    "server": ("server", "status"),
    "update": ("server", "status"),
    "autostart": ("autostart", "status"),
}

_CODE_GUIDANCE = {
    "invalid_request": "Check the arguments and allowed values in command help.",
    "method_not_found": "The server does not recognize this operation. Check that the CLI and "
    "target server use compatible vBot versions; update on the machine that owns each install.",
    "internal_error": "The server could not finish the request. Inspect current state and "
    "the server logs before deciding which step to retry.",
    "active_run": "A Run is active. Inspect its Session and wait for it to finish before retrying.",
    "agent_busy": "The Agent is busy. Inspect its Sessions and wait for active work to finish.",
    "session_busy": "The Session is busy. Inspect it and wait for active work to finish.",
    "project_busy": "The Project is busy. Inspect its Agents and wait for active work to finish.",
    "agent_in_use": "The Agent is still referenced. Inspect its configuration and dependencies "
    "before changing or removing those references.",
    "project_in_use": "The Project is still referenced. Inspect its Agents and dependencies "
    "before changing or removing those references.",
    "last_agent": "The last Agent cannot be removed. Inspect the Agent list and keep at least one.",
    "agent_order_conflict": "The Agent list changed. Read its current order before submitting "
    "a new complete order.",
    "oauth_not_supported": "This Connection does not support OAuth. Inspect the Provider's "
    "Connections and choose the authentication method they advertise.",
    "run_cancelled": "The Run was cancelled. Inspect its Session for completed work before "
    "starting another Run.",
    "session_capability_expired": "The Session capability expired. Reload the Extension page "
    "to obtain a current capability.",
    "channel_config_error": "Inspect the Channel configuration and the reported invalid field.",
    "performance_recording_active": "A performance recording is already running. Inspect it "
    "and stop it before starting another.",
    "performance_recording_inactive": "No performance recording is running. Inspect the "
    "current status or list stored recordings.",
}

# Names both lists without parsing the address: 'agent list' holds only Identity Agents.
_AGENT_NOT_FOUND_EXPLANATION = (
    "The Agent was not found. 'vbot agent list' shows Identity Agents; for a Project "
    "Team member addressed as agent@project, 'vbot project show <project>' shows that "
    "Project's Team. Reuse an exact id from the matching list."
)


def recovery_guidance(args: argparse.Namespace, result: CommandResult | None) -> RecoveryGuidance:
    """Choose valid read/help commands from known context, without inferring rollback."""
    failure = result.failure if result else None
    code = failure.code if failure else None
    explanation = _CODE_GUIDANCE.get(
        code or "",
        "Inspect current state and the details above "
        "before retrying; earlier steps may already be applied.",
    )
    area = args.area
    inspection = _inspection(args)
    if code == "agent_not_found":
        explanation = _AGENT_NOT_FOUND_EXPLANATION
    elif code and code.endswith("_not_found"):
        explanation = (
            "The requested resource was not found. Read the current list and reuse "
            "an exact id; an omitted row in a paginated list does not prove absence."
        )
    elif code and code.endswith("_already_exists"):
        explanation = (
            "A resource with this identifier already exists. Inspect it before "
            "choosing another id or updating the existing resource."
        )
    if code in {"project_not_found", "project_already_exists", "project_in_use", "project_busy"}:
        inspection = ["project", "list"]
    elif code in {"channel_not_found", "channel_already_exists"}:
        inspection = ["channel", "list"]
    elif code in {"agent_not_found", "last_agent", "agent_order_conflict", "agent_in_use"}:
        inspection = ["agent", "list"]
    elif code == "skill_not_found":
        inspection = ["skill", "inventory"]
    elif code == "agent_busy" and getattr(args, "id", None) and area == "agent":
        inspection = ["session", "list", args.id]
    elif code == "oauth_not_supported":
        inspection = ["provider", "list"]

    if failure and failure.request_state in {"not_sent", "unknown"}:
        # The transport result already explains delivery and possible partial effects.
        explanation = ""
        if (
            failure.request_state == "not_sent"
            and result
            and result.instance.host in {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}
        ):
            inspection = ["server", "status"]

    commands = []
    if inspection:
        commands.append(("vbot", *inspection, *_target_options(args, result, inspection[0])))
    if code == "internal_error" and area != "log":
        commands.append(("vbot", "log", "list", *_target_options(args, result, "log")))
    commands.append(("vbot", *args._command_path.split(), "--help"))
    # Values originate only from resource/target fields, never mutation payloads or credentials.
    safe_commands = tuple(
        command
        for command in commands
        if all(not any(ord(char) < 32 for char in token) for token in command)
    )
    return RecoveryGuidance(explanation, safe_commands)


def _inspection(args: argparse.Namespace) -> list[str] | None:
    area = args.area
    if area in {"session", "memory"}:
        agent = getattr(args, "agent", None)
        if not agent:
            return ["agent", "list"]
        return (
            [area, "list", agent, "--scope", args.scope]
            if area == "memory"
            else [area, "list", agent]
        )
    if area == "config" and getattr(args, "path", None):
        return ["config", "describe", args.path]
    if area == "prompt":
        return ["prompt", "list", "--scope", args.scope]
    if area == "skill" and getattr(args, "scope", None):
        if args.scope == "own":
            # This Run shorthand belongs to install, not the read command.
            return ["skill", "inventory"]
        return ["skill", "read", "--scope", args.scope]
    if area == "extensions" and getattr(args, "selector", None) not in {
        None,
        "list",
        "reload",
        "enable",
        "disable",
    }:
        if getattr(args, "rest", None) and args.rest[0] != "set":
            return ["extensions", "operations", args.selector]
        return ["extensions", "show", args.selector]
    if area == "task-model" and getattr(args, "task_type", None):
        return ["task-model", "option", "list", args.task_type]
    if area in {"doctor", "home", "uninstall", "desktop"}:
        # These can fail before a target is resolved, or use an install-record target.
        # Do not invent defaults and thereby inspect a different instance.
        return None
    return list(_INSPECTIONS[area]) if area in _INSPECTIONS else None


def _target_options(args: argparse.Namespace, result: CommandResult | None, area: str) -> list[str]:
    options = []
    for field in ("host", "port", "data_dir"):
        value = getattr(result.instance, field) if result else getattr(args, field, None)
        if value is not None:
            options.extend(["--" + field.replace("_", "-"), str(value)])
    if area in {"server", "autostart"}:
        for field in ("service_name", "task_name") if area == "autostart" else ("service_name",):
            value = getattr(args, field, None)
            if value:
                options.extend(["--" + field.replace("_", "-"), value])
    return options


def format_command(tokens: tuple[str, ...]) -> str:
    """Render literal arguments for the platform's usual shell, including paths with spaces."""
    if os.name != "nt":
        return shlex.join(tokens)
    return " ".join(
        token if re.fullmatch(r"[\w./:@=,+%-]+", token) else "'" + token.replace("'", "''") + "'"
        for token in tokens
    )
