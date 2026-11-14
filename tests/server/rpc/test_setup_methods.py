"""The voluntary first-setup contract shared by every Accessor."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.settings.setup_state import SETUP_STEPS, SetupStateStore
from core.settings.validation import validate_data_dir_config
from tests.server.rpc_test_support import (
    StubAdapter,
    StubAgent,
    make_state,
    resource_changes,
    rpc_error,
    rpc_result,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("step", SETUP_STEPS)
async def test_setup_can_finish_at_every_step_without_a_model(tmp_path: Path, step: str) -> None:
    state = make_state(tmp_path, StubAdapter(), initial_agent=StubAgent(id="main", model=""))
    initial = await rpc_result(state, "setup.get")
    assert initial == {
        "status": "pending",
        "step": "provider",
        "agent_id": "main",
        "auto_show": True,
    }
    await rpc_result(state, "setup.update", step=step)
    ended = await rpc_result(state, "setup.finish")
    assert ended == {**initial, "status": "skipped", "step": step, "auto_show": False}
    # A new Accessor/store sees the same decision and progress. Reopening and
    # navigating never resets the terminal state or overwrites ordinary Settings.
    assert SetupStateStore(tmp_path).read().payload() == ended
    reopened = await rpc_result(state, "setup.update", step="model")
    assert reopened["status"] == "skipped"
    assert reopened["auto_show"] is False
    assert state.runtime.agents.get("main").model == ""
    assert resource_changes(state, "setup")


@pytest.mark.asyncio
async def test_manual_model_choice_latches_setup_before_later_removal(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter(), initial_agent=StubAgent(id="main", model=""))
    await rpc_result(state, "setup.update", step="model")
    await rpc_result(state, "agent.update", id="main", model="openai/gpt-5.2")
    # No setup read is needed to latch a manual choice. Provider health and
    # catalog membership do not enter the historical completion decision.
    assert SetupStateStore(tmp_path).read().status == "completed"
    await rpc_result(state, "agent.update", id="main", model="")
    state.runtime.disabled_connections.add("openai:api-key")
    state.runtime.models._models.clear()
    result = await rpc_result(state, "setup.get")
    assert result["status"] == "completed"
    assert result["auto_show"] is False
    assert (await rpc_result(state, "setup.finish"))["status"] == "completed"
    assert (await rpc_result(state, "setup.update", step="services"))["status"] == "completed"


@pytest.mark.asyncio
async def test_existing_model_and_manual_defaults_complete_setup(tmp_path: Path) -> None:
    # Startup enrolls an already configured instance, including an absent or
    # expired Provider. A stored reference is evidence of the user's selection.
    existing = make_state(
        tmp_path / "existing",
        StubAdapter(),
        initial_agent=StubAgent(id="renamed", model="retired/model"),
    )
    assert (await rpc_result(existing, "setup.get"))["status"] == "completed"
    assert (await rpc_result(existing, "setup.get"))["agent_id"] == "renamed"

    fresh = make_state(
        tmp_path / "fresh", StubAdapter(), initial_agent=StubAgent(id="main", model="")
    )
    await rpc_result(fresh, "settings.update", defaults={"agent": {"model": "openai/gpt-5.2"}})
    assert SetupStateStore(tmp_path / "fresh").read().status == "completed"


@pytest.mark.asyncio
async def test_setup_validates_before_mutation_and_keeps_unknown_fields(tmp_path: Path) -> None:
    state = make_state(tmp_path, StubAdapter(), initial_agent=StubAgent(id="main", model=""))
    await rpc_result(state, "setup.update", step="model")
    path = tmp_path / "setup.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["future"] = {"kept": True}
    path.write_text(json.dumps(saved), encoding="utf-8")
    before = path.read_bytes()
    error = await rpc_error(state, "setup.update", step="unknown")
    assert error["code"] == "invalid_request"
    error = await rpc_error(state, "setup.finish", reset=True)
    assert error["code"] == "invalid_request"
    assert path.read_bytes() == before
    await rpc_result(state, "setup.finish")
    assert json.loads(path.read_text(encoding="utf-8"))["future"] == {"kept": True}

    # Corrupt state is never taken for a fresh installation and overwritten.
    path.write_text('{"format_version": 1, "status": "future-status"}', encoding="utf-8")
    before = path.read_bytes()
    error = await rpc_error(state, "setup.finish")
    assert error["code"] == "domain_error"
    assert path.read_bytes() == before
    reports = validate_data_dir_config(tmp_path)
    assert not next(report for report in reports if report.file_path == path).ok
