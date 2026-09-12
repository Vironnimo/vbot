"""Provider diagnostic probe files media coverage."""

from __future__ import annotations

from argparse import Namespace

import core.tools.edit as tool_edit
import core.tools.glob as tool_glob
import core.tools.grep as tool_grep
import core.tools.image as tool_image
import core.tools.read as tool_read
import core.tools.speech as tool_speech
import core.tools.web_fetch as tool_web_fetch
import core.tools.web_search as tool_web_search
import core.tools.write as tool_write
import scripts.provider_probe.choices as probe_choices
import scripts.provider_probe.measurements as probe_measurements
import scripts.provider_probe.scenario_files as probe_scenario_files
import scripts.provider_probe.scenario_web as probe_scenario_web
import scripts.provider_probe.scenarios as probe_scenarios
from tests.scripts.provider_probe_helpers import expected_arguments


def test_web_fetch_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in probe_choices.WEB_FETCH_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="web_fetch",
                web_fetch_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_web_fetch.WEB_FETCH_TOOL_PARAMETERS
        assert arguments is not None
        contracts[tool_web_fetch.WEB_FETCH_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_web_fetch.WEB_FETCH_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    default = probe_scenario_web._web_fetch_scenario("default")
    assert default.expected_arguments == {"url": "https://example.com/provider-tool-probe"}


def test_write_scenario_uses_production_schema_and_exact_arguments() -> None:
    scenario = probe_scenarios._scenario(Namespace(scenario="write", lines=8))
    contracts = probe_measurements._compile_probe_contracts(
        scenario.tools,
        require_closed_input=scenario.require_closed_input,
    )
    arguments = scenario.expected_arguments

    assert scenario.tools[0]["parameters"] is tool_write.WRITE_TOOL_PARAMETERS
    assert arguments == {
        "path": "notes/provider-tool-probe.txt",
        "content": "first line\nsecond line\n",
    }
    contracts[tool_write.WRITE_TOOL_NAME].validate_arguments(arguments)
    assert (
        probe_measurements._expected_argument_measurements(
            [{"name": tool_write.WRITE_TOOL_NAME, "arguments": arguments}],
            scenario,
        )["expected_arguments_match"]
        is True
    )


def test_edit_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in probe_choices.EDIT_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="edit",
                edit_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_edit.EDIT_TOOL_PARAMETERS
        assert arguments is not None
        contracts[tool_edit.EDIT_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_edit.EDIT_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    default = probe_scenario_files._edit_scenario("default")
    assert default.expected_arguments == {
        "edits": [
            {
                "path": "src/provider_tool_probe.py",
                "old_string": "value = 1",
                "new_string": "value = 2",
            }
        ]
    }
    assert len(expected_arguments(probe_scenario_files._edit_scenario("multi_file"))["edits"]) == 2
    same_file_edits = expected_arguments(probe_scenario_files._edit_scenario("same_file_sequence"))[
        "edits"
    ]
    assert [edit["path"] for edit in same_file_edits] == [
        "src/provider_tool_probe.py",
        "src/provider_tool_probe.py",
    ]
    assert same_file_edits[1]["old_string"] == same_file_edits[0]["new_string"]


def test_analyze_image_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.ANALYZE_IMAGE_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="analyze_image",
                analyze_image_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_image.ANALYZE_IMAGE_TOOL_PARAMETERS
        assert arguments is not None
        contracts[tool_image.ANALYZE_IMAGE_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_image.ANALYZE_IMAGE_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )


def test_image_generation_cases_use_production_profiles_and_exact_arguments() -> None:
    for case_name in probe_choices.IMAGE_GENERATION_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="image_generation",
                image_generation_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments
        expected_schema = (
            tool_image.IMAGE_GENERATION_TEXT_ONLY_TOOL_PARAMETERS
            if case_name.startswith("text_")
            else tool_image.IMAGE_GENERATION_TOOL_PARAMETERS
        )

        assert scenario.tools[0]["parameters"] is expected_schema
        assert arguments is not None
        contracts[tool_image.IMAGE_GENERATION_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [
                    {
                        "name": tool_image.IMAGE_GENERATION_TOOL_NAME,
                        "arguments": arguments,
                    }
                ],
                scenario,
            )["expected_arguments_match"]
            is True
        )


def test_read_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in probe_choices.READ_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="read",
                read_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_read.READ_TOOL_PARAMETERS
        assert arguments is not None
        contracts[tool_read.READ_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_read.READ_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    path_only = probe_scenario_files._read_scenario("path_only")
    assert path_only.expected_arguments == {"path": "src/provider_tool_probe.py"}


def test_glob_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in probe_choices.GLOB_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="glob",
                glob_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_glob.GLOB_TOOL_PARAMETERS
        assert arguments is not None
        contracts[tool_glob.GLOB_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_glob.GLOB_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    default = probe_scenario_files._glob_scenario("default")
    assert default.expected_arguments == {"pattern": "**/*.py"}


def test_grep_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in probe_choices.GREP_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="grep",
                grep_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_grep.GREP_TOOL_PARAMETERS
        assert arguments is not None
        contracts[tool_grep.GREP_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [{"name": tool_grep.GREP_TOOL_NAME, "arguments": arguments}],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    default = probe_scenario_files._grep_scenario("default")
    assert default.expected_arguments == {"pattern": "TODO|FIXME"}


def test_web_search_cases_use_production_schema_and_exact_expected_arguments() -> None:
    for case_name in probe_choices.WEB_SEARCH_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="web_search",
                web_search_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_web_search.WEB_SEARCH_TOOL_PARAMETERS
        assert arguments is not None
        contracts[tool_web_search.WEB_SEARCH_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [
                    {
                        "name": tool_web_search.WEB_SEARCH_TOOL_NAME,
                        "arguments": arguments,
                    }
                ],
                scenario,
            )["expected_arguments_match"]
            is True
        )

    default = probe_scenario_web._web_search_scenario("default")
    assert default.expected_arguments == {"query": "vBot Tool schemas"}


def test_text_to_speech_cases_use_production_schema_and_exact_arguments() -> None:
    for case_name in probe_choices.TEXT_TO_SPEECH_CASES:
        scenario = probe_scenarios._scenario(
            Namespace(
                scenario="text_to_speech",
                speech_case=case_name,
                lines=8,
            ),
        )
        contracts = probe_measurements._compile_probe_contracts(
            scenario.tools,
            require_closed_input=scenario.require_closed_input,
        )
        arguments = scenario.expected_arguments

        assert scenario.tools[0]["parameters"] is tool_speech.TEXT_TO_SPEECH_TOOL_PARAMETERS
        assert arguments is not None
        contracts[tool_speech.TEXT_TO_SPEECH_TOOL_NAME].validate_arguments(arguments)
        assert (
            probe_measurements._expected_argument_measurements(
                [
                    {
                        "name": tool_speech.TEXT_TO_SPEECH_TOOL_NAME,
                        "arguments": arguments,
                    }
                ],
                scenario,
            )["expected_arguments_match"]
            is True
        )
