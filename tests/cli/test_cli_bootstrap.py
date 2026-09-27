"""Tests for the ``vbot bootstrap`` commands: current-Run targets, RPC and output."""

from __future__ import annotations

import json
from typing import Any

import pytest

from cli import main as cli_main
from tests.cli.cli_test_support import FakeRpc, RunCli, make_instance

CREATE = ("bootstrap", "create", "--name", "Verify update", "--prompt", "Check", "--mode", "once")


def test_create_needs_an_agent_or_the_current_session(rpc: FakeRpc, run_cli: RunCli) -> None:
    code, out, err = run_cli("bootstrap", "create", "--prompt", "Check", "--mode", "once")

    assert code == 1
    assert "<agent>" in out + err and "--current-session" in out + err
    assert rpc.calls == []


def test_current_session_create_targets_the_injected_run(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "builder")
    monkeypatch.setenv("VBOT_RUN_SESSION_ID", "session-one")
    monkeypatch.setenv("VBOT_RUN_PROJECT_ID", "vbot")
    rpc.reply("bootstrap.create", {"id": "job-one"})

    code, out, _err = run_cli(*CREATE, "--current-session")

    assert code == 0
    assert "job-one" in out
    assert rpc.calls == [
        (
            "bootstrap.create",
            {
                "name": "Verify update",
                "prompt": "Check",
                "mode": "once",
                "agent_id": "builder@vbot",
                "session_id": "session-one",
            },
        )
    ]


@pytest.mark.parametrize(
    ("host", "run_context", "problem"),
    [
        pytest.param("127.0.0.1", False, "only available inside a vBot Run", id="outside-run"),
        pytest.param("server.example", True, "cannot target a remote", id="remote-target"),
    ],
)
def test_current_session_create_is_refused_without_a_local_run(
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    rpc: FakeRpc,
    host: str,
    run_context: bool,
    problem: str,
) -> None:
    for name, value in (("VBOT_RUN_AGENT_ID", "main"), ("VBOT_RUN_SESSION_ID", "session-one")):
        if run_context:
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)
    target = make_instance(tmp_path, host=host)

    code = cli_main.run([*CREATE, "--current-session"], resolve=lambda **_: target)

    assert code == 1
    assert problem in "".join(capsys.readouterr())
    assert rpc.calls == []


def test_bootstrap_list_prints_the_health_fields_of_each_job(rpc: FakeRpc, run_cli: RunCli) -> None:
    job = {
        "id": "job-one",
        "target": "main",
        "name": "Verify update",
        "prompt": "Check status and logs",
        "mode": "once",
        "status": "completed",
        "session_id": "session-one",
        "last_outcome": "success",
        "last_error": None,
    }
    rpc.reply("bootstrap.list", {"jobs": [job]})

    code, out, _err = run_cli("bootstrap", "list")

    assert code == 0
    assert out.splitlines()[1:] == [
        "- name=Verify update id=job-one agent=main mode=once status=completed "
        "session=session-one last_outcome=success last_error=- prompt=Check status and logs",
    ]


@pytest.mark.parametrize(
    "jobs",
    [
        pytest.param([], id="no-jobs"),
        pytest.param([{"id": "another"}], id="other-job"),
        pytest.param(
            [{"id": "wanted", "prompt": "long prompt " * 40, "session_id": "pinned"}],
            id="exact-job",
        ),
    ],
)
def test_bootstrap_show_prints_exactly_the_complete_saved_job(
    rpc: FakeRpc, run_cli: RunCli, jobs: list[dict[str, Any]]
) -> None:
    rpc.reply("bootstrap.list", {"jobs": jobs})

    code, out, _err = run_cli("bootstrap", "show", "wanted")

    assert rpc.calls == [("bootstrap.list", {})]
    if jobs and jobs[0]["id"] == "wanted":
        assert (code, json.loads(out)) == (0, jobs[0])
    else:
        assert code == 1
