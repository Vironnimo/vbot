#!/usr/bin/env python
"""Probe one configured Provider for structural Tool-contract conformance.

The probe deliberately prints only structural measurements. It never prints
credentials, prompts, generated content, Tool arguments, or raw Provider
responses.

Examples:
    python scripts/probe_provider_tool_call.py --model glm-5.2 --mode stream
    python scripts/probe_provider_tool_call.py --wire openai --profile explicit_non_strict \
        --scenario nested_operation --mode nonstream
    python scripts/probe_provider_tool_call.py --provider openai \
        --connection openai:subscription --model gpt-5.6-luna --wire openai \
        --profile explicit_non_strict --scenario optional_booleans
    python scripts/probe_provider_tool_call.py --scenario large_arguments --lines 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.providers.tool_schema import (
    ToolSchemaProfile,
    render_tool_definitions,
)
from core.runtime.runtime import Runtime
from core.tools.contracts import ToolContract, ToolContractError, compile_tool_contract
from core.tools.glob import (
    GLOB_TOOL_DESCRIPTION,
    GLOB_TOOL_NAME,
    GLOB_TOOL_PARAMETERS,
)
from core.tools.process import (
    PROCESS_TOOL_DESCRIPTION,
    PROCESS_TOOL_NAME,
    PROCESS_TOOL_PARAMETERS,
)
from core.tools.read import (
    READ_TOOL_DESCRIPTION,
    READ_TOOL_NAME,
    READ_TOOL_PARAMETERS,
)
from core.tools.skill import (
    SKILL_LIST_TOOL_DESCRIPTION,
    SKILL_LIST_TOOL_NAME,
    SKILL_LIST_TOOL_PARAMETERS,
    SKILL_TOOL_DESCRIPTION,
    SKILL_TOOL_NAME,
    SKILL_TOOL_PARAMETERS,
)
from core.tools.web_fetch import (
    WEB_FETCH_TOOL_DESCRIPTION,
    WEB_FETCH_TOOL_NAME,
    WEB_FETCH_TOOL_PARAMETERS,
)
from core.tools.write import (
    WRITE_TOOL_DESCRIPTION,
    WRITE_TOOL_NAME,
    WRITE_TOOL_PARAMETERS,
)
from core.utils.config import Config

DEFAULT_PROVIDER = "opencode-go"
DEFAULT_CONNECTION = "opencode-go:api-key"
DEFAULT_MODEL = "glm-5.2"
DEFAULT_LINES = 8
DEFAULT_IDLE_TIMEOUT_SECONDS = 180.0
DEFAULT_TOTAL_TIMEOUT_SECONDS = 900.0
PROBE_TOOL_NAME = "inspect_probe"
PROBE_SCENARIOS = (
    "direct_required",
    "nested_operation",
    "optional_null",
    "optional_booleans",
    "optional_booleans_bare",
    "optional_booleans_schema_defaults",
    "wrong_type_pressure",
    "missing_required_pressure",
    "unknown_property_pressure",
    "large_arguments",
    "glob",
    "process",
    "read",
    "skill",
    "skill_list",
    "web_fetch",
    "write",
)
OPTIONAL_BOOLEAN_CASES = ("omit", "include_links", "raw", "both")
GLOB_CASES = (
    "default",
    "path",
    "limit",
    "offset",
    "page",
    "include_false",
    "include_true",
    "all",
)
PROCESS_CASES = (
    "status_list",
    "status_one",
    "input_omit_omit",
    "input_true_omit",
    "input_false_omit",
    "input_omit_true",
    "input_omit_false",
    "input_true_true",
    "input_true_false",
    "input_false_true",
    "input_false_false",
    "input_empty_newline",
    "input_empty_eof",
    "kill",
)
READ_CASES = (
    "path_only",
    "offset_line",
    "offset_character",
    "limit_only",
    "offset_line_limit",
    "offset_character_limit",
)
SKILL_CASES = ("activate", "skill_md", "reference", "script", "asset")
WEB_FETCH_CASES = ("default", "markdown", "text", "raw")

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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--connection", default=DEFAULT_CONNECTION)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--mode", choices=("stream", "nonstream"), default="stream")
    parser.add_argument("--wire", choices=("auto", "openai", "anthropic"), default="auto")
    parser.add_argument("--scenario", choices=PROBE_SCENARIOS, default="direct_required")
    parser.add_argument(
        "--optional-case",
        choices=OPTIONAL_BOOLEAN_CASES,
        default="omit",
        help="Requested argument shape for the optional_booleans scenarios.",
    )
    parser.add_argument(
        "--glob-case",
        choices=GLOB_CASES,
        default="default",
        help="Exact glob argument shape requested by the glob scenario.",
    )
    parser.add_argument(
        "--process-case",
        choices=PROCESS_CASES,
        default="status_list",
        help="Exact process argument shape requested by the process scenario.",
    )
    parser.add_argument(
        "--read-case",
        choices=READ_CASES,
        default="path_only",
        help="Exact read argument shape requested by the read scenario.",
    )
    parser.add_argument(
        "--skill-case",
        choices=SKILL_CASES,
        default="activate",
        help="Exact skill argument shape requested by the skill scenario.",
    )
    parser.add_argument(
        "--web-fetch-case",
        choices=WEB_FETCH_CASES,
        default="default",
        help="Exact web_fetch argument shape requested by the web_fetch scenario.",
    )
    parser.add_argument(
        "--profile",
        choices=("auto", "explicit_non_strict", "omit_strict"),
        default="auto",
        help="Expected production Tool-schema profile for this route.",
    )
    parser.add_argument("--lines", type=int, default=DEFAULT_LINES)
    parser.add_argument(
        "--tool-choice",
        choices=("auto", "required", "explicit"),
        default="auto",
    )
    parser.add_argument("--thinking-effort", default="high")
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT_SECONDS)
    parser.add_argument("--total-timeout", type=float, default=DEFAULT_TOTAL_TIMEOUT_SECONDS)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--trace-request",
        type=Path,
        help=(
            "Replay the request body from a vBot Provider debug trace. The trace "
            "content is read locally but never printed."
        ),
    )
    parser.add_argument(
        "--continue-trace-response",
        action="store_true",
        help=(
            "Append the trace's partial assistant response plus an internal recovery "
            "instruction before replaying it."
        ),
    )
    return parser


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


def _optional_boolean_scenario(
    name: str,
    *,
    schema_defaults: bool,
    describe_defaults: bool,
    case_name: str,
) -> ProbeScenario:
    include_links: dict[str, Any] = {"type": "boolean"}
    raw: dict[str, Any] = {"type": "boolean"}
    if describe_defaults:
        include_links["description"] = (
            "Optional JSON boolean. Omit it to preserve Markdown links; the value used "
            "when omitted is true. Set false only to remove link targets."
        )
        raw["description"] = (
            "Optional JSON boolean. Omit it for cleaned text; the value used when "
            "omitted is false. Set true only to request raw HTML."
        )
    else:
        include_links["description"] = (
            "Optional JSON boolean. Send it only when the user explicitly requests a "
            "value; otherwise omit the field."
        )
        raw["description"] = (
            "Optional JSON boolean. Send it only when the user explicitly requests a "
            "value; otherwise omit the field."
        )
    if schema_defaults:
        include_links["default"] = True
        raw["default"] = False
    tool = {
        "name": PROBE_TOOL_NAME,
        "description": "Inspect one synthetic URL without fetching or changing external state.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 1},
                "include_links": include_links,
                "raw": raw,
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    }
    instructions = {
        "omit": (
            "Call the supplied Tool exactly once with url=https://example.com/omit. "
            "Omit both include_links and raw. Do not add either omitted field."
        ),
        "include_links": (
            "Call the supplied Tool exactly once with url=https://example.com/links and "
            "include_links=false. Omit raw. Do not add the omitted field."
        ),
        "raw": (
            "Call the supplied Tool exactly once with url=https://example.com/raw and "
            "raw=true. Omit include_links. Do not add the omitted field."
        ),
        "both": (
            "Call the supplied Tool exactly once with url=https://example.com/both, "
            "include_links=false, and raw=true."
        ),
    }
    return ProbeScenario(
        name,
        [tool],
        _probe_messages(instructions[case_name]),
        PROBE_TOOL_NAME,
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


def _process_scenario(case_name: str) -> ProbeScenario:
    session_id = "process-probe-session"
    text = "probe input"
    process_arguments: dict[str, dict[str, Any]] = {
        "status_list": {"action": "status"},
        "status_one": {"action": "status", "session_id": session_id},
        "input_omit_omit": {
            "action": "input",
            "session_id": session_id,
            "text": text,
        },
        "input_true_omit": {
            "action": "input",
            "session_id": session_id,
            "text": text,
            "newline": True,
        },
        "input_false_omit": {
            "action": "input",
            "session_id": session_id,
            "text": text,
            "newline": False,
        },
        "input_omit_true": {
            "action": "input",
            "session_id": session_id,
            "text": text,
            "eof": True,
        },
        "input_omit_false": {
            "action": "input",
            "session_id": session_id,
            "text": text,
            "eof": False,
        },
        "input_true_true": {
            "action": "input",
            "session_id": session_id,
            "text": text,
            "newline": True,
            "eof": True,
        },
        "input_true_false": {
            "action": "input",
            "session_id": session_id,
            "text": text,
            "newline": True,
            "eof": False,
        },
        "input_false_true": {
            "action": "input",
            "session_id": session_id,
            "text": text,
            "newline": False,
            "eof": True,
        },
        "input_false_false": {
            "action": "input",
            "session_id": session_id,
            "text": text,
            "newline": False,
            "eof": False,
        },
        "input_empty_newline": {
            "action": "input",
            "session_id": session_id,
            "text": "",
            "newline": True,
        },
        "input_empty_eof": {
            "action": "input",
            "session_id": session_id,
            "text": "",
            "newline": False,
            "eof": True,
        },
        "kill": {"action": "kill", "session_id": session_id},
    }
    expected_arguments = process_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {PROCESS_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "process",
        [
            {
                "name": PROCESS_TOOL_NAME,
                "description": PROCESS_TOOL_DESCRIPTION,
                "parameters": PROCESS_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        PROCESS_TOOL_NAME,
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


def _web_fetch_scenario(case_name: str) -> ProbeScenario:
    url = "https://example.com/provider-tool-probe"
    web_fetch_arguments: dict[str, dict[str, Any]] = {
        "default": {"url": url},
        "markdown": {"url": url, "output": "markdown"},
        "text": {"url": url, "output": "text"},
        "raw": {"url": url, "output": "raw"},
    }
    expected_arguments = web_fetch_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {WEB_FETCH_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "web_fetch",
        [
            {
                "name": WEB_FETCH_TOOL_NAME,
                "description": WEB_FETCH_TOOL_DESCRIPTION,
                "parameters": WEB_FETCH_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        WEB_FETCH_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _skill_scenario(case_name: str) -> ProbeScenario:
    name = "vbot-cli"
    skill_arguments: dict[str, dict[str, Any]] = {
        "activate": {"name": name},
        "skill_md": {"name": name, "file_path": "SKILL.md"},
        "reference": {"name": name, "file_path": "references/commands.md"},
        "script": {"name": name, "file_path": "scripts/run.py"},
        "asset": {"name": name, "file_path": "assets/template.txt"},
    }
    expected_arguments = skill_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {SKILL_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "skill",
        [
            {
                "name": SKILL_TOOL_NAME,
                "description": SKILL_TOOL_DESCRIPTION,
                "parameters": SKILL_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        SKILL_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _skill_list_scenario() -> ProbeScenario:
    expected_arguments: dict[str, Any] = {}
    instruction = (
        f"Call {SKILL_LIST_TOOL_NAME} exactly once with an empty JSON object as its "
        "arguments. Do not add any field."
    )
    return ProbeScenario(
        "skill_list",
        [
            {
                "name": SKILL_LIST_TOOL_NAME,
                "description": SKILL_LIST_TOOL_DESCRIPTION,
                "parameters": SKILL_LIST_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        SKILL_LIST_TOOL_NAME,
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


def _scenario(args: argparse.Namespace) -> ProbeScenario:
    direct = json.loads(json.dumps(PROBE_TOOL))
    name = str(args.scenario)
    if name == "direct_required":
        return ProbeScenario(name, [direct], _probe_messages("Inspect key alpha."), PROBE_TOOL_NAME)
    if name == "nested_operation":
        nested = {
            "name": PROBE_TOOL_NAME,
            "description": "Inspect one synthetic key or list synthetic keys.",
            "parameters": {
                "type": "object",
                "properties": {
                    "request": {
                        "type": "object",
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "operation": {"type": "string", "enum": ["inspect"]},
                                    "key": {"type": "string", "minLength": 1},
                                },
                                "required": ["operation", "key"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "operation": {"type": "string", "enum": ["list"]},
                                },
                                "required": ["operation"],
                                "additionalProperties": False,
                            },
                        ],
                    }
                },
                "required": ["request"],
                "additionalProperties": False,
            },
        }
        return ProbeScenario(
            name,
            [nested],
            _probe_messages("Use the inspect operation for key alpha."),
            PROBE_TOOL_NAME,
        )
    if name == "optional_null":
        direct["parameters"]["properties"]["note"] = {
            "type": ["string", "null"],
            "description": "Optional synthetic note; null and omission have the same meaning.",
        }
        return ProbeScenario(
            name,
            [direct],
            _probe_messages("Inspect key alpha without a note."),
            PROBE_TOOL_NAME,
        )
    if name in {
        "optional_booleans",
        "optional_booleans_bare",
        "optional_booleans_schema_defaults",
    }:
        return _optional_boolean_scenario(
            name,
            schema_defaults=name == "optional_booleans_schema_defaults",
            describe_defaults=name != "optional_booleans_bare",
            case_name=str(getattr(args, "optional_case", "omit")),
        )
    if name == "wrong_type_pressure":
        direct["parameters"]["properties"]["count"] = {"type": "integer", "minimum": 1}
        direct["parameters"]["required"].append("count")
        return ProbeScenario(
            name,
            [direct],
            _probe_messages('Inspect key alpha with count shown as quoted text "7".'),
            PROBE_TOOL_NAME,
        )
    if name == "missing_required_pressure":
        return ProbeScenario(
            name,
            [direct],
            _probe_messages("Call the inspection Tool but omit its required key."),
            PROBE_TOOL_NAME,
        )
    if name == "unknown_property_pressure":
        return ProbeScenario(
            name,
            [direct],
            _probe_messages("Inspect key alpha and also include an extra field named surprise."),
            PROBE_TOOL_NAME,
        )
    if name == "large_arguments":
        content = _probe_content(args.lines)
        direct["parameters"]["properties"]["content"] = {"type": "string", "minLength": 1}
        direct["parameters"]["required"].append("content")
        return ProbeScenario(
            name,
            [direct],
            _probe_messages(
                "Inspect key alpha and copy the payload between the markers verbatim into "
                f"content.\n<PAYLOAD>\n{content}\n</PAYLOAD>"
            ),
            PROBE_TOOL_NAME,
        )
    if name == "glob":
        return _glob_scenario(str(args.glob_case))
    if name == "process":
        return _process_scenario(str(args.process_case))
    if name == "read":
        return _read_scenario(str(args.read_case))
    if name == "skill":
        return _skill_scenario(str(args.skill_case))
    if name == "skill_list":
        return _skill_list_scenario()
    if name == "web_fetch":
        return _web_fetch_scenario(str(args.web_fetch_case))
    if name == "write":
        return _write_scenario()
    raise AssertionError(f"unsupported probe scenario: {name}")


def _expected_profile(args: argparse.Namespace) -> ToolSchemaProfile:
    if args.profile == "explicit_non_strict":
        return "explicit_non_strict"
    if args.profile == "omit_strict":
        return "omit_strict"
    if args.provider == "openai":
        return "explicit_non_strict"
    return "omit_strict"


def _tool_choice(value: str, tool_name: str) -> str | dict[str, Any] | None:
    if value == "auto":
        return None
    if value == "required":
        return "required"
    return {"type": "function", "function": {"name": tool_name}}


def _request_kwargs(
    args: argparse.Namespace,
    traced_request: dict[str, Any] | None = None,
    *,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if traced_request is not None:
        kwargs = {
            key: traced_request[key]
            for key in ("max_tokens", "temperature", "reasoning_effort", "thinking_effort")
            if traced_request.get(key) is not None
        }
        kwargs["tools"] = _provider_tools_from_wire(traced_request.get("tools"))
    else:
        kwargs = {
            "thinking_effort": args.thinking_effort,
            "tools": tools or [PROBE_TOOL],
        }
    if args.max_tokens is not None:
        kwargs["max_tokens"] = args.max_tokens
    selected_tools = kwargs["tools"]
    selected_name = (
        str(selected_tools[0].get("name", PROBE_TOOL_NAME))
        if isinstance(selected_tools, list) and selected_tools
        else PROBE_TOOL_NAME
    )
    if (choice := _tool_choice(args.tool_choice, selected_name)) is not None:
        kwargs["tool_choice"] = choice
    return kwargs


def _provider_tools_from_wire(raw_tools: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_tools, list):
        return []
    tools: list[dict[str, Any]] = []
    for raw_tool in raw_tools:
        if not isinstance(raw_tool, dict):
            continue
        function = raw_tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        description = function.get("description")
        parameters = function.get("parameters")
        if isinstance(name, str) and isinstance(description, str) and isinstance(parameters, dict):
            tools.append(
                {
                    "name": name,
                    "description": description,
                    "parameters": parameters,
                }
            )
    return tools


def _load_trace(path: Path) -> dict[str, Any]:
    trace = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(trace, dict):
        raise ValueError("trace root must be a JSON object")
    return trace


def _trace_request(trace: dict[str, Any]) -> dict[str, Any]:
    request = trace.get("request")
    if not isinstance(request, dict):
        raise ValueError("trace has no request object")
    body = request.get("body")
    if not isinstance(body, str):
        raise ValueError("trace request body is not text")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("trace request body must be a JSON object")
    return parsed


def _partial_assistant_from_trace(trace: dict[str, Any]) -> dict[str, Any]:
    response = trace.get("response")
    if not isinstance(response, dict):
        raise ValueError("trace has no response object")
    body = response.get("body")
    if not isinstance(body, str):
        raise ValueError("trace response body is not text")
    reasoning_parts: list[str] = []
    content_parts: list[str] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            continue
        reasoning = delta.get("reasoning_content")
        if not isinstance(reasoning, str):
            reasoning = delta.get("reasoning")
        content = delta.get("content")
        if isinstance(reasoning, str):
            reasoning_parts.append(reasoning)
        if isinstance(content, str):
            content_parts.append(content)
    reasoning = "".join(reasoning_parts)
    content = "".join(content_parts)
    if not reasoning and not content:
        raise ValueError("trace response contains no partial assistant output")
    return {
        "role": "assistant",
        "content": content or None,
        "reasoning": reasoning or None,
    }


def _append_interrupted_continuation(
    messages: list[dict[str, Any]],
    trace: dict[str, Any],
) -> None:
    messages.append(_partial_assistant_from_trace(trace))
    messages.append(
        {
            "role": "user",
            "content": (
                "[Internal recovery notice: The preceding assistant response was "
                "interrupted by the Provider before a finish signal. Continue the same "
                "work from that exact point without repeating preceding text. If you "
                "announced a Tool action, perform the Tool Call now.]"
            ),
        }
    )


def _messages_from_wire(raw_messages: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_messages, list):
        raise ValueError("trace request has no messages list")
    messages: list[dict[str, Any]] = []
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            raise ValueError("trace request contains a non-object message")
        role = raw_message.get("role")
        message = {
            key: value
            for key, value in raw_message.items()
            if key not in {"reasoning_content", "tool_calls"}
        }
        if role == "assistant":
            reasoning = raw_message.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                message["reasoning"] = reasoning
            raw_tool_calls = raw_message.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                tool_calls: list[dict[str, Any]] = []
                for raw_call in raw_tool_calls:
                    if not isinstance(raw_call, dict):
                        continue
                    function = raw_call.get("function")
                    if not isinstance(function, dict):
                        continue
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {}
                    tool_calls.append(
                        {
                            "id": str(raw_call.get("id", "")),
                            "name": str(function.get("name", "")),
                            "arguments": arguments,
                        }
                    )
                if tool_calls:
                    message["tool_calls"] = tool_calls
        messages.append(message)
    return messages


def _argument_measurements(tool_calls: Any) -> tuple[int, int, int]:
    if not isinstance(tool_calls, list):
        return 0, 0, 0
    call_count = 0
    argument_chars = 0
    content_chars = 0
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        call_count += 1
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            continue
        argument_chars += len(json.dumps(arguments, ensure_ascii=False))
        content = arguments.get("content")
        if isinstance(content, str):
            content_chars += len(content)
    return call_count, argument_chars, content_chars


def _optional_boolean_measurements(
    tool_calls: Any,
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    if len(tools) != 1:
        return {}
    parameters = tools[0].get("parameters")
    properties = parameters.get("properties") if isinstance(parameters, dict) else None
    if not isinstance(properties, dict) or not {
        "url",
        "include_links",
        "raw",
    }.issubset(properties):
        return {}

    measurements: list[dict[str, Any]] = []
    if isinstance(tool_calls, list):
        for index, call in enumerate(tool_calls, start=1):
            arguments = call.get("arguments") if isinstance(call, dict) else None
            if not isinstance(arguments, dict):
                measurements.append(
                    {
                        "call": index,
                        "url": "invalid",
                        "include_links": "invalid",
                        "raw": "invalid",
                        "unexpected_fields": None,
                    }
                )
                continue
            measurements.append(
                {
                    "call": index,
                    "url": "present" if isinstance(arguments.get("url"), str) else "invalid",
                    "include_links": _boolean_argument_state(arguments, "include_links"),
                    "raw": _boolean_argument_state(arguments, "raw"),
                    "unexpected_fields": len(set(arguments) - {"url", "include_links", "raw"}),
                }
            )
    return {"optional_boolean_calls": measurements}


def _boolean_argument_state(arguments: dict[str, Any], name: str) -> str:
    if name not in arguments:
        return "omitted"
    value = arguments[name]
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "invalid"


def _start_probe_runtime(runtime: Runtime) -> None:
    """Bootstrap Provider dependencies without starting background services."""

    def _do_not_start() -> None:
        return None

    for hook_name in (
        "_start_process_manager",
        "_start_channel_service",
        "_start_cron_service",
        "_start_provider_usage_service",
    ):
        setattr(runtime, hook_name, _do_not_start)
    runtime.start()


def _compile_probe_contracts(
    tools: list[dict[str, Any]],
    *,
    require_closed_input: bool = True,
) -> dict[str, ToolContract]:
    contracts: dict[str, ToolContract] = {}
    for tool in tools:
        name = tool.get("name")
        parameters = tool.get("parameters")
        if not isinstance(name, str) or not isinstance(parameters, dict):
            raise ValueError("probe Tool definitions require name and parameters")
        contracts[name] = compile_tool_contract(
            name=name,
            input_schema=parameters,
            require_closed_input=require_closed_input,
        )
    return contracts


def _expected_argument_measurements(
    tool_calls: Any,
    scenario: ProbeScenario,
) -> dict[str, Any]:
    expected = scenario.expected_arguments
    if expected is None:
        return {}
    if not isinstance(tool_calls, list) or len(tool_calls) != 1:
        return {
            "expected_arguments_match": False,
            "expected_call_count": 1,
            "actual_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
            "missing_expected_fields": sorted(expected),
            "unexpected_fields": [],
            "mismatched_fields": [],
        }
    call = tool_calls[0]
    arguments = call.get("arguments") if isinstance(call, dict) else None
    if not isinstance(arguments, dict):
        return {
            "expected_arguments_match": False,
            "expected_call_count": 1,
            "actual_call_count": 1,
            "missing_expected_fields": sorted(expected),
            "unexpected_fields": [],
            "mismatched_fields": [],
        }
    expected_keys = set(expected)
    actual_keys = set(arguments)
    mismatched = sorted(
        key for key in expected_keys & actual_keys if arguments[key] != expected[key]
    )
    missing = sorted(expected_keys - actual_keys)
    unexpected = sorted(actual_keys - expected_keys)
    tool_name_matches = call.get("name") == scenario.primary_tool_name
    return {
        "expected_arguments_match": (
            tool_name_matches and not missing and not unexpected and not mismatched
        ),
        "expected_call_count": 1,
        "actual_call_count": 1,
        "missing_expected_fields": missing,
        "unexpected_fields": unexpected,
        "mismatched_fields": mismatched,
    }


def _validation_measurements(
    tool_calls: Any,
    contracts: dict[str, ToolContract],
) -> dict[str, Any]:
    if not isinstance(tool_calls, list) or not tool_calls:
        return {
            "schema_valid": False,
            "validation_path": None,
            "validation_keyword": None,
            "validation_error_class": "missing_tool_call",
        }
    for call in tool_calls:
        if not isinstance(call, dict):
            return _invalid_measurement("invalid_call_shape")
        name = call.get("name")
        arguments = call.get("arguments")
        if not isinstance(name, str) or name not in contracts:
            return _invalid_measurement("unknown_tool")
        if not isinstance(arguments, dict):
            return _invalid_measurement("arguments_not_object")
        try:
            contracts[name].validate_arguments(arguments)
        except ToolContractError as error:
            path, keyword = _validation_location(str(error))
            return {
                "schema_valid": False,
                "validation_path": path,
                "validation_keyword": keyword,
                "validation_error_class": "ToolContractError",
            }
    return {
        "schema_valid": True,
        "validation_path": None,
        "validation_keyword": None,
        "validation_error_class": None,
    }


def _invalid_measurement(error_class: str) -> dict[str, Any]:
    return {
        "schema_valid": False,
        "validation_path": None,
        "validation_keyword": None,
        "validation_error_class": error_class,
    }


def _validation_location(message: str) -> tuple[str | None, str | None]:
    match = re.match(r"^arguments(?P<path>[^:]*):.*\[(?P<keyword>[^\]]+)\]$", message)
    if match is None:
        return None, None
    return match.group("path") or "/", match.group("keyword")


async def _probe_stream(
    adapter: Any,
    messages: list[dict[str, Any]],
    args: argparse.Namespace,
    traced_request: dict[str, Any] | None,
    tools: list[dict[str, Any]],
    contracts: dict[str, ToolContract],
    scenario: ProbeScenario,
) -> dict[str, Any]:
    started = time.monotonic()
    first_delta_seconds: float | None = None
    last_delta_seconds: float | None = None
    counts: dict[str, int] = {}
    content_chars = 0
    reasoning_chars = 0
    tool_argument_chars = 0
    tool_name_chars = 0
    tool_names_by_id: dict[str, str] = {}
    tool_arguments_by_id: dict[str, str] = {}
    finish_reason: str | None = None
    status = "stream_ended"
    error_type: str | None = None

    stream = adapter.stream(
        messages,
        model_id=args.model,
        **_request_kwargs(args, traced_request, tools=tools),
    )
    iterator = stream.__aiter__()
    try:
        async with asyncio.timeout(args.total_timeout):
            while True:
                try:
                    delta = await asyncio.wait_for(
                        iterator.__anext__(),
                        timeout=args.idle_timeout,
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    status = "idle_timeout"
                    break
                elapsed = time.monotonic() - started
                if first_delta_seconds is None:
                    first_delta_seconds = elapsed
                last_delta_seconds = elapsed
                delta_type = str(delta.get("type", "unknown"))
                counts[delta_type] = counts.get(delta_type, 0) + 1
                text = delta.get("text")
                if delta_type == "content_delta" and isinstance(text, str):
                    content_chars += len(text)
                elif delta_type == "reasoning_delta" and isinstance(text, str):
                    reasoning_chars += len(text)
                elif delta_type == "tool_call_delta":
                    tool_call_id = str(delta.get("id", ""))
                    name_delta = str(delta.get("name_delta", ""))
                    tool_names_by_id[tool_call_id] = (
                        tool_names_by_id.get(tool_call_id, "") + name_delta
                    )
                    arguments_delta = str(delta.get("arguments_delta", ""))
                    tool_arguments_by_id[tool_call_id] = (
                        tool_arguments_by_id.get(tool_call_id, "") + arguments_delta
                    )
                    tool_name_chars += len(name_delta)
                    tool_argument_chars += len(arguments_delta)
                elif delta_type == "finish":
                    finish_reason = str(delta.get("reason", ""))
    except TimeoutError:
        status = "total_timeout"
    except Exception as exc:  # noqa: BLE001 - diagnostics must classify all Provider failures
        status = "error"
        error_type = type(exc).__name__
    finally:
        await stream.aclose()

    parsed_calls: list[dict[str, Any]] = []
    validation = _invalid_measurement("invalid_json")
    try:
        for tool_call_id, name in tool_names_by_id.items():
            arguments = json.loads(tool_arguments_by_id.get(tool_call_id, ""))
            parsed_calls.append({"name": name, "arguments": arguments})
    except json.JSONDecodeError:
        pass
    else:
        validation = _validation_measurements(parsed_calls, contracts)

    return {
        "mode": "stream",
        "status": status,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "first_delta_seconds": (
            round(first_delta_seconds, 3) if first_delta_seconds is not None else None
        ),
        "last_delta_seconds": (
            round(last_delta_seconds, 3) if last_delta_seconds is not None else None
        ),
        "delta_counts": counts,
        "reasoning_chars": reasoning_chars,
        "content_chars": content_chars,
        "tool_name_chars": tool_name_chars,
        "tool_calls": len(parsed_calls),
        "tool_argument_chars": tool_argument_chars,
        "finish_reason": finish_reason,
        "error_type": error_type,
        **_optional_boolean_measurements(parsed_calls, tools),
        **_expected_argument_measurements(parsed_calls, scenario),
        **validation,
    }


async def _probe_nonstream(
    adapter: Any,
    messages: list[dict[str, Any]],
    args: argparse.Namespace,
    traced_request: dict[str, Any] | None,
    tools: list[dict[str, Any]],
    contracts: dict[str, ToolContract],
    scenario: ProbeScenario,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        async with asyncio.timeout(args.total_timeout):
            raw = await adapter.send(
                messages,
                model_id=args.model,
                **_request_kwargs(args, traced_request, tools=tools),
            )
        normalized = adapter.normalize_response(raw, model_id=args.model)
        normalized_tool_calls = normalized.get("tool_calls")
        tool_calls, argument_chars, tool_content_chars = _argument_measurements(
            normalized_tool_calls
        )
        content = normalized.get("content")
        reasoning = normalized.get("reasoning")
        return {
            "mode": "nonstream",
            "status": "complete",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
            "content_chars": len(content) if isinstance(content, str) else 0,
            "tool_calls": tool_calls,
            "tool_argument_chars": argument_chars,
            "tool_content_chars": tool_content_chars,
            "error_type": None,
            **_optional_boolean_measurements(normalized_tool_calls, tools),
            **_expected_argument_measurements(normalized_tool_calls, scenario),
            **_validation_measurements(normalized_tool_calls, contracts),
        }
    except TimeoutError:
        status = "total_timeout"
        error_type = None
    except Exception as exc:  # noqa: BLE001 - diagnostics must classify all Provider failures
        status = "error"
        error_type = type(exc).__name__
    return {
        "mode": "nonstream",
        "status": status,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "error_type": error_type,
        **_invalid_measurement(error_type or status),
    }


async def _run(args: argparse.Namespace) -> int:
    trace = _load_trace(args.trace_request) if args.trace_request else None
    traced_request = _trace_request(trace) if trace is not None else None
    profile = _expected_profile(args)
    if traced_request is None:
        scenario = _scenario(args)
        messages = scenario.messages
        tools = scenario.tools
    else:
        messages = _messages_from_wire(traced_request.get("messages"))
        tools = _provider_tools_from_wire(traced_request.get("tools"))
        if not tools:
            raise ValueError("trace request contains no supported function Tool definitions")
        if args.continue_trace_response:
            if trace is None:
                raise ValueError("--continue-trace-response requires --trace-request")
            _append_interrupted_continuation(messages, trace)
        traced_model = traced_request.get("model")
        if isinstance(traced_model, str) and traced_model:
            args.model = traced_model
        scenario = ProbeScenario("trace_replay", tools, messages, str(tools[0]["name"]))
    contracts = _compile_probe_contracts(
        tools,
        require_closed_input=scenario.require_closed_input,
    )
    rendered = render_tool_definitions(tools, profile=profile)
    strict_true_tool_count = sum(1 for tool in rendered if tool.get("strict") is True)
    if strict_true_tool_count:
        raise AssertionError("vBot must never render a Tool with strict mode enabled")
    explicit_non_strict_tool_count = sum(1 for tool in rendered if tool.get("strict") is False)
    runtime = Runtime(Config(data_dir=args.data_dir))
    _start_probe_runtime(runtime)
    adapter = runtime.get_adapter(args.provider, args.connection)
    request_adapter: Any = adapter
    try:
        if args.wire == "anthropic":
            request_adapter = getattr(adapter, "_messages", None)
            if request_adapter is None:
                raise ValueError("selected Provider adapter has no Anthropic Messages route")
        if args.mode == "stream":
            result = await _probe_stream(
                request_adapter,
                messages,
                args,
                traced_request,
                tools,
                contracts,
                scenario,
            )
        else:
            result = await _probe_nonstream(
                request_adapter,
                messages,
                args,
                traced_request,
                tools,
                contracts,
                scenario,
            )
    finally:
        await adapter.aclose()
        await runtime.aclose()

    result.update(
        {
            "provider": args.provider,
            "connection": args.connection,
            "model": args.model,
            "scenario": scenario.name,
            "profile_id": profile,
            "strict_true_tool_count": strict_true_tool_count,
            "explicit_non_strict_tool_count": explicit_non_strict_tool_count,
            "schema_fingerprint_prefix": contracts[scenario.primary_tool_name].schema_fingerprint[
                :12
            ],
            "tool_choice": args.tool_choice,
            "requested_lines": args.lines if scenario.name == "large_arguments" else None,
            "optional_case": (
                args.optional_case
                if scenario.name
                in {
                    "optional_booleans",
                    "optional_booleans_bare",
                    "optional_booleans_schema_defaults",
                }
                else None
            ),
            "glob_case": args.glob_case if scenario.name == "glob" else None,
            "process_case": args.process_case if scenario.name == "process" else None,
            "read_case": args.read_case if scenario.name == "read" else None,
            "skill_case": args.skill_case if scenario.name == "skill" else None,
            "web_fetch_case": (args.web_fetch_case if scenario.name == "web_fetch" else None),
            "request_messages": len(messages),
            "request_tools": len(tools),
            "trace_replay": traced_request is not None,
            "trace_continuation": args.continue_trace_response,
            "wire": args.wire,
        }
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    successful = (
        result["status"] in {"complete", "stream_ended"}
        and result.get("finish_reason", "tool_calls") == "tool_calls"
        and (result.get("tool_argument_chars", 0) > 0 or result.get("tool_calls", 0) > 0)
        and result.get("schema_valid") is True
        and result.get("expected_arguments_match", True) is True
    )
    return 0 if successful else 1


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
