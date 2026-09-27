"""Swarm start: Runs from a saved profile, their configuration, replays and Tools."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from core.chat import CommandDispatcher, CommandExecutionContext, ReplySurface
from tests.resources.extensions.swarm.swarm_test_support import (
    SWARM_COORDINATION_TIMEOUT_SECONDS,
    PausedSwarmAdapter,
    single_participant_profile,
    wait_idle,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "participants",
    # Forty real Runs are a load case; their durable Session writes need a longer timeout.
    [3, pytest.param(40, marks=[pytest.mark.stress, pytest.mark.timeout(120)])],
)
async def test_participants_become_idle_without_closing_the_swarm(
    lifecycle, tmp_path: Path, participants: int
):
    """Ordinary final replies leave all Sessions reachable without closing the group."""
    profile = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "slug": "many",
            "name": "Many participants",
            "participants": [{"model": "fixture/model", "count": participants}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
        },
        expected_revision=None,
    )

    started = await lifecycle.service.operation(
        "swarms.start",
        {"profile_id": profile["id"], "prompt": "shared goal", "request_id": "initial"},
    )
    run_ids = [entry["run_id"] for entry in started["runs"]]
    assert len(run_ids) == participants
    await asyncio.gather(
        *(lifecycle.runtime.chat_run_manager.get(run_id).wait() for run_id in run_ids)
    )

    snapshot = await wait_idle(lifecycle.service, started["swarm_id"])
    assert {item["state"] for item in snapshot["participants"]} == {"idle"}
    # Every route delivers automatically by default, so no Session offers swarm_inbox.
    assert {tool["name"] for tool in lifecycle.runtime.adapter.requests[0]["kwargs"]["tools"]} == {
        "swarm_board",
        "swarm_state",
        "swarm_wiki",
    }
    for run_id in run_ids:
        assert (
            await lifecycle.groups.owned_run(started["swarm_id"], run_id)
        ).record.terminal_status == ("completed")


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["directory", "project"])
async def test_run_directory_override_survives_resume_without_changing_profile(
    lifecycle, tmp_path, source
):
    override = tmp_path / "run-directory"
    override.mkdir()
    working = (
        {"kind": "directory", "path": str(tmp_path)}
        if source == "directory"
        else {"kind": "project", "project_id": "unavailable-default-project"}
    )
    saved = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "name": "Directory override",
            "participants": [{"model": "fixture/model", "count": 1}],
            "working_directory": working,
            "tool_access": {"mode": "selected", "allowed": []},
        },
        expected_revision=None,
    )
    arguments = {
        "profile_id": saved["id"],
        "expected_profile_revision": saved["revision"],
        "prompt": "directory-goal-sentinel",
        "request_id": "override-start",
        "working_directory": str(override),
    }
    started = await lifecycle.service.operation("swarms.start", arguments)
    await lifecycle.runtime.chat_run_manager.get(started["runs"][0]["run_id"]).wait()
    snapshot = await lifecycle.service.store.get_swarm(started["swarm_id"])
    assert snapshot["effective_configuration"] == {"cwd": str(override), "project_id": None}
    assert snapshot["profile_snapshot"]["working_directory"] == working
    assert await lifecycle.service.store.get_profile(saved["id"]) == saved
    binding = (await lifecycle.groups.list(started["swarm_id"]))[0]
    agent = lifecycle.runtime.agent_resolver.temporary_agents.resolve(
        binding.address, generation_id=binding.generation_id
    )
    assert agent.cwd == override
    assert binding.address.project_id is None
    replayed = await lifecycle.service.operation("swarms.start", arguments)
    assert replayed["replayed"] is True
    assert replayed["runs"] == started["runs"]
    with pytest.raises(ValueError, match="^request_conflict$"):
        await lifecycle.service.operation(
            "swarms.start", {**arguments, "working_directory": str(tmp_path)}
        )
    await lifecycle.service.operation(
        "swarms.stop", {"swarm_id": started["swarm_id"], "request_id": "stop"}
    )
    resumed = await lifecycle.service.operation(
        "swarms.resume", {"swarm_id": started["swarm_id"], "request_id": "resume"}
    )
    await lifecycle.runtime.chat_run_manager.get(resumed["runs"][0]["run_id"]).wait()
    resumed_binding = (await lifecycle.groups.list(started["swarm_id"]))[0]
    assert resumed_binding.address == binding.address
    resumed_agent = lifecycle.runtime.agent_resolver.temporary_agents.resolve(
        resumed_binding.address, generation_id=resumed_binding.generation_id
    )
    assert resumed_agent.cwd == override


@pytest.mark.asyncio
async def test_run_rejects_invalid_directory_before_creating_sessions(lifecycle, tmp_path):
    file = tmp_path / "file.txt"
    file.write_text("sentinel")
    saved = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "name": "Directory validation",
            "participants": [{"model": "fixture/model", "count": 1}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
        },
        expected_revision=None,
    )
    for index, value in enumerate([None, 42, "", "relative", str(tmp_path / "missing"), str(file)]):
        with pytest.raises(ValueError, match="^invalid_arguments$"):
            await lifecycle.service.operation(
                "swarms.start",
                {
                    "profile_id": saved["id"],
                    "prompt": "goal",
                    "request_id": f"invalid-directory-{index}",
                    "working_directory": value,
                },
            )
    assert not (await lifecycle.service.store.list_swarms()).entries
    assert lifecycle.runtime.adapter.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [
        None,
        {
            "enabled": False,
            "trigger": {"type": "input_tokens", "tokens": 50_000},
            "strategy": {"type": "continuation"},
        },
    ],
)
async def test_profile_compaction_policy_reaches_every_participant(lifecycle, tmp_path, policy):
    from resources.extensions.swarm._extension_values import _participant_config

    saved = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "name": "Compaction policy",
            "participants": [
                {"model": "fixture/model", "count": 1},
                {"model": "fixture/model", "count": 1, "temperature": 0.2},
            ],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
            "compaction_policy": policy,
        },
        expected_revision=None,
    )
    started = await lifecycle.service.operation(
        "swarms.start",
        {"profile_id": saved["id"], "prompt": "compaction-goal", "request_id": "start"},
    )
    for entry in started["runs"]:
        await lifecycle.runtime.chat_run_manager.get(entry["run_id"]).wait()
    # A later profile edit applies to new Runs only; this Run keeps its snapshot.
    await lifecycle.service.store.save_profile(
        {
            **saved,
            "compaction_policy": None
            if policy
            else {
                "enabled": True,
                "trigger": {"type": "context_ratio", "threshold": 0.5},
                "strategy": {"type": "summary_tail", "tail_tokens": 4_000},
            },
        },
        expected_revision=saved["revision"],
    )
    swarm = await lifecycle.service.store.get_swarm(started["swarm_id"])
    assert swarm["profile_snapshot"]["compaction_policy"] == policy
    bindings = await lifecycle.groups.list(started["swarm_id"])
    assert len(bindings) == 2
    for binding in bindings:
        agent = lifecycle.runtime.agent_resolver.temporary_agents.resolve(
            binding.address, generation_id=binding.generation_id
        )
        assert agent.compaction_policy == policy
    # Profiles and snapshots saved before the field existed have no key and inherit.
    legacy = {
        key: value for key, value in swarm["profile_snapshot"].items() if key != "compaction_policy"
    }
    participant = swarm["participants"][0]
    assert _participant_config(legacy, participant, tmp_path).compaction_policy is None


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [[], ["core:runtime"]])
async def test_profile_prompt_selection_reaches_model_without_hidden_orientation(
    lifecycle, tmp_path, selected
):
    from tests.core.prompts.prompts_test_support import StubStorage, _manager

    lifecycle.runtime.system_prompts = _manager(
        tmp_path,
        tools=lifecycle.tools,
        storage=StubStorage({"runtime.md": "runtime-sentinel", "tools.md": "tools-sentinel"}),
    )
    saved = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "name": "Prompt control",
            "slug": "prompt-control",
            "participants": [{"model": "fixture/model", "count": 1}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
            "instructions": "profile-body-sentinel",
            "prompt_blocks": selected,
        },
        expected_revision=None,
    )
    started = await lifecycle.service.operation(
        "swarms.start",
        {
            "profile_id": saved["id"],
            "prompt": "goal-sentinel",
            "request_id": "start",
        },
    )
    await lifecycle.runtime.chat_run_manager.get(started["runs"][0]["run_id"]).wait()
    messages = lifecycle.runtime.adapter.requests[0]["messages"]
    assert [message["content"] for message in messages if message["role"] == "system"] == [
        "runtime-sentinel\n\nprofile-body-sentinel" if selected else "profile-body-sentinel"
    ]
    inputs = [message["content"] for message in messages if message["role"] == "user"]
    swarm = await lifecycle.service.store.get_swarm(started["swarm_id"])
    assert len(inputs) == 1
    # The initial input names the exact call that reads the goal post.
    assert '{"action": "read", "message_id": "#0"}' in inputs[0]
    assert "goal-sentinel" not in str(messages)
    goal = await lifecycle.service.store.read_human_posts(
        swarm["id"], message_id=swarm["goal_post_id"]
    )
    assert goal.entries[0]["text"] == "goal-sentinel"
    assert not lifecycle.runtime.extensions.records()[0].declarations.session_prompt_blocks


@pytest.mark.asyncio
async def test_swarm_command_preserves_quoted_unicode_prompt_and_starts_distinct_swarms(
    lifecycle, tmp_path
):
    """The registered immediate Command shares start semantics without idempotent user intent."""

    await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "slug": "command",
            "name": "Command profile",
            "participants": [{"model": "fixture/model", "count": 1}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": []},
        },
        expected_revision=None,
    )
    dispatcher = CommandDispatcher(lifecycle.runtime.chat_run_manager)
    lifecycle.runtime.extensions.apply_commands(dispatcher)
    quoted = dispatcher.prepare('/swarm command "研究 \\"quoted\\" goal"')
    unquoted = dispatcher.prepare("/swarm command plain Unicode 研究")
    assert quoted is not None and unquoted is not None
    context = CommandExecutionContext(
        agent_id="ordinary",
        session_id="command-session",
        project_id=None,
        reply_surface=ReplySurface.webui(),
    )
    first = await dispatcher.execute(quoted, context)
    second = await dispatcher.execute(unquoted, context)
    assert first.facts is not None and second.facts is not None
    assert first.facts["swarm_id"] != second.facts["swarm_id"]
    assert (await lifecycle.service.store.get_swarm(first.facts["swarm_id"]))[
        "prompt"
    ] == '研究 "quoted" goal'
    assert (await lifecycle.service.store.get_swarm(second.facts["swarm_id"]))[
        "prompt"
    ] == "plain Unicode 研究"


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [False, True])
async def test_replaying_start_preserves_the_active_run(lifecycle, tmp_path, concurrent):
    adapter = PausedSwarmAdapter(pause_at=1)
    lifecycle.runtime.adapter = adapter
    profile = await single_participant_profile(lifecycle, tmp_path)
    arguments = {"profile_id": profile["id"], "prompt": "review goal", "request_id": "same-start"}
    if concurrent:
        first, replay = await asyncio.gather(
            lifecycle.service.operation("swarms.start", arguments),
            lifecycle.service.operation("swarms.start", arguments),
        )
    else:
        first = await lifecycle.service.operation("swarms.start", arguments)
        replay = await lifecycle.service.operation("swarms.start", arguments)
    await asyncio.wait_for(adapter.started.wait(), timeout=SWARM_COORDINATION_TIMEOUT_SECONDS)
    run = lifecycle.runtime.chat_run_manager.get(first["runs"][0]["run_id"])
    assert replay == {**first, "replayed": True}
    assert run.status.value == "running"
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "pinned_revision"),
    [
        ("profile_update", False),
        ("profile_update", True),
        ("profile_delete", True),
        ("directory", False),
        ("catalog", False),
    ],
)
async def test_start_replay_uses_admitted_snapshot(lifecycle, tmp_path, change, pinned_revision):
    working = tmp_path / "working"
    working.mkdir()
    profile = await single_participant_profile(lifecycle, working)
    arguments = {"profile_id": profile["id"], "prompt": "saved goal", "request_id": "saved-start"}
    if pinned_revision:
        arguments["expected_profile_revision"] = profile["revision"]
    started = await lifecycle.service.operation("swarms.start", arguments)
    await lifecycle.runtime.chat_run_manager.get(started["runs"][0]["run_id"]).wait()
    requests_before = len(lifecycle.runtime.adapter.requests)

    if change == "profile_update":
        await lifecycle.service.store.save_profile(
            {
                **profile,
                "name": "Changed",
                "working_directory": {"kind": "directory", "path": str(tmp_path)},
            },
            expected_revision=profile["revision"],
        )
    elif change == "profile_delete":
        await lifecycle.service.store.delete_profile(
            profile["id"], expected_revision=profile["revision"]
        )
    elif change == "directory":
        working.rmdir()
    else:

        async def empty_catalog():
            return {"models": [], "tools": [], "skills": [], "projects": []}

        lifecycle.service.host = replace(lifecycle.service.host, catalog=empty_catalog)

    replay = await lifecycle.service.operation("swarms.start", arguments)
    assert replay == {**started, "replayed": True}
    assert len(lifecycle.runtime.adapter.requests) == requests_before
    for changed in (
        {"profile_id": "another-profile"},
        {"prompt": "another goal"},
        {"expected_profile_revision": profile["revision"] + 1},
        {"working_directory": str(tmp_path)},
    ):
        with pytest.raises(ValueError, match="^request_conflict$"):
            await lifecycle.service.operation("swarms.start", {**arguments, **changed})


@pytest.mark.asyncio
async def test_started_run_is_titled_in_the_background_and_listed_by_title(lifecycle, tmp_path):
    adapter = PausedSwarmAdapter(pause_at=1)
    lifecycle.runtime.adapter = adapter
    changes = []
    # Each change records how many titles existed when the page was told to refresh.
    lifecycle.service.host = replace(
        lifecycle.service.host,
        publish_change=lambda *args: changes.append((args[0:2], len(lifecycle.titled))),
    )
    profile = await single_participant_profile(lifecycle, tmp_path)

    started = await lifecycle.service.operation(
        "swarms.start",
        {"profile_id": profile["id"], "prompt": "Review the parser", "request_id": "titled"},
    )
    swarm_id = started["swarm_id"]
    async with asyncio.timeout(SWARM_COORDINATION_TIMEOUT_SECONDS):
        while not lifecycle.titled or lifecycle.service._title_tasks:  # noqa: SLF001
            await asyncio.sleep(0.01)
    listed = await lifecycle.service.operation("swarms.list", {})

    assert lifecycle.titled == [(swarm_id, "Review the parser")]
    assert [entry["title"] for entry in listed["entries"]] == ["Generated Run title"]
    assert (("swarms", [swarm_id]), 1) in changes
    adapter.release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("denied", "waiting"),
    [
        (["swarm_state"], "main"),
        (["swarm_wiki"], "discussion"),
        (["swarm_state"], "ping"),
        (["swarm_board", "swarm_inbox"], "main"),
        (["swarm_board", "swarm_inbox", "swarm_state", "swarm_wiki"], "main"),
    ],
)
async def test_disabled_swarm_tools_stay_unavailable_on_start_and_resume(
    lifecycle, tmp_path, denied, waiting
):
    # swarm_inbox is offered because one route holds posts back until the participant is idle;
    # with every route delivering at once it is not (see the participants test).
    names = {"swarm_board", "swarm_inbox", "swarm_state", "swarm_wiki"}
    profile = await lifecycle.service.store.save_profile(
        {
            "schema_version": 1,
            "name": "Selective",
            "participants": [{"model": "fixture/model", "count": 1}],
            "working_directory": {"kind": "directory", "path": str(tmp_path)},
            "tool_access": {"mode": "selected", "allowed": [], "denied": denied},
            "delivery": {waiting: {"mode": "idle", "wake_idle": True}},
        },
        expected_revision=None,
    )
    started = await lifecycle.service.operation(
        "swarms.start",
        {
            "profile_id": profile["id"],
            "prompt": "original-request-sentinel",
            "request_id": "start-selective",
        },
    )
    for run in started["runs"]:
        await lifecycle.runtime.chat_run_manager.get(run["run_id"]).wait()
    await wait_idle(lifecycle.service, started["swarm_id"])
    first = lifecycle.runtime.adapter.requests[0]
    assert {tool["name"] for tool in first["kwargs"].get("tools", [])} == names - set(denied)
    if "swarm_board" in denied:
        assert "original-request-sentinel" in json.dumps(first["messages"])
    await lifecycle.service.operation(
        "swarms.stop", {"swarm_id": started["swarm_id"], "request_id": "stop-selective"}
    )
    resumed = await lifecycle.service.operation(
        "swarms.resume", {"swarm_id": started["swarm_id"], "request_id": "resume-selective"}
    )
    for run in resumed["runs"]:
        await lifecycle.runtime.chat_run_manager.get(run["run_id"]).wait()
    assert all(
        {tool["name"] for tool in request["kwargs"].get("tools", [])} == names - set(denied)
        for request in lifecycle.runtime.adapter.requests
    )
