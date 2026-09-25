import asyncio
import json
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.database import DatabaseUnavailableError, write_bootstrap_marker
from core.model_tasks.decision_types import DecisionError
from core.model_tasks.decisions import DecisionService


def service(tmp_path):
    bindings = SimpleNamespace(
        binding_is_usable=lambda task: True,
        binding_for=lambda task: SimpleNamespace(target="openrouter/typesafe/jev-1.13::api_key"),
    )
    write_bootstrap_marker(tmp_path)
    return DecisionService(bindings, None, tmp_path / "decisions.db")


async def finished(owner, identifier):
    async with asyncio.timeout(5):
        while (record := await owner.evaluation(identifier))["status"] == "running":
            await asyncio.sleep(0.01)
    return record


@pytest.mark.asyncio
async def test_disconnect_during_admission_still_starts_once(tmp_path, monkeypatch):
    owner = service(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    evaluate = AsyncMock(return_value={"answers": {}})
    monkeypatch.setattr(owner, "_evaluate", evaluate)
    original = owner.get_experiment

    async def delayed(identifier):
        entered.set()
        await release.wait()
        return await original(identifier)

    monkeypatch.setattr(owner, "get_experiment", delayed)
    try:
        exp = await owner.save_experiment(
            {
                "title": "Test",
                "state": "same",
                "questions": [{"id": "x", "type": "noul", "instructions": "Is this valid?"}],
            }
        )
        request = asyncio.create_task(owner.start(exp["id"], 1, "same-request"))
        await entered.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        release.set()
        record = await owner.start(exp["id"], 1, "same-request")
        assert (await finished(owner, record["id"]))["status"] == "completed"
        assert evaluate.await_count == 1
        assert (await owner.start(exp["id"], 1, "same-request"))["id"] == record["id"]
        with pytest.raises(DecisionError):
            await owner.start(exp["id"], 1, "same-request", "control")
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_control_uses_only_fixed_commands_fresh_state_and_stops_on_done(
    tmp_path, monkeypatch
):
    owner = service(tmp_path)
    observation = {"argv": ["observe"], "cwd": str(tmp_path)}
    action = {"argv": ["act", "literal; argument"], "cwd": str(tmp_path)}
    calls = []

    async def command(value, timeout):
        calls.append(value)
        if value == observation:
            return json.dumps(
                {"state": {"remaining": 1 if len(calls) == 1 else 0}, "done": len(calls) > 1}
            )
        return "applied"

    monkeypatch.setattr("core.model_tasks.decisions.run_command", command)
    evaluate = AsyncMock(return_value={"answers": {"action": {"type": "choice", "choice": "act"}}})
    monkeypatch.setattr(owner, "_evaluate", evaluate)
    draft = {
        "title": "Control",
        "state": "",
        "questions": [],
        "control": {
            "instructions": "Finish work",
            "observe": observation,
            "actions": {
                "act": {"description": "One step", "command": action},
                "wait": {"description": "Wait", "command": None},
            },
            "interval_ms": 0,
            "max_steps": 5,
            "timeout_seconds": 10,
        },
    }
    try:
        exp = await owner.save_experiment(draft)
        record = await owner.start(exp["id"], 1, "control", "control")
        result = await finished(owner, record["id"])
        assert result["status"] == "completed"
        assert result["result"]["stop_reason"] == "application_done"
        assert result["result"]["steps_completed"] == 1
        assert calls == [observation, action, observation]
        assert evaluate.await_args.args[1] == {"remaining": 1}
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_explicit_cancel_and_shutdown_retain_distinct_outcomes(tmp_path, monkeypatch):
    owner = service(tmp_path)
    entered = asyncio.Event()

    async def evaluate(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(owner, "_evaluate", evaluate)
    exp = await owner.save_experiment(
        {
            "title": "Cancel",
            "state": "",
            "questions": [{"id": "x", "type": "noul", "instructions": "Question"}],
        }
    )
    record = await owner.start(exp["id"], 1, "first")
    await entered.wait()
    assert (await owner.cancel(record["id"]))["status"] == "cancelled"
    entered.clear()
    second = await owner.start(exp["id"], 1, "second")
    await entered.wait()
    await owner.aclose()
    assert owner.database.is_closed()
    # Read the closed file directly: reopening would run startup recovery.
    with closing(sqlite3.connect(f"file:{tmp_path / 'decisions.db'}?mode=ro", uri=True)) as db:
        (status,) = db.execute(
            "SELECT status FROM evaluations WHERE id=?", (second["id"],)
        ).fetchone()
    assert status == "interrupted"


@pytest.mark.asyncio
async def test_store_work_runs_on_the_decisions_database_pool_until_close(tmp_path, monkeypatch):
    owner = service(tmp_path)
    threads = []
    original = owner._store.list

    def listing():
        threads.append(threading.current_thread().name)
        return original()

    monkeypatch.setattr(owner._store, "list", listing)
    assert (await owner.list_experiments())["experiments"] == []
    assert threads[0].startswith(f"vbot-db-{owner.database.name}_")

    await owner.aclose()

    with pytest.raises(DatabaseUnavailableError):
        await owner.list_experiments()
    with pytest.raises(DatabaseUnavailableError):
        await owner.save_experiment({"title": "Late", "state": "", "questions": []})
