"""Provider Tool probe: scenario files."""

from __future__ import annotations

import json
from typing import Any

from core.tools.edit import EDIT_TOOL_DESCRIPTION, EDIT_TOOL_NAME, EDIT_TOOL_PARAMETERS
from core.tools.glob import GLOB_TOOL_DESCRIPTION, GLOB_TOOL_NAME, GLOB_TOOL_PARAMETERS
from core.tools.grep import GREP_TOOL_DESCRIPTION, GREP_TOOL_NAME, GREP_TOOL_PARAMETERS
from core.tools.read import READ_TOOL_DESCRIPTION, READ_TOOL_NAME, READ_TOOL_PARAMETERS
from core.tools.write import WRITE_TOOL_DESCRIPTION, WRITE_TOOL_NAME, WRITE_TOOL_PARAMETERS
from scripts.provider_probe.common import ProbeScenario, _probe_messages


def _edit_scenario(case_name: str) -> ProbeScenario:
    base = {
        "path": "src/provider_tool_probe.py",
        "old_string": "value = 1",
        "new_string": "value = 2",
    }
    edit_arguments: dict[str, dict[str, Any]] = {
        "default": base,
        "replace_false": {**base, "replace_all": False},
        "replace_true": {**base, "replace_all": True},
        "multiline": {
            "path": "src/provider_tool_probe.py",
            "old_string": "def old():\n    return 1\n",
            "new_string": "def new():\n    return 2\n",
        },
        "delete": {
            "path": "src/provider_tool_probe.py",
            "old_string": "obsolete = True\n",
            "new_string": "",
        },
    }
    batched_arguments: dict[str, list[dict[str, Any]]] = {
        "multi_file": [
            base,
            {
                "path": "tests/provider_tool_probe.py",
                "old_string": "expected = 1",
                "new_string": "expected = 2",
            },
        ],
        "same_file_sequence": [
            base,
            {
                "path": "src/provider_tool_probe.py",
                "old_string": "value = 2",
                "new_string": "value = 3",
            },
        ],
    }
    edits = (
        batched_arguments[case_name]
        if case_name in batched_arguments
        else [edit_arguments[case_name]]
    )
    expected_arguments = {"edits": edits}
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {EDIT_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "edit",
        [
            {
                "name": EDIT_TOOL_NAME,
                "description": EDIT_TOOL_DESCRIPTION,
                "parameters": EDIT_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        EDIT_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _glob_scenario(case_name: str) -> ProbeScenario:
    pattern = "**/*.py"
    glob_arguments: dict[str, dict[str, Any]] = {
        "default": {"pattern": pattern},
        "path": {"pattern": pattern, "path": "src"},
        "limit": {"pattern": pattern, "limit": 25},
        "offset": {"pattern": pattern, "offset": 10},
        "page": {"pattern": pattern, "limit": 25, "offset": 10},
        "include_false": {"pattern": pattern, "include_ignored": False},
        "include_true": {"pattern": pattern, "include_ignored": True},
        "all": {
            "pattern": pattern,
            "path": "src",
            "limit": 25,
            "offset": 10,
            "include_ignored": True,
        },
    }
    expected_arguments = glob_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {GLOB_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "glob",
        [
            {
                "name": GLOB_TOOL_NAME,
                "description": GLOB_TOOL_DESCRIPTION,
                "parameters": GLOB_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        GLOB_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _grep_scenario(case_name: str) -> ProbeScenario:
    pattern = "TODO|FIXME"
    common_all = {
        "pattern": pattern,
        "path": "src",
        "glob": "**/*.py",
        "ignore_case": True,
        "literal": True,
        "multiline": True,
        "limit": 25,
        "offset": 10,
        "include_ignored": True,
    }
    grep_arguments: dict[str, dict[str, Any]] = {
        "default": {"pattern": pattern},
        "content": {"pattern": pattern, "output_mode": "content"},
        "files": {"pattern": pattern, "output_mode": "files_with_matches"},
        "count": {"pattern": pattern, "output_mode": "count"},
        "path": {"pattern": pattern, "path": "src"},
        "glob": {"pattern": pattern, "glob": "**/*.py"},
        "ignore_case_false": {"pattern": pattern, "ignore_case": False},
        "ignore_case_true": {"pattern": pattern, "ignore_case": True},
        "literal_false": {"pattern": pattern, "literal": False},
        "literal_true": {"pattern": pattern, "literal": True},
        "multiline_false": {"pattern": pattern, "multiline": False},
        "multiline_true": {"pattern": pattern, "multiline": True},
        "context_zero": {"pattern": pattern, "context": 0},
        "context_positive": {"pattern": pattern, "context": 3},
        "limit": {"pattern": pattern, "limit": 25},
        "offset": {"pattern": pattern, "offset": 10},
        "page": {"pattern": pattern, "limit": 25, "offset": 10},
        "include_ignored_false": {"pattern": pattern, "include_ignored": False},
        "include_ignored_true": {"pattern": pattern, "include_ignored": True},
        "all_content": {**common_all, "output_mode": "content", "context": 3},
        "all_files": {**common_all, "output_mode": "files_with_matches"},
        "all_count": {**common_all, "output_mode": "count"},
    }
    expected_arguments = grep_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {GREP_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "grep",
        [
            {
                "name": GREP_TOOL_NAME,
                "description": GREP_TOOL_DESCRIPTION,
                "parameters": GREP_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        GREP_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _read_scenario(case_name: str) -> ProbeScenario:
    path = "src/provider_tool_probe.py"
    read_arguments: dict[str, dict[str, Any]] = {
        "path_only": {"path": path},
        "offset_line": {"path": path, "offset": 25},
        "offset_character": {"path": path, "offset": "25:80"},
        "limit_only": {"path": path, "limit": 120},
        "offset_line_limit": {"path": path, "offset": 25, "limit": 120},
        "offset_character_limit": {
            "path": path,
            "offset": "25:80",
            "limit": 120,
        },
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


def _write_scenario() -> ProbeScenario:
    expected_arguments = {
        "path": "notes/provider-tool-probe.txt",
        "content": "first line\nsecond line\n",
    }
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {WRITE_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and do not add any field."
    )
    return ProbeScenario(
        "write",
        [
            {
                "name": WRITE_TOOL_NAME,
                "description": WRITE_TOOL_DESCRIPTION,
                "parameters": WRITE_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        WRITE_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )
