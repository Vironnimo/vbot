"""Declarations and display summaries for the Computer Use Extension."""

from __future__ import annotations

from typing import Any

from core.extensions import ExtensionAPI
from core.tools import ToolDisplay

from ._tools import (
    COMPUTER_APPS_DESCRIPTION,
    COMPUTER_APPS_PARAMETERS,
    COMPUTER_BATCH_DESCRIPTION,
    COMPUTER_BATCH_PARAMETERS,
    COMPUTER_DESCRIPTION,
    COMPUTER_PARAMETERS,
    RESULT_SCHEMA,
    normalize_apps,
    normalize_batch,
    normalize_computer,
)

TOOL_FAMILY = "computer_use"


def _summary(arguments: dict[str, Any]) -> str | None:
    actions = arguments.get("actions")
    if not isinstance(actions, list):
        return None
    words = [str(item.get("action") or "?") for item in actions if isinstance(item, dict)]
    return f"{len(actions)} actions: " + "; ".join(words)


def _apps_summary(arguments: dict[str, Any]) -> str | None:
    apps = arguments.get("apps")
    subject = arguments.get("app") or arguments.get("query")
    if isinstance(apps, list):
        subject = ", ".join(str(app) for app in apps)
    action = arguments.get("action")
    return f"{action} · {subject}" if action and subject else None


def register(api: ExtensionAPI) -> None:
    from .extension import ASK_SETTING, READINESS_HINT, ComputerUseService

    service = ComputerUseService(api)
    api.register_settings(
        [
            {
                "key": ASK_SETTING,
                "type": "toggle",
                "label": "Ask before each app",
                "description": (
                    "When on, an Agent asks you in the WebUI for each app it wants to use in a "
                    "Session, and apps you have not approved are hidden from it. When off, every "
                    "Agent allowed to use Computer Use operates all apps without asking."
                ),
                "default": False,
            }
        ]
    )
    api.operations.startup.append(service.start)
    api.operations.pending_inputs = service.pending_inputs
    api.operations.input_response_operation = "respond"
    api.operations.register(
        "control",
        "Inspect Computer Use's desktop control, or end it as the user.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "stop"]},
                "control_id": {"type": "string", "minLength": 1},
            },
            "allOf": [
                {
                    "if": {"properties": {"action": {"const": "stop"}}, "required": ["action"]},
                    "then": {"required": ["control_id"]},
                }
            ],
            "additionalProperties": False,
        },
        service.control,
    )
    api.operations.register(
        "respond",
        "Answer one pending Computer Use access request with accept, decline or cancel.",
        {
            "type": "object",
            "properties": {"request_id": {"type": "string"}, "response": {"type": "object"}},
            "required": ["request_id", "response"],
            "additionalProperties": False,
        },
        service.respond,
        secret=True,
    )
    api.on_shutdown(service.close)
    api.on("run_end", service.run_end)
    api.register_tool_family(TOOL_FAMILY, "Computer Use")
    shared: dict[str, Any] = {
        "requires_opt_in": True,
        "parallel_safe": False,
        "open_input_schema": True,
        "ready": service.ready,
        "readiness_hint": READINESS_HINT,
        "result_schema": RESULT_SCHEMA,
        "family": TOOL_FAMILY,
    }
    api.register_tool(
        "computer",
        COMPUTER_DESCRIPTION,
        COMPUTER_PARAMETERS,
        service.computer,
        summary="Operate a computer's desktop with mouse and keyboard.",
        display=ToolDisplay(summary_fields=("action",)),
        argument_normalizer=normalize_computer,
        secret_sinks={"text": "typing"},
        **shared,
    )
    api.register_tool(
        "computer_batch",
        COMPUTER_BATCH_DESCRIPTION,
        COMPUTER_BATCH_PARAMETERS,
        service.computer_batch,
        summary="Run several desktop steps in one call.",
        display=ToolDisplay(summary_builder=_summary),
        argument_normalizer=normalize_batch,
        secret_sinks={"actions": "typing"},
        **shared,
    )
    api.register_tool(
        "computer_apps",
        COMPUTER_APPS_DESCRIPTION,
        COMPUTER_APPS_PARAMETERS,
        service.computer_apps,
        summary="Find, focus or approve desktop apps.",
        display=ToolDisplay(summary_fields=("action",), summary_builder=_apps_summary),
        argument_normalizer=normalize_apps,
        **shared,
    )


__all__ = ["register"]
