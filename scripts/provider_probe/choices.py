"""CLI choices for synthetic and built-in Tool-contract probe scenarios."""

from __future__ import annotations

from typing import Any

DEFAULT_PROVIDER = "ollama-cloud"


DEFAULT_MODEL = "deepseek-v4.1-flash"


DEFAULT_LINES = 8


DEFAULT_IDLE_TIMEOUT_SECONDS = 180.0


DEFAULT_TOTAL_TIMEOUT_SECONDS = 900.0


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
    "tool_first_use",
    "search_files",
    "tool_tolerance",
    "recall_workflow",
    "live_tools",
    "terminal",
    "apply_patch",
    "reflection_workflow",
    "swarm_tool",
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
    "generate_image",
    "generate_speech",
    "ha_call_service",
    "ha_get_state",
    "ha_list_entities",
    "ha_list_services",
    "memory",
    "project",
    "read",
    "session_search",
    "skill",
    "skill_manage",
    "status",
    "subagent",
    "web_fetch",
    "web_search",
    "word_count",
)


OPTIONAL_BOOLEAN_CASES = ("omit", "include_links", "raw", "both")


ANALYZE_IMAGE_CASES = ("single", "multiple")


BASH_CASES = (
    "top_foreground_default",
    "top_foreground",
    "top_foreground_description",
    "top_foreground_workdir",
    "top_foreground_timeout",
    "top_foreground_unbounded",
    "top_foreground_long_timeout",
    "top_foreground_env_one",
    "top_foreground_env_many",
    "top_foreground_all_multiline",
    "top_background",
    "top_background_workdir",
    "top_background_timeout",
    "top_background_unbounded",
    "top_background_all",
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


HA_LIST_ENTITIES_CASES = ("default", "domain", "area", "all")


HA_GET_STATE_CASES = ("light", "sensor")


HA_LIST_SERVICES_CASES = ("default", "domain")


HA_CALL_SERVICE_CASES = ("base", "entity", "empty_data", "data", "all")


READ_CASES = (
    "path_only",
    "offset_line",
    "limit_only",
    "offset_line_limit",
)


SESSION_SEARCH_CASES = (
    "subagents",
    "exclude_subagents",
    "open_period",
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
    "send",
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
    "list_all",
    "list",
    "cancel",
)


TEXT_TO_SPEECH_CASES = ("plain", "unicode_multiline")


WEB_FETCH_CASES = ("default", "find")


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
    "recency_week",
    "recency_month",
    "recency_year",
    "all",
)


WORD_COUNT_CASES = ("plain", "empty", "unicode_multiline")
