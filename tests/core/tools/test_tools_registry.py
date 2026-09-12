"""Tools: registry behavior."""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import replace

import pytest

from core.tools import (
    DuplicateToolError,
    Tool,
    ToolCall,
    ToolContext,
    ToolDefinitionProfile,
    ToolDefinitionProfileContext,
    ToolDisplay,
    ToolExecutor,
    ToolNotAllowedError,
    ToolRegistry,
    tool_is_ready,
    tool_success,
)
from tests.core.tools.tools_helpers import (
    READ_FILE_SCHEMA,
    JsonObject,
    make_context,
    make_execution_config,
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


class TestToolRegistryRegister:
    def test_register_returns_tool_and_get_finds_it(self) -> None:
        registry = ToolRegistry()

        tool = register_read_file(registry)

        assert registry.get("read_file") is tool

    def test_register_copies_parameter_schema(self) -> None:
        registry = ToolRegistry()
        parameters = {"type": "object"}

        tool = registry.register(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            parameters=parameters,
            handler=read_file_handler,
        )
        parameters["type"] = "array"

        assert tool.parameters == {"type": "object"}

    def test_duplicate_name_raises(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        with pytest.raises(DuplicateToolError, match="read_file"):
            register_read_file(registry)

    def test_empty_name_raises_value_error(self) -> None:
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="name"):
            registry.register("", "Description", READ_FILE_SCHEMA, read_file_handler)

    def test_empty_description_raises_value_error(self) -> None:
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="description"):
            registry.register("read_file", "", READ_FILE_SCHEMA, read_file_handler)

    def test_non_object_parameters_raise_value_error(self) -> None:
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="parameters"):
            registry.register(
                "read_file",
                "Read a UTF-8 text file from the workspace.",
                [],  # type: ignore[arg-type]
                read_file_handler,
            )

    def test_non_callable_handler_raises_value_error(self) -> None:
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="handler"):
            registry.register(
                "read_file",
                "Read a UTF-8 text file from the workspace.",
                READ_FILE_SCHEMA,
                None,  # type: ignore[arg-type]
            )

    def test_non_display_metadata_raises_value_error(self) -> None:
        registry = ToolRegistry()

        with pytest.raises(ValueError, match="display"):
            registry.register(
                "read_file",
                "Read a UTF-8 text file from the workspace.",
                READ_FILE_SCHEMA,
                read_file_handler,
                display=object(),  # type: ignore[arg-type]
            )


class TestToolRegistryAllowlistFiltering:
    def test_empty_registry_lists_no_tools(self) -> None:
        registry = ToolRegistry()

        assert registry.list_tools(["*"]) == []

    def test_none_allowlist_returns_all_tools_sorted(self) -> None:
        registry = ToolRegistry()
        register_write_file(registry)
        register_read_file(registry)

        tools = registry.list_tools()

        assert [tool.name for tool in tools] == ["read_file", "write_file"]

    def test_wildcard_allowlist_returns_all_tools_sorted(self) -> None:
        registry = ToolRegistry()
        register_write_file(registry)
        register_read_file(registry)

        tools = registry.list_tools(["*"])

        assert [tool.name for tool in tools] == ["read_file", "write_file"]

    def test_empty_allowlist_returns_no_tools(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        assert registry.list_tools([]) == []

    def test_explicit_allowlist_returns_matching_tools_only(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)
        register_write_file(registry)

        tools = registry.list_tools(["write_file"])

        assert [tool.name for tool in tools] == ["write_file"]

    def test_unknown_allowlisted_tool_is_ignored_for_exposure(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        tools = registry.list_tools(["missing_tool"])

        assert tools == []


def test_registry_preserves_declarative_tool_relationship_metadata() -> None:
    registry = ToolRegistry()

    tool = registry.register(
        "session_read",
        "Read a Session.",
        READ_FILE_SCHEMA,
        read_file_handler,
        family="sessions",
        activation="follows",
        activation_source="session_search",
        constraints=("identity_agent",),
    )

    assert tool.family == "sessions"
    assert tool.activation == "follows"
    assert tool.activation_source == "session_search"
    assert tool.constraints == ("identity_agent",)


def test_registry_owns_family_labels_and_rejects_unknown_membership() -> None:
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

    with pytest.raises(ValueError, match="not registered"):
        registry.register(
            "weather_tomorrow",
            "Read tomorrow's forecast.",
            READ_FILE_SCHEMA,
            read_file_handler,
            family="extension:weather:missing",
        )


def test_registry_never_unregisters_builtin_family_metadata() -> None:
    registry = ToolRegistry()

    registry.unregister_family("files")

    assert registry.get_family("files").label == "Files"


def test_registry_rejects_invalid_activation_metadata() -> None:
    registry = ToolRegistry()

    with pytest.raises(ValueError, match="activation"):
        registry.register(
            "read_file",
            "Read a file.",
            READ_FILE_SCHEMA,
            read_file_handler,
            activation="mystery",
        )


class TestToolRegistryDefinitions:
    def test_provider_definitions_include_schema_for_allowed_tools(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)
        register_write_file(registry)

        definitions = registry.provider_definitions(["read_file"])

        assert definitions == [
            {
                "name": "read_file",
                "description": "Read a UTF-8 text file from the workspace.",
                "parameters": READ_FILE_SCHEMA,
            }
        ]

    def test_provider_definitions_do_not_expose_handler_or_context(self) -> None:
        registry = ToolRegistry()
        registry.register(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            parameters=READ_FILE_SCHEMA,
            handler=read_file_handler,
            display=ToolDisplay(summary_fields=("path",)),
        )

        definition = registry.provider_definitions(["read_file"])[0]

        assert set(definition) == {"name", "description", "parameters"}

    def test_provider_definitions_copy_schema(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        definitions = registry.provider_definitions(["read_file"])
        definitions[0]["parameters"]["type"] = "array"

        assert registry.get("read_file").parameters["type"] == "object"

    def test_prompt_definitions_include_name_and_description_only(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        definitions = registry.prompt_definitions(["read_file"])

        assert definitions == [
            {
                "name": "read_file",
                "description": "Read a UTF-8 text file from the workspace.",
            }
        ]

    def test_configuration_profile_is_stable_and_shared_by_provider_and_prompt(self) -> None:
        registry = ToolRegistry()

        def resolve(context: ToolDefinitionProfileContext) -> ToolDefinitionProfile:
            return ToolDefinitionProfile(
                key=f"agent:{context.agent_id}:readme-only",
                description="Read this Agent's README file.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "enum": ["README.md"],
                        }
                    },
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

        first = registry.provider_definitions(["read_file"], profile_context=context)
        second = registry.provider_definitions(["read_file"], profile_context=context)
        prompt = registry.prompt_definitions(["read_file"], profile_context=context)

        assert first == second
        assert first[0]["parameters"]["properties"]["path"]["enum"] == ["README.md"]
        assert prompt == [
            {
                "name": "read_file",
                "description": "Read this Agent's README file.",
            }
        ]
        first[0]["parameters"]["properties"]["path"]["enum"].append("SECRET.md")
        assert registry.provider_definitions(
            ["read_file"],
            profile_context=context,
        )[0]["parameters"]["properties"]["path"]["enum"] == ["README.md"]

    def test_configuration_profile_can_hide_tool_for_one_agent(self) -> None:
        registry = ToolRegistry()
        registry.register(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            parameters=READ_FILE_SCHEMA,
            handler=read_file_handler,
            definition_profile_resolver=lambda context: (
                ToolDefinitionProfile(
                    key="enabled",
                    description="Read a UTF-8 text file from the workspace.",
                    parameters=READ_FILE_SCHEMA,
                )
                if context.agent_id == "enabled-agent"
                else None
            ),
        )

        assert (
            registry.provider_definitions(
                ["read_file"],
                profile_context=ToolDefinitionProfileContext(agent_id="disabled-agent"),
            )
            == []
        )
        assert (
            registry.prompt_definitions(
                ["read_file"],
                profile_context=ToolDefinitionProfileContext(agent_id="disabled-agent"),
            )
            == []
        )

    def test_empty_allowlist_omits_tools_from_both_definition_sets(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        assert registry.provider_definitions([]) == []
        assert registry.prompt_definitions([]) == []

    def test_session_scoped_tool_requires_grant_and_overrides_allowlist(self) -> None:
        registry = ToolRegistry()
        registry.register(
            name="history",
            description="Read this Session's earlier original records.",
            parameters={"type": "object"},
            handler=read_file_handler,
            session_scoped=True,
        )

        assert registry.provider_definitions(["*"]) == []
        assert registry.prompt_definitions([]) == []
        assert [
            definition["name"]
            for definition in registry.provider_definitions([], session_grants=["history"])
        ] == ["history"]
        assert [
            definition["name"]
            for definition in registry.prompt_definitions([], session_grants=["history"])
        ] == ["history"]

    def test_configurable_listing_can_exclude_session_scoped_tools(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)
        registry.register(
            name="history",
            description="Read this Session's earlier original records.",
            parameters={"type": "object"},
            handler=read_file_handler,
            session_scoped=True,
        )

        assert [tool.name for tool in registry.list_tools()] == ["history", "read_file"]
        assert [tool.name for tool in registry.list_tools(include_session_scoped=False)] == [
            "read_file"
        ]


class TestToolReadiness:
    def test_tool_without_predicate_is_ready(self) -> None:
        registry = ToolRegistry()
        tool = register_read_file(registry)

        assert tool.ready is None
        assert tool_is_ready(tool) is True

    def test_not_ready_tool_hidden_from_model_facing_surfaces_but_not_list_tools(self) -> None:
        registry = ToolRegistry()
        registry.register(
            name="gated",
            description="A gated tool.",
            parameters={"type": "object"},
            handler=read_file_handler,
            ready=lambda: False,
        )

        # Registered and visible in a plain list, but filtered from the
        # model-facing surfaces (which default to ready_only=True).
        assert [tool.name for tool in registry.list_tools()] == ["gated"]
        assert registry.list_tools(ready_only=True) == []
        assert registry.provider_definitions(["gated"]) == []
        assert registry.prompt_definitions(["gated"]) == []

    def test_ready_predicate_true_keeps_tool_visible(self) -> None:
        registry = ToolRegistry()
        registry.register(
            name="gated",
            description="A gated tool.",
            parameters={"type": "object"},
            handler=read_file_handler,
            ready=lambda: True,
        )

        assert [tool.name for tool in registry.list_tools(ready_only=True)] == ["gated"]
        assert [definition["name"] for definition in registry.provider_definitions(["gated"])] == [
            "gated"
        ]

    def test_raising_predicate_counts_as_not_ready_and_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        registry = ToolRegistry()

        def boom() -> bool:
            raise RuntimeError("predicate exploded")

        tool = registry.register(
            name="gated",
            description="A gated tool.",
            parameters={"type": "object"},
            handler=read_file_handler,
            ready=boom,
        )

        with caplog.at_level(logging.WARNING):
            assert tool_is_ready(tool) is False

        assert registry.list_tools(ready_only=True) == []
        assert any("readiness predicate raised" in record.getMessage() for record in caplog.records)

    def test_register_rejects_non_callable_ready(self) -> None:
        registry = ToolRegistry()

        with pytest.raises(ValueError):
            registry.register(
                name="gated",
                description="A gated tool.",
                parameters={"type": "object"},
                handler=read_file_handler,
                ready="nope",  # type: ignore[arg-type]
            )

    def test_dispatch_of_not_ready_tool_returns_envelope_without_running_handler(self) -> None:
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

    def test_flipping_backing_state_makes_tool_reappear_without_reregistration(self) -> None:
        registry = ToolRegistry()
        token = {"value": ""}
        registry.register(
            name="gated",
            description="A gated tool.",
            parameters={"type": "object"},
            handler=read_file_handler,
            ready=lambda: bool(token["value"]),
        )

        assert registry.list_tools(ready_only=True) == []

        token["value"] = "present"

        assert [tool.name for tool in registry.list_tools(ready_only=True)] == ["gated"]
        assert [definition["name"] for definition in registry.provider_definitions(["gated"])] == [
            "gated"
        ]


class TestToolRegistryDispatch:
    @pytest.mark.asyncio
    async def test_session_scoped_dispatch_checks_grant_before_allowlist(self) -> None:
        registry = ToolRegistry()
        registry.register(
            name="history",
            description="Read this Session's earlier original records.",
            parameters={"type": "object"},
            handler=lambda _context, _arguments: tool_success({}),
            session_scoped=True,
        )
        executor = ToolExecutor(registry)

        unavailable = await executor.execute_many(
            [ToolCall(id="call-1", name="history", arguments={})],
            make_execution_config(allowed_tools=[]),
        )
        denied = await executor.execute_many(
            [ToolCall(id="call-2", name="history", arguments={})],
            replace(
                make_execution_config(allowed_tools=[]),
                session_tool_grants=("history",),
            ),
        )
        granted = await executor.execute_many(
            [ToolCall(id="call-3", name="history", arguments={})],
            replace(
                make_execution_config(allowed_tools=["history"]),
                session_tool_grants=("history",),
            ),
        )

        assert unavailable[0]["error"]["code"] == "history_unavailable"
        assert denied[0]["error"]["code"] == "tool_not_allowed"
        assert granted[0]["ok"] is True

    def test_display_for_call_uses_registered_tool_display(self) -> None:
        registry = ToolRegistry()
        registry.register(
            name="write_file",
            description="Write UTF-8 text to a workspace file.",
            parameters=WRITE_FILE_SCHEMA,
            handler=write_file_handler,
            display=ToolDisplay(summary_fields=("path",), hidden_argument_keys=("content",)),
        )

        payload = registry.display_for_call(
            "write_file",
            {"path": "notes.md", "content": "large body"},
        )

        assert payload == {
            "version": 1,
            "summary": "notes.md",
            "hidden_argument_keys": ["content"],
            "primary": [
                {
                    "kind": "text",
                    "value": "notes.md",
                    "full_value": "notes.md",
                    "truncate": "end",
                    "tooltip": "truncated",
                    "max_characters": 64,
                    "quote": False,
                    "copyable": False,
                }
            ],
            "facts": [],
        }

    @pytest.mark.asyncio
    async def test_dispatch_passes_context_to_sync_handler(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        result = await registry.dispatch(make_context(), {"path": "SOUL.md"}, ["*"])

        assert result == tool_success({"content": "read SOUL.md", "tool_call_id": "call_1"})

    @pytest.mark.asyncio
    async def test_dispatch_async_handler(self) -> None:
        registry = ToolRegistry()
        register_write_file(registry)

        result = await registry.dispatch(
            make_context("write_file"),
            {"path": "SOUL.md", "content": "hello"},
            ["write_file"],
        )

        assert result == tool_success({"written": "SOUL.md", "bytes": 5, "workspace": "workspace"})

    @pytest.mark.asyncio
    async def test_dispatch_runs_sync_handler_on_event_loop_thread(self) -> None:
        registry = ToolRegistry()
        loop_thread_id = threading.get_ident()
        seen_thread_ids: list[int] = []

        def sync_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            seen_thread_ids.append(threading.get_ident())
            return tool_success({"thread_id": seen_thread_ids[-1]})

        registry.register(
            "sync_tool",
            "Run a sync handler and return its thread id.",
            {"type": "object"},
            sync_handler,
        )

        result = await registry.dispatch(make_context("sync_tool"), {}, ["*"])

        assert seen_thread_ids == [loop_thread_id]
        assert result == tool_success({"thread_id": loop_thread_id})

    @pytest.mark.asyncio
    async def test_dispatch_non_envelope_result_raises_value_error(self) -> None:
        registry = ToolRegistry()

        def invalid_handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
            return {"content": "not enveloped"}

        registry.register(
            "invalid_tool",
            "Return an invalid result for testing.",
            {"type": "object"},
            invalid_handler,
        )

        with pytest.raises(ValueError, match="envelope"):
            await registry.dispatch(make_context("invalid_tool"), {}, ["*"])

    @pytest.mark.asyncio
    async def test_internal_tool_dispatch_ignores_empty_allowlist(self) -> None:
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
    async def test_empty_allowlist_still_blocks_normal_tool_dispatch(self) -> None:
        registry = ToolRegistry()
        register_read_file(registry)

        with pytest.raises(ToolNotAllowedError):
            await registry.dispatch(make_context("read_file"), {"path": "SOUL.md"}, [])
