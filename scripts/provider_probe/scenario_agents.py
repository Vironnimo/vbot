"""Provider Tool probe: scenario agents."""

from __future__ import annotations

import json
from typing import Any

from core.channels import ChannelConfig
from core.tools.bash import (
    BASH_TOOL_DESCRIPTION,
    BASH_TOOL_NAME,
    BASH_TOOL_PARAMETERS,
    project_bash_tool_definitions,
)
from core.tools.channel import CHANNEL_SEND_TOOL_NAME, _channel_send_definition_profile
from core.tools.process import PROCESS_TOOL_DESCRIPTION, PROCESS_TOOL_NAME, PROCESS_TOOL_PARAMETERS
from core.tools.project import PROJECT_TOOL_DESCRIPTION, PROJECT_TOOL_NAME, PROJECT_TOOL_PARAMETERS
from core.tools.skill import SKILL_TOOL_DESCRIPTION, SKILL_TOOL_NAME, SKILL_TOOL_PARAMETERS
from core.tools.skill_manage import (
    SKILL_MANAGE_TOOL_DESCRIPTION,
    SKILL_MANAGE_TOOL_NAME,
    SKILL_MANAGE_TOOL_PARAMETERS,
)
from core.tools.subagent import (
    SUBAGENT_TOOL_DESCRIPTION,
    SUBAGENT_TOOL_NAME,
    SUBAGENT_TOOL_PARAMETERS,
)
from scripts.provider_probe.common import ProbeScenario, _probe_messages


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
