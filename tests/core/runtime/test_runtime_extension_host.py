"""Owner-bound Extension hosts: temporary groups, catalogs, prompt previews, payloads."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.agents.temporary import TemporaryAgentConfig
from core.chat import ChatMessage
from core.chat.messages import ToolCall
from core.extensions import ExtensionRegistrationIdentity
from core.extensions.operations import ExtensionHost
from core.runs import RunAdmission, RunAdmissionBlockedError, RunExecutionOwner
from core.runtime.runtime import Runtime
from core.sessions import ToolResultFacts, ToolResultPayload
from core.tools import ToolContext
from core.tools.availability import ToolAccess
from core.utils.config import Config
from tests.core.runtime.runtime_test_support import write_project_skill, write_skill
from tests.core.sessions.history_fixtures import complete_run

SWARM_PRIVATE_TOOLS = {"swarm_board", "swarm_inbox", "swarm_state", "swarm_wiki"}


def _owner_host(runtime: Runtime, name: str) -> ExtensionHost:
    """The host a loaded Extension receives, bound to its current registration."""
    assert runtime.extensions is not None
    root = runtime._extension_host()  # noqa: SLF001 - the Runtime hands hosts only to Extensions.
    assert root.for_owner is not None
    return root.for_owner(runtime.extensions.registration_identity(name))


def _participant(
    cwd: Path, *, model: str = "fixture/model", **tool_access: Any
) -> TemporaryAgentConfig:
    return TemporaryAgentConfig(
        model=model,
        cwd=cwd,
        tool_access=ToolAccess(mode="selected", allowed=(), **tool_access),
        allowed_skills=[],
        tools={},
        name="Peer",
    )


def _accept_every_model(runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime.agent_resolver, "require_model_configured", lambda _model: None)


@pytest.mark.asyncio
async def test_owner_prompt_inspection_uses_selected_blocks_and_private_tool_denials(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _owner_host(runtime, "swarm")
    assert host.inspect_prompt is not None
    config = replace(
        _participant(tmp_path),
        name="Preview",
        instructions="preview-body-sentinel",
        prompt_blocks=["core:agent_body"],
    )

    def no_session(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Preview must not create a Session")

    monkeypatch.setattr(runtime.chat_sessions, "create_bound_temporary_session", no_session)
    preview = await host.inspect_prompt(config, None)

    assert preview["text"] == "preview-body-sentinel"
    assert {tool["name"] for tool in preview["tools"]} == SWARM_PRIVATE_TOOLS
    blocks = {block["id"]: block for block in preview["blocks"]}
    assert blocks["core:runtime"]["enabled"] is False
    assert blocks["core:runtime"]["text"]
    assert blocks["core:agent_body"]["included"] is True
    assert not any(key.startswith("extension_session:") for key in blocks)

    denied = replace(config, tool_access=replace(config.tool_access, denied=("swarm_state",)))
    preview = await host.inspect_prompt(denied, None)
    assert {tool["name"] for tool in preview["tools"]} == SWARM_PRIVATE_TOOLS - {"swarm_state"}


@pytest.mark.asyncio
async def test_owner_catalog_projects_registry_metadata_until_the_registration_retires(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-key")
    repo = tmp_path / "project-a"
    write_project_skill(repo, "project-skill", "Project sentinel.")
    write_skill(runtime.global_skills_dir, "global-skill", "global sentinel")
    runtime.reload_skills()
    runtime.projects.create("project-a", "Project A", repo)
    project = runtime.projects.update(
        "project-a", allowed_tools=["read"], skills_global_enabled=["global-skill"]
    )
    host = _owner_host(runtime, "swarm")
    assert host.catalog is not None

    catalog = await host.catalog()

    assert catalog["projects"] == [
        {
            "id": "project-a",
            "name": "Project A",
            "cwd": project.cwd,
            "allowed_tools": ["read"],
            "allowed_skills": ["global-skill", "project-skill"],
        }
    ]
    assert {"name": "global-skill", "description": "global sentinel"} in catalog["skills"]
    assert "project-skill" not in {skill["name"] for skill in catalog["skills"]}
    assert "core:runtime" in {block["id"] for block in catalog["prompt_blocks"]}
    # Tool family and activation come from the Tool registry; Session-scoped and
    # catalog-hidden Tools stay out.
    tools = {tool["name"]: tool for tool in catalog["tools"]}
    read = runtime.tools.get("read")
    assert (tools["read"]["family"], tools["read"]["activation"]) == (read.family, read.activation)
    assert not {"history", *SWARM_PRIVATE_TOOLS} & tools.keys()
    assert catalog["tool_settings"] == {
        "bash": {
            "type": "object",
            "properties": {
                "allowed_env": {"type": "array", "items": {"type": "string"}, "uniqueItems": True}
            },
            "additionalProperties": False,
        },
        "subagent": {
            "type": "object",
            "properties": {
                "allowed_agents": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                }
            },
            "additionalProperties": False,
        },
    }
    # Models list only usable Connections, with capabilities from the Model registry.
    models = {model["id"]: model for model in catalog["models"]}
    assert models
    for model_id, entry in models.items():
        provider_id, _, model_name = model_id.partition("/")
        assert entry["connections"]
        for connection in entry["connections"]:
            assert runtime.provider_credentials.is_usable(
                provider_id, f"{provider_id}:{connection}"
            )
        model = runtime.models.get(provider_id, model_name)
        reasoning = model.capabilities.reasoning
        assert (entry["name"], entry["context_window"]) == (model.name, model.context_window)
        assert entry["capabilities"] == {
            "tools": model.capabilities.tools,
            "reasoning": {
                "supported": reasoning.supported,
                "control": reasoning.control,
                "levels": list(reasoning.levels),
                "budget_max": reasoning.budget_max,
            },
        }
    assert any(model_id.startswith("openai/") for model_id in models)

    # A registration retired while the projection ran on its worker is refused.
    extensions = runtime.extensions
    assert extensions is not None
    list_projects = runtime.projects.list

    def retire_while_listing() -> Any:
        extensions.retire_registration()
        return list_projects()

    monkeypatch.setattr(runtime.projects, "list", retire_while_listing)
    with pytest.raises(ValueError, match="no longer current"):
        await host.catalog()


@pytest.mark.asyncio
async def test_temporary_group_preflight_validates_participants_before_opening(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(config)
    runtime.start()
    try:
        groups = _owner_host(runtime, "swarm").temporary_agents
        assert groups is not None
        valid_cwd = tmp_path / "valid"
        valid_cwd.mkdir()

        # An unconfigured Model or a missing working directory keeps the group closed.
        for group_id, participant in (
            ("bad-model", _participant(valid_cwd, model="missing/model")),
            ("bad-cwd", _participant(tmp_path / "missing")),
        ):
            await groups.create(group_id, "peer", participant)
            with pytest.raises(RuntimeError, match="temporary execution is unavailable"):
                await groups.open_group(group_id)
            assert groups._groups[group_id].open is False  # noqa: SLF001
            _accept_every_model(runtime, monkeypatch)

        # Profiles may deny the owner's private Tools, one or all of them.
        for group_id, denied in (
            ("one-denied", ("swarm_state",)),
            ("all-denied", tuple(sorted(SWARM_PRIVATE_TOOLS))),
        ):
            binding = await groups.create(group_id, "peer", _participant(valid_cwd, denied=denied))
            assert runtime.extensions is not None
            capability = runtime.extensions.session_capability(binding, runtime.tools)
            assert capability is not None
            assert not set(denied) & set(capability.tool_names)
            await groups.open_group(group_id)

        # A Project's Tool whitelist bounds its Team, not a participant's selection.
        runtime.projects.create("narrow", "Narrow", valid_cwd)
        runtime.projects.update("narrow", allowed_tools=["read"])
        await groups.create(
            "in-project",
            "peer",
            replace(
                _participant(valid_cwd),
                tool_access=ToolAccess(mode="selected", allowed=("read", "search_files")),
            ),
            project_id="narrow",
        )
        await groups.open_group("in-project")

        # The owner is rechecked after the blocking validation on its worker.
        await groups.create("retired", "peer", _participant(valid_cwd))
        resolve_temporary_agent = runtime.agent_resolver.resolve_temporary_agent

        def retire_after_resolution(*args: Any, **kwargs: Any) -> Any:
            resolved = resolve_temporary_agent(*args, **kwargs)
            assert runtime.extensions is not None
            runtime.extensions.retire_registration()
            return resolved

        monkeypatch.setattr(
            runtime.agent_resolver, "resolve_temporary_agent", retire_after_resolution
        )
        with pytest.raises(RuntimeError, match="temporary execution is unavailable"):
            await groups.open_group("retired")
        assert groups._groups["retired"].open is False  # noqa: SLF001
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_owner_groups_run_observably_and_reject_completions_after_close(
    config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = Runtime(config)
    runtime.start()
    try:
        _accept_every_model(runtime, monkeypatch)
        groups = _owner_host(runtime, "swarm").temporary_agents
        assert groups is not None
        # The two public loops differ in whether a pending Provider response
        # exposes live Model deltas; Extension pages subscribe to those Runs.
        assert groups._chat is runtime.streaming_chat_loop  # noqa: SLF001

        binding = await groups.create("group", "peer", _participant(tmp_path))
        handle = await groups.open_group("group")
        owner = RunExecutionOwner("swarm", "group", "peer", binding.generation_id, handle.epoch)
        groups.validate(binding.address, RunAdmission(owner=owner))
        service = runtime.trigger_service
        validate = service._completion_delivery._owned_completion_validator  # noqa: SLF001
        assert validate is not None
        validate(binding.address, owner)  # the live group admits its owner

        await groups.close_group("group")

        with pytest.raises(RunAdmissionBlockedError):
            validate(binding.address, owner)
        late = service.submit_completion(
            binding.address.agent_id,
            binding.address.session_id,
            project_id=binding.address.project_id,
            notice_id="late",
            origin_run_id="origin",
            body="late owned result",
            execution_owner=owner,
        )
        assert late.cancelled()
        # Usage is the Statistics projection of the owner's group; no Run finished yet.
        usage = await groups.usage("group", {})
        assert (usage["group_id"], usage["owned_run_count"], usage["participants"]) == (
            "group",
            0,
            [],
        )
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_owner_group_titles_reach_the_shared_title_generator(runtime: Runtime) -> None:
    groups = _owner_host(runtime, "swarm").temporary_agents
    assert groups is not None

    # Without participants no Model qualifies, so the local title is final.
    title = await groups.title_group("swr_new", "  Review the\n parser  ")

    assert title == "Review the parser"
    assert await groups.group_titles(["swr_new", "swr_other"]) == {"swr_new": "Review the parser"}
    stored = await runtime.chat_sessions.temporary_group_titles_async(
        owner_name="swarm", group_ids=["swr_new"]
    )
    assert stored == {"swr_new": "Review the parser"}


@pytest.mark.asyncio
async def test_result_payloads_load_for_their_owner_through_the_calling_session(
    runtime: Runtime, tmp_path: Path
) -> None:
    session = runtime.chat_sessions.create("agent", session_id="source")
    runtime.chat_sessions.create("agent", session_id="other")
    run = session.start_run("run-one")
    assistant = ChatMessage.assistant(
        model="model", content=None, tool_calls=[ToolCall(id="call", name="mcp_x", arguments={})]
    )
    run.append_many([ChatMessage.user("question"), assistant])
    run.assistant_message_id = assistant.id
    run.append_many(
        [ChatMessage.tool(tool_call_id="call", name="mcp_x", content="receipt")],
        tool_results={
            "call": ToolResultFacts(
                "completed", True, payloads=(ToolResultPayload("res_one", "mcp", "[1]"),)
            )
        },
    )
    complete_run(
        run,
        ChatMessage.run_summary(
            run_id="run-one",
            status="completed",
            iteration_count=1,
            timing={
                "started_at": "2026-09-19T10:00:00Z",
                "completed_at": "2026-09-19T10:00:01Z",
                "duration_ms": 1000,
            },
        ),
    )
    root = runtime._extension_host()  # noqa: SLF001
    assert root.for_owner is not None
    mcp = _owner_host(runtime, "mcp")
    swarm = _owner_host(runtime, "swarm")
    assert mcp.load_result_payload is not None
    assert swarm.load_result_payload is not None

    def call(session_id: str, *, in_session: bool = True) -> ToolContext:
        return ToolContext(
            agent_id="agent",
            session_id=session_id,
            run_id="run",
            tool_call_id="later-call",
            tool_name="mcp_x",
            tool_call_index=0,
            workspace=tmp_path,
            vbot_root=tmp_path,
            data_root=tmp_path,
            result_payload_hook=(lambda *_args: "unused") if in_session else None,
        )

    load = mcp.load_result_payload
    assert root.load_result_payload is None
    assert await load(call("source"), "res_one") == [1]
    # Another Extension, another Session, a call outside any Session, an unsafe id.
    assert await swarm.load_result_payload(call("source"), "res_one") is None
    assert await load(call("other"), "res_one") is None
    assert await load(call("source", in_session=False), "res_one") is None
    assert await load(call("source"), "../res_one") is None
    stale = root.for_owner(ExtensionRegistrationIdentity("mcp", "retired-epoch"))
    assert stale.load_result_payload is not None
    with pytest.raises(ValueError, match="no longer current"):
        await stale.load_result_payload(call("source"), "res_one")
