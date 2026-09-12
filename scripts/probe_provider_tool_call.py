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
import ast
import asyncio
import importlib
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from core.channels import ChannelConfig
from core.providers.accounts import ConnectionRef
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
from core.tools.calendar import (
    CALENDAR_TOOL_DESCRIPTION,
    CALENDAR_TOOL_NAME,
    CALENDAR_TOOL_PARAMETERS,
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
_WORD_COUNT_EXAMPLE = importlib.import_module(
    "resources.skills.vbot-cli.assets.extensions.word_count"
)
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
MCP_CASE_ARGUMENTS: dict[str, dict[str, Any]] = {
    "search": {"action": "search"},
    "query": {"action": "search", "query": "scene material"},
    "offset": {"action": "search", "offset": 10},
    "limit": {"action": "search", "limit": 2},
    "describe": {"action": "describe", "target": "tool:inspect:test-owned-target"},
    "call_empty": {"action": "call", "target": "operation:ping:test-owned-target"},
    "call_arguments": {
        "action": "call",
        "target": "tool:inspect:test-owned-target",
        "arguments": {"value": "test-owned-value", "nested": {"number": 4}},
    },
    "read": {"action": "read", "result_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    "pointer": {
        "action": "read",
        "result_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "pointer": "/content/0/text",
    },
    "read_offset": {"action": "read", "result_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "offset": 5},
    "read_limit": {"action": "read", "result_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "limit": 12},
    "fields": {
        "action": "read",
        "result_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "pointer": "/rows",
        "fields": ["name", "id"],
    },
    "fields_empty": {
        "action": "read",
        "result_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "fields": [],
    },
    "invalid_extra": {"action": "search", "unknown": True},
    "invalid_combination": {"action": "describe", "target": "connection", "query": "wrong"},
    "kind_tool": {"action": "search", "kind": "tool"},
    "kind_resource": {"action": "search", "kind": "resource"},
    "kind_template": {"action": "search", "kind": "template"},
    "kind_prompt": {"action": "search", "kind": "prompt"},
    "kind_operation": {"action": "search", "kind": "operation"},
    "kind_connection": {"action": "search", "kind": "connection"},
}
PROBE_SCENARIOS = (
    "terminal",
    "apply_patch",
    "reflection_workflow",
    "swarm_tool",
    "computer",
    "mcp_workflow",
    "mcp",
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
    "calendar",
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
    "skill_manage",
    "status",
    "subagent",
    "text_to_speech",
    "web_fetch",
    "web_search",
    "write",
    "word_count",
)


COMPUTER_CASE_ARGUMENTS: dict[str, dict[str, Any]] = {
    **{action: {"action": action} for action in ("status", "apps", "windows", "close")},
    **{
        f"capture_{mode}": {
            "action": "capture",
            "pid": 101,
            "window_id": 202,
            **({"mode": mode} if mode != "default" else {}),
        }
        for mode in ("default", "som", "vision", "ax")
    },
    "click_preview": {
        "action": "click",
        "pid": 101,
        "window_id": 202,
        "element": "1",
        "apply": False,
    },
    "click_element": {
        "action": "click",
        "pid": 101,
        "window_id": 202,
        "element": "s00000001:1",
        "apply": True,
    },
    "click_coordinates": {
        "action": "click",
        "pid": 101,
        "window_id": 202,
        "view_id": "vtest",
        "coordinate": [20, 30],
        "apply": True,
    },
    "click_right_double": {
        "action": "click",
        "pid": 101,
        "window_id": 202,
        "view_id": "vtest",
        "coordinate": [20, 30],
        "button": "right",
        "count": 2,
        "apply": True,
        "foreground": False,
    },
    "click_middle": {
        "action": "click",
        "pid": 101,
        "window_id": 202,
        "view_id": "vtest",
        "coordinate": [20, 30],
        "button": "middle",
        "count": 1,
        "apply": False,
    },
    "type": {
        "action": "type",
        "pid": 101,
        "window_id": 202,
        "text": "test-owned draft",
        "apply": True,
    },
    "type_element": {
        "action": "type",
        "pid": 101,
        "window_id": 202,
        "element": "1",
        "text": "test-owned draft",
        "apply": True,
    },
    "key_single": {
        "action": "key",
        "pid": 101,
        "window_id": 202,
        "shortcut": "enter",
        "apply": True,
    },
    "key_foreground": {
        "action": "key",
        "pid": 101,
        "window_id": 202,
        "shortcut": "ctrl+a",
        "apply": True,
        "foreground": True,
    },
    **{
        f"scroll_{direction}": {
            "action": "scroll",
            "pid": 101,
            "window_id": 202,
            "direction": direction,
            "apply": True,
        }
        for direction in ("up", "down", "left", "right")
    },
    "scroll_element": {
        "action": "scroll",
        "pid": 101,
        "window_id": 202,
        "direction": "down",
        "element": "1",
        "amount": 5,
        "apply": True,
    },
    "invalid_target": {"action": "capture", "pid": 101},
    "invalid_field": {"action": "windows", "pid": 101},
}

_COMPUTER_WINDOW = {"pid": 101, "window_id": 202}
COMPUTER_CASE_ARGUMENTS.update(
    {
        "capture_original": {"action": "capture", **_COMPUTER_WINDOW, "resolution": "original"},
        "capture_query": {"action": "capture", **_COMPUTER_WINDOW, "query": "Draft", "limit": 25},
        "capture_desktop": {"action": "capture"},
        "zoom": {
            "action": "zoom",
            **_COMPUTER_WINDOW,
            "view_id": "vtest",
            "coordinate": [10, 10],
            "to_coordinate": [100, 100],
        },
        "drag": {
            "action": "drag",
            **_COMPUTER_WINDOW,
            "view_id": "vtest",
            "coordinate": [10, 10],
            "to_coordinate": [100, 100],
            "apply": True,
        },
        "set_value": {
            "action": "set_value",
            **_COMPUTER_WINDOW,
            "element": "1",
            "text": "draft",
            "apply": True,
        },
        "set_value_empty": {
            "action": "set_value",
            **_COMPUTER_WINDOW,
            "element": "1",
            "text": "",
            "apply": True,
        },
        "menu": {
            "action": "menu",
            **_COMPUTER_WINDOW,
            "menu_path": ["File", "Save"],
            "apply": True,
        },
        "resize": {
            "action": "resize",
            **_COMPUTER_WINDOW,
            "coordinate": [-100, 0],
            "size": [900, 700],
            "apply": True,
        },
        "launch_preview": {"action": "launch", "app": "Notepad", "apply": False},
        "launch": {"action": "launch", "app": "Notepad", "apply": True},
        "verify_window": {
            "action": "verify",
            **_COMPUTER_WINDOW,
            "expect": [{"window": {"exists": True}}],
        },
        "verify_element": {
            "action": "verify",
            **_COMPUTER_WINDOW,
            "timeout_ms": 0,
            "mode": "ax",
            "expect": [
                {
                    "element": {
                        "selector": {"role": "Edit", "label_contains": "Draft"},
                        "exists": True,
                        "enabled": True,
                        "selected": False,
                        "value_equals": "draft",
                    }
                }
            ],
        },
        "sequence": {
            "action": "sequence",
            **_COMPUTER_WINDOW,
            "apply": True,
            "steps": [
                {"action": "click", "element": "1"},
                {"action": "key", "shortcut": "ctrl+a"},
                {"action": "type", "text": "draft"},
            ],
        },
        "invalid_sequence": {
            "action": "sequence",
            **_COMPUTER_WINDOW,
            "steps": [{"action": "key", "shortcut": "enter"}, {"action": "click", "element": "1"}],
        },
        "invalid_view": {"action": "click", **_COMPUTER_WINDOW, "coordinate": [1, 1]},
    }
)


COMPUTER_CASE_ARGUMENTS.update(
    {
        "monitors": {"action": "monitors"},
        "capture_monitor": {"action": "capture", "monitor": 1},
        "move": {
            "action": "move",
            **_COMPUTER_WINDOW,
            "view_id": "vtest",
            "coordinate": [20, 30],
            "apply": True,
        },
        "hold_key": {
            "action": "key",
            **_COMPUTER_WINDOW,
            "shortcut": "shift",
            "duration_ms": 100,
            "apply": True,
            "foreground": True,
        },
        "invalid_duration": {"action": "key", "shortcut": "shift", "duration_ms": 3000},
        "wait_default": {"action": "wait"},
        "wait_explicit": {"action": "wait", "duration_ms": 0},
        "wait_window": {"action": "wait", **_COMPUTER_WINDOW, "duration_ms": 10},
        "type_no_capture": {
            "action": "type",
            "text": "draft",
            "apply": True,
            "capture_after": False,
        },
        "type_capture": {"action": "type", "text": "draft", "apply": True, "capture_after": True},
        **{
            f"modifier_{modifier}": {
                "action": "click",
                "coordinate": [20, 30],
                "view_id": "vtest",
                "modifiers": [modifier],
                "apply": True,
            }
            for modifier in ("ctrl", "shift", "alt", "win")
        },
        "drag_modifiers": {
            "action": "drag",
            "coordinate": [20, 30],
            "to_coordinate": [40, 50],
            "view_id": "vtest",
            "modifiers": ["ctrl", "shift"],
            "apply": True,
        },
        "scroll_modifiers": {
            "action": "scroll",
            "coordinate": [20, 30],
            "view_id": "vtest",
            "direction": "down",
            "modifiers": ["ctrl"],
            "apply": True,
        },
        "sequence_wait_no_capture": {
            "action": "sequence",
            "apply": True,
            "capture_after": False,
            "steps": [{"action": "wait", "duration_ms": 0}, {"action": "type", "text": "draft"}],
        },
        "invalid_wait": {"action": "wait", "duration_ms": 10001},
        "invalid_modifiers": {
            "action": "click",
            **_COMPUTER_WINDOW,
            "element": "1",
            "modifiers": ["ctrl"],
        },
    }
)

COMPUTER_CASE_ARGUMENTS.update(
    {
        **{
            f"background_{case}": {**COMPUTER_CASE_ARGUMENTS[case], "foreground": False}
            for case in (
                "capture_default",
                "capture_vision",
                "capture_ax",
                "click_coordinates",
                "move",
                "wait_window",
                "click_element",
            )
        },
        "background_set_value": {
            "action": "set_value",
            **_COMPUTER_WINDOW,
            "element": "1",
            "text": "draft",
            "foreground": False,
            "apply": True,
        },
        "background_drag_duration": {
            "action": "drag",
            **_COMPUTER_WINDOW,
            "view_id": "vtest",
            "coordinate": [20, 30],
            "to_coordinate": [40, 50],
            "duration_ms": 1800,
        },
        "invalid_background_desktop": {"action": "capture", "foreground": False},
        "invalid_background_hold": {
            "action": "key",
            **_COMPUTER_WINDOW,
            "shortcut": "shift",
            "duration_ms": 100,
            "foreground": False,
        },
        "click_default": {
            "action": "click",
            **_COMPUTER_WINDOW,
            "element": "1",
        },
        "launch_default": {"action": "launch", "app": "Notepad"},
        "zoom_view_target": {
            "action": "zoom",
            "view_id": "vtest",
            "coordinate": [10, 10],
            "to_coordinate": [100, 100],
        },
        "click_view_target": {
            "action": "click",
            "view_id": "vtest",
            "coordinate": [20, 30],
        },
        "sequence_shared_view": {
            "action": "sequence",
            "view_id": "vtest",
            "steps": [
                {"action": "click", "coordinate": [10, 10]},
                {"action": "drag", "coordinate": [20, 30], "to_coordinate": [100, 100]},
            ],
        },
        "type_then_elements": {
            "action": "type",
            **_COMPUTER_WINDOW,
            "text": "draft",
            "mode": "som",
            "query": "Draft",
            "limit": 10,
        },
        "invalid_vision_query": {
            "action": "capture",
            **_COMPUTER_WINDOW,
            "mode": "vision",
            "query": "Draft",
        },
        "type_view": {"action": "type", "view_id": "vtest", "text": "draft"},
        "key_view": {"action": "key", "view_id": "vtest", "shortcut": "enter"},
        "type_unicode": {
            "action": "type",
            **_COMPUTER_WINDOW,
            "text": "draft",
            "text_mode": "unicode",
        },
        "type_keyboard": {
            "action": "type",
            **_COMPUTER_WINDOW,
            "foreground": True,
            "text": "0.45",
            "text_mode": "keyboard",
        },
        "zoom_foreground": {
            "action": "zoom",
            "view_id": "vtest",
            "foreground": True,
            "coordinate": [10, 10],
            "to_coordinate": [100, 100],
        },
        "sequence_mixed_shared_view": {
            "action": "sequence",
            "view_id": "vtest",
            "foreground": True,
            "steps": [
                {"action": "click", "coordinate": [10, 10]},
                {"action": "key", "shortcut": "g"},
                {"action": "type", "text": "0.45", "text_mode": "keyboard"},
                {"action": "key", "shortcut": "enter"},
            ],
        },
        "invalid_keyboard_background": {
            "action": "type",
            "foreground": False,
            **_COMPUTER_WINDOW,
            "text": "draft",
            "text_mode": "keyboard",
        },
        "invalid_keyboard_element": {
            "action": "type",
            **_COMPUTER_WINDOW,
            "foreground": True,
            "element": "1",
            "text": "draft",
            "text_mode": "keyboard",
        },
    }
)

COMPUTER_CASE_ARGUMENTS.update(
    {
        "capture_view": {"action": "capture", "view_id": "vtest"},
        "capture_view_query": {"action": "capture", "view_id": "vtest", "query": "Brush"},
        "wait_view": {"action": "wait", "view_id": "vtest", "duration_ms": 0},
        "verify_view": {
            "action": "verify",
            "view_id": "vtest",
            "expect": [{"window": {"exists": True}}],
        },
        "element_target": {"action": "click", "element": "s00000001:1"},
        "sequence_element_target": {
            "action": "sequence",
            "steps": [{"action": "click", "element": "s00000001:1"}],
        },
        "capture_reset_background": {"action": "capture", "view_id": "vtest", "foreground": False},
    }
)

OPTIONAL_BOOLEAN_CASES = ("omit", "include_links", "raw", "both")
ANALYZE_IMAGE_CASES = ("single", "multiple")
BASH_CASES = (
    "top_foreground_default",
    "top_foreground",
    "top_foreground_description",
    "top_foreground_workdir",
    "top_foreground_timeout",
    "top_foreground_env_one",
    "top_foreground_env_many",
    "top_foreground_all_multiline",
    "top_auto_default",
    "top_auto_zero",
    "top_auto_background_after",
    "top_auto_timeout",
    "top_auto_all",
    "top_background",
    "top_background_workdir",
    "top_background_timeout",
    "top_background_all",
    "sub_foreground_default",
    "sub_foreground",
    "sub_foreground_description",
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
CALENDAR_CASES = (
    "add_action_default",
    "add_action_target",
    "add_action_session",
    "add_action_at_end",
    "update_action_time",
    "update_action_prompt",
    "update_action_target",
    "update_action_session",
    "delete_action",
    "list_default",
    "list_when_week",
    "list_when_range",
    "create_timed",
    "create_timed_full",
    "create_allday",
    "create_recurring",
    "create_recurring_count",
    "update_fields",
    "update_stop_repeat",
    "update_notes",
    "delete_whole",
    "delete_occurrence",
    "find_free_default",
    "find_free_when",
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
EDIT_CASES = (
    "default",
    "replace_false",
    "replace_true",
    "multiline",
    "delete",
    "multi_file",
    "same_file_sequence",
)
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
    "kill",
    "running",
    "finished",
    "all",
    "limit_min",
    "limit_max",
    "before",
    "kill_filter",
    "status_one_limit",
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
    "message",
    "agent",
    "continuation",
    "all_messages",
    "all",
)
SESSION_SEARCH_CASES = (
    "list",
    "query",
    "period",
    "agent",
    "session",
    "all",
)
SKILL_CASES = ("list", "activate", "skill_md", "reference", "script", "asset")
SKILL_MANAGE_CASES = (
    "create_own",
    "edit",
    "patch_default",
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
    "run_description",
    "run_all",
    "thinking_minimal",
    "thinking_low",
    "thinking_medium",
    "thinking_high",
    "thinking_xhigh",
    "thinking_max",
    "thinking_none",
    "status_all",
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


def _mcp_scenario(case_name: str) -> ProbeScenario:
    from resources.extensions.mcp.extension import MCP_DESCRIPTION, MCP_PARAMETERS

    expected_arguments = MCP_CASE_ARGUMENTS[case_name]
    rendered = json.dumps(expected_arguments, ensure_ascii=False, separators=(",", ":"))
    return ProbeScenario(
        "mcp",
        [{"name": "mcp_example", "description": MCP_DESCRIPTION, "parameters": MCP_PARAMETERS}],
        _probe_messages(
            f"Call mcp_example exactly once with exactly these arguments: {rendered}. "
            "Preserve every value and omit all other fields. This is a test-owned fixture."
        ),
        "mcp_example",
        require_closed_input=False,
        expected_arguments=expected_arguments,
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


def _calendar_scenario(case_name: str) -> ProbeScenario:
    calendar_arguments: dict[str, dict[str, Any]] = {
        "add_action_default": {
            "action": "add_action",
            "id": "event-123",
            "when": "start - 1h",
            "prompt": "Prepare the meeting notes.",
        },
        "add_action_target": {
            "action": "add_action",
            "id": "event-123",
            "when": "end + 30m",
            "prompt": "Review the meeting.",
            "target": "builder@project",
        },
        "add_action_session": {
            "action": "add_action",
            "id": "event-123",
            "when": "start",
            "prompt": "Start the meeting.",
            "session": "session-123",
        },
        "add_action_at_end": {
            "action": "add_action",
            "id": "event-123",
            "when": "end",
            "prompt": "Review the meeting.",
        },
        "update_action_time": {
            "action": "update_action",
            "id": "action-123",
            "when": "start - 30m",
        },
        "update_action_prompt": {
            "action": "update_action",
            "id": "action-123",
            "prompt": "Prepare the updated notes.",
        },
        "update_action_target": {
            "action": "update_action",
            "id": "action-123",
            "target": "builder@project",
        },
        "update_action_session": {
            "action": "update_action",
            "id": "action-123",
            "session": "session-123",
        },
        "delete_action": {"action": "delete_action", "id": "action-123"},
        "list_default": {"action": "list"},
        "list_when_week": {"action": "list", "when": "this week"},
        "list_when_range": {"action": "list", "when": "2026-09-10..2026-09-14"},
        "create_timed": {
            "action": "create",
            "title": "Dentist",
            "start": "2026-09-10T15:00",
        },
        "create_timed_full": {
            "action": "create",
            "title": "Dentist",
            "start": "2026-09-10T15:00",
            "duration": 45,
            "notes": "Bring the insurance card.",
        },
        "create_allday": {
            "action": "create",
            "title": "Trip",
            "start": "2026-09-14",
            "duration": 3,
        },
        "create_recurring": {
            "action": "create",
            "title": "Standup",
            "start": "2026-08-31T09:00",
            "rrule": {"freq": "weekly", "by_weekday": ["mo", "we"]},
        },
        "create_recurring_count": {
            "action": "create",
            "title": "Focus block",
            "start": "2026-09-01T09:00",
            "rrule": {"freq": "daily", "count": 10},
        },
        "update_fields": {
            "action": "update",
            "id": "event-123",
            "title": "Dentist moved",
            "start": "2026-09-10T16:00",
        },
        "update_stop_repeat": {
            "action": "update",
            "id": "event-123",
            "rrule": None,
        },
        "update_notes": {
            "action": "update",
            "id": "event-123",
            "notes": "Rescheduled by the practice.",
        },
        "delete_whole": {"action": "delete", "id": "event-123"},
        "delete_occurrence": {
            "action": "delete",
            "id": "event-123",
            "start": "2026-09-14T09:00:00",
        },
        "find_free_default": {"action": "find_free"},
        "find_free_when": {
            "action": "find_free",
            "when": "next week",
            "duration": 60,
        },
    }
    expected_arguments = calendar_arguments[case_name]
    rendered_arguments = json.dumps(expected_arguments, separators=(",", ":"))
    instruction = (
        f"Call {CALENDAR_TOOL_NAME} exactly once with exactly this JSON object as its "
        f"arguments: {rendered_arguments}. Preserve every value and do not add any field."
    )
    return ProbeScenario(
        "calendar",
        [
            {
                "name": CALENDAR_TOOL_NAME,
                "description": CALENDAR_TOOL_DESCRIPTION,
                "parameters": CALENDAR_TOOL_PARAMETERS,
            }
        ],
        _probe_messages(instruction),
        CALENDAR_TOOL_NAME,
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
        "top_foreground_default": {"command": "python --version"},
        "top_foreground": {"mode": "foreground", "command": "python --version"},
        "top_foreground_description": {
            "mode": "foreground",
            "command": "python --version",
            "description": "Check Python version",
        },
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
            "description": "Run Bash tool tests",
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
            "background_after_seconds": 0,
        },
        "top_auto_background_after": {
            "mode": "auto",
            "command": "python -m pytest tests/core/tools/test_bash.py -q",
            "background_after_seconds": 5,
        },
        "top_auto_timeout": {
            "mode": "auto",
            "command": "python -m pytest tests/core/tools/test_bash.py -q",
            "timeout": 120,
        },
        "top_auto_all": {
            "mode": "auto",
            "command": "python -m pytest tests/core/tools/test_bash.py -q",
            "description": "Run Bash tool tests",
            "workdir": "src",
            "background_after_seconds": 5,
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
            "description": "Serve local preview",
            "workdir": "public",
            "timeout": 600,
        },
        "sub_foreground_default": {"command": "python --version"},
        "sub_foreground": {"mode": "foreground", "command": "python --version"},
        "sub_foreground_description": {
            "mode": "foreground",
            "command": "python --version",
            "description": "Check Python version",
        },
        "sub_foreground_all": {
            "mode": "foreground",
            "command": "python --version",
            "description": "Check Python version",
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
            "background_after_seconds": 0,
        },
        "sub_auto_all": {
            "mode": "auto",
            "command": "python -m pytest tests/core/tools/test_bash.py -q",
            "description": "Run Bash tool tests",
            "workdir": "src",
            "background_after_seconds": 300,
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
    process_id = "process-probe-process"
    process_arguments: dict[str, dict[str, Any]] = {
        "status_list": {"action": "status"},
        "status_one": {"action": "status", "process_id": process_id},
        "kill": {"action": "kill", "process_id": process_id},
        "running": {"action": "status", "filter": "running"},
        "finished": {"action": "status", "filter": "finished"},
        "all": {"action": "status", "filter": "all"},
        "limit_min": {"action": "status", "filter": "all", "limit": 1},
        "limit_max": {"action": "status", "filter": "all", "limit": 100},
        "before": {"action": "status", "filter": "finished", "limit": 20, "before": process_id},
        "kill_filter": {"action": "kill", "process_id": process_id, "filter": "all"},
        "status_one_limit": {"action": "status", "process_id": process_id, "limit": 1},
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
        "run_description": {
            "action": "run",
            "content": "Inspect the Tool contract and report concise findings.",
            "description": "Review Tool contract",
        },
        "run_all": {
            "action": "run",
            "content": "Now verify the remaining edge case.",
            "description": "Verify remaining edge case",
            "agent_id": "reviewer",
            "session_id": "session-123",
            "model": "openai/gpt-5.6-luna",
            "thinking_effort": "high",
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
        "status_all": {"action": "status"},
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
    message_id = "message-123"
    continuation = "r1:1234:" + ("a" * 64)
    session_read_arguments: dict[str, dict[str, Any]] = {
        "whole": {"session_id": session_id},
        "message": {"session_id": session_id, "message_id": message_id},
        "agent": {"session_id": session_id, "agent_id": agent_id},
        "continuation": {
            "session_id": session_id,
            "message_id": message_id,
            "continuation": continuation,
        },
        "all_messages": {"session_id": session_id, "all_messages": True},
        "all": {
            "session_id": session_id,
            "agent_id": agent_id,
            "all_messages": True,
            "continuation": continuation,
        },
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
    session_id = "session-123"
    session_search_arguments: dict[str, dict[str, Any]] = {
        "list": {},
        "query": {"query": query},
        "period": {"period": "2026-07-01/2026-07-31"},
        "agent": {"agent_id": "tester"},
        "session": {"query": query, "session_id": session_id},
        "all": {
            "query": query,
            "period": "2026-07-01/2026-07-31",
            "agent_id": "tester",
            "session_id": session_id,
        },
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
        "list": {},
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
        "edit": {
            "action": "edit",
            "name": "provider-probe",
            "content": edited_skill_md,
        },
        "patch_default": {
            "action": "patch",
            "name": "provider-probe",
            "match": "Follow the probe instructions.",
            "content": "Follow the revised probe instructions.",
        },
        "patch_support": {
            "action": "patch",
            "name": "provider-probe",
            "file_path": "scripts/check.py",
            "match": "value = 1",
            "content": "value = 2",
        },
        "patch_delete": {
            "action": "patch",
            "name": "provider-probe",
            "file_path": "references/notes.md",
            "match": "obsolete line\n",
            "content": "",
        },
        "write_script": {
            "action": "write_file",
            "name": "provider-probe",
            "file_path": "scripts/check.py",
            "content": "print('provider probe')\n",
        },
        "write_reference": {
            "action": "write_file",
            "name": "provider-probe",
            "file_path": "references/notes.md",
            "content": "Provider probe notes.\nSecond line.\n",
        },
        "write_asset_empty": {
            "action": "write_file",
            "name": "provider-probe",
            "file_path": "assets/placeholder.txt",
            "content": "",
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
    if name == "computer":
        from resources.extensions.computer_use.extension import (
            COMPUTER_DESCRIPTION,
            COMPUTER_PARAMETERS,
        )

        expected = COMPUTER_CASE_ARGUMENTS[args.computer_case]
        instruction = (
            "This is an inert Tool-contract test; no desktop action will execute. "
            "The target is a test-owned window with a fresh capture and element 1 / s00000001:1. "
            "The user explicitly requested foreground input where specified. "
            "Emit exactly one computer call using only these arguments, "
            "including deliberate invalid cases: " + json.dumps(expected)
        )
        return ProbeScenario(
            name,
            [
                {
                    "name": "computer",
                    "description": COMPUTER_DESCRIPTION,
                    "parameters": COMPUTER_PARAMETERS,
                }
            ],
            _probe_messages(instruction),
            "computer",
            require_closed_input=False,
            expected_arguments=expected,
        )
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
    if name == "calendar":
        return _calendar_scenario(str(args.calendar_case))
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
    if name == "mcp":
        return _mcp_scenario(str(args.mcp_case))
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
        "_start_calendar_service",
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


async def _probe_mcp_workflow(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Let a real Model discover and drive an inert application through the real MCP host.

    The application records intent without evaluating generated code or touching Blender.
    Only structural outcomes are printed; all result files belong to a temporary directory.
    """
    from mcp.server import MCPServer

    from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
    from core.extensions.operations import ExtensionHost
    from core.tools.availability import ToolAccess
    from core.tools.tools import ToolContext, ToolDefinitionProfileContext, ToolRegistry
    from resources.extensions.mcp.client import ConnectionRunner
    from resources.extensions.mcp.config import validate_connection
    from resources.extensions.mcp.extension import MCPService

    observed: list[str] = []
    rendered = False
    report_read = False
    server = MCPServer(
        "Blender workflow fixture",
        instructions=(
            "Inspect the current scene before changing it. This application is a test fixture."
        ),
    )

    @server.tool()
    def get_scene_info() -> dict[str, Any]:
        """Inspect the current scene and its camera, objects, and settings."""
        observed.append("scene")
        return {"camera": "Camera", "objects": ["Cube"], "engine": "BLENDER_EEVEE_NEXT"}

    @server.tool()
    def execute_blender_code(code: str) -> dict[str, Any]:
        """Execute Python code in Blender. Use small, focused steps."""
        nonlocal rendered
        observed.append("code")
        try:
            nodes = ast.walk(ast.parse(code))
            rendered = any(
                isinstance(node, ast.Call) and ast.unparse(node.func) == "bpy.ops.render.render"
                for node in nodes
            )
        except SyntaxError:
            rendered = False
        return {"render_completed": rendered, "path": "audit-render.png" if rendered else None}

    @server.tool()
    def get_render_report() -> str:
        """Read the complete report for the last render."""
        observed.append("report")
        return "scene diagnostic " * 600 + "\nFinal render status: completed. Reference: 739251."

    with TemporaryDirectory(prefix="vbot-mcp-probe-") as directory:
        root = Path(directory)
        agent = SimpleNamespace(
            tool_access=ToolAccess(), memory_prompt_mode="off", workspace=directory
        )

        async def sample(*_: Any) -> dict[str, Any]:
            raise ValueError("Sampling is not part of this workflow fixture")

        host = ExtensionHost(
            data_dir=root,
            sample=sample,
            resolve_agent=lambda *_: agent,
            store_attachment=lambda *_: None,
            resolve_credential=lambda _: "",
            set_credential=lambda *_: None,
        )
        api = ExtensionAPI(
            "mcp", ExtensionDeclarations(), config={}, logger=logging.getLogger("probe")
        )
        registry = ToolRegistry()
        api.operations.bind(registry)
        service = MCPService(api)
        await service.start(host)
        service.connections["blender"] = validate_connection(
            {"id": "blender", "transport": "stdio", "command": "unused", "agents": ["probe"]}
        )

        class FixtureRunner(ConnectionRunner):
            async def _transport(self, stack: Any) -> Any:
                return server

        runner = FixtureRunner(
            service.connections["blender"], host, service.inputs, service._publish
        )
        service.runners[runner.id] = runner
        service._publish(runner, {"tools": []})
        context = ToolContext(
            agent_id="probe",
            session_id="probe",
            run_id="probe",
            tool_call_id="probe",
            tool_name="mcp_blender",
            tool_call_index=0,
            workspace=root,
            vbot_root=root,
            data_root=root,
        )
        definitions = registry.provider_definitions(
            profile_context=ToolDefinitionProfileContext(agent_id="probe")
        )
        prompt = (
            "Lies den Renderbericht und nenne mir den abschliessenden Status "
            "und die Referenznummer."
            if args.mcp_workflow_case == "large_result"
            else "Rendere die aktuelle Blender-Szene und speichere das Bild als audit-render.png."
        )
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "Complete the user's task using the available tools. "
                    "Report only verified results."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        actions: list[str] = []
        final = ""
        invalid = 0

        async def dispatch(call: dict[str, Any]) -> None:
            nonlocal report_read, invalid
            inputs = call["arguments"]
            call_context = replace(context, tool_call_id=call["id"], tool_name=call["name"])
            actions.append(str(inputs.get("action")))
            try:
                result = await registry.dispatch(call_context, inputs)
            except (ValueError, ToolContractError):
                invalid += 1
                result = {
                    "ok": False,
                    "error": {"code": "invalid_arguments"},
                    "data": None,
                    "artifacts": [],
                }
            if inputs.get("action") == "read" and "739251" in json.dumps(result):
                report_read = True
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}
            )

        try:
            if args.mcp_workflow_case == "no_match":
                seed = {
                    "id": "seed",
                    "name": "mcp_blender",
                    "arguments": {"action": "search", "query": "rendern", "kind": "tool"},
                }
                messages.append({"role": "assistant", "content": None, "tool_calls": [seed]})
                await dispatch(seed)
            async with asyncio.timeout(args.total_timeout):
                for _ in range(24):
                    raw = await adapter.send(
                        messages,
                        model_id=args.model,
                        tools=definitions,
                        thinking_effort=args.thinking_effort,
                        max_tokens=args.max_tokens or 2500,
                    )
                    response = adapter.normalize_response(raw, model_id=args.model)
                    calls = response.get("tool_calls") or []
                    messages.append(
                        {
                            key: response[key]
                            for key in ("content", "tool_calls", "reasoning", "reasoning_meta")
                            if key in response
                        }
                        | {"role": "assistant"}
                    )
                    if not calls:
                        final = response.get("content") or ""
                        break
                    for call in calls:
                        await dispatch(call)
        finally:
            await service.close()
        guidance_observed = (
            "scene" in observed
            and "code" in observed
            and observed.index("scene") < observed.index("code")
        )
        passed = (
            report_read and "739251" in final
            if args.mcp_workflow_case == "large_result"
            else rendered and guidance_observed and bool(final)
        )
        return {
            "scenario": "mcp_workflow",
            "case": args.mcp_workflow_case,
            "passed": passed,
            "actions": actions,
            "application_calls": observed,
            "guidance_observed": guidance_observed,
            "render_intent_verified": rendered,
            "complete_report_read": report_read,
            "invalid_calls": invalid,
            "model": args.model,
        }


async def _probe_swarm_tool(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    """Probe actual registered Swarm contracts and canonical receipt effects in isolation."""
    from core.agents.temporary import (
        TemporaryAgentConfig,
        TemporaryAgentRegistry,
        TemporaryExecutionGroups,
    )
    from core.chat import ChatMessage
    from core.extensions import ExtensionRegistry
    from core.extensions.extensions import purge_extension_modules
    from core.extensions.operations import ExtensionHost
    from core.runs import ChatRunManager, RunExecutionOwner
    from core.sessions import ChatSessionManager
    from core.sessions.format import write_bootstrap_marker
    from core.tools import ToolContext, ToolRegistry
    from core.tools.availability import ToolAccess

    tool_name = args.swarm_tool
    from resources.extensions.swarm.agent_text import DEFAULT_INSTRUCTIONS

    with TemporaryDirectory(prefix="vbot-swarm-probe-") as directory:
        root = Path(directory)
        write_bootstrap_marker(root)
        sessions = ChatSessionManager(root)
        manager = ChatRunManager()
        identity = SimpleNamespace(name="swarm", epoch="probe-registration")
        groups = TemporaryExecutionGroups(
            TemporaryAgentRegistry(sessions),
            None,
            lambda value: value is identity,
            identity,
            run_manager=manager,
        )
        extension_root = Path(__file__).resolve().parents[1] / "resources" / "extensions"
        # Runtime may have imported a different checkout's bundled package already.
        # Reload the isolated fixture from this script's own source tree.
        purge_extension_modules()
        extensions = ExtensionRegistry.load(
            root / "extensions",
            bundled_dir=extension_root,
            disabled={path.name for path in extension_root.iterdir() if path.name != "swarm"},
        )
        if not any(
            record.name == "swarm" and record.status == "loaded" for record in extensions.records()
        ):
            raise RuntimeError("Production Swarm package did not load")
        registry = ToolRegistry()
        extensions.apply_tools(registry)

        async def unavailable(*_arguments: Any) -> Any:
            raise RuntimeError("No Model Runs are admitted by the contract fixture")

        host = ExtensionHost(
            data_dir=root,
            state_dir=root,
            temporary_agents=groups,
            sample=unavailable,
            resolve_agent=lambda *_: None,
            store_attachment=lambda *_: None,
            resolve_credential=lambda *_: "",
            set_credential=lambda *_: None,
        )
        registered_handler: Any = registry.get("swarm_board").handler
        service = registered_handler.__self__
        await service.start(host)
        try:
            store = service.store
            profile = await store.save_profile(
                {
                    "schema_version": 1,
                    "slug": "probe",
                    "instructions": DEFAULT_INSTRUCTIONS,
                    "name": "Probe",
                    "participants": [{"model": "probe/model", "count": 3}],
                    "working_directory": {"kind": "directory", "path": directory},
                    "tool_access": {"mode": "selected", "allowed": []},
                },
                expected_revision=None,
            )
            started = await store.create_swarm(
                profile["id"],
                "probe goal",
                {"cwd": directory},
                request_id="start",
                expected_profile_revision=1,
            )
            swarm = await store.get_swarm(started["swarm_id"])
            sid = swarm["id"]
            bindings = []
            for row in swarm["participants"]:
                binding = await groups.create(
                    sid,
                    row["id"],
                    TemporaryAgentConfig(
                        model="probe/model",
                        cwd=root,
                        tool_access=ToolAccess(mode="selected", allowed=()),
                        allowed_skills=[],
                        tools={},
                        name=row["display_name"],
                    ),
                )
                await store.bind_participant_session(binding)
                bindings.append(binding)
            handle = await groups.open_group(sid)
            await store.bind_execution_epoch(sid, expected_epoch=0, execution_epoch=handle.epoch)
            binding = bindings[0]
            pid, peer = binding.participant_id, bindings[1].participant_id
            context = ToolContext(
                agent_id=binding.address.agent_id,
                session_id=binding.address.session_id,
                project_id=binding.address.project_id,
                run_id="probe-run",
                tool_call_id="probe",
                tool_name=tool_name,
                tool_call_index=0,
                workspace=root,
                vbot_root=root,
                data_root=root,
                session_tool_grants=tuple(tool.name for tool in registry.list_tools()),
                execution_owner=RunExecutionOwner(
                    extension="swarm",
                    group_id=sid,
                    participant_id=pid,
                    generation_id=binding.generation_id,
                    epoch=handle.epoch,
                ),
                delivery_receipt_hook=lambda *_: None,
                request_turn_end_hook=lambda *_: None,
            )
            topic = await store.create_discussion(
                sid, peer, title="Topic", text="opening", request_id="seed-topic"
            )
            disc = topic["discussion_id"]
            for index in range(3):
                await store.post(sid, peer, text=f"seed-{index}", request_id=f"seed-{index}")
            listed = await service.board(context, {"action": "list", "limit": 1})
            read = await service.board(context, {"action": "read", "limit": 1})
            main = swarm["main_discussion_id"]
            post = {"action": "post", "text": "probe contribution", "request_id": "post-default"}
            create = {
                "action": "create",
                "title": "New topic",
                "text": "Opening contribution",
                "request_id": "create-default",
            }
            cases: list[tuple[str, dict[str, Any], bool]] = [
                ("list_default", {"action": "list"}, True),
                ("list_one", {"action": "list", "limit": 1}, True),
                ("list_max", {"action": "list", "limit": 100}, True),
                ("list_cursor", listed["data"]["next_call"]["arguments"], True),
                ("read_default", {"action": "read"}, True),
                ("read_discussion", {"action": "read", "discussion_id": disc}, True),
                ("read_one", {"action": "read", "limit": 1}, True),
                ("read_max", {"action": "read", "limit": 100}, True),
                ("read_cursor", read["data"]["next_call"]["arguments"], True),
                ("read_message", {"action": "read", "message_id": topic["opening_post_id"]}, True),
                ("post_default", post, True),
                ("post_replay", post, True),
                ("post_conflict", {**post, "text": "changed"}, False),
                ("post_main", {**post, "discussion_id": main, "request_id": "main"}, True),
                ("post_empty_pings", {**post, "recipients": [], "request_id": "empty-pings"}, True),
                ("post_discussion", {**post, "discussion_id": disc, "request_id": "disc"}, True),
                (
                    "post_reply",
                    {
                        **post,
                        "discussion_id": disc,
                        "reply_to": topic["opening_post_id"],
                        "request_id": "reply",
                    },
                    True,
                ),
                ("post_ping", {**post, "recipients": [peer], "request_id": "ping"}, True),
                (
                    "post_duplicate_pings",
                    {**post, "recipients": [peer, peer], "request_id": "ping-duplicate"},
                    True,
                ),
                (
                    "post_nonmember_ping",
                    {
                        **post,
                        "discussion_id": disc,
                        "recipients": [bindings[2].participant_id],
                        "request_id": "nonmember",
                    },
                    True,
                ),
                ("post_unicode", {**post, "text": "Grüße 日本語", "request_id": "unicode"}, True),
                (
                    "reply_inferred",
                    {**post, "reply_to": topic["opening_post_id"], "request_id": "inferred"},
                    True,
                ),
                ("create", create, True),
                (
                    "create_empty_pings",
                    {**create, "recipients": [], "request_id": "create-empty"},
                    True,
                ),
                (
                    "create_ping",
                    {**create, "recipients": [peer, peer], "request_id": "create-ping"},
                    True,
                ),
                (
                    "create_ping_replay",
                    {**create, "recipients": [peer, peer], "request_id": "create-ping"},
                    True,
                ),
                (
                    "create_ping_conflict",
                    {**create, "recipients": [], "request_id": "create-ping"},
                    False,
                ),
                (
                    "create_foreign_ping",
                    {**create, "recipients": ["foreign"], "request_id": "create-foreign"},
                    False,
                ),
                ("create_replay", create, True),
                ("join", {"action": "join", "discussion_id": disc}, True),
                ("join_replay", {"action": "join", "discussion_id": disc}, True),
                ("leave", {"action": "leave", "discussion_id": disc}, True),
                ("leave_replay", {"action": "leave", "discussion_id": disc}, True),
                ("leave_main", {"action": "leave", "discussion_id": main}, False),
                (
                    "foreign_recipient",
                    {**post, "request_id": "foreign", "recipients": ["foreign"]},
                    False,
                ),
                ("foreign_discussion", {"action": "read", "discussion_id": "foreign"}, False),
                ("foreign_message", {"action": "read", "message_id": "foreign"}, False),
                (
                    "foreign_cursor",
                    {
                        "action": "read",
                        "cursor": listed["data"]["next_call"]["arguments"]["cursor"],
                    },
                    False,
                ),
                ("invalid_cursor", {"action": "list", "cursor": "invalid"}, False),
                (
                    "reply_mismatch",
                    {
                        **post,
                        "discussion_id": main,
                        "reply_to": topic["opening_post_id"],
                        "request_id": "mismatch",
                    },
                    False,
                ),
            ]
            invalids = [
                {},
                {"action": "unsupported"},
                {"action": "list", "swarm_id": sid},
                {"action": "list", "sender": pid},
                {"action": "read", "text": "wrong"},
                {"action": "list", "limit": True},
                {"action": "list", "limit": "1"},
                {"action": "list", "limit": 0},
                {"action": "list", "limit": 101},
                {"action": "read", "message_id": topic["opening_post_id"], "limit": 20},
                {"action": "read", "cursor": None},
                {"action": "post", "text": "missing request"},
                {"action": "post", "request_id": "missing-text"},
                {**post, "text": "", "request_id": "empty"},
                {"action": "create", "text": "missing title", "request_id": "missing-title"},
                {"action": "join"},
                {"action": "leave"},
            ]
            cases.extend((f"invalid_{index}", value, False) for index, value in enumerate(invalids))
            cases.extend(
                [
                    (
                        "bounds_post_max",
                        {**post, "text": "x" * 16000, "request_id": "max-text"},
                        True,
                    ),
                    (
                        "bounds_post_over",
                        {**post, "text": "x" * 16001, "request_id": "over-text"},
                        False,
                    ),
                    (
                        "bounds_title_max",
                        {**create, "title": "x" * 120, "request_id": "max-title"},
                        True,
                    ),
                    (
                        "bounds_title_over",
                        {**create, "title": "x" * 121, "request_id": "over-title"},
                        False,
                    ),
                    ("bounds_request_max", {**post, "request_id": "x" * 128}, True),
                    ("bounds_request_over", {**post, "request_id": "x" * 129}, False),
                    (
                        "bounds_title_empty",
                        {**create, "title": "", "request_id": "empty-title"},
                        False,
                    ),
                    ("bounds_request_empty", {**post, "request_id": ""}, False),
                ]
            )
            if tool_name == "swarm_inbox":
                for index in range(25):
                    await store.post(
                        sid, peer, text=f"backlog-{index}", request_id=f"backlog-{index}"
                    )
                cases = [
                    ("receive_one", {"limit": 1}, True),
                    ("receive_continuation", {"limit": 1}, True),
                    ("receive_default", {}, True),
                    ("receive_max", {"limit": 100}, True),
                    ("receive_empty", {}, True),
                ]
                cases.extend(
                    (f"invalid_{index}", value, False)
                    for index, value in enumerate(
                        [
                            {"action": "receive"},
                            {"swarm_id": sid},
                            {"participant_id": pid},
                            {"cursor": "invented"},
                            {"limit": True},
                            {"limit": "1"},
                            {"limit": 0},
                            {"limit": 101},
                            {"limit": None},
                            {"limit": 1.5},
                        ]
                    )
                )
            if tool_name == "swarm_state":
                await store.record_run_started(sid, pid, run_id=context.run_id, expected_epoch=0)
                status = await store.participant_status(sid, pid, limit=1)
                cases = [
                    ("status_default", {}, True),
                    ("status_one", {"limit": 1}, True),
                    ("status_max", {"limit": 100}, True),
                    (
                        "status_cursor",
                        {"cursor": status["cursor"], "limit": 1},
                        True,
                    ),
                    (
                        "status_cursor_changed_detail",
                        {
                            "cursor": status["cursor"],
                            "limit": 1,
                            "include_summaries": True,
                        },
                        False,
                    ),
                    (
                        "status_cursor_changed_limit",
                        {
                            "cursor": status["cursor"],
                            "limit": 2,
                        },
                        False,
                    ),
                    ("name_rejected", {"action": "name", "name": "Analyst"}, False),
                    ("name_field_rejected", {"name": "Analyst"}, False),
                ]
                cases.extend(
                    (f"invalid_{index}", value, False)
                    for index, value in enumerate(
                        [
                            {"action": "status"},
                            {"action": "unsupported"},
                            {"swarm_id": sid},
                            {"participant_id": pid},
                            {"limit": True},
                            {"limit": "1"},
                            {"limit": 0},
                            {"limit": 101},
                            {"cursor": None},
                            {"cursor": "foreign"},
                            {"include_summaries": "true"},
                            {"include_summaries": None},
                            {"action": "wait", "include_summaries": True},
                            {"action": "name"},
                            {"action": "name", "name": "Another", "limit": 1},
                            {"action": "wait", "needs_user": "true"},
                            {"action": "wait", "needs_user": 1},
                            {"action": "wait", "needs_user": None},
                            {"action": "wait", "reason": None},
                            {"action": "wait", "summary": "inapplicable"},
                            {"action": "done"},
                            {"action": "done", "summary": ""},
                            {"action": "done", "summary": None},
                            {"action": "done", "summary": "done", "artifacts": "report.md"},
                            {"action": "done", "summary": "done", "artifacts": [123]},
                        ]
                    )
                )
            if args.swarm_case in {"workflow", "unassisted"}:
                return await _probe_swarm_workflow(
                    adapter, args, extensions, registry, service, sessions, context, binding, peer
                )
            definitions = registry.provider_definitions(
                [tool_name], session_grants=context.session_tool_grants
            )
            rendered = render_tool_definitions(definitions, profile=_expected_profile(args))
            strict_count = sum(item.get("strict") is True for item in rendered)
            rows = []
            for name, expected, should_succeed in cases:
                # Exact character-count copying is a resource-boundary diagnostic,
                # separate from the action/default/type invocation matrix. The
                # deterministic adapter test runs these through production dispatch.
                if args.swarm_case == "all" and name.startswith("bounds_"):
                    continue
                if (
                    args.swarm_case != "all"
                    and name != args.swarm_case
                    and not name.startswith(args.swarm_case + "_")
                ):
                    continue
                async with asyncio.timeout(args.total_timeout):
                    raw = await adapter.send(
                        [
                            {
                                "role": "system",
                                "content": "This is an isolated Tool contract conformance test. "
                                "Emit exactly one Tool Call with the supplied arguments, including "
                                "intentional invalid values. "
                                "Do not repair intentional test inputs. "
                                "Do not add optional fields.",
                            },
                            {
                                "role": "user",
                                "content": f"Call {tool_name} with this test input: "
                                + json.dumps(expected, ensure_ascii=False),
                            },
                        ],
                        model_id=args.model,
                        tools=definitions,
                        thinking_effort=args.thinking_effort,
                        max_tokens=args.max_tokens or 3500,
                    )
                response = adapter.normalize_response(raw, model_id=args.model)
                calls = response.get("tool_calls") or []
                valid_call = (
                    len(calls) == 1
                    and calls[0].get("name") == tool_name
                    and calls[0].get("arguments") == expected
                )
                success = False
                actual_code = None
                durable = True
                if valid_call:
                    call_context = replace(context, tool_call_id=f"{name}-{calls[0]['id']}")
                    try:
                        result = await registry.dispatch(
                            call_context, expected, allowed_tools=[tool_name]
                        )
                        success = result["ok"]
                        actual_code = (result.get("error") or {}).get("code")
                    except ToolContractError:
                        result = {
                            "ok": False,
                            "error": {"code": "invalid_arguments"},
                            "data": None,
                            "artifacts": [],
                        }
                        actual_code = "invalid_arguments"
                    receipts = call_context._delivery_receipts
                    if success and receipts:
                        durable = all(
                            [not await store.reconcile_delivery(receipt[0]) for receipt in receipts]
                        )
                        await sessions.append_messages_with_receipts_async(
                            binding.address,
                            generation_id=binding.generation_id,
                            owner_name="swarm",
                            messages=[
                                ChatMessage.tool(
                                    tool_call_id=call_context.tool_call_id,
                                    name=tool_name,
                                    content=json.dumps(result),
                                )
                            ],
                            receipts=[(0, *receipt, "tool") for receipt in receipts],
                        )
                        durable = durable and all(
                            [await store.reconcile_delivery(receipt[0]) for receipt in receipts]
                        )
                row = {
                    "case": name,
                    "model_call_valid": valid_call,
                    "runtime_ok": success,
                    "expected_ok": should_succeed,
                    "error_code": actual_code,
                    "durable_receipt_verified": durable,
                    "passed": valid_call and success == should_succeed and durable,
                }
                rows.append(row)
                print(json.dumps(row), flush=True)
            return {
                "scenario": "swarm_tool",
                "tool": tool_name,
                "model": args.model,
                "strict_true_tool_count": strict_count,
                "cases": rows,
                "passed": bool(rows) and not strict_count and all(row["passed"] for row in rows),
            }
        finally:
            await service.close()
            await manager.aclose()
            sessions.close()


async def _probe_swarm_workflow(
    adapter: Any,
    args: argparse.Namespace,
    extensions: Any,
    registry: Any,
    service: Any,
    sessions: Any,
    context: Any,
    binding: Any,
    peer: str,
) -> dict[str, Any]:
    """Evaluate first-use choices separately from exact-argument conformance.

    This disposable fixture uses production scoped wording, definitions, handlers,
    Board effects and canonical receipts. Actual Chat admission/terminal ownership
    is verified by the owning lifecycle integration tests, not simulated here.
    """
    from core.chat import ChatMessage
    from core.chat.wire_shaping import _notes_to_request_messages
    from resources.extensions.swarm.agent_text import RESUME_REMINDER

    names = ("swarm_board", "swarm_inbox", "swarm_state")
    definitions = registry.provider_definitions(names, session_grants=names)
    if {tool["name"] for tool in definitions} != set(names):
        raise RuntimeError("Fresh-participant evaluation requires the complete production Tool set")
    store = service.store
    sid, pid = binding.group_id, binding.participant_id
    profile = (await store.get_swarm(sid))["profile_snapshot"]
    context = replace(context, session_tool_grants=names)
    await store.record_run_started(sid, pid, run_id=context.run_id, expected_epoch=0)
    unassisted = args.swarm_case == "unassisted"
    peers = [row["id"] for row in (await store.get_swarm(sid))["participants"] if row["id"] != pid]
    if unassisted:
        for index, participant_id in enumerate(peers):
            await store.post(
                sid,
                participant_id,
                text="I suggest a short checklist covering factual accuracy, clarity, and "
                "completeness. I agree to that approach and can review your contribution.",
                request_id=f"unassisted-proposal-{index}",
            )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": profile["instructions"]},
        {
            "role": "user",
            "content": (
                "Work with your peers to agree on a three-step checklist for reviewing a "
                "short text report. Incorporate their feedback and finish your contribution. "
                "Keep the checklist in this conversation; no files are needed."
            )
            if unassisted
            else "Prepare a three-step checklist for reviewing a short text report with "
            "your peers. Inspect the group and existing Board, introduce your approach, and "
            "join the existing Topic discussion. Create a review discussion containing your "
            "draft checklist and publicly ping a peer for feedback. Receive incoming messages "
            "while working. Yield once while awaiting feedback. When I resume your work, "
            "review the feedback and finish your contribution after receiving pending work. "
            "This task needs no filesystem changes.",
        },
    ]
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    receipts_verified = 0
    resumed = False
    finished = False
    feedback_ids: set[str] = set()
    published_ids: set[str] = set()
    feedback_received = False
    received_ids: set[str] = set()
    published_after_feedback = False
    for step in range(32):
        async with asyncio.timeout(args.total_timeout):
            raw = await adapter.send(
                messages,
                model_id=args.model,
                tools=definitions,
                thinking_effort=args.thinking_effort,
                max_tokens=args.max_tokens or 3500,
            )
        response = adapter.normalize_response(raw, model_id=args.model)
        calls = response.get("tool_calls") or []
        messages.append(
            {
                **{
                    key: response[key]
                    for key in ("content", "tool_calls", "reasoning", "reasoning_meta")
                    if key in response
                },
                "role": "assistant",
            }
        )
        if not calls:
            if not unassisted and not resumed:
                await store.post(
                    sid,
                    peer,
                    text="I reviewed the draft: check factual accuracy, clarity, and completeness. "
                    "The checklist is ready; no further changes are needed.",
                    request_id="workflow-feedback",
                )
                await store.reconcile_run_finished(
                    sid, pid, run_id=context.run_id, expected_epoch=0, outcome="completed"
                )
                context = replace(context, run_id="workflow-resumed")
                await store.record_run_started(sid, pid, run_id=context.run_id, expected_epoch=0)
                reminder = ChatMessage.note(RESUME_REMINDER)
                await sessions.append_messages_with_receipts_async(
                    binding.address,
                    generation_id=binding.generation_id,
                    owner_name="swarm",
                    messages=[reminder],
                    receipts=[],
                )
                messages.extend(_notes_to_request_messages([reminder]))
                resumed = True
                continue
            finished = True
            break
        carriers = []
        receipts: list[tuple[int, str, str, str, str]] = []
        for index, call in enumerate(calls):
            name, arguments = call["name"], call["arguments"]
            call_context = replace(
                context, tool_name=name, tool_call_id=call["id"], tool_call_index=index
            )
            result = await registry.dispatch(call_context, arguments, allowed_tools=names)
            carriers.append(
                ChatMessage.tool(tool_call_id=call["id"], name=name, content=json.dumps(result))
            )
            if result["ok"]:
                data = result["data"]
                received = {entry["id"] for entry in data.get("entries", [])}
                received.update(entry["id"] for entry in data.get("recent", {}).get("entries", []))
                received_ids.update(received)
                if feedback_ids and feedback_ids.issubset(received_ids):
                    feedback_received = True
                if name == "swarm_board" and arguments.get("action") in {"post", "create"}:
                    post_id = data.get("post_id", data.get("opening_post_id"))
                    if post_id and post_id not in published_ids:
                        published_ids.add(post_id)
                        published_after_feedback |= feedback_received
                seen.add((name, arguments.get("action", "")))
                receipts.extend(
                    (index, *receipt, "tool") for receipt in call_context._delivery_receipts
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": name,
                    "content": json.dumps(result),
                }
            )
            row = {
                "step": step,
                "tool": name,
                "action": arguments.get("action"),
                "ok": result["ok"],
                "error_code": (result.get("error") or {}).get("code"),
            }
            rows.append(row)
            print(json.dumps(row), flush=True)
        await sessions.append_messages_with_receipts_async(
            binding.address,
            generation_id=binding.generation_id,
            owner_name="swarm",
            messages=carriers,
            receipts=receipts,
        )
        for receipt in receipts:
            if not await store.reconcile_delivery(receipt[1]):
                raise RuntimeError(
                    "Workflow delivery did not reconcile against its canonical carrier"
                )
            receipts_verified += 1
        if unassisted and published_ids and not feedback_ids:
            for index, participant_id in enumerate(peers):
                feedback = await store.post(
                    sid,
                    participant_id,
                    text="I reviewed your contribution. Please make the checklist actionable: "
                    "verify consequential claims against sources, check whether the intended "
                    "reader can follow it, and check the report against the requested scope. "
                    "I agree to conclude once those checks appear in the final checklist.",
                    request_id=f"unassisted-feedback-{index}",
                )
                feedback_ids.add(feedback["post_id"])
    required = {
        ("swarm_state", ""),
        ("swarm_board", "post"),
        ("swarm_board", "create"),
        ("swarm_board", "join"),
        ("swarm_inbox", ""),
    }
    if unassisted:
        required = set()
    coordinated = feedback_received and published_after_feedback if unassisted else resumed
    strict_count = sum(
        item.get("strict") is True
        for item in render_tool_definitions(definitions, profile=_expected_profile(args))
    )
    return {
        "scenario": "swarm_unassisted" if unassisted else "swarm_workflow",
        "feedback_received": feedback_received,
        "published_after_feedback": published_after_feedback,
        "model": args.model,
        "strict_true_tool_count": strict_count,
        "calls": rows,
        "missing_actions": sorted(required - seen),
        "durable_receipts": receipts_verified,
        "resumed": resumed,
        "final_response_received": finished,
        "scope": "Model choices, Board effects and canonical carriers; "
        "actual Chat lifecycle is tested separately",
        "passed": required.issubset(seen)
        and coordinated
        and finished
        and receipts_verified > 0
        and strict_count == 0,
    }


def _reflection_cases() -> list[dict[str, Any]]:
    path = Path(__file__).resolve().parents[1] / "tests/fixtures/reflection/cases.json"
    cases: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    return cases


async def _probe_reflection_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any], scope: str
) -> dict[str, Any]:
    """Evaluate decisions using production prose and real disposable Tool effects.

    Only synthetic history and production context reach the Model; expectations
    stay in the observer. Chat fork/cadence integration is tested separately.
    """
    from core.automation.reflection import REFLECT_FRAGMENT_NAMES, REFLECTION_TOOL_RESTRICTIONS
    from core.memory.memory import MemoryService, memory_block_definition
    from core.prompts.prompts import _format_skill_catalog
    from core.skills import SkillAuthoringService, SkillRegistry
    from core.tools.memory import register_memory_tool
    from core.tools.skill import register_skill_tool
    from core.tools.skill_manage import register_skill_manage_tool
    from core.tools.tools import ToolContext, ToolRegistry, tool_failure

    resources = Path(__file__).resolve().parents[1] / "resources/prompts"
    names = ("memory", "skill", "skill_manage")
    allowed = names if scope == "learn" else REFLECTION_TOOL_RESTRICTIONS[scope]  # type: ignore[index]
    with TemporaryDirectory(prefix="vbot-reflection-probe-") as temporary:
        root = Path(temporary)
        own, bundled = root / "skills", root / "bundled"
        memory = MemoryService()
        for memory_scope, entries in case.get("memory", {}).items():
            for entry in entries:
                memory.add_entry(root, memory_scope, entry)
        for skill in case.get("skills", []):
            home = bundled if skill.get("readonly") else own
            path = home / skill["name"] / "SKILL.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(skill["content"], encoding="utf-8")

        def skills() -> SkillRegistry:
            return SkillRegistry.load(own, extra_dirs=[bundled], origins=["agent", "bundled"])

        registry = ToolRegistry()
        register_memory_tool(registry, memory)
        register_skill_tool(registry, lambda *_args: skills(), lambda: None)
        register_skill_manage_tool(
            registry,
            SkillAuthoringService(protected_roots=[bundled]),
            lambda _agent: own,
            lambda _agent: None,
            resolve_external_skill_scope=lambda _agent, name, _project: (
                "bundled" if (bundled / name / "SKILL.md").is_file() else None
            ),
        )
        definitions = registry.provider_definitions(names)
        if {tool["name"] for tool in definitions} != set(names):
            raise RuntimeError("Reflection probe requires all three production Tool definitions")
        catalog = _format_skill_catalog(skills().filter_allowed(["*"]))
        memory_text = memory.read_prompt_files(root, "agent_user")
        if case.get("stale_memory_prompt"):
            memory_text = "# Agent Memory\nNo entries yet.\n# User Profile\nNo entries yet."
        system = "\n\n".join(
            [
                (memory_block_definition().default_text or "").replace(
                    "{generated:memory_files}", memory_text
                ),
                (resources / "skills.md")
                .read_text(encoding="utf-8")
                .replace("{generated:skill_catalog}", catalog),
                (resources / "skill_maintenance.md").read_text(encoding="utf-8"),
            ]
        )
        fragment = "learn.md" if scope == "learn" else REFLECT_FRAGMENT_NAMES[scope]  # type: ignore[index]
        brief = (resources / fragment).read_text(encoding="utf-8").strip()
        if scope == "learn":
            brief += "\n\nThe request to learn from:\n" + case["learn_request"]
        messages = [
            {"role": "system", "content": system},
            *case["history"],
            {"role": "user", "content": "<system-reminder>\n" + brief + "\n</system-reminder>"},
        ]

        def snapshot() -> dict[str, Any]:
            return {
                "memory": {
                    scope_name: [entry.content for entry in memory.list_entries(root, scope_name)]
                    for scope_name in ("user", "agent")
                },
                "files": {
                    path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
                    for home in (own, bundled)
                    for path in home.rglob("*")
                    if path.is_file()
                },
            }

        before = snapshot()
        reads: set[tuple[str, str]] = set()
        actions: list[dict[str, Any]] = []
        violations: list[str] = []
        finished = False
        steps = 0
        for step in range(12):
            steps = step + 1
            raw = await adapter.send(
                messages,
                model_id=args.model,
                tools=definitions,
                thinking_effort=args.thinking_effort,
                max_tokens=args.max_tokens or 4000,
            )
            response = adapter.normalize_response(raw, model_id=args.model)
            calls = response.get("tool_calls") or []
            messages.append(
                {
                    "role": "assistant",
                    **{
                        key: response[key]
                        for key in ("content", "tool_calls", "reasoning", "reasoning_meta")
                        if key in response
                    },
                }
            )
            if not calls:
                finished = bool(str(response.get("content") or "").strip())
                break
            for index, call in enumerate(calls):
                name, arguments = call["name"], call["arguments"]
                action = arguments.get("action", "")
                target = (
                    arguments.get("scope", "") if name == "memory" else arguments.get("name", "")
                )
                file_path = arguments.get("file_path", "SKILL.md")
                mutation = (name == "memory" and action != "list") or name == "skill_manage"
                if mutation:
                    if name == "memory" and ("memory", target) not in reads:
                        violations.append("memory_write_without_current_list")
                    if name == "skill_manage":
                        if action == "create" and ("skill", "catalog") not in reads:
                            violations.append("create_without_current_catalog")
                        if (own / target / file_path).is_file() and (
                            target,
                            file_path,
                        ) not in reads:
                            violations.append("skill_write_without_current_file")
                context = ToolContext(
                    agent_id="probe",
                    session_id="probe-session",
                    run_id="probe-run",
                    tool_call_id=call["id"],
                    tool_name=name,
                    tool_call_index=index,
                    workspace=root,
                    vbot_root=root,
                    data_root=root,
                    cwd=root,
                )
                try:
                    result = await registry.dispatch(context, arguments, allowed)
                except Exception as error:
                    result = tool_failure("probe_dispatch_rejected", type(error).__name__)
                if not result["ok"]:
                    violations.append("tool_call_rejected")
                elif name == "memory" and action == "list":
                    reads.add(("memory", target))
                elif name == "skill" and not arguments:
                    reads.add(("skill", "catalog"))
                elif name == "skill" and arguments.get("file_path"):
                    reads.add((target, file_path))
                if mutation:
                    actions.append({"tool": name, "action": action, "ok": result["ok"]})
                messages.append(
                    {
                        "role": "tool",
                        "name": name,
                        "tool_call_id": call["id"],
                        "content": json.dumps(result),
                    }
                )
        after = snapshot()
        expected = case["expected"][scope]
        kind = expected["kind"]
        changed_files = {
            path
            for path in before["files"].keys() | after["files"].keys()
            if before["files"].get(path) != after["files"].get(path)
        }
        if kind == "none":
            effect_ok = before == after and not actions
            payload = ""
        elif kind in ("user", "agent"):
            other = "agent" if kind == "user" else "user"
            payload = "\n".join(after["memory"][kind])
            effect_ok = (
                not changed_files
                and before["memory"][other] == after["memory"][other]
                and len(after["memory"][kind]) == expected.get("count", 1)
                and before["memory"][kind] != after["memory"][kind]
            )
        else:
            payload = "\n".join(after["files"].get(path, "") for path in changed_files)
            created = [
                path
                for path in changed_files
                if path.endswith("/SKILL.md") and path not in before["files"]
            ]
            effect_ok = (
                bool(changed_files)
                and before["memory"] == after["memory"]
                and all(path.startswith("skills/") for path in changed_files)
            )
            if kind == "create":
                effect_ok = effect_ok and len(created) == 1
            else:
                effect_ok = effect_ok and changed_files == {
                    "skills/" + expected["name"] + "/SKILL.md"
                }
        evidence_ok = all(
            token.lower() in payload.lower() for token in expected.get("contains", [])
        ) and all(token.lower() not in payload.lower() for token in expected.get("excludes", []))
        return {
            "case": case["id"],
            "scope": scope,
            "steps": steps,
            "actions": actions,
            "violations": violations,
            "final_response_received": finished,
            "effect_ok": effect_ok,
            "evidence_ok": evidence_ok,
            "passed": finished and effect_ok and evidence_ok and not violations,
        }


async def _probe_reflection_workflow(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    selected = [
        (case, scope)
        for case in _reflection_cases()
        for scope in case["expected"]
        if args.reflection_case in ("all", case["id"]) and args.reflection_scope in ("all", scope)
    ]
    if not selected:
        raise ValueError("No matching reflection scenario")
    slots = asyncio.Semaphore(3)

    async def evaluate(case: dict[str, Any], scope: str) -> dict[str, Any]:
        async with slots:
            try:
                async with asyncio.timeout(args.total_timeout):
                    return await _probe_reflection_case(adapter, args, case, scope)
            except Exception as error:  # noqa: BLE001 - retain other independent case results
                return {
                    "case": case["id"],
                    "scope": scope,
                    "passed": False,
                    "error_type": type(error).__name__,
                }

    rows = await asyncio.gather(*(evaluate(case, scope) for case, scope in selected))
    return {
        "scenario": "reflection_workflow",
        "model": args.model,
        "cases": rows,
        "passed": all(row["passed"] for row in rows),
    }


def _apply_patch_cases() -> list[dict[str, Any]]:
    def patch(body: str) -> dict[str, str]:
        return {"patch": "*** Begin Patch\n" + body + "\n*** End Patch"}

    return [
        {
            "id": "create",
            "before": {},
            "task": "Create new.txt containing exactly hello followed by a newline.",
            "expected": {"new.txt": "hello\n"},
        },
        {
            "id": "batch_locations",
            "before": {
                "settings.txt": "timeout=10\nseparator\nretries=1\n",
                "notes.txt": "Status: draft\n",
            },
            "task": (
                "Set timeout to 20 and retries to 3 in settings.txt, and mark notes.txt as ready."
            ),
            "expected": {
                "settings.txt": "timeout=20\nseparator\nretries=3\n",
                "notes.txt": "Status: ready\n",
            },
        },
        {
            "id": "batch_file_operations",
            "before": {"source.txt": "keep\n", "obsolete.txt": "obsolete\n"},
            "task": (
                "Move source.txt to nested/moved.txt, delete obsolete.txt, and create "
                "notes.txt containing done followed by a newline."
            ),
            "expected": {"nested/moved.txt": "keep\n", "notes.txt": "done\n"},
        },
        {
            "id": "partial_hunks",
            "before": {"one.txt": "first=old\nsecond=old\n"},
            "arguments": patch(
                "*** Update File: one.txt\n@@\n-first=old\n+first=new\n@@\n"
                "-completely missing declaration\n+unused\n@@\n-second=old\n+second=new"
            ),
            "expected": {"one.txt": "first=new\nsecond=new\n"},
            "status": "partial",
            "entries": ["applied", "failed", "applied"],
        },
        {
            "id": "partial_files",
            "before": {},
            "arguments": patch(
                "*** Add File: first.txt\n+one\n*** Delete File: missing.txt\n"
                "*** Add File: last.txt\n+two"
            ),
            "expected": {"first.txt": "one\n", "last.txt": "two\n"},
            "status": "partial",
            "entries": ["applied", "failed", "applied"],
        },
        {
            "id": "failed_move_dependency",
            "before": {"source.txt": "source\n", "destination.txt": "destination\n"},
            "arguments": patch(
                "*** Move File: source.txt -> destination.txt\n"
                "*** Update File: destination.txt\n@@\n-destination\n+clobbered\n"
                "*** Add File: good.txt\n+done"
            ),
            "expected": {
                "source.txt": "source\n",
                "destination.txt": "destination\n",
                "good.txt": "done\n",
            },
            "status": "partial",
            "entries": ["failed", "skipped", "applied"],
        },
        {
            "id": "recover_without_replay",
            "before": {
                "log.txt": "start\n",
                "one.txt": "def deploy():\n    timeout = 30\n    retries = 5\n",
            },
            "task": (
                "Append done followed by a newline to log.txt and change the deploy timeout "
                "to 60 in one.txt. Preserve the retries setting."
            ),
            "seed": patch(
                "*** Update File: log.txt\n@@\n+done\n*** Update File: one.txt\n@@\n"
                " def deploy():\n-    completely unrelated declaration\n"
                "+    timeout = 60\n     retries = 5"
            ),
            "expected": {
                "log.txt": "start\ndone\n",
                "one.txt": "def deploy():\n    timeout = 60\n    retries = 5\n",
            },
            "only_paths": ["one.txt"],
        },
        {
            "id": "unframed_implicit_hunk",
            "before": {"one.txt": "heading\nold\ntail\n"},
            "arguments": {"patch": "*** Update File: one.txt\nheading\n-old\n+new\ntail"},
            "expected": {"one.txt": "heading\nnew\ntail\n"},
        },
        {
            "id": "binary_update",
            "before": {"one.txt": "a\x00b"},
            "arguments": patch("*** Update File: one.txt\n@@\n-a\n+b"),
            "error": "binary_file",
        },
        {
            "id": "empty",
            "before": {},
            "task": "Create an empty new.txt file.",
            "expected": {"new.txt": ""},
        },
        {
            "id": "update",
            "before": {"one.txt": "alpha\nold\nomega\n"},
            "task": "Change the line old to new in one.txt.",
            "expected": {"one.txt": "alpha\nnew\nomega\n"},
        },
        {
            "id": "delete",
            "before": {"gone.txt": "obsolete\n"},
            "task": "Delete gone.txt.",
            "expected": {},
        },
        {
            "id": "move",
            "before": {"one.txt": "keep\n"},
            "task": "Move one.txt to nested/new.txt without changing its contents.",
            "expected": {"nested/new.txt": "keep\n"},
        },
        {
            "id": "update_move",
            "before": {"one.txt": "old\n"},
            "task": "Move one.txt to moved.txt and change old to new in it.",
            "expected": {"moved.txt": "new\n"},
        },
        {
            "id": "move_with_hunks",
            "before": {"one.txt": "old\n"},
            "arguments": patch("*** Move File: one.txt -> moved.txt\n@@\n-old\n+new"),
            "expected": {"moved.txt": "new\n"},
        },
        {
            "id": "sequence",
            "before": {},
            "arguments": patch(
                "*** Add File: one.txt\n+old\n*** Update File: one.txt\n@@\n-old\n+new"
            ),
            "expected": {"one.txt": "new\n"},
        },
        {
            "id": "append",
            "before": {"one.txt": "first\n"},
            "arguments": patch("*** Update File: one.txt\n@@\n+last"),
            "expected": {"one.txt": "first\nlast\n"},
        },
        {
            "id": "hint",
            "before": {"one.txt": "first\nold\nsecond\nold\n"},
            "arguments": patch("*** Update File: one.txt\n@@ second\n-old\n+new"),
            "expected": {"one.txt": "first\nold\nsecond\nnew\n"},
        },
        {
            "id": "eof",
            "before": {"one.txt": "old\nsecond\nold\n"},
            "arguments": patch("*** Update File: one.txt\n@@\n-old\n+new\n*** End of File"),
            "expected": {"one.txt": "old\nsecond\nnew\n"},
        },
        {
            "id": "retry",
            "before": {"one.txt": "alpha\nnew\nomega\n"},
            "arguments": patch("*** Update File: one.txt\n@@\n alpha\n-old\n+new\n omega"),
            "expected": {"one.txt": "alpha\nnew\nomega\n"},
        },
        {
            "id": "ambiguous",
            "before": {"one.txt": "old\nother\nold\n"},
            "arguments": patch("*** Update File: one.txt\n@@\n-old\n+new"),
            "error": "ambiguous_match",
        },
        {
            "id": "missing",
            "before": {},
            "arguments": patch("*** Delete File: missing.txt"),
            "error": "file_not_found",
        },
        {
            "id": "collision",
            "before": {"one.txt": "keep\n"},
            "arguments": patch("*** Add File: one.txt\n+clobber"),
            "error": "destination_exists",
        },
        {
            "id": "malformed",
            "before": {},
            "arguments": patch("*** Unknown File: one.txt"),
            "error": "invalid_patch",
        },
        {
            "id": "unknown_field",
            "before": {},
            "arguments": {**patch("*** Add File: new.txt\n+hello"), "path": "new.txt"},
            "error": "invalid_arguments",
        },
    ]


async def _probe_apply_patch_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any]
) -> dict[str, Any]:
    from core.tools._patch_syntax import _parse
    from core.tools.apply_patch import register_apply_patch_tool
    from core.tools.file_state import FileReadState
    from core.tools.tools import ToolContext, ToolRegistry

    with TemporaryDirectory(prefix="vbot-patch-probe-") as directory:
        root = Path(directory).resolve()
        for name, content in case["before"].items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        registry = ToolRegistry()
        register_apply_patch_tool(registry, file_state=FileReadState())
        definitions = registry.provider_definitions(allowed_tools=["apply_patch"])
        if "arguments" in case:
            task = (
                "Exercise this file-tool request exactly once, including any invalid fields or "
                "patch syntax so its validation is exercised. Do not repair the request: "
                + json.dumps(case["arguments"])
            )
        else:
            task = case["task"]
        instruction = (
            task + "\nCurrent files and their exact contents:\n" + json.dumps(case["before"])
        )
        # Natural tasks receive no call-count instruction or expected arguments.
        messages: list[dict[str, Any]] = (
            [
                {
                    "role": "system",
                    "content": "Exercise the requested Tool Call exactly once. Preserve the "
                    "requested arguments verbatim, including deliberately invalid fields "
                    "or syntax: the Tool's runtime validation is under test. "
                    "Do not repair or omit invalid arguments. Do not answer with ordinary text.",
                },
                {"role": "user", "content": instruction},
            ]
            if "arguments" in case
            else [{"role": "user", "content": instruction}]
        )
        if "seed" in case:
            seed_context = ToolContext(
                agent_id="probe",
                session_id="probe-session",
                run_id="probe-run",
                tool_call_id="seed",
                tool_name="apply_patch",
                tool_call_index=0,
                workspace=root,
                cwd=root,
                vbot_root=root,
                data_root=root,
            )
            seed_result = await registry.dispatch(seed_context, case["seed"], ["apply_patch"])
            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "seed", "name": "apply_patch", "arguments": case["seed"]}
                        ],
                    },
                    {"role": "tool", "tool_call_id": "seed", "content": json.dumps(seed_result)},
                ]
            )
        raw = await adapter.send(
            messages,
            model_id=args.model,
            tools=definitions,
            thinking_effort=args.thinking_effort,
            max_tokens=args.max_tokens or 4000,
        )
        response = adapter.normalize_response(raw, model_id=args.model)
        calls = response.get("tool_calls") or []
        outcomes: list[dict[str, Any]] = []
        touched_paths: set[str] = set()
        for index, call in enumerate(calls):
            arguments = call.get("arguments", {})
            # The evaluator can only mutate its disposable directory, even if
            # the model invents a path. Runtime file tools retain normal agency.
            try:
                operations = _parse(arguments.get("patch", ""))
            except Exception:
                operations = []  # Let the real handler diagnose malformed input.
            safe = all(
                (root / name).resolve().is_relative_to(root)
                for operation in operations
                for name in (operation.path, operation.destination)
                if name is not None
            )
            if not safe or call.get("name") != "apply_patch":
                outcomes.append({"ok": False, "error": {"code": "probe_scope_violation"}})
                continue
            touched_paths.update(
                (root / name).resolve().relative_to(root).as_posix()
                for operation in operations
                for name in (operation.path, operation.destination)
                if name is not None
            )
            context = ToolContext(
                agent_id="probe",
                session_id="probe-session",
                run_id="probe-run",
                tool_call_id=call["id"],
                tool_name="apply_patch",
                tool_call_index=index,
                workspace=root,
                cwd=root,
                vbot_root=root,
                data_root=root,
            )
            outcomes.append(await registry.dispatch(context, arguments, ["apply_patch"]))
        snapshot = {
            p.relative_to(root).as_posix(): p.read_bytes().decode("utf-8")
            for p in root.rglob("*")
            if p.is_file()
        }
        expected = case.get("expected", case["before"])
        codes = [outcome["error"]["code"] if not outcome["ok"] else None for outcome in outcomes]
        data = (outcomes[0].get("data") or {}) if len(outcomes) == 1 else {}
        entries = [entry["status"] for entry in data.get("results", [])]
        result_ok = (
            (case.get("status", "success") == data.get("status")) if not case.get("error") else True
        )
        entries_ok = entries == case["entries"] if "entries" in case else True
        recovery_ok = touched_paths == set(case["only_paths"]) if "only_paths" in case else True
        passed = (
            len(outcomes) == 1
            and snapshot == expected
            and codes == [case.get("error")]
            and result_ok
            and entries_ok
            and recovery_ok
        )
        return {
            "case": case["id"],
            "passed": passed,
            "calls": len(calls),
            "effect_ok": snapshot == expected,
            "error_codes": codes,
            "result_ok": result_ok and entries_ok,
            "recovery_ok": recovery_ok,
            "definition_chars": len(json.dumps(definitions, separators=(",", ":"))),
        }


async def _probe_apply_patch(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    from core.tools import apply_patch as patch_module

    expected_source = Path(__file__).resolve().parents[1] / "core/tools/apply_patch.py"
    if Path(patch_module.__file__).resolve() != expected_source:
        raise RuntimeError(
            "The probe loaded apply_patch from another checkout. Run "
            "python -m scripts.probe_provider_tool_call from the checkout being evaluated."
        )
    limit = asyncio.Semaphore(4)

    async def evaluate(case: dict[str, Any]) -> dict[str, Any]:
        async with limit:
            return await _probe_apply_patch_case(adapter, args, case)

    rows = await asyncio.gather(*(evaluate(case) for case in _apply_patch_cases()))
    return {
        "scenario": "apply_patch",
        "model": args.model,
        "cases": rows,
        "passed": all(row["passed"] for row in rows),
    }


def _terminal_cases() -> list[dict[str, Any]]:
    from core.tools.terminal_manager import (
        TERMINAL_MAX_COLUMNS,
        TERMINAL_MAX_ROWS,
        TERMINAL_MIN_COLUMNS,
        TERMINAL_MIN_ROWS,
    )

    cases = []

    def add(case_id, action, error=None, **fields):
        arguments = {"action": action, **fields}
        if action not in {"start", "list"}:
            arguments["terminal_id"] = "{terminal_id}"
        cases.append({"id": case_id, "arguments": arguments, "error": error})

    add("start_default", "start")
    add("start_command", "start", command="fixture")
    add("start_args", "start", command="fixture", args=["--label", "two words"])
    add("start_text", "start", command="fixture", text="first task")
    for name, value in (("relative", "."), ("absolute", "{root}"), ("project", "project:probe")):
        add("start_workdir_" + name, "start", command="fixture", workdir=value)
    add("start_name", "start", command="fixture", name="Review")
    add("start_group", "start", command="fixture", group="Review")
    add("list", "list")
    add("attach", "attach")
    add("attach_idempotent", "attach")
    add("detach", "detach")
    add("status_default", "status")
    add("status_lines_min", "status", lines=1)
    add("status_lines_max", "status", lines=100)
    add("status_page_default", "status", start_line=0)
    add("status_page_later", "status", start_line=3, lines=2)
    add("wait_default", "wait")
    add("wait_revision", "wait", after_revision=0)
    add("wait_timeout", "wait", after_revision=999, timeout_ms=0)
    add("wait_max", "wait", timeout_ms=10000)
    add("input_noop", "input")
    add("input_text", "input", text="hello")
    add("input_submit", "input", text="hello", key="enter")
    add("input_multiline", "input", text="one\ntwo", key="enter")
    add("input_data", "input", data="\x1b[2~")
    for key in ("enter", "up", "shift_tab", "f12", "ctrl_c"):
        add("input_key_" + key, "input", key=key)
    add("input_guard", "input", text="y", expected_screen_revision="{revision}")
    add("resize_min", "resize", columns=TERMINAL_MIN_COLUMNS, rows=TERMINAL_MIN_ROWS)
    add("resize_max", "resize", columns=TERMINAL_MAX_COLUMNS, rows=TERMINAL_MAX_ROWS)
    add("kill", "kill")
    add("invalid_fields", "list", "invalid_arguments", text="extra")
    add("invalid_combination", "input", "invalid_arguments", data="x", text="y")
    add("invalid_resize", "resize", "invalid_arguments", columns=80)
    add("invalid_stale", "input", "stale_screen", text="y", expected_screen_revision=999)
    add("invalid_project", "start", "project_not_found", workdir="project:missing")
    add("invalid_args", "start", "invalid_arguments", args=["orphan"])
    add("invalid_owner", "input", "terminal_not_owned", text="y")
    cases.extend(
        [
            {
                "id": "natural_start",
                "task": (
                    "Open the default interactive shell so I can inspect it before typing anything."
                ),
                "expected": {"action": "start"},
            },
            {
                "id": "natural_submit",
                "task": "Type hello into the terminal and submit it.",
                "expected": {
                    "action": "input",
                    "text": "hello",
                    "key": "enter",
                    "terminal_id": "{terminal_id}",
                },
            },
            {
                "id": "natural_history",
                "task": "Read the first 10 lines of the retained terminal buffer.",
                "expected": {
                    "action": "status",
                    "start_line": 0,
                    "lines": 10,
                    "terminal_id": "{terminal_id}",
                },
            },
            {
                "id": "natural_notice",
                "task": (
                    "The terminal asks whether to proceed with the fixture check. "
                    "Answer y and submit it."
                ),
                "expected": {
                    "action": "input",
                    "text": "y",
                    "key": "enter",
                    "expected_screen_revision": "{revision}",
                    "terminal_id": "{terminal_id}",
                },
            },
        ]
    )
    cases.append(
        {
            "id": "natural_continue",
            "reference": "codex.md",
            "task": (
                'Continue the existing task in terminal {terminal_id}: send "hello" and submit it.'
            ),
            "expected": {
                "action": "input",
                "terminal_id": "{terminal_id}",
                "text": "hello",
                "key": "enter",
            },
        }
    )
    return cases


async def _probe_terminal_case(
    adapter: Any, args: argparse.Namespace, case: dict[str, Any]
) -> dict[str, Any]:
    # Reuse the existing disposable PTY fixture; Model output never launches a host program.
    from core.projects import ProjectStore
    from core.tools._terminal_events import _attention_body
    from core.tools._terminal_input import _input_chunks
    from core.tools.terminal import register_terminal_tool
    from core.tools.terminal_manager import TerminalManager
    from core.tools.tools import ToolRegistry
    from core.utils.tokens import estimate_json_tokens
    from tests.core.tools.test_terminal import make_context
    from tests.core.tools.test_terminal_manager import (
        AdapterFactory,
        PendingTriggerService,
        eventually,
        owner,
    )

    with TemporaryDirectory(prefix="vbot-terminal-probe-") as directory:
        root = Path(directory)
        factory = AdapterFactory()
        trigger = PendingTriggerService()
        manager = TerminalManager(trigger, adapter_factory=factory, activity_quiet_seconds=0.03)
        manager.start()
        projects = ProjectStore(root / "data")
        projects.create("probe", "Probe", root)
        registry = ToolRegistry()
        register_terminal_tool(registry, manager, projects)
        context = make_context(root)
        definitions = registry.provider_definitions(allowed_tools=["terminal"])
        try:
            seed = await registry.dispatch(
                context,
                {"action": "start", "command": case.get("expected", {}).get("command", "codex")},
                ["terminal"],
            )
            terminal_id = seed["data"]["terminal_id"]
            session = manager.get_session(terminal_id, owner())
            await manager.send_operator_input(terminal_id, "open")
            factory.adapters[0].emit(
                "Ready for next instruction> "
                if case["id"] == "natural_continue"
                else "fixture history\r\nProceed with fixture check? [y/n]"
            )
            await eventually(lambda: session.attention is not None)
            assert session.attention is not None
            revision = session.renderer.revision
            expected = dict(case.get("arguments", case.get("expected", {})))
            for key, value in expected.items():
                if value == "{terminal_id}":
                    expected[key] = terminal_id
                elif value == "{revision}":
                    expected[key] = revision
                elif value == "{root}":
                    expected[key] = root.as_posix()
            if case["id"] in {"attach", "invalid_owner"}:
                manager.detach(terminal_id, owner())
            if "task" in case:
                skill = (
                    Path(__file__).resolve().parents[1] / "resources/skills/coding-agents/SKILL.md"
                ).read_text(encoding="utf-8")
                if case.get("reference"):
                    skill += "\n\n" + (
                        Path(__file__).resolve().parents[1]
                        / "resources/skills/coding-agents/references"
                        / case["reference"]
                    ).read_text(encoding="utf-8")
                observation = (
                    _attention_body(session, session.attention)
                    if case["id"] == "natural_notice"
                    else json.dumps(await manager.snapshot(terminal_id, owner()))
                )
                messages = [
                    {"role": "system", "content": skill},
                    {
                        "role": "user",
                        "content": (
                            "Previous terminal observation:\n"
                            + observation
                            + "\nUser request:\n"
                            + case["task"]
                            .replace("{root}", root.as_posix())
                            .replace("{terminal_id}", terminal_id)
                        ),
                    },
                ]
                if case["id"] == "natural_start":
                    # Opening a host shell does not activate the coding-agent playbook.
                    messages = [{"role": "user", "content": case["task"]}]
                elif case.get("reference"):
                    # Keep the earlier terminal's observation in Tool history, separate
                    # from the new user request, as in an actual ongoing conversation.
                    read_arguments = {"action": "status", "terminal_id": terminal_id}
                    observed = await registry.dispatch(context, read_arguments, ["terminal"])
                    messages = [
                        {"role": "system", "content": skill},
                        {"role": "user", "content": "Show the terminal for my earlier task."},
                        {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "previous_terminal",
                                    "name": "terminal",
                                    "arguments": read_arguments,
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "previous_terminal",
                            "name": "terminal",
                            "content": json.dumps(observed),
                        },
                        {
                            "role": "user",
                            "content": case["task"]
                            .replace("{root}", root.as_posix())
                            .replace("{terminal_id}", terminal_id),
                        },
                    ]
            else:
                messages = [
                    {
                        "role": "system",
                        "content": (
                            "Exercise exactly one Tool Call using the supplied test arguments "
                            "verbatim, including intentional invalid fields. Do not repair them "
                            "or add optional fields."
                        ),
                    },
                    {"role": "user", "content": json.dumps(expected)},
                ]
            async with asyncio.timeout(args.total_timeout):
                raw = await adapter.send(
                    messages,
                    model_id=args.model,
                    tools=definitions,
                    thinking_effort=args.thinking_effort,
                    max_tokens=args.max_tokens or 2500,
                )
            response = adapter.normalize_response(raw, model_id=args.model)
            calls = response.get("tool_calls") or []
            valid = len(calls) == 1 and calls[0].get("name") == "terminal"
            actual = calls[0].get("arguments", {}) if valid else {}
            # A guard is also a valid conservative choice for natural plain input.
            compared = dict(actual)
            if (
                case["id"] in {"natural_submit", "natural_continue"}
                and compared.get("expected_screen_revision") == revision
            ):
                compared.pop("expected_screen_revision")
            if case["id"] == "natural_start":
                compared.pop("name", None)
                if compared.get("args") == []:
                    compared.pop("args")
            valid = valid and compared == expected
            writes_before = list(factory.adapters[0].writes)
            launches_before = len(factory.calls)
            result = None
            effect_ok = False
            if valid:
                try:
                    result = await registry.dispatch(context, actual, ["terminal"])
                except ToolContractError:
                    result = {"ok": False, "error": {"code": "invalid_arguments"}}
                data = result.get("data") or {}
                if case.get("error"):
                    effect_ok = (
                        factory.adapters[0].writes == writes_before
                        and len(factory.calls) == launches_before
                    )
                elif actual["action"] == "input":
                    chunks = _input_chunks(
                        data=actual.get("data"), text=actual.get("text"), key=actual.get("key")
                    )
                    effect_ok = factory.adapters[0].writes == writes_before + list(
                        chunks
                    ) and data.get("characters_sent") == sum(map(len, chunks))
                elif actual["action"] == "start":
                    child = manager.get_session(data["terminal_id"], owner())
                    if actual.get("text"):
                        child.renderer.feed("ready> ")
                        await eventually(
                            lambda: (
                                bool(factory.adapters[-1].writes)
                                and factory.adapters[-1].writes[-1] == "\r"
                            )
                        )
                    effect_ok = (
                        len(factory.calls) == launches_before + 1
                        and child.attachment == owner()
                        and child.cwd == root
                    )
                    if "command" in actual:
                        effect_ok = effect_ok and factory.calls[-1][0] == [
                            actual["command"],
                            *actual.get("args", []),
                        ]
                    effect_ok = effect_ok and child.name == actual.get("name")
                    if actual.get("text"):
                        effect_ok = effect_ok and factory.adapters[-1].writes == [
                            actual["text"],
                            "\r",
                        ]
                    if actual.get("group"):
                        effect_ok = effect_ok and child.group_id is not None
                elif actual["action"] == "status":
                    effect_ok = ("screen" in data) == ("start_line" not in actual)
                    if "start_line" in actual:
                        effect_ok = effect_ok and data["scrollback"]["line_count"] <= actual.get(
                            "lines", 30
                        )
                elif actual["action"] == "wait":
                    effect_ok = (
                        data["timed_out"] == (case["id"] == "wait_timeout")
                        and session.adapter.is_alive()
                    )
                elif actual["action"] == "resize":
                    effect_ok = factory.adapters[0].resizes[-1] == (
                        actual["rows"],
                        actual["columns"],
                    )
                elif actual["action"] == "attach":
                    effect_ok = session.attachment == owner()
                elif actual["action"] == "detach":
                    effect_ok = session.attachment is None and session.adapter.is_alive()
                elif actual["action"] == "kill":
                    effect_ok = not session.adapter.is_alive() and data["state"] == "exited"
                else:
                    effect_ok = data["terminals"][0]["terminal_id"] == terminal_id
            code = ((result or {}).get("error") or {}).get("code")
            rendered = render_tool_definitions(definitions, profile="explicit_non_strict")
            passed = (
                valid
                and result is not None
                and bool(result["ok"]) == (case.get("error") is None)
                and code == case.get("error")
                and effect_ok
            )
            return {
                "case": case["id"],
                "passed": passed,
                "model_call_valid": valid,
                "effect_ok": effect_ok,
                "error_code": code,
                "calls": len(calls),
                "definition_tokens": estimate_json_tokens(definitions[0])[0],
                "non_strict": all(t.get("strict") is False for t in rendered),
            }
        finally:
            await manager.aclose()


async def _probe_terminal(adapter: Any, args: argparse.Namespace) -> dict[str, Any]:
    from core.tools import terminal

    expected_source = Path(__file__).resolve().parents[1] / "core/tools/terminal.py"
    if Path(terminal.__file__).resolve() != expected_source:
        raise RuntimeError("Terminal probe imported a different checkout")
    cases = [
        case
        for case in _terminal_cases()
        if args.terminal_case == "all" or case["id"] == args.terminal_case
    ]
    if not cases:
        raise ValueError("Unknown terminal case")
    limit = asyncio.Semaphore(4)

    async def evaluate(case):
        async with limit:
            return await _probe_terminal_case(adapter, args, case)

    rows = await asyncio.gather(*(evaluate(case) for case in cases))
    return {
        "scenario": "terminal",
        "model": args.model,
        "cases": rows,
        "passed": all(row["passed"] for row in rows),
    }


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
