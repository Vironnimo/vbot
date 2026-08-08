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
import importlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.channels.channels import ChannelConfig
from core.providers.tool_schema import (
    ToolSchemaProfile,
    render_tool_definitions,
)
from core.runtime.runtime import Runtime
from core.tools.bash import (
    BASH_TOOL_DESCRIPTION,
    BASH_TOOL_NAME,
    BASH_TOOL_PARAMETERS,
    project_bash_tool_definitions,
)
from core.tools.channel import (
    CHANNEL_SEND_TOOL_NAME,
    _channel_send_definition_profile,
)
from core.tools.contracts import ToolContract, ToolContractError, compile_tool_contract
from core.tools.cron import (
    CRON_TOOL_DESCRIPTION,
    CRON_TOOL_NAME,
    CRON_TOOL_PARAMETERS,
)
from core.tools.edit import (
    EDIT_TOOL_DESCRIPTION,
    EDIT_TOOL_NAME,
    EDIT_TOOL_PARAMETERS,
)
from core.tools.glob import (
    GLOB_TOOL_DESCRIPTION,
    GLOB_TOOL_NAME,
    GLOB_TOOL_PARAMETERS,
)
from core.tools.grep import (
    GREP_TOOL_DESCRIPTION,
    GREP_TOOL_NAME,
    GREP_TOOL_PARAMETERS,
)
from core.tools.history import (
    HISTORY_TOOL_DESCRIPTION,
    HISTORY_TOOL_NAME,
    HISTORY_TOOL_PARAMETERS,
)
from core.tools.image import (
    ANALYZE_IMAGE_TOOL_DESCRIPTION,
    ANALYZE_IMAGE_TOOL_NAME,
    ANALYZE_IMAGE_TOOL_PARAMETERS,
    IMAGE_GENERATION_TEXT_ONLY_TOOL_DESCRIPTION,
    IMAGE_GENERATION_TEXT_ONLY_TOOL_PARAMETERS,
    IMAGE_GENERATION_TOOL_DESCRIPTION,
    IMAGE_GENERATION_TOOL_NAME,
    IMAGE_GENERATION_TOOL_PARAMETERS,
)
from core.tools.memory import (
    MEMORY_TOOL_DESCRIPTION,
    MEMORY_TOOL_NAME,
    MEMORY_TOOL_PARAMETERS,
)
from core.tools.process import (
    PROCESS_TOOL_DESCRIPTION,
    PROCESS_TOOL_NAME,
    PROCESS_TOOL_PARAMETERS,
)
from core.tools.project import (
    PROJECT_TOOL_DESCRIPTION,
    PROJECT_TOOL_NAME,
    PROJECT_TOOL_PARAMETERS,
)
from core.tools.read import (
    READ_TOOL_DESCRIPTION,
    READ_TOOL_NAME,
    READ_TOOL_PARAMETERS,
)
from core.tools.session_search import (
    SESSION_READ_TOOL_DESCRIPTION,
    SESSION_READ_TOOL_NAME,
    SESSION_READ_TOOL_PARAMETERS,
    SESSION_SEARCH_TOOL_DESCRIPTION,
    SESSION_SEARCH_TOOL_NAME,
    SESSION_SEARCH_TOOL_PARAMETERS,
)
from core.tools.skill import (
    SKILL_LIST_TOOL_DESCRIPTION,
    SKILL_LIST_TOOL_NAME,
    SKILL_LIST_TOOL_PARAMETERS,
    SKILL_TOOL_DESCRIPTION,
    SKILL_TOOL_NAME,
    SKILL_TOOL_PARAMETERS,
)
from core.tools.skill_manage import (
    SKILL_MANAGE_TOOL_DESCRIPTION,
    SKILL_MANAGE_TOOL_NAME,
    SKILL_MANAGE_TOOL_PARAMETERS,
)
from core.tools.speech import (
    TEXT_TO_SPEECH_TOOL_DESCRIPTION,
    TEXT_TO_SPEECH_TOOL_NAME,
    TEXT_TO_SPEECH_TOOL_PARAMETERS,
)
from core.tools.status import (
    STATUS_TOOL_DESCRIPTION,
    STATUS_TOOL_NAME,
    STATUS_TOOL_PARAMETERS,
)
from core.tools.subagent import (
    SUBAGENT_TOOL_DESCRIPTION,
    SUBAGENT_TOOL_NAME,
    SUBAGENT_TOOL_PARAMETERS,
)
from core.tools.web_fetch import (
    WEB_FETCH_TOOL_DESCRIPTION,
    WEB_FETCH_TOOL_NAME,
    WEB_FETCH_TOOL_PARAMETERS,
)
from core.tools.web_search import (
    WEB_SEARCH_TOOL_DESCRIPTION,
    WEB_SEARCH_TOOL_NAME,
    WEB_SEARCH_TOOL_PARAMETERS,
)
from core.tools.write import (
    WRITE_TOOL_DESCRIPTION,
    WRITE_TOOL_NAME,
    WRITE_TOOL_PARAMETERS,
)
from core.utils.config import Config

_HOMEASSISTANT_EXTENSION = importlib.import_module("resources.extensions.homeassistant.extension")
HA_LIST_ENTITIES_DESCRIPTION = _HOMEASSISTANT_EXTENSION.HA_LIST_ENTITIES_DESCRIPTION
HA_LIST_ENTITIES_NAME = _HOMEASSISTANT_EXTENSION.HA_LIST_ENTITIES_NAME
HA_LIST_ENTITIES_PARAMETERS = _HOMEASSISTANT_EXTENSION.HA_LIST_ENTITIES_PARAMETERS
HA_GET_STATE_DESCRIPTION = _HOMEASSISTANT_EXTENSION.HA_GET_STATE_DESCRIPTION
HA_GET_STATE_NAME = _HOMEASSISTANT_EXTENSION.HA_GET_STATE_NAME
HA_GET_STATE_PARAMETERS = _HOMEASSISTANT_EXTENSION.HA_GET_STATE_PARAMETERS
HA_LIST_SERVICES_DESCRIPTION = _HOMEASSISTANT_EXTENSION.HA_LIST_SERVICES_DESCRIPTION
HA_LIST_SERVICES_NAME = _HOMEASSISTANT_EXTENSION.HA_LIST_SERVICES_NAME
HA_LIST_SERVICES_PARAMETERS = _HOMEASSISTANT_EXTENSION.HA_LIST_SERVICES_PARAMETERS
HA_CALL_SERVICE_DESCRIPTION = _HOMEASSISTANT_EXTENSION.HA_CALL_SERVICE_DESCRIPTION
HA_CALL_SERVICE_NAME = _HOMEASSISTANT_EXTENSION.HA_CALL_SERVICE_NAME
HA_CALL_SERVICE_PARAMETERS = _HOMEASSISTANT_EXTENSION.HA_CALL_SERVICE_PARAMETERS
_WORD_COUNT_EXAMPLE = importlib.import_module("examples.extensions.word_count")
WORD_COUNT_NAME = _WORD_COUNT_EXAMPLE.WORD_COUNT_NAME
WORD_COUNT_DESCRIPTION = _WORD_COUNT_EXAMPLE.WORD_COUNT_DESCRIPTION
WORD_COUNT_PARAMETERS = _WORD_COUNT_EXAMPLE.WORD_COUNT_PARAMETERS

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
    "analyze_image",
    "bash",
    "channel_send",
    "cron",
    "edit",
    "glob",
    "grep",
    "ha_call_service",
    "ha_get_state",
    "ha_list_entities",
    "ha_list_services",
    "history",
    "image_generation",
    "memory",
    "process",
    "project",
    "read",
    "session_read",
    "session_search",
    "skill",
    "skill_list",
    "skill_manage",
    "status",
    "subagent",
    "text_to_speech",
    "web_fetch",
    "web_search",
    "write",
    "word_count",
)
OPTIONAL_BOOLEAN_CASES = ("omit", "include_links", "raw", "both")
ANALYZE_IMAGE_CASES = ("single", "multiple")
BASH_CASES = (
    "top_foreground",
    "top_foreground_workdir",
    "top_foreground_timeout",
    "top_foreground_env_one",
    "top_foreground_env_many",
    "top_foreground_all_multiline",
    "top_auto_default",
    "top_auto_zero",
    "top_auto_yield",
    "top_auto_timeout",
    "top_auto_all",
    "top_background",
    "top_background_workdir",
    "top_background_timeout",
    "top_background_all",
    "sub_foreground",
    "sub_foreground_all",
    "sub_auto_default",
    "sub_auto_zero",
    "sub_auto_all",
)
CHANNEL_SEND_CASES = (
    "telegram_message",
    "telegram_target",
    "telegram_file",
    "telegram_files",
    "telegram_message_file",
    "telegram_thread",
    "telegram_button",
    "telegram_button_rows",
    "discord_message",
    "discord_target",
    "discord_file",
    "discord_message_file",
    "mixed_telegram_button",
    "mixed_discord_file",
)
CRON_CASES = (
    "create_cron",
    "create_interval",
    "create_once_relative",
    "create_once_iso",
    "create_target",
    "create_name",
    "create_repeat",
    "create_repeat_null",
    "create_all",
    "list",
    "update_target",
    "update_name",
    "update_prompt",
    "update_schedule",
    "update_repeat",
    "update_repeat_null",
    "update_all",
    "delete",
    "enable",
    "disable",
)
EDIT_CASES = ("default", "replace_false", "replace_true", "multiline", "delete")
IMAGE_GENERATION_CASES = (
    "full_default",
    "full_source_one",
    "full_source_many",
    "full_aspect",
    "full_resolution",
    "full_output_dir",
    "full_all",
    "text_default",
    "text_aspect",
    "text_resolution",
    "text_output_dir",
    "text_all",
)
MEMORY_CASES = (
    "list_user",
    "list_agent",
    "add_user",
    "add_agent",
    "replace_user",
    "replace_agent",
    "remove_user",
    "remove_agent",
)
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
GREP_CASES = (
    "default",
    "content",
    "files",
    "count",
    "path",
    "glob",
    "ignore_case_false",
    "ignore_case_true",
    "literal_false",
    "literal_true",
    "multiline_false",
    "multiline_true",
    "context_zero",
    "context_positive",
    "limit",
    "offset",
    "page",
    "include_ignored_false",
    "include_ignored_true",
    "all_content",
    "all_files",
    "all_count",
)
HA_LIST_ENTITIES_CASES = ("default", "domain", "area", "all")
HA_GET_STATE_CASES = ("light", "sensor")
HA_LIST_SERVICES_CASES = ("default", "domain")
HA_CALL_SERVICE_CASES = ("base", "entity", "empty_data", "data", "all")
HISTORY_CASES = (
    "overview_default",
    "overview_limit_min",
    "overview_limit_max",
    "overview_cursor",
    "search_default",
    "search_checkpoint",
    "search_roles_one",
    "search_roles_all",
    "search_match_all_terms",
    "search_match_phrase",
    "search_match_any_term",
    "search_limit",
    "search_all",
    "search_cursor",
    "read_default",
    "read_checkpoint",
    "read_roles_empty",
    "read_roles",
    "read_direction_start",
    "read_direction_end",
    "read_limit",
    "read_all",
    "read_cursor",
    "around_default",
    "around_checkpoint",
    "around_roles",
    "around_before_zero",
    "around_before_max",
    "around_after_zero",
    "around_after_max",
    "around_all",
    "around_cursor",
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
SESSION_READ_CASES = (
    "whole",
    "agent",
    "start",
    "end",
    "range",
    "all",
    "cursor",
)
SESSION_SEARCH_CASES = (
    "list",
    "query",
    "period",
    "agent",
    "session",
    "limit_min",
    "limit_max",
    "all",
    "cursor",
)
SKILL_CASES = ("activate", "skill_md", "reference", "script", "asset")
SKILL_MANAGE_CASES = (
    "create_own",
    "create_global",
    "edit",
    "patch_default",
    "patch_false",
    "patch_true",
    "patch_support",
    "patch_delete",
    "write_script",
    "write_reference",
    "write_asset_empty",
    "remove_file",
    "delete",
)
STATUS_CASES = ("current", "session", "agent_session")
SUBAGENT_CASES = (
    "run_self",
    "run_agent",
    "run_continue",
    "run_model",
    "run_all",
    "thinking_default",
    "thinking_minimal",
    "thinking_low",
    "thinking_medium",
    "thinking_high",
    "thinking_xhigh",
    "thinking_max",
    "thinking_none",
    "status",
    "cancel",
)
TEXT_TO_SPEECH_CASES = ("plain", "unicode_multiline")
WEB_FETCH_CASES = ("default", "markdown", "text", "raw")
WEB_SEARCH_CASES = (
    "default",
    "operator_query",
    "domains_one",
    "domains_many",
    "count_min",
    "count_max",
    "page_first",
    "page_later",
    "recency_day",
    "recency_month",
    "recency_year",
    "all",
)
WORD_COUNT_CASES = ("plain", "empty", "unicode_multiline")

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


def _analyze_image_scenario(case_name: str) -> ProbeScenario:
    analyze_arguments = {
        "single": {
            "prompt": "Read every visible label and report uncertainty.",
            "images": ["images/photo.png"],
        },
        "multiple": {
            "prompt": "Vergleiche beide Bilder.\nNenne Unterschiede und Unsicherheit.",
            "images": ["images/photo.png", "C:/images/reference.png"],
        },
    }
    expected_arguments = analyze_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"), ensure_ascii=False)
    instruction = (
        f"Call {ANALYZE_IMAGE_TOOL_NAME} exactly once with exactly this JSON object as "
        f"its arguments: {rendered_arguments}. Preserve every character, path, and array "
        "item; do not add any field."
    )
    return ProbeScenario(
        "analyze_image",
        [
            {
                "name": ANALYZE_IMAGE_TOOL_NAME,
                "description": ANALYZE_IMAGE_TOOL_DESCRIPTION,
                "parameters": ANALYZE_IMAGE_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        ANALYZE_IMAGE_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _image_generation_scenario(case_name: str) -> ProbeScenario:
    prompt = "A red fox in snow, cinematic light."
    one_source = ["images/source.png"]
    many_sources = ["images/source.png", "C:/images/reference.png"]
    image_generation_arguments: dict[str, dict[str, Any]] = {
        "full_default": {"prompt": prompt},
        "full_source_one": {"prompt": prompt, "source_images": one_source},
        "full_source_many": {"prompt": prompt, "source_images": many_sources},
        "full_aspect": {"prompt": prompt, "aspect_ratio": "16:9"},
        "full_resolution": {"prompt": prompt, "resolution": "4K"},
        "full_output_dir": {"prompt": prompt, "output_dir": "assets/generated"},
        "full_all": {
            "prompt": "Ändere das Licht.\nBehalte Motiv und Komposition unverändert.",
            "source_images": many_sources,
            "aspect_ratio": "16:9",
            "resolution": "4K",
            "output_dir": "assets/generated",
        },
        "text_default": {"prompt": prompt},
        "text_aspect": {"prompt": prompt, "aspect_ratio": "16:9"},
        "text_resolution": {"prompt": prompt, "resolution": "4K"},
        "text_output_dir": {"prompt": prompt, "output_dir": "assets/generated"},
        "text_all": {
            "prompt": prompt,
            "aspect_ratio": "16:9",
            "resolution": "4K",
            "output_dir": "assets/generated",
        },
    }
    expected_arguments = image_generation_arguments[case_name]
    text_only = case_name.startswith("text_")
    description = (
        IMAGE_GENERATION_TEXT_ONLY_TOOL_DESCRIPTION
        if text_only
        else IMAGE_GENERATION_TOOL_DESCRIPTION
    )
    parameters = (
        IMAGE_GENERATION_TEXT_ONLY_TOOL_PARAMETERS
        if text_only
        else IMAGE_GENERATION_TOOL_PARAMETERS
    )
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"), ensure_ascii=False)
    instruction = (
        f"Call {IMAGE_GENERATION_TOOL_NAME} exactly once with exactly this JSON object as "
        f"its arguments: {rendered_arguments}. Preserve every character, path, and array "
        "item; omit every field not shown."
    )
    return ProbeScenario(
        "image_generation",
        [
            {
                "name": IMAGE_GENERATION_TOOL_NAME,
                "description": description,
                "parameters": parameters,
            }
        ],
        _probe_messages(instruction),
        IMAGE_GENERATION_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _cron_scenario(case_name: str) -> ProbeScenario:
    cron_arguments: dict[str, dict[str, Any]] = {
        "create_cron": {
            "action": "create",
            "prompt": "Prepare the daily operations summary.",
            "schedule": "0 9 * * *",
        },
        "create_interval": {
            "action": "create",
            "prompt": "Check the service health.",
            "schedule": "every 2h",
        },
        "create_once_relative": {
            "action": "create",
            "prompt": "Send the deployment reminder.",
            "schedule": "in 30m",
            "repeat": 1,
        },
        "create_once_iso": {
            "action": "create",
            "prompt": "Send the deployment reminder.",
            "schedule": "2026-08-01T09:00:00+02:00",
        },
        "create_target": {
            "action": "create",
            "target": "reviewer@vbot",
            "prompt": "Review the current release state.",
            "schedule": "0 10 * * 1",
        },
        "create_name": {
            "action": "create",
            "name": "Daily operations summary",
            "prompt": "Prepare the daily operations summary.",
            "schedule": "0 9 * * *",
        },
        "create_repeat": {
            "action": "create",
            "prompt": "Check the service health.",
            "schedule": "every 2h",
            "repeat": 3,
        },
        "create_repeat_null": {
            "action": "create",
            "prompt": "Check the service health.",
            "schedule": "every 2h",
            "repeat": None,
        },
        "create_all": {
            "action": "create",
            "target": "reviewer@vbot",
            "name": "Release review",
            "prompt": "Review the current release state.",
            "schedule": "0 10 * * 1",
            "repeat": 6,
        },
        "list": {"action": "list"},
        "update_target": {
            "action": "update",
            "id": "job-123",
            "target": "reviewer@vbot",
        },
        "update_name": {
            "action": "update",
            "id": "job-123",
            "name": "Updated operations summary",
        },
        "update_prompt": {
            "action": "update",
            "id": "job-123",
            "prompt": "Prepare the revised operations summary.",
        },
        "update_schedule": {
            "action": "update",
            "id": "job-123",
            "schedule": "every 4h",
        },
        "update_repeat": {
            "action": "update",
            "id": "job-123",
            "repeat": 4,
        },
        "update_repeat_null": {
            "action": "update",
            "id": "job-123",
            "repeat": None,
        },
        "update_all": {
            "action": "update",
            "id": "job-123",
            "target": "reviewer@vbot",
            "name": "Updated release review",
            "prompt": "Review the revised release state.",
            "schedule": "0 11 * * 1",
            "repeat": 8,
        },
        "delete": {"action": "delete", "id": "job-123"},
        "enable": {"action": "enable", "id": "job-123"},
        "disable": {"action": "disable", "id": "job-123"},
    }
    expected_arguments = cron_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {CRON_TOOL_NAME} exactly once with exactly this JSON object as its arguments: "
        f"{rendered_arguments}. Preserve every value and do not add any field."
    )
    return ProbeScenario(
        "cron",
        [
            {
                "name": CRON_TOOL_NAME,
                "description": CRON_TOOL_DESCRIPTION,
                "parameters": CRON_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        CRON_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


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
    expected_arguments = edit_arguments[case_name]
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


def _history_scenario(case_name: str) -> ProbeScenario:
    cursor = "opaque-history-cursor"
    all_roles = [
        "system",
        "user",
        "assistant",
        "tool",
        "note",
        "error",
        "run_summary",
        "agent_takeover",
    ]
    history_arguments: dict[str, dict[str, Any]] = {
        "overview_default": {"action": "overview"},
        "overview_limit_min": {"action": "overview", "limit": 1},
        "overview_limit_max": {"action": "overview", "limit": 100},
        "overview_cursor": {"action": "overview", "cursor": cursor},
        "search_default": {"action": "search", "query": "deployment failure"},
        "search_checkpoint": {
            "action": "search",
            "query": "deployment failure",
            "checkpoint": 1,
        },
        "search_roles_one": {
            "action": "search",
            "query": "deployment failure",
            "roles": ["assistant"],
        },
        "search_roles_all": {
            "action": "search",
            "query": "deployment failure",
            "roles": all_roles,
        },
        "search_match_all_terms": {
            "action": "search",
            "query": "deployment failure",
            "match": "all_terms",
        },
        "search_match_phrase": {
            "action": "search",
            "query": "deployment failure",
            "match": "phrase",
        },
        "search_match_any_term": {
            "action": "search",
            "query": "deployment failure",
            "match": "any_term",
        },
        "search_limit": {
            "action": "search",
            "query": "deployment failure",
            "limit": 37,
        },
        "search_all": {
            "action": "search",
            "query": "deployment failure",
            "checkpoint": 2,
            "roles": ["user", "assistant", "error"],
            "match": "phrase",
            "limit": 37,
        },
        "search_cursor": {"action": "search", "cursor": cursor},
        "read_default": {"action": "read"},
        "read_checkpoint": {"action": "read", "checkpoint": 1},
        "read_roles_empty": {"action": "read", "roles": []},
        "read_roles": {"action": "read", "roles": ["tool", "note"]},
        "read_direction_start": {"action": "read", "direction": "start"},
        "read_direction_end": {"action": "read", "direction": "end"},
        "read_limit": {"action": "read", "limit": 42},
        "read_all": {
            "action": "read",
            "checkpoint": 3,
            "roles": ["system", "agent_takeover"],
            "direction": "end",
            "limit": 42,
        },
        "read_cursor": {"action": "read", "cursor": cursor},
        "around_default": {"action": "around", "message_id": "message-123"},
        "around_checkpoint": {
            "action": "around",
            "message_id": "message-123",
            "checkpoint": 1,
        },
        "around_roles": {
            "action": "around",
            "message_id": "message-123",
            "roles": ["user", "assistant"],
        },
        "around_before_zero": {
            "action": "around",
            "message_id": "message-123",
            "before": 0,
        },
        "around_before_max": {
            "action": "around",
            "message_id": "message-123",
            "before": 100,
        },
        "around_after_zero": {
            "action": "around",
            "message_id": "message-123",
            "after": 0,
        },
        "around_after_max": {
            "action": "around",
            "message_id": "message-123",
            "after": 100,
        },
        "around_all": {
            "action": "around",
            "message_id": "message-123",
            "checkpoint": 4,
            "roles": ["assistant", "error"],
            "before": 17,
            "after": 23,
        },
        "around_cursor": {"action": "around", "cursor": cursor},
    }
    expected_arguments = history_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {HISTORY_TOOL_NAME} exactly once with exactly this JSON object as its arguments: "
        f"{rendered_arguments}. Preserve every value and array item; do not add any field."
    )
    return ProbeScenario(
        "history",
        [
            {
                "name": HISTORY_TOOL_NAME,
                "description": HISTORY_TOOL_DESCRIPTION,
                "parameters": HISTORY_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        HISTORY_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _ha_list_entities_scenario(case_name: str) -> ProbeScenario:
    arguments_by_case: dict[str, dict[str, Any]] = {
        "default": {},
        "domain": {"domain": "light"},
        "area": {"area": "Living Room"},
        "all": {"domain": "climate", "area": "Upstairs"},
    }
    expected_arguments = arguments_by_case[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {HA_LIST_ENTITIES_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value; do not add any field."
    )
    return ProbeScenario(
        "ha_list_entities",
        [
            {
                "name": HA_LIST_ENTITIES_NAME,
                "description": HA_LIST_ENTITIES_DESCRIPTION,
                "parameters": HA_LIST_ENTITIES_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        HA_LIST_ENTITIES_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _ha_get_state_scenario(case_name: str) -> ProbeScenario:
    arguments_by_case = {
        "light": {"entity_id": "light.living_room"},
        "sensor": {"entity_id": "sensor.outdoor_temperature"},
    }
    expected_arguments = arguments_by_case[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {HA_GET_STATE_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve the value; do not add any field."
    )
    return ProbeScenario(
        "ha_get_state",
        [
            {
                "name": HA_GET_STATE_NAME,
                "description": HA_GET_STATE_DESCRIPTION,
                "parameters": HA_GET_STATE_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        HA_GET_STATE_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _ha_list_services_scenario(case_name: str) -> ProbeScenario:
    arguments_by_case: dict[str, dict[str, Any]] = {
        "default": {},
        "domain": {"domain": "climate"},
    }
    expected_arguments = arguments_by_case[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {HA_LIST_SERVICES_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value; do not add any field."
    )
    return ProbeScenario(
        "ha_list_services",
        [
            {
                "name": HA_LIST_SERVICES_NAME,
                "description": HA_LIST_SERVICES_DESCRIPTION,
                "parameters": HA_LIST_SERVICES_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        HA_LIST_SERVICES_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _ha_call_service_scenario(case_name: str) -> ProbeScenario:
    base = {"domain": "light", "service": "turn_on"}
    arguments_by_case: dict[str, dict[str, Any]] = {
        "base": base,
        "entity": {**base, "entity_id": "light.living_room"},
        "empty_data": {**base, "data": {}},
        "data": {**base, "data": {"brightness": 180, "transition": 2.5}},
        "all": {
            **base,
            "entity_id": "light.living_room",
            "data": {"brightness": 180, "rgb_color": [255, 120, 40]},
        },
    }
    expected_arguments = arguments_by_case[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {HA_CALL_SERVICE_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and nested item; do not add "
        "any field."
    )
    return ProbeScenario(
        "ha_call_service",
        [
            {
                "name": HA_CALL_SERVICE_NAME,
                "description": HA_CALL_SERVICE_DESCRIPTION,
                "parameters": HA_CALL_SERVICE_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        HA_CALL_SERVICE_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _bash_scenario(case_name: str) -> ProbeScenario:
    bash_arguments: dict[str, dict[str, Any]] = {
        "top_foreground": {"mode": "foreground", "command": "python --version"},
        "top_foreground_workdir": {
            "mode": "foreground",
            "command": "python --version",
            "workdir": "src",
        },
        "top_foreground_timeout": {
            "mode": "foreground",
            "command": "python --version",
            "timeout": 120,
        },
        "top_foreground_env_one": {
            "mode": "foreground",
            "command": "python -c \"import os; print(bool(os.environ['OPENAI_API_KEY']))\"",
            "env_keys": ["OPENAI_API_KEY"],
        },
        "top_foreground_env_many": {
            "mode": "foreground",
            "command": 'python -c "import os; print(len(os.environ))"',
            "env_keys": ["OPENAI_API_KEY", "OPENROUTER_API_KEY"],
        },
        "top_foreground_all_multiline": {
            "mode": "foreground",
            "command": ("python --version\npython -m pytest tests/core/tools/test_bash.py -q"),
            "workdir": "src",
            "timeout": 120,
        },
        "top_auto_default": {
            "mode": "auto",
            "command": "python -m pytest tests/core/tools/test_bash.py -q",
        },
        "top_auto_zero": {
            "mode": "auto",
            "command": "python -m pytest tests/core/tools/test_bash.py -q",
            "yield_after": 0,
        },
        "top_auto_yield": {
            "mode": "auto",
            "command": "python -m pytest tests/core/tools/test_bash.py -q",
            "yield_after": 5,
        },
        "top_auto_timeout": {
            "mode": "auto",
            "command": "python -m pytest tests/core/tools/test_bash.py -q",
            "timeout": 120,
        },
        "top_auto_all": {
            "mode": "auto",
            "command": "python -m pytest tests/core/tools/test_bash.py -q",
            "workdir": "src",
            "yield_after": 5,
            "timeout": 120,
        },
        "top_background": {
            "mode": "background",
            "command": "python -m http.server 8765",
        },
        "top_background_workdir": {
            "mode": "background",
            "command": "python -m http.server 8765",
            "workdir": "public",
        },
        "top_background_timeout": {
            "mode": "background",
            "command": "python -m http.server 8765",
            "timeout": 600,
        },
        "top_background_all": {
            "mode": "background",
            "command": "python -m http.server 8765",
            "workdir": "public",
            "timeout": 600,
        },
        "sub_foreground": {"mode": "foreground", "command": "python --version"},
        "sub_foreground_all": {
            "mode": "foreground",
            "command": "python --version",
            "workdir": "src",
            "timeout": 120,
        },
        "sub_auto_default": {
            "mode": "auto",
            "command": "python -m pytest tests/core/tools/test_bash.py -q",
        },
        "sub_auto_zero": {
            "mode": "auto",
            "command": "python -m pytest tests/core/tools/test_bash.py -q",
            "yield_after": 0,
        },
        "sub_auto_all": {
            "mode": "auto",
            "command": "python -m pytest tests/core/tools/test_bash.py -q",
            "workdir": "src",
            "yield_after": 300,
            "timeout": 600,
        },
    }
    expected_arguments = bash_arguments[case_name]
    definition = {
        "name": BASH_TOOL_NAME,
        "description": BASH_TOOL_DESCRIPTION,
        "parameters": BASH_TOOL_PARAMETERS,
    }
    if case_name.startswith("sub_"):
        definition = project_bash_tool_definitions([definition], nesting_depth=1)[0]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {BASH_TOOL_NAME} exactly once with exactly this JSON object as its arguments: "
        f"{rendered_arguments}. Preserve every value and do not add any field. Do not execute "
        "or describe the command yourself."
    )
    return ProbeScenario(
        "bash",
        [definition],
        _probe_messages(instruction),
        BASH_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _channel_send_scenario(case_name: str) -> ProbeScenario:
    channel_arguments: dict[str, dict[str, Any]] = {
        "telegram_message": {
            "channel_id": "telegram-probe",
            "message": "Provider Tool probe complete.",
        },
        "telegram_target": {
            "channel_id": "telegram-probe",
            "message": "Provider Tool probe complete.",
            "platform_target": "123456789",
        },
        "telegram_file": {
            "channel_id": "telegram-probe",
            "file_paths": ["artifacts/provider-tool-probe.txt"],
        },
        "telegram_files": {
            "channel_id": "telegram-probe",
            "file_paths": [
                "artifacts/provider-tool-probe.txt",
                "artifacts/provider-tool-probe.png",
            ],
            "platform_target": "123456789",
        },
        "telegram_message_file": {
            "channel_id": "telegram-probe",
            "message": "Attached probe result.",
            "file_paths": ["artifacts/provider-tool-probe.txt"],
        },
        "telegram_thread": {
            "channel_id": "telegram-probe",
            "message": "Threaded probe result.",
            "platform_target": "123456789",
            "thread_id": "42",
        },
        "telegram_button": {
            "channel_id": "telegram-probe",
            "message": "Continue the probe?",
            "buttons": [[{"label": "Continue", "data": "run:continue"}]],
        },
        "telegram_button_rows": {
            "channel_id": "telegram-probe",
            "message": "Choose a probe result.",
            "buttons": [
                [
                    {"label": "Accept", "data": "run:accept"},
                    {"label": "Retry", "data": "run:retry"},
                ],
                [{"label": "Cancel", "data": "run:cancel"}],
            ],
            "platform_target": "123456789",
        },
        "discord_message": {
            "channel_id": "discord-probe",
            "message": "Provider Tool probe complete.",
        },
        "discord_target": {
            "channel_id": "discord-probe",
            "message": "Provider Tool probe complete.",
            "platform_target": "987654321",
        },
        "discord_file": {
            "channel_id": "discord-probe",
            "file_paths": ["artifacts/provider-tool-probe.txt"],
        },
        "discord_message_file": {
            "channel_id": "discord-probe",
            "message": "Attached probe result.",
            "file_paths": ["artifacts/provider-tool-probe.txt"],
            "platform_target": "987654321",
        },
        "mixed_telegram_button": {
            "channel_id": "telegram-probe",
            "message": "Continue the mixed-profile probe?",
            "buttons": [[{"label": "Continue", "data": "run:continue"}]],
        },
        "mixed_discord_file": {
            "channel_id": "discord-probe",
            "file_paths": ["artifacts/provider-tool-probe.txt"],
        },
    }
    expected_arguments = channel_arguments[case_name]
    platforms = (
        ("discord", "telegram")
        if case_name.startswith("mixed_")
        else (("discord",) if case_name.startswith("discord_") else ("telegram",))
    )
    configs = [
        ChannelConfig(
            id=f"{platform}-probe",
            platform=platform,
            agent_id="probe-agent",
            token_env_var=f"PROBE_{platform.upper()}_TOKEN",
        )
        for platform in platforms
    ]
    profile = _channel_send_definition_profile(configs)
    rendered_arguments = json.dumps(expected_arguments, ensure_ascii=False, separators=(",", ":"))
    instruction = (
        f"Call {CHANNEL_SEND_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and do not add any field."
    )
    return ProbeScenario(
        "channel_send",
        [
            {
                "name": CHANNEL_SEND_TOOL_NAME,
                "description": profile.description,
                "parameters": profile.parameters,
            }
        ],
        _probe_messages(instruction),
        CHANNEL_SEND_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _memory_scenario(case_name: str) -> ProbeScenario:
    memory_arguments: dict[str, dict[str, Any]] = {
        "list_user": {"action": "list", "scope": "user"},
        "list_agent": {"action": "list", "scope": "agent"},
        "add_user": {
            "action": "add",
            "scope": "user",
            "content": "Prefers concise answers.",
        },
        "add_agent": {
            "action": "add",
            "scope": "agent",
            "content": "Workspace uses PowerShell.",
        },
        "replace_user": {
            "action": "replace",
            "scope": "user",
            "entry_id": 2,
            "content": "Prefers direct, concise answers.",
        },
        "replace_agent": {
            "action": "replace",
            "scope": "agent",
            "entry_id": 2,
            "content": "Workspace uses PowerShell 7.",
        },
        "remove_user": {"action": "remove", "scope": "user", "entry_id": 2},
        "remove_agent": {"action": "remove", "scope": "agent", "entry_id": 2},
    }
    expected_arguments = memory_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {MEMORY_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "memory",
        [
            {
                "name": MEMORY_TOOL_NAME,
                "description": MEMORY_TOOL_DESCRIPTION,
                "parameters": MEMORY_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        MEMORY_TOOL_NAME,
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


def _project_scenario() -> ProbeScenario:
    expected_arguments = {"project_id": "vbot"}
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {PROJECT_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve the value and do not add any field."
    )
    return ProbeScenario(
        "project",
        [
            {
                "name": PROJECT_TOOL_NAME,
                "description": PROJECT_TOOL_DESCRIPTION,
                "parameters": PROJECT_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        PROJECT_TOOL_NAME,
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


def _status_scenario(case_name: str) -> ProbeScenario:
    status_arguments = {
        "current": {},
        "session": {"session_id": "session-123"},
        "agent_session": {"session_id": "session-123", "agent_id": "tester"},
    }
    expected_arguments = status_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {STATUS_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "status",
        [
            {
                "name": STATUS_TOOL_NAME,
                "description": STATUS_TOOL_DESCRIPTION,
                "parameters": STATUS_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        STATUS_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _subagent_scenario(case_name: str) -> ProbeScenario:
    subagent_arguments: dict[str, dict[str, Any]] = {
        "run_self": {
            "action": "run",
            "content": "Inspect the Tool contract and report concise findings.",
        },
        "run_agent": {
            "action": "run",
            "content": "Inspect the Tool contract and report concise findings.",
            "agent_id": "reviewer",
        },
        "run_continue": {
            "action": "run",
            "content": "Now verify the remaining edge case.",
            "agent_id": "reviewer",
            "session_id": "session-123",
        },
        "run_model": {
            "action": "run",
            "content": "Inspect the Tool contract and report concise findings.",
            "model": "openai/gpt-5.6-luna",
        },
        "run_all": {
            "action": "run",
            "content": "Now verify the remaining edge case.",
            "agent_id": "reviewer",
            "session_id": "session-123",
            "model": "openai/gpt-5.6-luna",
            "thinking_effort": "high",
        },
        "thinking_default": {
            "action": "run",
            "content": "Inspect the Tool contract and report concise findings.",
            "thinking_effort": "",
        },
        "thinking_minimal": {
            "action": "run",
            "content": "Inspect the Tool contract and report concise findings.",
            "thinking_effort": "minimal",
        },
        "thinking_low": {
            "action": "run",
            "content": "Inspect the Tool contract and report concise findings.",
            "thinking_effort": "low",
        },
        "thinking_medium": {
            "action": "run",
            "content": "Inspect the Tool contract and report concise findings.",
            "thinking_effort": "medium",
        },
        "thinking_high": {
            "action": "run",
            "content": "Inspect the Tool contract and report concise findings.",
            "thinking_effort": "high",
        },
        "thinking_xhigh": {
            "action": "run",
            "content": "Inspect the Tool contract and report concise findings.",
            "thinking_effort": "xhigh",
        },
        "thinking_max": {
            "action": "run",
            "content": "Inspect the Tool contract and report concise findings.",
            "thinking_effort": "max",
        },
        "thinking_none": {
            "action": "run",
            "content": "Inspect the Tool contract and report concise findings.",
            "thinking_effort": "none",
        },
        "status": {"action": "status", "id": "sub_123"},
        "cancel": {"action": "cancel", "id": "sub_123"},
    }
    expected_arguments = subagent_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {SUBAGENT_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and do not add any field."
    )
    return ProbeScenario(
        "subagent",
        [
            {
                "name": SUBAGENT_TOOL_NAME,
                "description": SUBAGENT_TOOL_DESCRIPTION,
                "parameters": SUBAGENT_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        SUBAGENT_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _text_to_speech_scenario(case_name: str) -> ProbeScenario:
    speech_arguments = {
        "plain": {"text": "Please read this sentence aloud."},
        "unicode_multiline": {"text": "Grüße aus Köln.\nZweite Zeile: 你好 — fertig."},
    }
    expected_arguments = speech_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"), ensure_ascii=False)
    instruction = (
        f"Call {TEXT_TO_SPEECH_TOOL_NAME} exactly once with exactly this JSON object as "
        f"its arguments: {rendered_arguments}. Preserve every character and do not add "
        "any field."
    )
    return ProbeScenario(
        "text_to_speech",
        [
            {
                "name": TEXT_TO_SPEECH_TOOL_NAME,
                "description": TEXT_TO_SPEECH_TOOL_DESCRIPTION,
                "parameters": TEXT_TO_SPEECH_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        TEXT_TO_SPEECH_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _session_read_scenario(case_name: str) -> ProbeScenario:
    session_id = "session-123"
    agent_id = "tester"
    start_message_id = "message-start"
    end_message_id = "message-end"
    session_read_arguments = {
        "whole": {"session_id": session_id},
        "agent": {"session_id": session_id, "agent_id": agent_id},
        "start": {"session_id": session_id, "start_message_id": start_message_id},
        "end": {"session_id": session_id, "end_message_id": end_message_id},
        "range": {
            "session_id": session_id,
            "start_message_id": start_message_id,
            "end_message_id": end_message_id,
        },
        "all": {
            "session_id": session_id,
            "agent_id": agent_id,
            "start_message_id": start_message_id,
            "end_message_id": end_message_id,
        },
        "cursor": {"cursor": "session-read-cursor-token"},
    }
    expected_arguments = session_read_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {SESSION_READ_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "session_read",
        [
            {
                "name": SESSION_READ_TOOL_NAME,
                "description": SESSION_READ_TOOL_DESCRIPTION,
                "parameters": SESSION_READ_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        SESSION_READ_TOOL_NAME,
        require_closed_input=False,
        expected_arguments=expected_arguments,
    )


def _session_search_scenario(case_name: str) -> ProbeScenario:
    query = "Tool schema defaults"
    session_search_arguments: dict[str, dict[str, Any]] = {
        "list": {},
        "query": {"query": query},
        "period": {"period": "2026-07-01/2026-07-31"},
        "agent": {"agent_id": "tester"},
        "session": {"session_id": "session-123"},
        "limit_min": {"limit": 1},
        "limit_max": {"limit": 100},
        "all": {
            "query": query,
            "period": "2026-07-01/2026-07-31",
            "agent_id": "tester",
            "session_id": "session-123",
            "limit": 100,
        },
        "cursor": {"cursor": "session-search-cursor-token"},
    }
    expected_arguments = session_search_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {SESSION_SEARCH_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "session_search",
        [
            {
                "name": SESSION_SEARCH_TOOL_NAME,
                "description": SESSION_SEARCH_TOOL_DESCRIPTION,
                "parameters": SESSION_SEARCH_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        SESSION_SEARCH_TOOL_NAME,
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


def _web_search_scenario(case_name: str) -> ProbeScenario:
    query = "vBot Tool schemas"
    web_search_arguments: dict[str, dict[str, Any]] = {
        "default": {"query": query},
        "operator_query": {"query": 'vBot "Tool schema" -deprecated'},
        "domains_one": {"query": query, "domains": ["openai.com"]},
        "domains_many": {
            "query": query,
            "domains": ["docs.python.org", "openai.com"],
        },
        "count_min": {"query": query, "count": 1},
        "count_max": {"query": query, "count": 20},
        "page_first": {"query": query, "page": 1},
        "page_later": {"query": query, "page": 3},
        "recency_day": {"query": query, "recency": "day"},
        "recency_month": {"query": query, "recency": "month"},
        "recency_year": {"query": query, "recency": "year"},
        "all": {
            "query": query,
            "domains": ["docs.python.org", "openai.com"],
            "count": 20,
            "page": 3,
            "recency": "month",
        },
    }
    expected_arguments = web_search_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {WEB_SEARCH_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and omit every field not "
        "shown."
    )
    return ProbeScenario(
        "web_search",
        [
            {
                "name": WEB_SEARCH_TOOL_NAME,
                "description": WEB_SEARCH_TOOL_DESCRIPTION,
                "parameters": WEB_SEARCH_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        WEB_SEARCH_TOOL_NAME,
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


def _skill_manage_scenario(case_name: str) -> ProbeScenario:
    skill_md = (
        "---\nname: provider-probe\ndescription: Verify Provider Tool Calls.\n---\n\n"
        "# Provider Probe\n\nFollow the probe instructions.\n"
    )
    edited_skill_md = skill_md.replace(
        "Follow the probe instructions.",
        "Follow the revised probe instructions.",
    )
    skill_manage_arguments: dict[str, dict[str, Any]] = {
        "create_own": {
            "action": "create",
            "name": "provider-probe",
            "content": skill_md,
        },
        "create_global": {
            "action": "create",
            "name": "provider-probe",
            "scope": "global",
            "content": skill_md,
        },
        "edit": {
            "action": "edit",
            "name": "provider-probe",
            "content": edited_skill_md,
        },
        "patch_default": {
            "action": "patch",
            "name": "provider-probe",
            "old_string": "Follow the probe instructions.",
            "new_string": "Follow the revised probe instructions.",
        },
        "patch_false": {
            "action": "patch",
            "name": "provider-probe",
            "old_string": "probe",
            "new_string": "provider probe",
            "replace_all": False,
        },
        "patch_true": {
            "action": "patch",
            "name": "provider-probe",
            "old_string": "probe",
            "new_string": "provider probe",
            "replace_all": True,
        },
        "patch_support": {
            "action": "patch",
            "name": "provider-probe",
            "file_path": "scripts/check.py",
            "old_string": "value = 1",
            "new_string": "value = 2",
        },
        "patch_delete": {
            "action": "patch",
            "name": "provider-probe",
            "file_path": "references/notes.md",
            "old_string": "obsolete line\n",
            "new_string": "",
        },
        "write_script": {
            "action": "write_file",
            "name": "provider-probe",
            "file_path": "scripts/check.py",
            "file_content": "print('provider probe')\n",
        },
        "write_reference": {
            "action": "write_file",
            "name": "provider-probe",
            "file_path": "references/notes.md",
            "file_content": "Provider probe notes.\nSecond line.\n",
        },
        "write_asset_empty": {
            "action": "write_file",
            "name": "provider-probe",
            "file_path": "assets/placeholder.txt",
            "file_content": "",
        },
        "remove_file": {
            "action": "remove_file",
            "name": "provider-probe",
            "file_path": "references/notes.md",
        },
        "delete": {"action": "delete", "name": "provider-probe"},
    }
    expected_arguments = skill_manage_arguments[case_name]
    rendered_arguments = json.dumps(
        expected_arguments,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    instruction = (
        f"Call {SKILL_MANAGE_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and do not add any field."
    )
    return ProbeScenario(
        "skill_manage",
        [
            {
                "name": SKILL_MANAGE_TOOL_NAME,
                "description": SKILL_MANAGE_TOOL_DESCRIPTION,
                "parameters": SKILL_MANAGE_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        SKILL_MANAGE_TOOL_NAME,
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


def _word_count_scenario(case_name: str) -> ProbeScenario:
    arguments_by_case = {
        "plain": {"text": "Count these three words"},
        "empty": {"text": ""},
        "unicode_multiline": {"text": "Grüße aus Berlin\nzweite Zeile 🙂"},
    }
    expected_arguments = arguments_by_case[case_name]
    rendered_arguments = json.dumps(expected_arguments, ensure_ascii=False, separators=(",", ":"))
    instruction = (
        f"Call {WORD_COUNT_NAME} exactly once with exactly this JSON object as its arguments: "
        f"{rendered_arguments}. Preserve every character and line break; do not add any field."
    )
    return ProbeScenario(
        "word_count",
        [
            {
                "name": WORD_COUNT_NAME,
                "description": WORD_COUNT_DESCRIPTION,
                "parameters": WORD_COUNT_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        WORD_COUNT_NAME,
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
    if name == "analyze_image":
        return _analyze_image_scenario(str(args.analyze_image_case))
    if name == "bash":
        return _bash_scenario(str(args.bash_case))
    if name == "channel_send":
        return _channel_send_scenario(str(args.channel_send_case))
    if name == "cron":
        return _cron_scenario(str(args.cron_case))
    if name == "edit":
        return _edit_scenario(str(args.edit_case))
    if name == "glob":
        return _glob_scenario(str(args.glob_case))
    if name == "grep":
        return _grep_scenario(str(args.grep_case))
    if name == "ha_call_service":
        return _ha_call_service_scenario(str(args.ha_call_service_case))
    if name == "ha_get_state":
        return _ha_get_state_scenario(str(args.ha_get_state_case))
    if name == "ha_list_entities":
        return _ha_list_entities_scenario(str(args.ha_list_entities_case))
    if name == "ha_list_services":
        return _ha_list_services_scenario(str(args.ha_list_services_case))
    if name == "history":
        return _history_scenario(str(args.history_case))
    if name == "image_generation":
        return _image_generation_scenario(str(args.image_generation_case))
    if name == "memory":
        return _memory_scenario(str(args.memory_case))
    if name == "process":
        return _process_scenario(str(args.process_case))
    if name == "project":
        return _project_scenario()
    if name == "read":
        return _read_scenario(str(args.read_case))
    if name == "session_read":
        return _session_read_scenario(str(args.session_read_case))
    if name == "session_search":
        return _session_search_scenario(str(args.session_search_case))
    if name == "skill":
        return _skill_scenario(str(args.skill_case))
    if name == "skill_list":
        return _skill_list_scenario()
    if name == "skill_manage":
        return _skill_manage_scenario(str(args.skill_manage_case))
    if name == "status":
        return _status_scenario(str(args.status_case))
    if name == "subagent":
        return _subagent_scenario(str(args.subagent_case))
    if name == "text_to_speech":
        return _text_to_speech_scenario(str(args.speech_case))
    if name == "web_fetch":
        return _web_fetch_scenario(str(args.web_fetch_case))
    if name == "web_search":
        return _web_search_scenario(str(args.web_search_case))
    if name == "write":
        return _write_scenario()
    if name == "word_count":
        return _word_count_scenario(str(args.word_count_case))
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


def _probe_tool_call_stream_key(delta: dict[str, Any]) -> str:
    """Return the stable Tool Call key used by the normalized stream contract."""
    slot = delta.get("slot")
    if isinstance(slot, int) and not isinstance(slot, bool):
        return f"index:{slot}"
    if isinstance(slot, str) and slot:
        return f"slot:{slot}"

    tool_call_id = delta.get("id")
    if isinstance(tool_call_id, str) and tool_call_id:
        return f"id:{tool_call_id}"
    raise ValueError("tool_call_delta must contain a slot or non-empty id")


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
    tool_names_by_stream_key: dict[str, str] = {}
    tool_arguments_by_stream_key: dict[str, str] = {}
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
                    stream_key = _probe_tool_call_stream_key(delta)
                    name_delta = str(delta.get("name_delta", ""))
                    tool_names_by_stream_key[stream_key] = (
                        tool_names_by_stream_key.get(stream_key, "") + name_delta
                    )
                    arguments_delta = str(delta.get("arguments_delta", ""))
                    tool_arguments_by_stream_key[stream_key] = (
                        tool_arguments_by_stream_key.get(stream_key, "") + arguments_delta
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
        for stream_key, name in tool_names_by_stream_key.items():
            arguments = json.loads(tool_arguments_by_stream_key.get(stream_key, ""))
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
            "analyze_image_case": (
                args.analyze_image_case if scenario.name == "analyze_image" else None
            ),
            "bash_case": args.bash_case if scenario.name == "bash" else None,
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
