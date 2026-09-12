"""Bash: registration behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import core.tools._bash_results as bash_results
import core.tools.bash as bash_module
from core.tools.bash import (
    BASH_SUBAGENT_TOOL_DESCRIPTION,
    BASH_SUBAGENT_TOOL_PARAMETERS,
    BASH_TOOL_DESCRIPTION,
    BASH_TOOL_PARAMETERS,
    project_bash_tool_definitions,
    register_bash_tool,
)
from core.tools.process_manager import ProcessManager
from core.tools.tools import (
    ToolCall,
    ToolContext,
    ToolExecutionConfig,
    ToolExecutor,
    ToolRegistry,
    tool_success,
)
from tests.core.tools.bash_helpers import (
    AGENT_ID,
    RUN_ID,
)
from tests.core.tools.bash_helpers import (
    manager as manager,
)
from tests.core.tools.bash_helpers import (
    shell_env_cache as shell_env_cache,
)


def test_register_bash_tool() -> None:
    registry = ToolRegistry()
    manager = ProcessManager(sweep_interval_seconds=3600)

    register_bash_tool(registry, manager)

    tool = registry.get("bash")
    assert tool.description == BASH_TOOL_DESCRIPTION
    assert tool.description
    assert tool.parameters == BASH_TOOL_PARAMETERS
    assert "oneOf" not in tool.parameters
    assert "additionalProperties" not in tool.parameters
    assert set(tool.parameters["properties"]) == {
        "mode",
        "command",
        "description",
        "workdir",
        "background_after_seconds",
        "timeout",
        "env_keys",
    }
    assert tool.parameters["required"] == ["command"]
    properties = tool.parameters["properties"]
    assert properties["description"]["type"] == "string"
    assert "maxLength" not in tool.parameters["properties"]["description"]
    assert tool.parameters["properties"]["mode"]["enum"] == [
        "foreground",
        "auto",
        "background",
    ]
    env_keys = properties["env_keys"]
    assert env_keys["type"] == "array"
    assert env_keys["items"] == {"type": "string", "minLength": 1}
    assert env_keys["minItems"] == 1
    assert env_keys["uniqueItems"] is True
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in properties.values()
    )
    display = registry.display_for_call(
        "bash",
        {
            "description": "Run the frontend tests",
            "command": "npm test -- --run",
            "mode": "foreground",
        },
    )
    assert display["primary"][0]["value"] == "Run the frontend tests"
    assert display["primary"][0]["kind"] == "description"
    assert tool.parameters["properties"]["background_after_seconds"]["default"] == 30
    assert tool.parallel_safe is True


def test_subagent_projection_exposes_only_non_handoff_bash_modes() -> None:
    definitions = [
        {
            "name": "bash",
            "description": BASH_TOOL_DESCRIPTION,
            "parameters": BASH_TOOL_PARAMETERS,
        },
        {
            "name": "read",
            "description": "Read a file.",
            "parameters": {"type": "object"},
        },
    ]

    assert project_bash_tool_definitions(definitions, nesting_depth=0) is definitions

    projected = project_bash_tool_definitions(definitions, nesting_depth=1)
    bash_definition = projected[0]

    assert bash_definition["description"] == BASH_SUBAGENT_TOOL_DESCRIPTION
    assert bash_definition["description"]
    assert bash_definition["parameters"] == BASH_SUBAGENT_TOOL_PARAMETERS
    parameters = bash_definition["parameters"]
    assert "oneOf" not in parameters
    assert "additionalProperties" not in parameters
    assert parameters["required"] == ["command"]
    assert parameters["properties"]["mode"]["enum"] == ["foreground", "auto"]
    assert parameters["properties"]["background_after_seconds"]["default"] == 1800
    assert projected[1] is definitions[1]
    assert definitions[0]["description"] == BASH_TOOL_DESCRIPTION
    assert definitions[0]["parameters"] == BASH_TOOL_PARAMETERS


@pytest.mark.asyncio
async def test_two_bash_calls_can_run_concurrently_by_default(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_count = 0
    max_active_count = 0
    both_started = asyncio.Event()

    async def fake_bash_handler(
        context: ToolContext,
        arguments: dict[str, Any],
        process_manager: ProcessManager,
        *,
        trigger_service: Any | None = None,
        credential_resolver: Callable[[str], str] | None = None,
    ) -> dict[str, Any]:
        nonlocal active_count, max_active_count
        assert process_manager is manager
        assert trigger_service is None
        assert credential_resolver is None
        assert arguments["command"].startswith("download-")
        active_count += 1
        max_active_count = max(max_active_count, active_count)
        if max_active_count == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        active_count -= 1
        return tool_success({"status": "completed", "call_id": context.tool_call_id})

    monkeypatch.setattr(bash_module, "bash_handler", fake_bash_handler)
    registry = ToolRegistry()
    register_bash_tool(registry, manager)
    executor = ToolExecutor(registry, per_run_limit=2, global_limit=2)

    results = await executor.execute_many(
        [
            ToolCall(
                id="download-1",
                name="bash",
                arguments={"command": "download-one", "mode": "foreground"},
            ),
            ToolCall(
                id="download-2",
                name="bash",
                arguments={"command": "download-two", "mode": "foreground"},
            ),
        ],
        ToolExecutionConfig(
            agent_id=AGENT_ID,
            session_id="session-a",
            run_id=RUN_ID,
            workspace=tmp_path,
            vbot_root=tmp_path,
            data_root=tmp_path,
            allowed_tools=["bash"],
        ),
    )

    assert max_active_count == 2
    assert [result["data"]["call_id"] for result in results] == [
        "download-1",
        "download-2",
    ]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "<1s"),
        (0.4, "<1s"),
        (0.6, "1s"),
        (45.2, "45s"),
        (845.0, "14m 5s"),
        (3723.0, "1h 2m 3s"),
    ],
)
def test_format_elapsed_duration_renders_compact_durations(seconds: float, expected: str) -> None:
    """Elapsed abort times render as compact h/m/s strings."""
    assert bash_results._format_elapsed_duration(seconds) == expected
