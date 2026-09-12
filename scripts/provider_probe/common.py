"""Scenario records, synthetic inputs and isolated probe Runtime startup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.runtime.runtime import Runtime

PROJECT_ROOT = Path(__file__).resolve().parents[2]


PROBE_TOOL_NAME = "inspect_probe"


PROBE_TOOL = {
    "name": PROBE_TOOL_NAME,
    "description": "Inspect one synthetic value without changing external state.",
    "parameters": {
        "type": "object",
        "properties": {
            "key": {"type": "string", "minLength": 1},
        },
        "required": ["key"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class ProbeScenario:
    name: str
    tools: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    primary_tool_name: str
    require_closed_input: bool = True
    expected_arguments: dict[str, Any] | None = None


def _probe_content(line_count: int) -> str:
    if line_count <= 0:
        raise ValueError("--lines must be positive")
    return "\n".join(
        f"{index:04d}: deterministic provider Tool Call probe line"
        for index in range(1, line_count + 1)
    )


def _probe_messages(instruction: str) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a deterministic Tool Call conformance probe. Call the supplied "
                "Tool exactly once. Do not answer with ordinary text. "
                "Follow its schema even if the user asks for an invalid representation."
            ),
        },
        {
            "role": "user",
            "content": instruction,
        },
    ]


def _start_probe_runtime(runtime: Runtime) -> None:
    """Bootstrap Provider dependencies without starting background services."""

    def _do_not_start() -> None:
        return None

    for hook_name in (
        "_start_process_manager",
        "_start_channel_service",
        "_start_cron_service",
        "_start_calendar_service",
        "_start_provider_usage_service",
    ):
        setattr(runtime, hook_name, _do_not_start)
    runtime.start()
