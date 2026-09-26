"""The acceptance harness must detect failures instead of teaching a passing call."""

import sys
from argparse import Namespace
from pathlib import Path

import pytest

from scripts.provider_probe.first_use_cases import first_use_cases
from scripts.provider_probe.first_use_fixture import FirstUseFixture, FixtureBoundaryError
from scripts.provider_probe.workflow_first_use import first_use_trial


class Responses:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def request_context_kwargs(self, **kwargs):
        return {}

    async def send(self, messages, **kwargs):
        self.requests.append((list(messages), kwargs))
        return next(self.responses)

    def normalize_response(self, raw, **kwargs):
        return raw


def call(name, arguments):
    return {
        "content": "",
        "tool_calls": [{"id": "test-call", "name": name, "arguments": arguments}],
    }


def case(name):
    return next(c for c in first_use_cases() if c["id"] == name)


def settings():
    return Namespace(
        total_timeout=30, model="fixture/model", thinking_effort="low", max_tokens=6000
    )


@pytest.mark.asyncio
async def test_black_box_catalog_keeps_competitors_and_does_not_supply_the_answer():
    adapter = Responses(
        call("search_files", {"args": ["-F", "add_recipe(", "src"]}),
        {"content": "src/recipes.py:2:def add_recipe(title):"},
    )
    result = await first_use_trial(adapter, settings(), case("search_symbols"), 1)
    assert result["first_attempt_success"], result
    initial, kwargs = adapter.requests[0]
    assert initial[-1] == {"role": "user", "content": case("search_symbols")["task"]}
    assert {tool["name"] for tool in kwargs["tools"]} >= {
        "search_files",
        "bash",
        "read",
        "apply_patch",
        "subagent",
    }
    assert "tool_choice" not in kwargs
    assert {tool["name"] for tool in result["model_definitions"]} >= {
        "powershell" if sys.platform == "win32" else "bash",
        "apply_patch",
    }
    assert result["responses"][-1]["content"] == "src/recipes.py:2:def add_recipe(title):"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply", [{"content": "No matches."}, call("search_files", {"args": ["other", "src"]})]
)
async def test_no_call_or_wrong_effect_is_not_success(reply):
    adapter = Responses(reply, {"content": "No matches."})
    result = await first_use_trial(adapter, settings(), case("search_symbols"), 1)
    assert not result["passed"]
    assert result["responses"][0] == reply


@pytest.mark.asyncio
async def test_correct_result_with_false_final_claim_fails():
    adapter = Responses(
        call("search_files", {"args": ["-F", "add_recipe(", "src"]}),
        {"content": "There are no matching lines."},
    )
    result = await first_use_trial(adapter, settings(), case("search_symbols"), 1)
    assert not result["passed"]
    assert not result["final_facts_verified"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["output_truncated", "error", "unknown"])
async def test_incomplete_final_is_not_task_success_even_when_it_contains_the_fact(outcome):
    adapter = Responses(
        call("read", {"path": "README.md"}), {"content": "ORCHID-73", "terminal_outcome": outcome}
    )
    result = await first_use_trial(adapter, settings(), case("read_control"), 1)
    assert not result["passed"]
    assert not result["finished"]


@pytest.mark.asyncio
async def test_reading_known_file_is_valid_matching_line_evidence():
    adapter = Responses(
        call("read", {"path": "src/recipes.py"}),
        {"content": "src/recipes.py:2:def add_recipe(title):"},
    )
    result = await first_use_trial(adapter, settings(), case("search_symbols"), 1)
    assert result["first_attempt_success"], result


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [["unrelated", "src"], ["retired_handler", "tests"]])
async def test_absence_claim_requires_correct_pattern_and_scope(args):
    adapter = Responses(
        call("search_files", {"args": args}),
        {"content": "No references to retired_handler under src."},
    )
    result = await first_use_trial(adapter, settings(), case("search_absent"), 1)
    assert not result["passed"]


@pytest.mark.asyncio
async def test_shell_fallback_is_recorded_and_not_replaced_or_reprompted():
    adapter = Responses(call("bash", {"command": "rg add_recipe src"}))
    result = await first_use_trial(adapter, settings(), case("search_symbols"), 1)
    assert not result["passed"]
    assert result["boundary_failure"]
    assert len(adapter.requests) == 1
    assert result["calls"][0]["arguments"] == {"command": "rg add_recipe src"}


@pytest.mark.asyncio
async def test_valid_shell_read_is_measured_separately_from_success():
    command = "Get-Content README.md" if sys.platform == "win32" else "cat README.md"
    adapter = Responses(call("bash", {"command": command}), {"content": "ORCHID-73"})
    result = await first_use_trial(adapter, settings(), case("read_control"), 1)
    assert result["first_attempt_success"], result
    assert not result["preferred_tool_used"]
    assert result["shell_calls"] == 1


@pytest.mark.asyncio
async def test_valid_shell_read_can_supply_line_search_evidence():
    command = "Get-Content src/recipes.py" if sys.platform == "win32" else "cat src/recipes.py"
    adapter = Responses(
        call("bash", {"command": command}),
        {"content": "src/recipes.py:2:def add_recipe(title):"},
    )
    result = await first_use_trial(adapter, settings(), case("search_symbols"), 1)
    assert result["first_attempt_success"], result
    assert not result["preferred_tool_used"]
    assert result["shell_calls"] == 1


@pytest.mark.asyncio
async def test_edit_grade_checks_exact_state_including_unduplicated_sections():
    task = case("edit_control")
    expected = task["expected_files"]["notes.md"]
    adapter = Responses(
        call("read", {"path": "notes.md"}),
        call("apply_patch", {"path": "notes.md", "content": expected}),
        {"content": "Done."},
    )
    result = await first_use_trial(adapter, settings(), task, 1)
    assert result["first_attempt_success"], result
    assert result["changed_files"] == {"notes.md": expected}


@pytest.mark.asyncio
async def test_edit_claim_without_effect_or_with_duplicated_section_fails():
    task = case("edit_control")
    for replies in (
        ({"content": "Done."},),
        (
            call("read", {"path": "notes.md"}),
            call("apply_patch", {"path": "notes.md", "content": task["files"]["notes.md"] * 2}),
            {"content": "Done."},
        ),
    ):
        result = await first_use_trial(Responses(*replies), settings(), task, 1)
        assert not result["passed"], result
        assert result["state_differences"] == ["notes.md"]


@pytest.mark.asyncio
async def test_recovery_does_not_erase_first_failure():
    adapter = Responses(
        call("search_files", {"args": ["-F", "add_recipe(", "absent"]}),
        call("search_files", {"args": ["-F", "add_recipe(", "src"]}),
        {"content": "src/recipes.py:2:def add_recipe(title):"},
    )
    result = await first_use_trial(adapter, settings(), case("search_symbols"), 1)
    assert result["passed"]
    assert not result["first_attempt_success"]
    assert result["failed_calls"] == 1
    assert len(result["calls"]) == 2


@pytest.mark.asyncio
async def test_actual_delegation_reaches_correct_persisted_child(tmp_path: Path):
    fixture = FirstUseFixture(tmp_path)
    try:
        task = "Read-only review of src/a.py, reference REVIEW-A."
        result = await fixture.dispatch(
            {
                "id": "dispatch",
                "name": "subagent",
                "arguments": {"action": "run", "agent_id": "reviewer", "content": task},
            }
        )
        assert result["ok"], result
        assert fixture.received[0]["content"] == task
        assert fixture.received[0]["agent_id"] == "reviewer"
        assert fixture.received[0]["session_id"] == result["data"]["session_id"]
    finally:
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tampered", ["check.py", "pathlib.py", "sitecustomize.py"])
async def test_shell_cannot_execute_code_written_by_an_earlier_edit(tmp_path, tampered):
    fixture = FirstUseFixture(tmp_path)
    fixture.write({"check.py": "print('CHECK-42 passed')\n"})
    try:
        (fixture.repo / tampered).write_text("raise RuntimeError('must not run')\n")
        with pytest.raises(FixtureBoundaryError):
            await fixture.dispatch({"name": "bash", "arguments": {"command": "python check.py"}})
    finally:
        await fixture.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"cwd": ".."}, {"env": {"PYTHONPATH": ".."}}])
async def test_shell_boundary_checks_interpreted_directory_and_environment(tmp_path, extra):
    fixture = FirstUseFixture(tmp_path)
    fixture.write({"check.py": "print('CHECK-42 passed')\n"})
    try:
        with pytest.raises(FixtureBoundaryError):
            await fixture.dispatch(
                {"name": "bash", "arguments": {"command": "python check.py", **extra}}
            )
    finally:
        await fixture.close()


@pytest.mark.asyncio
async def test_two_valid_calls_are_judged_by_effect_not_call_count():
    tasks = [
        {
            "id": str(i),
            "name": "subagent",
            "arguments": {"action": "run", "agent_id": "reviewer", "content": text},
        }
        for i, text in enumerate(
            (
                "Read-only review src/a.py REVIEW-A with line numbers",
                "Read-only review tests/b.PY REVIEW-B with line numbers",
            )
        )
    ]
    adapter = Responses({"tool_calls": tasks}, {"content": "Both reviews dispatched."})
    result = await first_use_trial(adapter, settings(), case("delegate_parallel"), 1)
    assert result["first_attempt_success"], result
    assert len(result["receiving_runs"]) == 2


@pytest.mark.asyncio
async def test_timeout_retains_prior_response_and_call():
    class TimedOut(Responses):
        async def send(self, messages, **kwargs):
            if self.requests:
                raise TimeoutError("test timeout")
            return await super().send(messages, **kwargs)

    adapter = TimedOut(call("search_files", {"args": ["-F", "add_recipe(", "src"]}))
    result = await first_use_trial(adapter, settings(), case("search_symbols"), 1)
    assert not result["passed"]
    assert result["exception"]["type"] == "TimeoutError"
    assert len(result["responses"]) == len(result["calls"]) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell fixture commands")
@pytest.mark.parametrize("label", ["EXIT=", "EXIT_CODE:", "EXITCODE=", "Exit code: "])
async def test_shell_control_accepts_harmless_exit_labels(tmp_path, label):
    fixture = FirstUseFixture(tmp_path)
    fixture.write({"check.py": "print('CHECK-42 passed')\n"})
    try:
        result = await fixture.dispatch(
            {
                "name": "bash",
                "arguments": {"command": f'python check.py; Write-Output "{label}$LASTEXITCODE"'},
            },
        )
        assert result["ok"], result
        assert result["data"]["exit_code"] == 0
        assert "CHECK-42 passed" in result["data"]["output"]
        assert label + "0" in result["data"]["output"]
    finally:
        await fixture.close()
