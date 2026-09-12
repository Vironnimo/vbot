"""Provider diagnostic probe context coverage."""

from __future__ import annotations

import json
from argparse import Namespace

import core.tools.bash as tool_bash
import core.tools.calendar as tool_calendar
import core.tools.channel as tool_channel
import core.tools.cron as tool_cron
import core.tools.history as tool_history
import core.tools.memory as tool_memory
import core.tools.process as tool_process
import core.tools.project as tool_project
import core.tools.session_search as tool_session_search
import core.tools.skill as tool_skill
import core.tools.skill_manage as tool_skill_manage
import core.tools.status as tool_status
import core.tools.subagent as tool_subagent
import scripts.provider_probe.choices as probe_choices
import scripts.provider_probe.measurements as probe_measurements
import scripts.provider_probe.scenario_agents as probe_scenario_agents
import scripts.provider_probe.scenario_automation as probe_scenario_automation
import scripts.provider_probe.scenario_history as probe_scenario_history
import scripts.provider_probe.scenarios as probe_scenarios
from tests.scripts.provider_probe_helpers import expected_arguments


def test_process_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in probe_choices.PROCESS_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="process",
                process_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_process.PROCESS_TOOL_PARAMETERS
        assert arguments is not None
        assert set(arguments) <= {"action", "process_id", "filter", "limit", "before"}
        assert arguments["action"] in {"status", "kill"}
        contracts[tool_process.PROCESS_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_process.PROCESS_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )


def test_process_argument_measurements_report_only_structural_differences() -> None:
    scenario = probe_scenario_agents._process_scenario("status_one")
    marker = "DO_NOT_PRINT_THIS_VALUE"
    result = probe_measurements._expected_argument_measurements(
        [
            {
                "name": tool_process.PROCESS_TOOL_NAME,
                "arguments": {
                    "action": "status",
                    "process_id": marker,
                    "eof": False,
                },
            }
        ],
        scenario,
    )

    assert result == {
        "expected_arguments_match": False,
        "expected_call_count": 1,
        "actual_call_count": 1,
        "missing_expected_fields": [],
        "unexpected_fields": ["eof"],
        "mismatched_fields": ["process_id"],
    }
    assert marker not in json.dumps(result)


def test_skill_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in probe_choices.SKILL_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="skill",
                skill_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_skill.SKILL_TOOL_PARAMETERS
        assert arguments is not None
        contracts[tool_skill.SKILL_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_skill.SKILL_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    activate = probe_scenario_agents._skill_scenario("activate")
    assert activate.expected_arguments == {"name": "vbot-cli"}
    catalog = probe_scenario_agents._skill_scenario("list")
    assert catalog.expected_arguments == {}


def test_skill_manage_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.SKILL_MANAGE_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="skill_manage",
                skill_manage_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_skill_manage.SKILL_MANAGE_TOOL_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in json.dumps(scenario.tools[0]["parameters"])
        assert "oneOf" not in scenario.tools[0]["parameters"]
        assert '"default"' not in json.dumps(scenario.tools[0]["parameters"])
        contracts[tool_skill_manage.SKILL_MANAGE_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [
                    {
                        "name": tool_skill_manage.SKILL_MANAGE_TOOL_NAME,
                        "arguments": arguments,
                    }
                ],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    create = probe_scenario_agents._skill_manage_scenario("create_own")
    patch = probe_scenario_agents._skill_manage_scenario("patch_default")
    assert "scope" not in expected_arguments(create)
    assert "file_path" not in expected_arguments(patch)
    assert "replace_all" not in expected_arguments(patch)
    assert (
        expected_arguments(probe_scenario_agents._skill_manage_scenario("write_asset_empty"))[
            "content"
        ]
        == ""
    )


def test_bash_cases_use_production_profiles_and_exact_arguments() -> None:
    for case_name in probe_choices.BASH_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="bash",
                bash_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments
        parameters = scenario.tools[0]["parameters"]

        assert arguments is not None
        assert "additionalProperties" not in json.dumps(parameters)
        assert "oneOf" not in parameters
        contracts[tool_bash.BASH_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_bash.BASH_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    top = probe_scenario_agents._bash_scenario("top_auto_default")
    sub = probe_scenario_agents._bash_scenario("sub_auto_default")
    assert top.tools[0]["parameters"] is tool_bash.BASH_TOOL_PARAMETERS
    assert probe_scenario_agents._bash_scenario("top_foreground_default").expected_arguments == {
        "command": "python --version"
    }
    assert probe_scenario_agents._bash_scenario("sub_foreground_default").expected_arguments == {
        "command": "python --version"
    }
    assert top.expected_arguments == {
        "mode": "auto",
        "command": "python -m pytest tests/core/tools/test_bash.py -q",
    }
    assert probe_scenario_agents._bash_scenario("top_foreground_env_one").expected_arguments == {
        "mode": "foreground",
        "command": "python -c \"import os; print(bool(os.environ['OPENAI_API_KEY']))\"",
        "env_keys": ["OPENAI_API_KEY"],
    }
    assert probe_scenario_agents._bash_scenario("top_foreground_env_many").expected_arguments == {
        "mode": "foreground",
        "command": 'python -c "import os; print(len(os.environ))"',
        "env_keys": ["OPENAI_API_KEY", "OPENROUTER_API_KEY"],
    }
    top_contracts = probe_measurements._compile_probe_contracts(
        top.tools,
        require_closed_input=top.require_closed_input,
    )
    for env_keys, keyword in (
        ([], "minItems"),
        ([""], "minLength"),
        (["OPENAI_API_KEY", "OPENAI_API_KEY"], "uniqueItems"),
    ):
        validation = probe_measurements._validation_measurements(
            [
                {
                    "name": tool_bash.BASH_TOOL_NAME,
                    "arguments": {
                        "mode": "foreground",
                        "command": "python --version",
                        "env_keys": env_keys,
                    },
                }
            ],
            top_contracts,
        )
        assert validation["schema_valid"] is False
        assert validation["validation_path"].startswith("/env_keys")
        assert validation["validation_keyword"] == keyword
    assert top.tools[0]["parameters"]["properties"]["background_after_seconds"]["default"] == 30
    assert sub.tools[0]["parameters"]["properties"]["mode"]["enum"] == [
        "foreground",
        "auto",
    ]
    assert sub.tools[0]["parameters"]["properties"]["background_after_seconds"]["default"] == 1800


def test_channel_send_cases_use_production_profiles_and_exact_arguments() -> None:
    for case_name in probe_choices.CHANNEL_SEND_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="channel_send",
                channel_send_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert arguments is not None
        assert "additionalProperties" not in json.dumps(scenario.tools[0]["parameters"])
        assert "oneOf" not in scenario.tools[0]["parameters"]
        contracts[tool_channel.CHANNEL_SEND_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_channel.CHANNEL_SEND_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    telegram = probe_scenario_agents._channel_send_scenario("telegram_message")
    discord = probe_scenario_agents._channel_send_scenario("discord_message")
    mixed = probe_scenario_agents._channel_send_scenario("mixed_telegram_button")
    assert "buttons" in telegram.tools[0]["parameters"]["properties"]
    assert "buttons" not in discord.tools[0]["parameters"]["properties"]
    assert mixed.tools[0]["parameters"]["properties"]["channel_id"]["enum"] == [
        "discord-probe",
        "telegram-probe",
    ]


def test_calendar_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.CALENDAR_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="calendar",
                calendar_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_calendar.CALENDAR_TOOL_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in json.dumps(scenario.tools[0]["parameters"])
        assert "oneOf" not in scenario.tools[0]["parameters"]
        contracts[tool_calendar.CALENDAR_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_calendar.CALENDAR_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert probe_scenario_automation._calendar_scenario("list_default").expected_arguments == {
        "action": "list"
    }
    assert probe_scenario_automation._calendar_scenario("create_timed").expected_arguments == {
        "action": "create",
        "title": "Dentist",
        "start": "2026-09-10T15:00",
    }
    assert probe_scenario_automation._calendar_scenario(
        "update_stop_repeat"
    ).expected_arguments == {
        "action": "update",
        "id": "event-123",
        "rrule": None,
    }


def test_cron_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.CRON_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="cron",
                cron_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_cron.CRON_TOOL_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in json.dumps(scenario.tools[0]["parameters"])
        assert "oneOf" not in scenario.tools[0]["parameters"]
        contracts[tool_cron.CRON_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_cron.CRON_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert probe_scenario_automation._cron_scenario("list").expected_arguments == {"action": "list"}
    assert probe_scenario_automation._cron_scenario("create_cron").expected_arguments == {
        "action": "create",
        "prompt": "Prepare the daily operations summary.",
        "schedule": "0 9 * * *",
    }
    assert (
        expected_arguments(probe_scenario_automation._cron_scenario("update_repeat_null"))["repeat"]
        is None
    )


def test_history_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.HISTORY_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="history",
                history_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_history.HISTORY_TOOL_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in json.dumps(scenario.tools[0]["parameters"])
        assert "oneOf" not in scenario.tools[0]["parameters"]
        contracts[tool_history.HISTORY_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_history.HISTORY_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert probe_scenario_history._history_scenario("overview_default").expected_arguments == {
        "action": "overview"
    }
    assert probe_scenario_history._history_scenario("search_default").expected_arguments == {
        "action": "search",
        "query": "deployment failure",
    }
    assert probe_scenario_history._history_scenario("read_default").expected_arguments == {
        "action": "read"
    }
    assert probe_scenario_history._history_scenario("around_default").expected_arguments == {
        "action": "around",
        "message_id": "message-123",
    }
    for action in ("overview", "search", "read", "around"):
        assert probe_scenario_history._history_scenario(f"{action}_cursor").expected_arguments == {
            "action": action,
            "cursor": "opaque-history-cursor",
        }


def test_memory_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.MEMORY_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="memory",
                memory_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_memory.MEMORY_TOOL_PARAMETERS
        assert arguments is not None
        contracts[tool_memory.MEMORY_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_memory.MEMORY_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )


def test_project_scenario_uses_production_schema_and_exact_arguments() -> None:
    scenario = probe_scenarios._scenario(Namespace(scenario="project", lines=8))
    contracts = probe_measurements._compile_probe_contracts(
        scenario.tools,
        require_closed_input=scenario.require_closed_input,
    )
    arguments = scenario.expected_arguments

    assert scenario.tools[0]["parameters"] is tool_project.PROJECT_TOOL_PARAMETERS
    assert arguments == {"project_id": "vbot"}
    contracts[tool_project.PROJECT_TOOL_NAME].validate_arguments(arguments)
    assert (
        probe_measurements._expected_argument_measurements(
            [{"name": tool_project.PROJECT_TOOL_NAME, "arguments": arguments}],
            scenario,
        )["expected_arguments_match"]
        is True
    )


def test_session_read_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.SESSION_READ_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="session_read",
                session_read_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_session_search.SESSION_READ_TOOL_PARAMETERS
        assert arguments is not None
        contracts[tool_session_search.SESSION_READ_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [
                    {
                        "name": tool_session_search.SESSION_READ_TOOL_NAME,
                        "arguments": arguments,
                    }
                ],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    whole = probe_scenario_history._session_read_scenario("whole")
    assert whole.expected_arguments == {"session_id": "session-123"}


def test_session_search_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.SESSION_SEARCH_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="session_search",
                session_search_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_session_search.SESSION_SEARCH_TOOL_PARAMETERS
        assert arguments is not None
        contracts[tool_session_search.SESSION_SEARCH_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [
                    {
                        "name": tool_session_search.SESSION_SEARCH_TOOL_NAME,
                        "arguments": arguments,
                    }
                ],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    listing = probe_scenario_history._session_search_scenario("list")
    assert listing.expected_arguments == {}


def test_status_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.STATUS_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="status",
                status_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_status.STATUS_TOOL_PARAMETERS
        assert arguments is not None
        contracts[tool_status.STATUS_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_status.STATUS_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    current = probe_scenario_history._status_scenario("current")
    assert current.expected_arguments == {}


def test_subagent_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.SUBAGENT_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="subagent",
                subagent_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_subagent.SUBAGENT_TOOL_PARAMETERS
        assert arguments is not None
        assert "additionalProperties" not in json.dumps(scenario.tools[0]["parameters"])
        assert "oneOf" not in scenario.tools[0]["parameters"]
        contracts[tool_subagent.SUBAGENT_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_subagent.SUBAGENT_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    assert probe_scenario_agents._subagent_scenario("run_self").expected_arguments == {
        "action": "run",
        "content": "Inspect the Tool contract and report concise findings.",
    }
    assert (
        expected_arguments(probe_scenario_agents._subagent_scenario("thinking_minimal"))[
            "thinking_effort"
        ]
        == "minimal"
    )
