"""The Live Tools probe judges the backend model's first Tool call offline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.model_tasks._live_arguments import PreparedLiveCall, prepare_live_call
from core.model_tasks._live_tools import LIVE_TOOL_NAMES
from core.providers.errors import ProviderError
from scripts.provider_probe.live_cases import (
    ScriptedVbot,
    describe,
    example_arguments,
    live_cases,
    matches,
)
from scripts.provider_probe.workflow_live import _probe_live_tools, evaluate_live_case
from tests.scripts.provider_probe_helpers import PROBE

CASES = {case.id: case for case in live_cases()}


class Adapter:
    """Answers each request with the next scripted turn, or with *reply* per request."""

    def __init__(self, *turns: Any, reply: Any = None) -> None:
        self.turns = list(turns)
        self.reply = reply
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.requests.append({"messages": list(messages), **kwargs})
        turn = self.reply(messages) if self.reply is not None else self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn

    def normalize_response(self, raw: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return raw

    async def aclose(self) -> None:
        self.closed = True


def call(name: str, arguments: Any) -> dict[str, Any]:
    return {"content": None, "tool_calls": [{"id": "c1", "name": name, "arguments": arguments}]}


def answer(text: str) -> dict[str, Any]:
    return {"content": text, "tool_calls": []}


def args(*extra: str) -> Any:
    return PROBE._parser().parse_args(["--scenario", "live_tools", *extra])


def evaluate(case_id: str, adapter: Adapter) -> dict[str, Any]:
    return asyncio.run(evaluate_live_case(adapter, args(), CASES[case_id]))


def test_cases_cover_every_tool_and_name_calls_the_live_tools_accept() -> None:
    assert len(CASES) == len(live_cases()) >= 10
    expected = [item for case in CASES.values() for item in case.right if item is not None]
    assert {item.tool for item in expected} == set(LIVE_TOOL_NAMES)
    for item in expected:
        prepared = prepare_live_call(item.tool, example_arguments(item))
        assert isinstance(prepared, PreparedLiveCall), describe(item)
        assert matches(item, prepared.name, prepared.arguments), describe(item)
        # The scripted vBot knows every right target.
        result = asyncio.run(ScriptedVbot()(prepared.name, dict(prepared.arguments)))
        assert result["ok"] is True, (describe(item), result)
    assert [describe(item) for item in CASES["sessions_count"].right] == [
        "start_agent_session(agent='Coder', count=3, task=contains 'Login')"
    ]


def test_a_right_first_call_is_ideal_and_the_trial_keeps_the_evidence() -> None:
    adapter = Adapter(
        call(
            "start_agent_session",
            {"agent": "coder", "count": 3, "task": "Prüf, ob die Login-Tests grün sind."},
        ),
        answer("Drei Sessions beim Coder laufen."),
    )

    row = evaluate("sessions_count", adapter)

    assert (row["verdict"], row["right"], row["failure"]) == ("ideal", True, None)
    assert row["first_call"]["run_arguments"] == {
        "agent": "coder",
        "count": 3,
        "task": "Prüf, ob die Login-Tests grün sind.",
    }
    assert row["first_call"]["result"] == (
        "Started 3 Sessions at Coder with the task: s6, s7, s8. They work in the background; "
        "an update follows when each finishes."
    )
    assert row["answer"] == "Drei Sessions beim Coder laufen."
    # The Model saw the production Live Tools and the voice-style request, never the verdict.
    first = adapter.requests[0]
    assert [tool["name"] for tool in first["tools"]] == list(LIVE_TOOL_NAMES)
    assert first["messages"][-1]["content"].endswith(
        "Delegated request: " + CASES["sessions_count"].request
    )
    assert "start_agent_session(" not in json.dumps(first["messages"])
    assert row["transcript"][-1]["role"] == "tool"
    # The shared Adapter stays open for the next trials.
    assert adapter.closed is False


@pytest.mark.parametrize(
    ("case_id", "turns", "verdict"),
    [
        ("sessions_count", [call("overview", {}), answer("Ok.")], "lookup"),
        ("stop_second", [call("overview", {"agent": "Coder"}), answer("Ok.")], "ideal"),
        ("stop_second", [call("stop", {"target": "S2"}), answer("Ok.")], "ideal"),
        ("answer_question", [call("send_message", {"target": "coder", "text": "ja"})], "wrong"),
        ("status", [call("start_agent_session", {"agent": "Coder", "task": "x"})], "wrong"),
        # A call the Live Tools reject is wrong even when its Tool would be right.
        ("status", [call("overview", {"verbose": True}), answer("Ok.")], "wrong"),
        (
            "claude_no_task",
            [call("start_coding_terminal", {"program": "claude", "folder": "vBot", "task": "fix"})],
            "wrong",
        ),
        ("unclear", [answer("Wen soll ich losschicken, und mit welcher Aufgabe?")], "ideal"),
        ("status", [answer("Gerade läuft nichts.")], "wrong"),
        ("status", [ProviderError("down", retryable=False)], "error"),
    ],
)
def test_verdicts_separate_right_lookup_wrong_and_failed_requests(
    case_id: str, turns: list[Any], verdict: str
) -> None:
    padded = [*turns, answer("Ok."), answer("Ok."), answer("Ok.")]
    row = evaluate(case_id, Adapter(*padded))

    assert row["verdict"] == verdict
    assert row["right"] is (verdict in {"ideal", "lookup"})


def test_probe_runs_the_selected_cases_repeatedly_and_keeps_transcripts_in_the_report(
    tmp_path: Path,
) -> None:
    def reply(messages: list[dict[str, Any]]) -> Any:
        return answer("Ok.") if messages[-1]["role"] == "tool" else call("overview", {})

    report = tmp_path / "live.json"
    output = asyncio.run(
        _probe_live_tools(
            Adapter(reply=reply),
            args(
                "--live-case", "status,unclear", "--repetitions", "2", "--live-report", str(report)
            ),
        )
    )

    assert {
        key: output[key] for key in ("trials", "planned_trials", "ideal", "lookup", "wrong")
    } == {
        "trials": 4,
        "planned_trials": 4,
        "ideal": 2,
        "lookup": 2,
        "wrong": 0,
    }
    assert output["passed"] is True
    assert [(row["case"], row["repetition"]) for row in output["results"]] == [
        ("status", 1),
        ("status", 2),
        ("unclear", 1),
        ("unclear", 2),
    ]
    assert all("transcript" not in row for row in output["results"])
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert all(row["transcript"] for row in saved["results"])


def test_probe_fails_on_a_wrong_first_call_and_rejects_unknown_cases() -> None:
    wrong = asyncio.run(
        _probe_live_tools(
            Adapter(reply=lambda _messages: answer("Nichts.")),
            args("--live-case", "status", "--repetitions", "1"),
        )
    )
    assert (wrong["wrong"], wrong["passed"]) == (1, False)

    with pytest.raises(ValueError, match="Unknown live case"):
        asyncio.run(_probe_live_tools(Adapter(), args("--live-case", "status,nope")))


def test_cli_runs_the_live_probe_with_the_runtime_models_and_closes_the_adapter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    adapter = Adapter(call("overview", {}), answer("Ok."))

    class Runtime:
        def __init__(self, _config: Any) -> None:
            self.models = SimpleNamespace(
                get=lambda _provider, _model: SimpleNamespace(recommended_temperature=0.7)
            )
            self.closed = False

        def get_adapter(self, _ref: Any) -> Adapter:
            return adapter

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(PROBE, "Runtime", Runtime)
    monkeypatch.setattr(PROBE, "Config", lambda **_: None)
    monkeypatch.setattr(PROBE, "_start_probe_runtime", lambda _runtime: None)

    code = asyncio.run(PROBE._run(args("--live-case", "status", "--repetitions", "1")))

    assert code == 0
    assert json.loads(capsys.readouterr().out)["ideal"] == 1
    assert adapter.requests[0]["temperature"] == 0.7
    assert adapter.closed is True
