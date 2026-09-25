"""Scenario records, synthetic inputs and isolated probe Runtime startup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.chat.wire_shaping import model_facing_request
from core.runtime.runtime import Runtime
from core.tools import registry_tool_name

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


class ModelFacingAdapter:
    """Provider adapter view that names Tools the way Chat's requests do.

    Probes call adapters directly, so without this they would offer the shell
    Tool as ``bash`` on Windows, where production offers ``powershell``. Probe
    code keeps registry names; requests are renamed and returned Tool Calls are
    mapped back. Streamed name deltas stay as the Model sent them.
    """

    def __init__(self, adapter: Any) -> None:
        self._adapter = adapter

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    async def send(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        messages, kwargs = _model_facing(messages, kwargs)
        return await self._adapter.send(messages, **kwargs)

    def stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        messages, kwargs = _model_facing(messages, kwargs)
        return self._adapter.stream(messages, **kwargs)

    def normalize_response(self, response: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        normalized: dict[str, Any] = self._adapter.normalize_response(response, **kwargs)
        calls = normalized.get("tool_calls")
        if not isinstance(calls, list):
            return normalized
        return {
            **normalized,
            "tool_calls": [
                {**call, "name": registry_tool_name(call["name"])}
                if isinstance(call, dict) and isinstance(call.get("name"), str)
                else call
                for call in calls
            ],
        }


def _model_facing(
    messages: list[dict[str, Any]], kwargs: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tools = kwargs.get("tools")
    messages, projected = model_facing_request(messages, list(tools or []))
    if tools is not None:
        kwargs = {**kwargs, "tools": projected}
    return messages, kwargs


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
