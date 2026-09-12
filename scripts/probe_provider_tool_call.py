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
import sys
from pathlib import Path
from typing import Any

# Resolve internal probe modules from the invoked checkout.
_project_root = Path(__file__).resolve().parents[1]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


from core.providers.accounts import ConnectionRef  # noqa: E402
from core.providers.tool_schema import render_tool_definitions  # noqa: E402
from core.runtime.runtime import Runtime  # noqa: E402
from core.utils.config import Config  # noqa: E402
from scripts.provider_probe.choices import (  # noqa: E402
    ANALYZE_IMAGE_CASES,
    BASH_CASES,
    CALENDAR_CASES,
    CHANNEL_SEND_CASES,
    CRON_CASES,
    DEFAULT_CONNECTION,
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_LINES,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    DEFAULT_TOTAL_TIMEOUT_SECONDS,
    EDIT_CASES,
    GLOB_CASES,
    GREP_CASES,
    HA_CALL_SERVICE_CASES,
    HA_GET_STATE_CASES,
    HA_LIST_ENTITIES_CASES,
    HA_LIST_SERVICES_CASES,
    HISTORY_CASES,
    IMAGE_GENERATION_CASES,
    MCP_CASE_ARGUMENTS,
    MEMORY_CASES,
    OPTIONAL_BOOLEAN_CASES,
    PROBE_SCENARIOS,
    PROCESS_CASES,
    READ_CASES,
    SESSION_READ_CASES,
    SESSION_SEARCH_CASES,
    SKILL_CASES,
    SKILL_MANAGE_CASES,
    STATUS_CASES,
    SUBAGENT_CASES,
    TEXT_TO_SPEECH_CASES,
    WEB_FETCH_CASES,
    WEB_SEARCH_CASES,
    WORD_COUNT_CASES,
)
from scripts.provider_probe.common import ProbeScenario, _start_probe_runtime  # noqa: E402
from scripts.provider_probe.computer_cases import COMPUTER_CASE_ARGUMENTS  # noqa: E402
from scripts.provider_probe.measurements import _compile_probe_contracts  # noqa: E402
from scripts.provider_probe.scenarios import _scenario  # noqa: E402
from scripts.provider_probe.trace import (  # noqa: E402
    _append_interrupted_continuation,
    _load_trace,
    _messages_from_wire,
    _provider_tools_from_wire,
    _trace_request,
)
from scripts.provider_probe.transport import (  # noqa: E402
    _expected_profile,
    _probe_nonstream,
    _probe_stream,
)
from scripts.provider_probe.workflow_mcp import _probe_mcp_workflow  # noqa: E402
from scripts.provider_probe.workflow_patch import _probe_apply_patch  # noqa: E402
from scripts.provider_probe.workflow_reflection import _probe_reflection_workflow  # noqa: E402
from scripts.provider_probe.workflow_swarm import _probe_swarm_tool  # noqa: E402
from scripts.provider_probe.workflow_terminal import _probe_terminal  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--connection", default=DEFAULT_CONNECTION)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--mode", choices=("stream", "nonstream"), default="stream")
    parser.add_argument("--wire", choices=("auto", "openai", "anthropic"), default="auto")
    parser.add_argument("--scenario", choices=PROBE_SCENARIOS, default="direct_required")
    parser.add_argument("--terminal-case", default="all")
    parser.add_argument("--swarm-case", default="all")
    parser.add_argument("--reflection-case", default="all")
    parser.add_argument(
        "--reflection-scope", choices=("all", "memory", "skill", "combined", "learn"), default="all"
    )
    parser.add_argument(
        "--swarm-tool", choices=("swarm_board", "swarm_inbox", "swarm_state"), default="swarm_board"
    )
    parser.add_argument(
        "--computer-case", choices=tuple(COMPUTER_CASE_ARGUMENTS), default="windows"
    )
    parser.add_argument("--mcp-case", choices=tuple(MCP_CASE_ARGUMENTS), default="search")
    parser.add_argument(
        "--mcp-workflow-case", choices=("render", "no_match", "large_result"), default="render"
    )
    parser.add_argument(
        "--optional-case",
        choices=OPTIONAL_BOOLEAN_CASES,
        default="omit",
        help="Requested argument shape for the optional_booleans scenarios.",
    )
    parser.add_argument(
        "--analyze-image-case",
        choices=ANALYZE_IMAGE_CASES,
        default="single",
        help="Exact analyze_image argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--bash-case",
        choices=BASH_CASES,
        default="top_foreground",
        help="Exact top-level or Sub-Agent bash argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--calendar-case",
        choices=CALENDAR_CASES,
        default="create_timed",
        help="Exact calendar action and argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--channel-send-case",
        choices=CHANNEL_SEND_CASES,
        default="telegram_message",
        help="Exact channel_send profile and argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--cron-case",
        choices=CRON_CASES,
        default="create_cron",
        help="Exact cron action and argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--edit-case",
        choices=EDIT_CASES,
        default="default",
        help="Exact edit argument shape requested by the edit scenario.",
    )
    parser.add_argument(
        "--image-generation-case",
        choices=IMAGE_GENERATION_CASES,
        default="full_default",
        help="Exact image_generation profile and argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--memory-case",
        choices=MEMORY_CASES,
        default="list_user",
        help="Exact memory action and argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--glob-case",
        choices=GLOB_CASES,
        default="default",
        help="Exact glob argument shape requested by the glob scenario.",
    )
    parser.add_argument(
        "--grep-case",
        choices=GREP_CASES,
        default="default",
        help="Exact grep argument shape requested by the grep scenario.",
    )
    parser.add_argument(
        "--history-case",
        choices=HISTORY_CASES,
        default="overview_default",
        help="Exact history action and argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--ha-list-entities-case",
        choices=HA_LIST_ENTITIES_CASES,
        default="default",
        help="Exact ha_list_entities argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--ha-get-state-case",
        choices=HA_GET_STATE_CASES,
        default="light",
        help="Exact ha_get_state argument value requested by the scenario.",
    )
    parser.add_argument(
        "--ha-list-services-case",
        choices=HA_LIST_SERVICES_CASES,
        default="default",
        help="Exact ha_list_services argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--ha-call-service-case",
        choices=HA_CALL_SERVICE_CASES,
        default="base",
        help="Exact ha_call_service argument shape requested by the scenario.",
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
        "--session-read-case",
        choices=SESSION_READ_CASES,
        default="whole",
        help="Exact session_read argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--session-search-case",
        choices=SESSION_SEARCH_CASES,
        default="list",
        help="Exact session_search argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--skill-case",
        choices=SKILL_CASES,
        default="activate",
        help="Exact skill argument shape requested by the skill scenario.",
    )
    parser.add_argument(
        "--skill-manage-case",
        choices=SKILL_MANAGE_CASES,
        default="create_own",
        help="Exact skill_manage action and argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--status-case",
        choices=STATUS_CASES,
        default="current",
        help="Exact status argument shape requested by the status scenario.",
    )
    parser.add_argument(
        "--subagent-case",
        choices=SUBAGENT_CASES,
        default="run_self",
        help="Exact subagent action and argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--speech-case",
        choices=TEXT_TO_SPEECH_CASES,
        default="plain",
        help="Exact text_to_speech argument shape requested by the scenario.",
    )
    parser.add_argument(
        "--web-fetch-case",
        choices=WEB_FETCH_CASES,
        default="default",
        help="Exact web_fetch argument shape requested by the web_fetch scenario.",
    )
    parser.add_argument(
        "--web-search-case",
        choices=WEB_SEARCH_CASES,
        default="default",
        help="Exact web_search argument shape requested by the web_search scenario.",
    )
    parser.add_argument(
        "--word-count-case",
        choices=WORD_COUNT_CASES,
        default="plain",
        help="Exact word_count argument value requested by the scenario.",
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


async def _run(args: argparse.Namespace) -> int:
    if args.scenario in {
        "terminal",
        "apply_patch",
        "mcp_workflow",
        "swarm_tool",
        "reflection_workflow",
    }:
        runtime = Runtime(Config(data_dir=args.data_dir))
        _start_probe_runtime(runtime)
        try:
            adapter = runtime.get_adapter(ConnectionRef(args.provider, args.connection))
            try:
                probe = (
                    _probe_terminal
                    if args.scenario == "terminal"
                    else _probe_apply_patch
                    if args.scenario == "apply_patch"
                    else _probe_reflection_workflow
                    if args.scenario == "reflection_workflow"
                    else _probe_swarm_tool
                    if args.scenario == "swarm_tool"
                    else _probe_mcp_workflow
                )
                result = await probe(adapter, args)
            finally:
                await adapter.aclose()
        finally:
            await runtime.aclose()
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1
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
    adapter = runtime.get_adapter(ConnectionRef(args.provider, args.connection))
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
            "analyze_image_case": (
                args.analyze_image_case if scenario.name == "analyze_image" else None
            ),
            "bash_case": args.bash_case if scenario.name == "bash" else None,
            "calendar_case": (args.calendar_case if scenario.name == "calendar" else None),
            "channel_send_case": (
                args.channel_send_case if scenario.name == "channel_send" else None
            ),
            "cron_case": args.cron_case if scenario.name == "cron" else None,
            "edit_case": args.edit_case if scenario.name == "edit" else None,
            "glob_case": args.glob_case if scenario.name == "glob" else None,
            "grep_case": args.grep_case if scenario.name == "grep" else None,
            "image_generation_case": (
                args.image_generation_case if scenario.name == "image_generation" else None
            ),
            "memory_case": args.memory_case if scenario.name == "memory" else None,
            "process_case": args.process_case if scenario.name == "process" else None,
            "read_case": args.read_case if scenario.name == "read" else None,
            "session_read_case": (
                args.session_read_case if scenario.name == "session_read" else None
            ),
            "session_search_case": (
                args.session_search_case if scenario.name == "session_search" else None
            ),
            "skill_case": args.skill_case if scenario.name == "skill" else None,
            "skill_manage_case": (
                args.skill_manage_case if scenario.name == "skill_manage" else None
            ),
            "status_case": args.status_case if scenario.name == "status" else None,
            "subagent_case": args.subagent_case if scenario.name == "subagent" else None,
            "speech_case": args.speech_case if scenario.name == "text_to_speech" else None,
            "web_fetch_case": (args.web_fetch_case if scenario.name == "web_fetch" else None),
            "web_search_case": (args.web_search_case if scenario.name == "web_search" else None),
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
