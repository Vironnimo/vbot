"""Tool dispatch: the ToolContext a Tool receives, dispatch gates, contracts and results."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from core.chat._skill_activation import _activate_triggered_skills
from core.chat.messages import ChatMessage, JsonObject
from core.runs import TOOL_CALL_RESULT_EVENT, TOOL_CALL_STARTED_EVENT
from core.skills import SkillRegistry
from core.tools import (
    ToolContext,
    ToolDisplay,
    ToolDisplayField,
    ToolRegistry,
    tool_success,
)
from core.tools.model_names import SHELL_MODEL_NAME
from tests.core.chat.tool_dispatch_test_support import (
    ToolDispatchHarness,
    call,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


def _recording_tools(*names: str) -> tuple[ToolRegistry, list[tuple[str, ToolContext]]]:
    """Tools that record which one ran with which ToolContext."""
    observed: list[tuple[str, ToolContext]] = []
    tools = ToolRegistry()
    for name in names:

        def handler(context: ToolContext, _arguments: JsonObject, name: str = name) -> JsonObject:
            observed.append((name, context))
            return tool_success({"tool": name})

        tools.register(name, f"Recording stub for {name}.", {"type": "object"}, handler)
    return tools, observed


def _env_skill_registry(tmp_path: Path) -> SkillRegistry:
    skill_dir = tmp_path / "skills" / "provider-probe"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: provider-probe
description: Probe provider APIs.
metadata:
  vbot:
    requirements:
      env: OPENAI_API_KEY
---

Probe the provider.
""",
        encoding="utf-8",
    )
    return SkillRegistry.load(tmp_path / "skills", environment={"OPENAI_API_KEY": "available"})


def test_triggered_env_skill_carries_bash_usage_guidance(tmp_path: Path) -> None:
    harness = ToolDispatchHarness(tmp_path, ToolRegistry(), allowed_skills=["provider-probe"])

    _activate_triggered_skills(
        harness.agent, harness.session, "$provider-probe run a probe", _env_skill_registry(tmp_path)
    )

    content = harness.session.activated_skill_contents()["provider-probe"]
    assert content.index("<environment_access>") < content.index("Probe the provider.")
    assert "- `OPENAI_API_KEY`" in content
    assert f"`env_keys` array of every `{SHELL_MODEL_NAME}` call" in content


@pytest.mark.asyncio
@pytest.mark.parametrize("project_run", [False, True], ids=["identity-run", "project-run"])
async def test_dispatch_builds_the_tool_context_from_the_run(
    tmp_path: Path, project_run: bool
) -> None:
    tools, observed = _recording_tools("probe")
    harness = ToolDispatchHarness(tmp_path, tools)
    harness.run.iteration_count = 3
    project_cwd = tmp_path / "repo"
    project_cwd.mkdir()

    def deny_remote(name: str) -> str | None:
        return "test-owned-denial" if name == "remote" else None

    await harness.dispatch(
        [call("probe")],
        project_cwd=project_cwd if project_run else None,
        project_id="acme" if project_run else None,
        tool_restriction=("probe",) if project_run else None,
        tool_denial_resolver=deny_remote if project_run else None,
    )

    [(_, context)] = observed
    assert context.iteration_number == 3
    if project_run:
        assert (context.effective_cwd, context.project_id) == (project_cwd, "acme")
        assert context.tool_restriction == ("probe",)
        assert context.tool_denial_resolver is deny_remote
    else:
        assert (context.effective_cwd, context.project_id) == (harness.agent.workspace, None)
        assert context.tool_restriction is None


@pytest.mark.asyncio
async def test_dispatch_exposes_active_skill_env_grants_until_compaction(tmp_path: Path) -> None:
    tools, observed = _recording_tools("probe")
    harness = ToolDispatchHarness(tmp_path, tools)
    registry = _env_skill_registry(tmp_path)

    async def granted_env_keys() -> tuple[str, ...]:
        await harness.dispatch([call("probe", f"call-{len(observed)}")], skill_registry=registry)
        return tuple(observed[-1][1].skill_env_keys)

    harness.session.activate_skill_context("provider-probe", {"activation_content": "active"})
    assert await granted_env_keys() == ("OPENAI_API_KEY",)
    harness.session.append(
        ChatMessage.compaction_checkpoint(
            summary="Compacted", projection=[ChatMessage.user("Tail")], compacted_token_count=10
        )
    )
    assert await granted_env_keys() == ()
    harness.session.register_skill_activation("provider-probe", "reloaded content")
    assert await granted_env_keys() == ("OPENAI_API_KEY",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowed_tools", "restriction", "ran", "denied"),
    [
        (["*"], ("memory", "skill"), ["memory"], ["read_file"]),
        (["read_file"], ("memory", "read_file"), ["read_file"], ["memory"]),
        (["*"], None, ["memory", "read_file"], []),
    ],
    ids=["restriction-narrows-wildcard", "intersection-not-union", "no-restriction"],
)
async def test_run_tool_restriction_narrows_dispatch_to_the_intersection(
    tmp_path: Path,
    allowed_tools: list[str],
    restriction: tuple[str, ...] | None,
    ran: list[str],
    denied: list[str],
) -> None:
    tools, observed = _recording_tools("memory", "read_file")
    harness = ToolDispatchHarness(tmp_path, tools, allowed_tools=allowed_tools)

    dispatched = await harness.dispatch(
        [call("memory"), call("read_file")], tool_restriction=restriction
    )

    assert [name for name, _context in observed] == ran
    results = dict(zip(["memory", "read_file"], dispatched.results, strict=True))
    for name in ran:
        assert results[name] == tool_success({"tool": name})
    for name in denied:
        assert results[name]["error"]["code"] == "tool_not_allowed"
    assert harness.run.tool_call_names == {"memory", "read_file"}


@pytest.mark.asyncio
async def test_a_removed_tool_fails_before_denials_and_lookup_without_running(
    tmp_path: Path,
) -> None:
    tools, observed = _recording_tools("probe")
    harness = ToolDispatchHarness(tmp_path, tools)

    dispatched = await harness.dispatch(
        [call("probe"), call("gone")],
        removed_tool_names=frozenset({"probe", "gone"}),
        tool_denial_resolver=lambda _name: "denied by the Run",
    )

    assert observed == []
    probe, gone = dispatched.results
    assert probe["error"] == {
        "code": "tool_removed",
        "message": (
            "Nothing was run: the Tool probe was removed from your Tools in this Session. "
            "Use your other Tools, or tell the user if the task needs probe."
        ),
        "retryable": False,
    }
    assert gone["error"]["code"] == "tool_removed"
    assert [event.payload["error_code"] for event in dispatched.events(TOOL_CALL_RESULT_EVENT)] == [
        "tool_removed",
        "tool_removed",
    ]
    assert len(dispatched.events(TOOL_CALL_STARTED_EVENT)) == 2


@pytest.mark.asyncio
async def test_session_tool_grant_precedes_agent_and_run_dispatch_gates(tmp_path: Path) -> None:
    tools = ToolRegistry()
    tools.register(
        "history",
        "Session history",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"ran": True}),
        session_scoped=True,
    )
    harness = ToolDispatchHarness(tmp_path, tools, allowed_tools=[])

    async def dispatch(**gates: Any) -> JsonObject:
        return (
            await harness.dispatch(
                [call("history")], run=harness.new_run(), base_allowed_tools=("history",), **gates
            )
        ).results[0]

    assert (await dispatch())["error"]["code"] == "history_unavailable"
    assert await dispatch(session_tool_grants=("history",)) == tool_success({"ran": True})
    restricted = await dispatch(session_tool_grants=("history",), tool_restriction=("read",))
    assert restricted["error"]["code"] == "tool_not_allowed"


@pytest.mark.asyncio
async def test_empty_additional_agent_targets_keep_subagent_dispatchable(tmp_path: Path) -> None:
    tools, _observed = _recording_tools("subagent")
    harness = ToolDispatchHarness(tmp_path, tools, agent_tools={"subagent": {"allowed_agents": []}})

    dispatched = await harness.dispatch([call("subagent")], base_allowed_tools=("subagent",))

    assert dispatched.results == [tool_success({"tool": "subagent"})]


@pytest.mark.asyncio
async def test_dispatch_validates_and_emits_exact_provider_cycle_contract(
    tmp_path: Path,
) -> None:
    handler_calls: list[JsonObject] = []

    def handler(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        handler_calls.append(arguments)
        return tool_success({})

    tools = ToolRegistry()
    tools.register(
        "profiled",
        "Canonical broad Tool.",
        {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
            "additionalProperties": False,
        },
        handler,
    )
    narrowed = {
        "type": "object",
        "properties": {"target": {"type": "string", "enum": ["visible"]}},
        "required": ["target"],
        "additionalProperties": False,
    }
    contracts = tools.contracts_for_provider_definitions(
        [{"name": "profiled", "description": "Narrow Provider-cycle Tool.", "parameters": narrowed}]
    )
    harness = ToolDispatchHarness(tmp_path, tools)

    dispatched = await harness.dispatch(
        [call("profiled", target="hidden")], tool_contracts=contracts
    )

    assert dispatched.results[0]["error"]["code"] == "invalid_arguments"
    assert handler_calls == []
    [started] = dispatched.events(TOOL_CALL_STARTED_EVENT)
    assert started.payload["schema_fingerprint"] == contracts["profiled"].schema_fingerprint


@pytest.mark.asyncio
async def test_dispatch_carries_final_ui_display_into_event_and_tool_message(
    tmp_path: Path,
) -> None:
    def handler(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        context.add_display_count(10, "matches")
        return tool_success({"content": "matches"})

    tools = ToolRegistry()
    tools.register(
        "profiled",
        "Profiled Tool.",
        {"type": "object", "additionalProperties": True},
        handler,
        display=ToolDisplay(
            primary_candidates=(
                ToolDisplayField("description", kind="description", quote=True),
                ToolDisplayField("query", kind="query", quote=True),
            )
        ),
        open_input_schema=True,
    )
    harness = ToolDispatchHarness(tmp_path, tools)

    dispatched = await harness.dispatch(
        [call("profiled", description="Find every version variable", query="VERSION_[A-Z_]+")]
    )

    [started] = dispatched.events(TOOL_CALL_STARTED_EVENT)
    [completed] = dispatched.events(TOOL_CALL_RESULT_EVENT)
    assert started.payload["display"]["primary"][0]["value"] == "Find every version variable"
    assert started.payload["display"]["facts"] == []
    assert completed.payload["display"]["facts"] == [
        {"kind": "count", "value": 10, "unit": "matches", "at_least": False}
    ]
    assert dispatched.messages[0].tool_display == completed.payload["display"]


@pytest.mark.asyncio
async def test_only_read_media_artifacts_become_media_outputs(tmp_path: Path) -> None:
    media = {
        "kind": "read_media",
        "attachment_id": "att-1",
        "filename": "diagram.png",
        "media_type": "image/png",
    }
    tools = ToolRegistry()
    tools.register(
        "read",
        "Reads media.",
        {"type": "object"},
        lambda _c, _a: tool_success({"content": "loaded"}, artifacts=[media]),
    )
    tools.register(
        "image_generation",
        "Generates images.",
        {"type": "object"},
        lambda _c, _a: tool_success(
            {"message": "image generated"},
            artifacts=[{"kind": "image", "url": "/api/x", "id": "img-1"}],
        ),
    )
    harness = ToolDispatchHarness(tmp_path, tools)

    dispatched = await harness.dispatch([call("read"), call("image_generation")])

    assert len(dispatched.messages) == 2
    assert dispatched.media_outputs == [
        {
            "tool_call_id": "call-read",
            "tool_message_id": dispatched.messages[0].id,
            "attachment_id": "att-1",
            "filename": "diagram.png",
            "media_type": "image/png",
        }
    ]


@pytest.mark.asyncio
async def test_unexpected_tool_crash_is_logged_and_becomes_an_error_result(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def crashing_handler(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        raise RuntimeError("handler exploded")

    tools = ToolRegistry()
    tools.register("boom", "Tool that crashes.", {"type": "object"}, crashing_handler)
    harness = ToolDispatchHarness(tmp_path, tools)
    caplog.set_level(logging.ERROR, logger="vbot.chat")

    dispatched = await harness.dispatch([call("boom")])

    assert dispatched.results[0]["ok"] is False
    assert dispatched.results[0]["error"]["code"] == "tool_execution_error"
    [record] = [
        record
        for record in caplog.records
        if record.name == "vbot.chat"
        and record.levelno == logging.ERROR
        and "crashed unexpectedly" in record.getMessage()
    ]
    assert "boom" in record.getMessage()
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)
