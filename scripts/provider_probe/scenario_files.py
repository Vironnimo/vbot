"""Provider Tool probe: scenario files."""

from __future__ import annotations

import json
from typing import Any

from core.tools.read import READ_TOOL_DESCRIPTION, READ_TOOL_NAME, READ_TOOL_PARAMETERS
from scripts.provider_probe.common import ProbeScenario, _probe_messages


def _read_scenario(case_name: str) -> ProbeScenario:
    path = "src/provider_tool_probe.py"
    read_arguments: dict[str, dict[str, Any]] = {
        "path_only": {"path": path},
        "offset_line": {"path": path, "offset": 25},
        "limit_only": {"path": path, "limit": 120},
        "offset_line_limit": {"path": path, "offset": 25, "limit": 120},
    }
    expected_arguments = read_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {READ_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "read",
        [
            {
                "name": READ_TOOL_NAME,
                "description": READ_TOOL_DESCRIPTION,
                "parameters": READ_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        READ_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )
