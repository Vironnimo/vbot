"""Tools: registration, catalog listings, Provider definitions, readiness and dispatch."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import pytest

from core.tools import (
    DuplicateToolError,
    Tool,
    ToolContext,
    ToolDefinitionProfile,
    ToolDefinitionProfileContext,
    ToolDisplay,
    ToolNotAllowedError,
    ToolRegistry,
    model_names,
    tool_success,
)
from tests.core.tools.tools_test_support import (
    READ_FILE_SCHEMA,
    JsonObject,
    make_context,
    read_file_handler,
    register_read_file,
)

WRITE_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
    "additionalProperties": False,
}


async def write_file_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
    return tool_success(
        {
            "written": arguments["path"],
            "bytes": len(arguments["content"]),
            "workspace": str(context.workspace),
        }
    )


def register_write_file(registry: ToolRegistry) -> Tool:
    return registry.register(
        name="write_file",
        description="Write UTF-8 text to a workspace file.",
        parameters=WRITE_FILE_SCHEMA,
        handler=write_file_handler,
    )


def _file_tools() -> ToolRegistry:
    registry = ToolRegistry()
    register_write_file(registry)
    register_read_file(registry)
    return registry


# --- Registration ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        ({"name": ""}, "Tool name is required"),
        ({"name": "1tool"}, "Tool name must start with a letter"),
        ({"name": "tool-name"}, "Tool name must start with a letter"),
        ({"description": ""}, "Tool description is required"),
        ({"parameters": []}, "Tool parameters must be a JSON Schema object"),
        ({"handler": None}, "Tool handler must be callable"),
        ({"display": object()}, "Tool display must be a ToolDisplay instance"),
        ({"ready": "nope"}, "Tool ready predicate must be callable"),
        ({"activation": "mystery"}, "Unsupported Tool activation: mystery"),
        ({"activation": "follows"}, "A followed Tool requires activation_source"),
        ({"activation_source": "read"}, "activation_source is only valid for a followed Tool"),
        (
            {"requires_opt_in": True, "internal": True},
            "Only configurable, non-internal Tools can require opt-in",
        ),
        (
            {"family": "extension:weather:missing"},
            "Tool family is not registered: extension:weather:missing",
        ),
    ],
)
def test_registration_rejects_an_invalid_declaration(
    declaration: dict[str, Any], message: str
) -> None:
    registry = ToolRegistry()

    with pytest.raises(ValueError, match=message):
        registry.register(
            **{
                "name": "read_file",
                "description": "Read a file.",
                "parameters": READ_FILE_SCHEMA,
                "handler": read_file_handler,
                **declaration,
            }
        )

    assert registry.list_tools() == []


def test_duplicate_name_raises() -> None:
    registry = ToolRegistry()
    register_read_file(registry)

    with pytest.raises(DuplicateToolError, match="read_file"):
        register_read_file(registry)


def test_name_the_model_sees_for_another_tool_is_reserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_names, "_MODEL_NAMES", {"read_file": "host_read"})
    monkeypatch.setattr(model_names, "_REGISTRY_NAMES", {"host_read": "read_file"})
    registry = ToolRegistry()
    register_read_file(registry)
    extension_tool = Tool(
        name="host_read",
        description="Extension Tool",
        parameters=READ_FILE_SCHEMA,
        handler=read_file_handler,
    )

    with pytest.raises(DuplicateToolError, match="host_read"):
        registry.register("host_read", "Extension Tool", READ_FILE_SCHEMA, read_file_handler)
    with pytest.raises(DuplicateToolError, match="host_read"):
        registry.replace_owned_tools([], [extension_tool])


def test_registry_owns_family_labels() -> None:
    registry = ToolRegistry()
    family = registry.register_family(
        "extension:weather:forecast",
        "Weather Forecast",
        extension="weather",
    )

    tool = registry.register(
        "weather_today",
        "Read today's forecast.",
        READ_FILE_SCHEMA,
        read_file_handler,
        family=family.id,
        extension="weather",
    )

    assert tool.family == "extension:weather:forecast"
    assert tool.family_label == "Weather Forecast"
    assert registry.get_family(family.id) == family


def test_registry_never_unregisters_builtin_family_metadata() -> None:
    registry = ToolRegistry()

    registry.unregister_family("files")

    assert registry.get_family("files").label == "Files"


# --- Listings and Provider definitions ------------------------------------------


@pytest.mark.parametrize(
    ("allowed_tools", "names"),
    [
        pytest.param(None, ["read_file", "write_file"], id="no-allowlist"),
        pytest.param(["*"], ["read_file", "write_file"], id="wildcard"),
        pytest.param([], [], id="empty"),
        pytest.param(["write_file"], ["write_file"], id="explicit"),
        pytest.param(["missing_tool"], [], id="unknown-name"),
    ],
)
def test_allowlist_selects_the_listed_and_offered_tools(
    allowed_tools: list[str] | None, names: list[str]
) -> None:
    registry = _file_tools()

    assert [tool.name for tool in registry.list_tools(allowed_tools)] == names
    assert [
        definition["name"] for definition in registry.provider_definitions(allowed_tools)
    ] == names
    assert [definition["name"] for definition in registry.prompt_definitions(allowed_tools)] == (
        names
    )


def test_definitions_expose_only_copies_of_name_description_and_schema() -> None:
    registry = ToolRegistry()
    parameters = {**READ_FILE_SCHEMA}
    registry.register(
        name="read_file",
        description="Read a UTF-8 text file from the workspace.",
        parameters=parameters,
        handler=read_file_handler,
        display=ToolDisplay(summary_fields=("path",)),
    )
    parameters["type"] = "array"

    definitions = registry.provider_definitions(["read_file"])
    definitions[0]["parameters"]["type"] = "array"

    assert registry.provider_definitions(["read_file"]) == [
        {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the workspace.",
            "parameters": READ_FILE_SCHEMA,
        }
    ]
    assert registry.prompt_definitions(["read_file"]) == [
        {"name": "read_file", "description": "Read a UTF-8 text file from the workspace."}
    ]


def test_configuration_profile_is_stable_and_shared_by_provider_and_prompt() -> None:
    registry = ToolRegistry()

    def resolve(context: ToolDefinitionProfileContext) -> ToolDefinitionProfile | None:
        if context.agent_id != "agent-1":
            return None
        return ToolDefinitionProfile(
            key=f"agent:{context.agent_id}:readme-only",
            description="Read this Agent's README file.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "enum": ["README.md"]}},
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    registry.register(
        name="read_file",
        description="Read a UTF-8 text file from the workspace.",
        parameters=READ_FILE_SCHEMA,
        handler=read_file_handler,
        definition_profile_resolver=resolve,
    )
    context = ToolDefinitionProfileContext(agent_id="agent-1")
    hidden = ToolDefinitionProfileContext(agent_id="agent-2")

    first = registry.provider_definitions(["read_file"], profile_context=context)
    second = registry.provider_definitions(["read_file"], profile_context=context)
    prompt = registry.prompt_definitions(["read_file"], profile_context=context)

    assert first == second
    assert first[0]["parameters"]["properties"]["path"]["enum"] == ["README.md"]
    assert prompt == [{"name": "read_file", "description": "Read this Agent's README file."}]
    first[0]["parameters"]["properties"]["path"]["enum"].append("SECRET.md")
    assert registry.provider_definitions(["read_file"], profile_context=context)[0]["parameters"][
        "properties"
    ]["path"]["enum"] == ["README.md"]
    assert registry.provider_definitions(["read_file"], profile_context=hidden) == []
    assert registry.prompt_definitions(["read_file"], profile_context=hidden) == []


def test_session_scoped_tool_is_offered_only_with_its_grant() -> None:
    registry = ToolRegistry()
    register_read_file(registry)
    registry.register(
        name="history",
        description="Read this Session's earlier original records.",
        parameters={"type": "object"},
        handler=read_file_handler,
        session_scoped=True,
    )

    assert [definition["name"] for definition in registry.provider_definitions(["*"])] == [
        "read_file"
    ]
    assert registry.prompt_definitions([]) == []
    assert [
        definition["name"]
        for definition in registry.provider_definitions([], session_grants=["history"])
    ] == ["history"]
    assert [
        definition["name"]
        for definition in registry.prompt_definitions([], session_grants=["history"])
    ] == ["history"]
    assert [tool.name for tool in registry.list_tools()] == ["history", "read_file"]
    assert [tool.name for tool in registry.list_tools(include_session_scoped=False)] == [
        "read_file"
    ]


# --- Readiness ------------------------------------------------------------------


def test_readiness_is_evaluated_live_for_offered_tools_only() -> None:
    registry = ToolRegistry()
    token = {"value": ""}
    registry.register(
        name="gated",
        description="A gated tool.",
        parameters={"type": "object"},
        handler=read_file_handler,
        ready=lambda: bool(token["value"]),
    )
    register_read_file(registry)

    # A not-ready Tool stays in a plain listing but leaves every offered surface.
    assert [tool.name for tool in registry.list_tools()] == ["gated", "read_file"]
    assert [tool.name for tool in registry.list_tools(ready_only=True)] == ["read_file"]
    assert registry.provider_definitions(["gated"]) == []
    assert registry.prompt_definitions(["gated"]) == []

    token["value"] = "present"

    assert [tool.name for tool in registry.list_tools(ready_only=True)] == ["gated", "read_file"]
    assert [definition["name"] for definition in registry.provider_definitions(["gated"])] == [
        "gated"
    ]


def test_raising_readiness_predicate_counts_as_not_ready_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = ToolRegistry()

    def boom() -> bool:
        raise RuntimeError("predicate exploded")

    registry.register(
        name="gated",
        description="A gated tool.",
        parameters={"type": "object"},
        handler=read_file_handler,
        ready=boom,
    )

    with caplog.at_level(logging.WARNING):
        assert registry.list_tools(ready_only=True) == []

    assert any("readiness predicate raised" in record.getMessage() for record in caplog.records)


def test_dispatch_of_not_ready_tool_returns_envelope_without_running_handler() -> None:
    registry = ToolRegistry()
    called: list[bool] = []

    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        called.append(True)
        return tool_success({})

    registry.register(
        name="gated",
        description="A gated tool.",
        parameters={"type": "object"},
        handler=handler,
        ready=lambda: False,
    )

    result = asyncio.run(registry.dispatch(make_context("gated"), {}))

    assert result["ok"] is False
    assert result["error"]["code"] == "tool_not_ready"
    assert result["error"]["retryable"] is False
    assert called == []


# --- Dispatch -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_runs_sync_handlers_on_the_loop_and_awaits_async_handlers() -> None:
    registry = _file_tools()
    loop_thread_id = threading.get_ident()
    seen_thread_ids: list[int] = []

    def sync_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        seen_thread_ids.append(threading.get_ident())
        return read_file_handler(context, arguments)

    registry.register("sync_read", "Read on the loop thread.", READ_FILE_SCHEMA, sync_handler)

    read = await registry.dispatch(make_context("sync_read"), {"path": "SOUL.md"}, ["*"])
    written = await registry.dispatch(
        make_context("write_file"), {"path": "SOUL.md", "content": "hello"}, ["write_file"]
    )

    assert read == tool_success({"content": "read SOUL.md", "tool_call_id": "call_1"})
    assert seen_thread_ids == [loop_thread_id]
    assert written == tool_success({"written": "SOUL.md", "bytes": 5, "workspace": "workspace"})


@pytest.mark.asyncio
async def test_internal_tool_dispatch_ignores_empty_allowlist() -> None:
    registry = ToolRegistry()
    registry.register(
        "internal_tool",
        "Internal tool for testing.",
        {"type": "object"},
        lambda _context, _arguments: tool_success({"called": True}),
        internal=True,
    )

    result = await registry.dispatch(make_context("internal_tool"), {}, [])

    assert result == tool_success({"called": True})


@pytest.mark.asyncio
async def test_domain_argument_repair_precedes_schema_and_preserves_input() -> None:
    calls: list[Any] = []

    def repair(arguments: JsonObject) -> Any:
        calls.append("repair")
        return arguments.pop("fetch")

    def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        calls.append(arguments)
        return tool_success({})

    registry = ToolRegistry()
    registry.register(
        "fixture",
        "Fixture",
        {
            "type": "object",
            "properties": {"count": {"type": "integer", "minimum": 1}},
            "required": ["count"],
            "additionalProperties": False,
        },
        handler,
        argument_normalizer=repair,
    )
    context = make_context("fixture")
    original = {"fetch": {"count": "3.0"}}

    assert (await registry.dispatch(context, original, ["fixture"]))["ok"]
    assert calls == ["repair", {"count": 3}]
    assert original == {"fetch": {"count": "3.0"}}
    with pytest.raises(ValueError):
        await registry.dispatch(context, {"fetch": {"count": 0}}, ["fixture"])
    assert calls[-1] == "repair"
    with pytest.raises(ToolNotAllowedError):
        await registry.dispatch(context, original, [])
    assert calls == ["repair", {"count": 3}, "repair"]
