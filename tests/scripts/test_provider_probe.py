"""Smoke test of the Provider Tool-contract probe CLI (``scripts/probe_provider_tool_call.py``).

The later tests keep single probe cases that reach production behavior no owner test covers yet.
"""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from types import SimpleNamespace
from typing import Any

import pytest

from tests.core.providers.adapter_test_support import AdapterHookDefaults
from tests.scripts.provider_probe_helpers import PROBE


class ScriptedAdapter(AdapterHookDefaults):
    """Answers each request with the next scripted Model turn."""

    def __init__(self, *turns: dict[str, Any]) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        self.requests.append({"messages": list(messages), **kwargs})
        return self.turns.pop(0)

    def normalize_response(self, raw: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return raw

    async def aclose(self) -> None:
        self.closed = True


def _call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"content": None, "tool_calls": [{"id": "c1", "name": name, "arguments": arguments}]}


@pytest.mark.parametrize(
    ("argv", "turns", "outcome"),
    [
        pytest.param(
            ["--mode", "nonstream"],
            [_call("inspect_probe", {"key": "alpha"})],
            ("schema_valid", True),
            id="structural-scenario",
        ),
        pytest.param(
            ["--scenario", "live_tools", "--live-case", "status", "--repetitions", "1"],
            [_call("overview", {}), {"content": "Ok.", "tool_calls": []}],
            ("ideal", 1),
            id="workflow-scenario",
        ),
    ],
)
def test_probe_cli_runs_a_scenario_against_the_configured_adapter(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    turns: list[dict[str, Any]],
    outcome: tuple[str, object],
) -> None:
    adapter = ScriptedAdapter(*turns)

    class Runtime:
        def __init__(self, _config: Any) -> None:
            self.models = SimpleNamespace(
                get=lambda _provider, _model: SimpleNamespace(recommended_temperature=None)
            )

        def get_adapter(self, _ref: Any) -> ScriptedAdapter:
            return adapter

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(PROBE, "Runtime", Runtime)
    monkeypatch.setattr(PROBE, "Config", lambda **_: None)
    monkeypatch.setattr(PROBE, "_start_probe_runtime", lambda _runtime: None)

    code = asyncio.run(PROBE._run(PROBE._parser().parse_args(argv)))

    key, value = outcome
    assert (code, json.loads(capsys.readouterr().out)[key]) == (0, value)
    assert adapter.closed is True
    assert adapter.turns == []


# Production behavior below is reached only through these probe runs: no owner test covers it
# yet. Each test names the production lines it holds; once the owner's tests (tests/core/tools,
# tests/core/providers) cover them, delete the test here.


class EchoAdapter:
    """Calls the probe's Tool with the arguments its instruction message carries."""

    def __init__(self, tool: str, arguments: Any = None) -> None:
        self.tool = tool
        self.arguments = arguments

    async def send(self, messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
        arguments = self.arguments
        if arguments is None:
            arguments = json.loads(messages[-1]["content"].removeprefix("Arguments: "))
        return {"tool_calls": [{"id": "fixture", "name": self.tool, "arguments": arguments}]}

    def normalize_response(self, raw: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return raw

    def request_context_kwargs(self, **_kwargs: Any) -> dict[str, Any]:
        return {}


@pytest.mark.parametrize(
    "case_id", ["start_group", "invalid_args", "invalid_fields", "wait_revision", "input_text"]
)
def test_terminal_probe_case_reaches_the_production_terminal_tool(case_id: str) -> None:
    # Holds core/tools/terminal.py and _terminal_arguments.py: `args` without `command`,
    # named groups, input fields on a call without a terminal, explicit `after_revision`
    # waits and attention acknowledgment after input.
    from scripts.provider_probe.workflow_terminal import _probe_terminal_case, _terminal_cases

    case = next(case for case in _terminal_cases() if case["id"] == case_id)
    args = PROBE._parser().parse_args(["--scenario", "terminal"])

    row = asyncio.run(_probe_terminal_case(EchoAdapter("terminal"), args, case))

    assert row["passed"], row


@pytest.mark.parametrize(
    "case_id", ["unrestricted", "ignore_controls", "early_stop", "diagnostics"]
)
def test_search_probe_case_reaches_the_production_search_tool(case_id: str) -> None:
    # Holds core/tools/search_files.py, _search_options.py and _search_ignores.py: `-uuu`,
    # disabled parent/dot/file ignore sources, early-stop notes, `--stats` and `--debug`.
    from scripts.provider_probe.workflow_search_files import _case, search_cases

    case = next(case for case in search_cases() if case["id"] == case_id)
    namespace = Namespace(total_timeout=30, model="fixture", thinking_effort="low", max_tokens=1500)

    result = asyncio.run(_case(EchoAdapter("search_files", case["arguments"]), namespace, case))

    assert result["passed"], result


def test_bash_probe_reports_the_violated_env_keys_keyword() -> None:
    # Holds the uniqueItems message in core/tools/contracts.py.
    from core.tools import bash
    from scripts.provider_probe.measurements import (
        _compile_probe_contracts,
        _validation_measurements,
    )
    from scripts.provider_probe.scenario_agents import _bash_scenario

    scenario = _bash_scenario("top_foreground_default")
    contracts = _compile_probe_contracts(
        scenario.tools, require_closed_input=scenario.require_closed_input
    )
    keywords = [
        _validation_measurements(
            [
                {
                    "name": bash.BASH_TOOL_NAME,
                    "arguments": {"mode": "foreground", "command": "ls", "env_keys": keys},
                }
            ],
            contracts,
        )["validation_keyword"]
        for keys in ([], [""], ["OPENAI_API_KEY", "OPENAI_API_KEY"])
    ]

    assert keywords == ["minItems", "minLength", "uniqueItems"]


def test_swarm_board_bounds_probe_passes_through_registered_handlers() -> None:
    # Holds the maxLength message for strings in core/tools/contracts.py.
    from scripts.provider_probe.workflow_swarm import _probe_swarm_tool

    class Adapter(EchoAdapter):
        async def send(self, messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
            tool = messages[-1]["content"].split()[1]
            arguments = json.loads(messages[-1]["content"].split(": ", 1)[1])
            return {"tool_calls": [{"id": "fixture", "name": tool, "arguments": arguments}]}

    args = PROBE._parser().parse_args(
        ["--scenario", "swarm_tool", "--profile", "explicit_non_strict"]
        + ["--swarm-tool", "swarm_board", "--swarm-case", "bounds"]
    )

    result = asyncio.run(_probe_swarm_tool(Adapter("swarm_board"), args))

    assert result["passed"], result


def test_swarm_probe_treats_an_unrecognized_terminal_outcome_as_unfinished() -> None:
    # Holds the fail-closed `unknown` outcome of core/providers/adapter.py
    # terminal_outcome_from_response for an unrecognized value.
    from scripts.provider_probe.workflow_swarm import _probe_swarm_tool

    class Adapter(EchoAdapter):
        async def send(self, messages: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
            return {"content": "", "terminal_outcome": ""}

    args = PROBE._parser().parse_args(
        ["--scenario", "swarm_tool", "--swarm-case", "communication_ack"]
    )

    result = asyncio.run(_probe_swarm_tool(Adapter("swarm_board"), args))

    assert (result["passed"], result["finished"]) == (False, False)
