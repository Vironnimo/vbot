"""Which Tool definitions the chat loop offers the Provider, and under which names."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.chat import ChatMessage
from core.extensions.operations import ExtensionOperations
from core.model_tasks import TASK_IMAGE_UNDERSTANDING
from core.tools import (
    ANALYZE_IMAGE_TOOL_NAME,
    BASH_SUBAGENT_TOOL_DESCRIPTION,
    BASH_SUBAGENT_TOOL_PARAMETERS,
    BASH_TOOL_DESCRIPTION,
    BASH_TOOL_NAME,
    BASH_TOOL_PARAMETERS,
    ToolAccess,
    ToolContext,
    ToolRegistry,
    model_names,
    model_tool_name,
    register_history_tool,
    tool_success,
)
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubModels,
    build_chat_loop,
    history,
)
from tests.core.chat.chat_loop_tools_test_support import (
    JsonObject,
    final,
    tool_results,
    tool_runtime,
    tool_turn,
)


def _name_recording_tools(*names: str) -> tuple[ToolRegistry, list[str]]:
    dispatched: list[str] = []

    def probe(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        dispatched.append(context.tool_name)
        return tool_success({"id": context.tool_call_id})

    tools = ToolRegistry()
    for name in names:
        tools.register(name, "Probe Tool.", {"type": "object"}, probe)
    return tools, dispatched


def _call_names(messages: list[Any]) -> list[str]:
    return [call.name for message in messages for call in message.tool_calls or []]


def _wire_call_names(request: JsonObject) -> list[str]:
    return [
        call["name"] for message in request["messages"] for call in message.get("tool_calls") or []
    ]


def _offered(request: JsonObject) -> set[str]:
    return {str(definition["name"]) for definition in request["kwargs"]["tools"]}


@pytest.mark.asyncio
async def test_provider_requests_use_model_tool_names_while_the_session_keeps_registry_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model_names, "_MODEL_NAMES", {"probe": "host_probe"})
    monkeypatch.setattr(model_names, "_REGISTRY_NAMES", {"host_probe": "probe"})
    tools, dispatched = _name_recording_tools("probe")
    runtime = tool_runtime(
        tmp_path,
        tools,
        [tool_turn(("first", "host_probe")), final("done")],
        allowed_tools=["probe"],
    )

    await build_chat_loop(runtime).send("coder", "probe", session_id="session-one")

    assert dispatched == ["probe"]
    assert _call_names(history(runtime)) == ["probe"]
    follow_up = runtime.adapter.requests[1]
    assert [tool["name"] for tool in follow_up["kwargs"]["tools"]] == ["host_probe"]
    assert _wire_call_names(follow_up) == ["host_probe"]
    assert [m.get("name") for m in follow_up["messages"] if m["role"] == "tool"] == ["host_probe"]


@pytest.mark.asyncio
async def test_tool_called_by_another_harness_name_runs_and_is_stored_under_its_name(
    tmp_path: Path,
) -> None:
    # "fetch" is registered but not offered: it keeps its own name and is refused rather
    # than remapped to the offered web_fetch.
    tools, dispatched = _name_recording_tools("web_fetch", "fetch")
    runtime = tool_runtime(
        tmp_path,
        tools,
        [
            tool_turn(("first", "functions.WebFetch"), ("second", "TodoWrite"), ("third", "fetch")),
            final("done"),
        ],
        allowed_tools=["web_fetch"],
    )

    await build_chat_loop(runtime).send("coder", "fetch", session_id="session-one")

    messages = history(runtime)
    assert dispatched == ["web_fetch"]
    assert _call_names(messages) == ["web_fetch", "TodoWrite", "fetch"]
    unknown = next(m for m in messages if m.role == "tool" and m.name == "TodoWrite")
    assert "Unknown Tool: TodoWrite. Call one of the available Tools instead: web_fetch." in str(
        unknown.content
    )
    assert tool_results(messages)[2]["error"]["code"] == "tool_not_allowed"
    assert _wire_call_names(runtime.adapter.requests[1]) == ["web_fetch", "TodoWrite", "fetch"]


@pytest.mark.asyncio
async def test_ambiguous_tool_spelling_never_dispatches_a_harness_alias(tmp_path: Path) -> None:
    tools, dispatched = _name_recording_tools("read", "read_file", "readfile")
    runtime = tool_runtime(tmp_path, tools, [tool_turn(("ambiguous", "ReadFile")), final("done")])

    await build_chat_loop(runtime).send("coder", "read", session_id="session-one")

    messages = history(runtime)
    assert dispatched == []
    assert _call_names(messages) == ["ReadFile"]
    assert tool_results(messages)[0]["error"]["code"] == "tool_not_found"


@pytest.mark.asyncio
async def test_nested_run_receives_non_handoff_bash_definition(tmp_path: Path) -> None:
    tools = ToolRegistry()
    tools.register(
        BASH_TOOL_NAME,
        BASH_TOOL_DESCRIPTION,
        BASH_TOOL_PARAMETERS,
        lambda _context, _arguments: tool_success({"status": "completed"}),
        open_input_schema=True,
    )
    runtime = tool_runtime(
        tmp_path,
        tools,
        [final("top-level done"), final("nested done")],
        allowed_tools=[BASH_TOOL_NAME],
    )
    parent = build_chat_loop(runtime)

    await parent.send("coder", "Top-level", session_id="top-level")
    await parent.child_loop(nesting_depth=1).send("coder", "Nested", session_id="nested")

    top_level_definition = runtime.adapter.requests[0]["kwargs"]["tools"][0]
    nested_definition = runtime.adapter.requests[1]["kwargs"]["tools"][0]
    # The Provider request carries the name the Model knows on this host, and the
    # description names no dedicated file Tool, since this Agent is offered none.
    usual_pointer = (
        "For reading, searching and editing files use read, search_files and apply_patch. "
    )
    assert top_level_definition == {
        "name": model_tool_name(BASH_TOOL_NAME),
        "description": BASH_TOOL_DESCRIPTION.replace(usual_pointer, ""),
        "parameters": BASH_TOOL_PARAMETERS,
    }
    assert nested_definition == {
        "name": model_tool_name(BASH_TOOL_NAME),
        "description": BASH_SUBAGENT_TOOL_DESCRIPTION.replace(usual_pointer, ""),
        "parameters": BASH_SUBAGENT_TOOL_PARAMETERS,
    }


@pytest.mark.asyncio
async def test_live_catalog_publication_refreshes_next_provider_cycle(tmp_path: Path) -> None:
    tools = ToolRegistry()
    operations = ExtensionOperations("test")
    operations.bind(tools)
    calls: list[str] = []

    async def installed(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        calls.append("installed-called")
        return tool_success({"sentinel": True})

    async def install(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        parameters = {"type": "object", "properties": {}, "additionalProperties": False}
        operations.replace_tools(
            "connection",
            [
                {
                    "name": "installed",
                    "description": "test-sentinel",
                    "parameters": parameters,
                    "handler": installed,
                }
            ],
        )
        return tool_success({"installed": True})

    tools.register("install", "test-sentinel", {"type": "object"}, install)
    runtime = tool_runtime(
        tmp_path,
        tools,
        [tool_turn(("install", "install")), tool_turn(("use", "installed")), final("finished")],
    )

    await build_chat_loop(runtime).send("coder", "install and use", session_id="session-one")

    assert calls == ["installed-called"]
    assert "installed" not in _offered(runtime.adapter.requests[0])
    assert "installed" in _offered(runtime.adapter.requests[1])


def _analyze_image_runtime(
    tmp_path: Path,
    *,
    model: str,
    image_input: bool,
    wire_images: bool,
    task_available: bool,
    responses: int = 1,
) -> Any:
    tools = ToolRegistry()
    tools.register(
        ANALYZE_IMAGE_TOOL_NAME,
        "Analyze images.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"analysis": "ok"}),
    )
    provider, model_id = model.split("/")
    return tool_runtime(
        tmp_path,
        tools,
        [],
        model=model,
        allowed_tools=[ANALYZE_IMAGE_TOOL_NAME],
        adapter=StubAdapter(
            [final("done")] * responses,
            wire_media_types=frozenset({"image/png"}) if wire_images else frozenset(),
        ),
        models=StubModels(
            {(provider, model_id): 128_000},
            input_modalities={
                (provider, model_id): ("text", "image") if image_input else ("text",)
            },
        ),
        available_task_models={TASK_IMAGE_UNDERSTANDING} if task_available else set(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image_input", "wire_images", "task_available", "visible"),
    [
        (False, False, True, True),
        (True, True, True, False),
        (True, False, True, True),
        (False, False, False, False),
    ],
    ids=[
        "text-route-with-binding",
        "route-views-images",
        "model-views-images-but-wire-does-not",
        "no-usable-binding",
    ],
)
async def test_analyze_image_is_offered_only_when_the_route_cannot_view_images_itself(
    tmp_path: Path, image_input: bool, wire_images: bool, task_available: bool, visible: bool
) -> None:
    runtime = _analyze_image_runtime(
        tmp_path,
        model="openai/route-model",
        image_input=image_input,
        wire_images=wire_images,
        task_available=task_available,
    )
    loop = build_chat_loop(runtime)

    preview = await loop.preview_tool_definitions(runtime.agents.get("coder"))
    assert not runtime.adapter.requests
    await loop.send("coder", "Inspect the image", session_id="s1")

    assert (ANALYZE_IMAGE_TOOL_NAME in {tool["name"] for tool in preview}) is visible
    assert (ANALYZE_IMAGE_TOOL_NAME in _offered(runtime.adapter.requests[0])) is visible
    effective_names = runtime.system_prompts.effective_tool_name_calls[-1]
    assert (ANALYZE_IMAGE_TOOL_NAME in effective_names) is visible


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "available", "visible"),
    [
        (ToolAccess(mode="all"), True, False),
        (ToolAccess(mode="all", granted=(ANALYZE_IMAGE_TOOL_NAME,)), True, True),
        (ToolAccess(mode="all", granted=(ANALYZE_IMAGE_TOOL_NAME,)), False, False),
        (ToolAccess(mode="none", granted=(ANALYZE_IMAGE_TOOL_NAME,)), True, False),
        (
            ToolAccess(
                mode="selected",
                denied=(ANALYZE_IMAGE_TOOL_NAME,),
                granted=(ANALYZE_IMAGE_TOOL_NAME,),
            ),
            True,
            False,
        ),
    ],
    ids=["no-grant", "granted", "granted-unavailable", "no-tools", "denied-wins"],
)
async def test_analyze_image_vision_grant_is_stable_and_respects_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: ToolAccess,
    available: bool,
    visible: bool,
) -> None:
    monkeypatch.setattr(StubAgent, "tool_access", property(lambda _self: policy))
    runtime = _analyze_image_runtime(
        tmp_path,
        model="openai/vision-model",
        image_input=True,
        wire_images=True,
        task_available=available,
        responses=2,
    )
    loop = build_chat_loop(runtime)
    preview = await loop.preview_tool_definitions(runtime.agents.get("coder"))
    assert not runtime.adapter.requests
    assert (ANALYZE_IMAGE_TOOL_NAME in {tool["name"] for tool in preview}) is visible

    for message in ("Read the handwriting with analyze_image", "What is two plus two?"):
        await loop.send("coder", message, session_id="s1")
        assert (
            ANALYZE_IMAGE_TOOL_NAME in runtime.system_prompts.effective_tool_name_calls[-1]
        ) is visible

    assert [request["kwargs"]["tools"] for request in runtime.adapter.requests] == [preview] * 2


@pytest.mark.asyncio
async def test_tool_restriction_denies_dispatch_without_changing_offered_definitions(
    tmp_path: Path,
) -> None:
    # A restricted Run dispatches only the listed Tools and fails every other call as
    # tool_not_allowed. The wire definitions, the System Prompt and the Tool names the
    # prompt is built from stay identical to an unrestricted Run, so the prompt cache holds.
    requests: dict[str, JsonObject] = {}
    dispatched: dict[str, list[str]] = {}
    results: dict[str, list[JsonObject]] = {}
    prompt_tool_names: dict[str, list[tuple[str, ...]]] = {}
    for label, restriction in (("restricted", ("memory",)), ("unrestricted", None)):
        tools, dispatched[label] = _name_recording_tools("memory", "weather")
        data_dir = tmp_path / label
        data_dir.mkdir()
        runtime = tool_runtime(
            data_dir,
            tools,
            [tool_turn(("call_memory", "memory"), ("call_weather", "weather")), final("done")],
        )
        runtime.chat_sessions.create("coder", session_id="session-one")
        run = await build_chat_loop(runtime).start_run(
            "coder", "Go", session_id="session-one", tool_restriction=restriction
        )
        await run.wait()
        requests[label] = runtime.adapter.requests[0]
        results[label] = tool_results(history(runtime))
        prompt_tool_names[label] = runtime.system_prompts.effective_tool_name_calls

    assert dispatched == {"restricted": ["memory"], "unrestricted": ["memory", "weather"]}
    memory_result, weather_result = results["restricted"]
    assert (memory_result["ok"], weather_result["ok"]) == (True, False)
    assert weather_result["error"]["code"] == "tool_not_allowed"
    restricted, unrestricted = requests["restricted"], requests["unrestricted"]
    assert json.dumps(restricted["kwargs"]["tools"]) == json.dumps(unrestricted["kwargs"]["tools"])
    assert restricted["messages"][0]["content"] == unrestricted["messages"][0]["content"]
    assert prompt_tool_names["restricted"] == prompt_tool_names["unrestricted"]
    assert [set(names) for names in prompt_tool_names["restricted"]] == [_offered(unrestricted)]


@pytest.mark.asyncio
async def test_checkpoint_granted_history_stays_offered_when_the_run_restricts_dispatch(
    tmp_path: Path,
) -> None:
    runtime = tool_runtime(
        tmp_path,
        None,
        [tool_turn(("history-call", "history", {"action": "overview"})), final("done")],
        allowed_tools=[],
    )
    register_history_tool(runtime.tools, runtime.chat_sessions)
    session = runtime.chat_sessions.create("coder", session_id="session-one")
    session.append(ChatMessage.user("Earlier request"))
    session.append(
        ChatMessage.compaction_checkpoint(
            summary="Earlier context", projection=[], compacted_token_count=10
        )
    )

    run = await build_chat_loop(runtime).start_run(
        "coder", "Continue", session_id="session-one", tool_restriction=("memory",)
    )
    await run.wait()

    assert [_offered(request) for request in runtime.adapter.requests] == [{"history"}] * 2
    assert tool_results(history(runtime))[0]["error"]["code"] == "tool_not_allowed"
