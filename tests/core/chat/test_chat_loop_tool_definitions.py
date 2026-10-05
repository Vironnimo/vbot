"""Which Tool definitions the chat loop offers the Provider, and under which names."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.chat._tool_epoch import tool_change_from_note
from core.extensions.operations import ExtensionOperations
from core.model_tasks import TASK_IMAGE_UNDERSTANDING
from core.tools import (
    ANALYZE_IMAGE_TOOL_NAME,
    SHELL_TOOL_DESCRIPTION,
    SHELL_TOOL_NAME,
    SHELL_TOOL_PARAMETERS,
    FileReadState,
    ToolAccess,
    ToolContext,
    ToolRegistry,
    model_names,
    model_tool_name,
    register_apply_patch_tool,
    register_edit_tools,
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


def _recording_handler(dispatched: list[str]) -> Any:
    def probe(context: ToolContext, _arguments: JsonObject) -> JsonObject:
        dispatched.append(context.tool_name)
        return tool_success({"id": context.tool_call_id})

    return probe


def _name_recording_tools(*names: str) -> tuple[ToolRegistry, list[str]]:
    dispatched: list[str] = []
    tools = ToolRegistry()
    for name in names:
        tools.register(name, "Probe Tool.", {"type": "object"}, _recording_handler(dispatched))
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
async def test_shell_definition_fits_the_offered_tools(tmp_path: Path) -> None:
    tools = ToolRegistry()
    tools.register(
        SHELL_TOOL_NAME,
        SHELL_TOOL_DESCRIPTION,
        SHELL_TOOL_PARAMETERS,
        lambda _context, _arguments: tool_success({"status": "exited"}),
        open_input_schema=True,
    )
    runtime = tool_runtime(tmp_path, tools, [final("done")], allowed_tools=[SHELL_TOOL_NAME])

    await build_chat_loop(runtime).send("coder", "Run it", session_id="session-one")

    shell = runtime.adapter.requests[0]["kwargs"]["tools"][0]
    # The Provider request carries the name the Model knows on this host; this
    # Agent is offered neither file Tools nor the terminal Tool.
    assert shell["name"] == model_tool_name(SHELL_TOOL_NAME)
    assert "read" not in shell["description"]
    assert "use terminal" not in shell["description"]
    assert "keeps running in the background" in shell["description"]
    assert shell["parameters"]["properties"]["mode"]["enum"] == ["foreground", "background"]


def _tools_sent(runtime: Any) -> list[list[JsonObject]]:
    return [request["kwargs"]["tools"] for request in runtime.adapter.requests]


def _announced(runtime: Any, session_id: str = "session-one") -> list[tuple[str, str]]:
    changes = [tool_change_from_note(message) for message in history(runtime, session_id)]
    return [(change.change, change.tool) for change in changes if change is not None]


def _reminders(request: JsonObject) -> str:
    return "\n".join(
        str(message["content"])
        for message in request["messages"]
        if message["role"] == "user" and "<system-reminder>" in str(message["content"])
    )


_PATH_PARAMETERS = {
    "type": "object",
    "properties": {"path": {"type": "string"}},
    "required": ["path"],
    "additionalProperties": False,
}
# Keys in the author's order, which is not alphabetical at any level.
_ORDERED_PARAMETERS = {
    "type": "object",
    "properties": {
        "zeta": {"type": "string", "description": "Listed first."},
        "alpha": {"type": "integer"},
    },
    "required": ["zeta"],
    "additionalProperties": False,
}


@pytest.mark.asyncio
async def test_a_tool_enabled_mid_run_is_announced_while_the_tool_list_stays_pinned(
    tmp_path: Path,
) -> None:
    tools = ToolRegistry()
    operations = ExtensionOperations("test")
    operations.bind(tools)
    calls: list[JsonObject] = []

    async def installed(_context: ToolContext, arguments: JsonObject) -> JsonObject:
        calls.append(arguments)
        return tool_success({"sentinel": True})

    async def install(_context: ToolContext, _arguments: JsonObject) -> JsonObject:
        operations.replace_tools(
            "connection",
            [
                {
                    "name": "installed",
                    "description": "Installed Tool.",
                    "parameters": _PATH_PARAMETERS,
                    "handler": installed,
                }
            ],
        )
        return tool_success({"installed": True})

    tools.register("install", "Install a Tool.", _ORDERED_PARAMETERS, install)
    runtime = tool_runtime(
        tmp_path,
        tools,
        [
            tool_turn(("install", "install", {"zeta": "now"})),
            # Another harness's spelling resolves to the announced Tool.
            tool_turn(("use", "functions.installed", {"path": "a"})),
            final("finished"),
            final("again"),
            final("after restart"),
        ],
    )

    await build_chat_loop(runtime).send("coder", "install and use", session_id="session-one")
    await build_chat_loop(runtime).send("coder", "once more", session_id="session-one")
    # A restarted runtime on the same storage reads the pinned bytes back.
    runtime.chat_sessions.close()
    runtime = tool_runtime(tmp_path, tools, [], adapter=runtime.adapter)
    await build_chat_loop(runtime).send("coder", "and again", session_id="session-one")

    first, after_install = runtime.adapter.requests[:2]
    assert calls == [{"path": "a"}]
    assert "installed" not in _offered(first)
    # Byte-identical from the pinning request on, across Runs and the restart,
    # with every definition's keys in the order its author wrote them.
    pinned_bytes = json.dumps(first["kwargs"]["tools"])
    assert [json.dumps(sent) for sent in _tools_sent(runtime)] == [pinned_bytes] * 5
    install_definition = next(t for t in first["kwargs"]["tools"] if t["name"] == "install")
    assert json.dumps(install_definition["parameters"]) == json.dumps(_ORDERED_PARAMETERS)
    # The announcement follows the Tool Result that caused it, and only once.
    assert [message["role"] for message in after_install["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert "The Tool installed was enabled for you in this Session." in _reminders(after_install)
    assert _announced(runtime) == [("added", "installed")]


@pytest.mark.asyncio
async def test_an_announced_tool_removed_again_fails_as_removed_under_its_own_name(
    tmp_path: Path,
) -> None:
    tools, dispatched = _name_recording_tools("web_fetch")
    runtime = tool_runtime(
        tmp_path,
        tools,
        [
            final("ready"),
            final("added"),
            tool_turn(("gone", "fetch")),
            final("done"),
            final("back"),
        ],
    )
    loop = build_chat_loop(runtime)
    handler = tools.get("web_fetch").handler

    await loop.send("coder", "Start", session_id="session-one")
    tools.register("fetch", "Probe Tool.", {"type": "object"}, handler)
    await loop.send("coder", "Added", session_id="session-one")
    tools.unregister("fetch")
    await loop.send("coder", "Fetch it", session_id="session-one")
    tools.register("fetch", "Probe Tool.", {"type": "object"}, handler)
    await loop.send("coder", "Back", session_id="session-one")

    # "fetch" is no longer registered, yet it never maps to the listed web_fetch.
    assert dispatched == []
    assert _call_names(history(runtime)) == ["fetch"]
    assert tool_results(history(runtime))[0]["error"]["code"] == "tool_removed"
    assert _announced(runtime) == [("added", "fetch"), ("removed", "fetch"), ("added", "fetch")]
    assert "The Tool fetch was removed from your Tools in this Session." in _reminders(
        runtime.adapter.requests[2]
    )
    assert _tools_sent(runtime) == [_tools_sent(runtime)[0]] * 5


@pytest.mark.asyncio
async def test_readiness_never_removes_a_listed_tool_and_a_tool_ready_later_is_announced(
    tmp_path: Path,
) -> None:
    ready = {"probe": True, "late": False}
    tools, dispatched = _name_recording_tools()
    for name in ready:

        def is_ready(name: str = name) -> bool:
            return ready[name]

        tools.register(
            name,
            "Probe Tool.",
            {"type": "object"},
            _recording_handler(dispatched),
            ready=is_ready,
        )
    runtime = tool_runtime(
        tmp_path,
        tools,
        [final("ready"), tool_turn(("first", "probe"), ("second", "late")), final("done")],
    )
    loop = build_chat_loop(runtime)

    await loop.send("coder", "Start", session_id="session-one")
    ready.update(probe=False, late=True)
    await loop.send("coder", "Use both", session_id="session-one")

    assert dispatched == ["late"]
    assert [result["ok"] for result in tool_results(history(runtime))] == [False, True]
    assert tool_results(history(runtime))[0]["error"]["code"] == "tool_not_ready"
    assert _announced(runtime) == [("added", "late")]
    assert "late" not in _offered(runtime.adapter.requests[0])
    assert _tools_sent(runtime) == [_tools_sent(runtime)[0]] * 3


@pytest.mark.asyncio
async def test_a_changed_tool_schema_is_announced_and_validates_calls(tmp_path: Path) -> None:
    tools, dispatched = _name_recording_tools()

    def register_probe(parameters: JsonObject, **options: Any) -> None:
        tools.register(
            "probe",
            "Probe Tool.",
            parameters,
            _recording_handler(dispatched),
            **options,
        )

    register_probe(_PATH_PARAMETERS)
    runtime = tool_runtime(
        tmp_path,
        tools,
        [
            final("ready"),
            tool_turn(("old", "probe", {"path": "a"}), ("new", "probe", {"count": 1})),
            final("done"),
        ],
    )
    loop = build_chat_loop(runtime)

    await loop.send("coder", "Start", session_id="session-one")
    tools.unregister("probe")
    register_probe(
        {
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
        # The Tool's own account of the change rides along with the announcement.
        definition_change_note=lambda _old, _new: "Probe now counts.",
    )
    await loop.send("coder", "Use it", session_id="session-one")

    assert dispatched == ["probe"]
    assert [result["ok"] for result in tool_results(history(runtime))] == [False, True]
    assert _announced(runtime) == [("changed", "probe")]
    reminders = _reminders(runtime.adapter.requests[1])
    assert "The Tool probe changed in this Session." in reminders
    assert "\nChange: Probe now counts.\nDescription: Probe Tool.\n" in reminders
    assert _tools_sent(runtime) == [_tools_sent(runtime)[0]] * 3


@pytest.mark.asyncio
async def test_a_route_that_drops_unlisted_tool_calls_lists_announced_tools(
    tmp_path: Path,
) -> None:
    tools, dispatched = _name_recording_tools("probe")
    runtime = tool_runtime(
        tmp_path,
        tools,
        [],
        adapter=StubAdapter(
            [final("ready"), tool_turn(("call", "extra")), final("done")],
            list_announced_tools=True,
        ),
    )
    loop = build_chat_loop(runtime)

    await loop.send("coder", "Start", session_id="session-one")
    tools.register("extra", "Extra Tool.", {"type": "object"}, tools.get("probe").handler)
    await loop.send("coder", "Use it", session_id="session-one")

    pinned = _tools_sent(runtime)[0]
    extra = {"name": "extra", "description": "Extra Tool.", "parameters": {"type": "object"}}
    assert dispatched == ["extra"]
    assert _tools_sent(runtime)[1:] == [[*pinned, extra]] * 2
    assert (
        "The Tool extra was enabled for you in this Session and now appears in your Tool list."
        in _reminders(runtime.adapter.requests[1])
    )


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
@pytest.mark.parametrize("backend_withdrawn", [True, False], ids=["backend-gone", "route-views"])
async def test_a_listed_analyze_image_is_removed_only_when_its_backend_is_withdrawn(
    tmp_path: Path, backend_withdrawn: bool
) -> None:
    runtime = _analyze_image_runtime(
        tmp_path,
        model="openai/route-model",
        image_input=False,
        wire_images=False,
        task_available=True,
    )
    first_adapter = runtime.adapter
    await build_chat_loop(runtime).send("coder", "First", session_id="s1")
    if backend_withdrawn:
        runtime.available_task_models.clear()
    else:
        # The Session moves to a route that views images itself.
        runtime.models = StubModels(
            {("openai", "route-model"): 128_000},
            input_modalities={("openai", "route-model"): ("text", "image")},
        )
    runtime.adapter = StubAdapter([final("done")], wire_media_types=frozenset({"image/png"}))
    await build_chat_loop(runtime).send("coder", "Second", session_id="s1")

    first_tools = first_adapter.requests[0]["kwargs"]["tools"]
    assert ANALYZE_IMAGE_TOOL_NAME in {tool["name"] for tool in first_tools}
    assert runtime.adapter.requests[0]["kwargs"]["tools"] == first_tools
    assert _announced(runtime, "s1") == (
        [("removed", ANALYZE_IMAGE_TOOL_NAME)] if backend_withdrawn else []
    )


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


EDIT_TOOLS = {"apply_patch", "edit", "write"}


def _file_edit_runtime(tmp_path: Path, family: str, responses: list[JsonObject]) -> Any:
    state = FileReadState()
    tools = ToolRegistry()
    register_apply_patch_tool(tools, file_state=state)
    register_edit_tools(tools, file_state=state)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return tool_runtime(
        tmp_path,
        tools,
        responses,
        model="provider/route-model",
        workspace=workspace,
        models=StubModels(
            {("provider", "route-model"): 128_000}, families={("provider", "route-model"): family}
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "offered"),
    [
        ("gpt", {"apply_patch"}),
        ("o-mini", {"apply_patch"}),
        ("claude-sonnet", {"edit", "write"}),
        ("gpt-oss", {"edit", "write"}),
        ("", {"edit", "write"}),
    ],
)
async def test_the_model_family_decides_which_file_edit_tools_are_offered(
    tmp_path: Path, family: str, offered: set[str]
) -> None:
    runtime = _file_edit_runtime(tmp_path, family, [final("done")])
    loop = build_chat_loop(runtime)

    preview = await loop.preview_tool_definitions(runtime.agents.get("coder"))
    await loop.send("coder", "Change a file", session_id="s1")

    assert {tool["name"] for tool in preview} & EDIT_TOOLS == offered
    assert _offered(runtime.adapter.requests[0]) & EDIT_TOOLS == offered


@pytest.mark.asyncio
async def test_a_model_change_keeps_the_file_edit_tools_of_the_prompt_epoch(
    tmp_path: Path,
) -> None:
    runtime = _file_edit_runtime(tmp_path, "claude-sonnet", [final("first")])
    await build_chat_loop(runtime).send("coder", "First", session_id="s1")
    pinned = runtime.adapter.requests[0]["kwargs"]["tools"]

    # The Session moves to a GPT Model; the epoch keeps edit and write.
    runtime.models = StubModels(
        {("provider", "route-model"): 128_000}, families={("provider", "route-model"): "gpt"}
    )
    runtime.adapter = StubAdapter(
        [
            tool_turn(
                (
                    "call_patch",
                    "apply_patch",
                    {"patch": "*** Begin Patch\n*** Add File: a.txt\n+a\n*** End Patch"},
                ),
                ("call_write", "write", {"path": "b.txt", "content": "b\n"}),
            ),
            final("second"),
        ]
    )
    await build_chat_loop(runtime).send("coder", "Second", session_id="s1")

    assert {tool["name"] for tool in pinned} & EDIT_TOOLS == {"edit", "write"}
    assert [request["kwargs"]["tools"] for request in runtime.adapter.requests] == [pinned] * 2
    assert _announced(runtime, "s1") == []
    # A call to apply_patch, which this epoch never offered, runs as apply_patch.
    messages = history(runtime, "s1")
    assert _call_names(messages) == ["apply_patch", "write"]
    assert [result["ok"] for result in tool_results(messages)] == [True, True]
    assert (tmp_path / "workspace" / "a.txt").read_bytes() == b"a\n"
