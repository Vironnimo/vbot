"""Extension capability surfaces: Tools, Commands, recall backends, interaction
prefixes and prompt blocks.

Each capability is declared through ``register(api)``, loaded from the real
filesystem and applied to its live owner (``ToolRegistry``, ``CommandDispatcher``,
``RecallBackendRegistry``, channel interaction dispatch, prompt definitions).
Collisions follow one policy: a built-in wins, between Extensions the first
declarer wins, both sides are diagnosed, and a skipped capability never fails
its Extension.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import (
    CommandDispatcher,
    CommandExecutionContext,
    CommandOutcome,
    ReplySurface,
)
from core.database import write_bootstrap_marker
from core.extensions import ExtensionRegistry
from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from core.recall.recall import RecallBackendContext, RecallBackendRegistry
from core.runs import ChatRunManager
from core.sessions import ChatSessionManager
from core.tools import ToolContractError, ToolRegistry
from tests.core.extensions.extension_test_support import (
    RecordingResponder,
    command_source,
    interaction_source,
    record,
    tap,
    tool_context,
    tool_source,
    write_extension,
)


def _recall_context(tmp_path: Path) -> RecallBackendContext:
    if not (tmp_path / "data-store.json").exists():
        write_bootstrap_marker(tmp_path)
    return RecallBackendContext(data_dir=tmp_path, sessions=ChatSessionManager(tmp_path))


def _loaded_tools(root: Path, *builtins: str) -> tuple[ExtensionRegistry, ToolRegistry]:
    """Load *root* and apply its Tools over a registry that already owns *builtins*."""
    registry = ExtensionRegistry.load(root)
    tools = ToolRegistry()
    for name in builtins:
        tools.register(name, f"builtin {name}", {"type": "object"}, lambda context, args: {})
    registry.apply_tools(tools)
    return registry, tools


# --- Tools -------------------------------------------------------------------


def test_extension_tools_dispatch_off_the_loop_and_obey_allowlists(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    write_extension(
        root,
        "echo_ext",
        "import threading\n"
        "from core.tools import tool_success\n"
        "def _handler(context, arguments):\n"
        "    return tool_success({'value': arguments.get('value'),"
        " 'thread_id': threading.get_ident()})\n"
        "def register(api):\n"
        "    api.register_tool('ext_echo', 'desc', {'type': 'object'}, _handler)\n",
    )
    registry, tools = _loaded_tools(root)

    result = asyncio.run(tools.dispatch(tool_context("ext_echo", tmp_path), {"value": "hi"}))

    assert result["ok"] is True
    assert result["data"]["value"] == "hi"
    # Sync handlers run on a worker thread, never on the event loop thread.
    assert result["data"]["thread_id"] != threading.get_ident()
    assert tools.get("ext_echo").parallel_safe is True
    assert [tool.name for tool in tools.list_tools(allowed_tools=["ext_echo"])] == ["ext_echo"]
    assert tools.list_tools(allowed_tools=[]) == []
    assert record(registry, "echo_ext").capability_errors == []


def test_extension_tool_families_are_namespaced_and_removed_with_their_tools(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_extension(
        root,
        "weather",
        "from core.tools import tool_success\n"
        "def _handler(context, arguments):\n"
        "    return tool_success({})\n"
        "def register(api):\n"
        "    api.register_tool_family('forecast', 'Weather Forecast')\n"
        "    api.register_tool('weather_today', 'Today.', {'type': 'object'}, "
        "_handler, family='forecast')\n"
        "    api.register_tool('weather_week', 'Week.', {'type': 'object'}, "
        "_handler, family='forecast')\n"
        "    api.register_tool('weather_alerts', 'Alerts.', {'type': 'object'}, "
        "_handler, family='missing')\n",
    )
    registry, tools = _loaded_tools(root)

    family_id = "extension:weather:forecast"
    assert tools.get("weather_today").family == family_id
    assert tools.get("weather_week").family == family_id
    assert tools.get("weather_today").family_label == "Weather Forecast"
    assert tools.get_family(family_id).extension == "weather"
    # A Tool naming an undeclared family stays standalone and is diagnosed.
    assert tools.get("weather_alerts").family is None
    errors = record(registry, "weather").capability_errors
    assert len(errors) == 1
    assert "undeclared tool family" in errors[0]

    registry.remove_applied_tools(tools)

    assert tools.list_tools() == []
    with pytest.raises(ValueError, match="not found"):
        tools.get_family(family_id)


def test_extension_tool_schemas_can_be_open_or_accept_unknown_arguments(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    declared = "{'type': 'object', 'properties': {'value': {'type': 'string'}}"
    write_extension(
        root,
        "open_ext",
        "from core.tools import tool_success\n"
        "def _handler(context, arguments):\n"
        "    return tool_success({'arguments': arguments})\n"
        "def register(api):\n"
        f"    api.register_tool('ext_open', 'desc', {declared}}}, "
        "_handler, open_input_schema=True)\n"
        f"    api.register_tool('ext_any', 'desc', {declared}, 'additionalProperties': True}}, "
        "_handler, open_input_schema=True)\n",
    )
    _registry, tools = _loaded_tools(root)

    def dispatch(name: str, arguments: dict[str, Any]) -> Any:
        return asyncio.run(tools.dispatch(tool_context(name, tmp_path), arguments))

    assert tools.get("ext_open").open_input_schema is True
    assert "additionalProperties" not in tools.get("ext_open").parameters
    assert dispatch("ext_open", {"value": "declared"})["data"] == {
        "arguments": {"value": "declared"}
    }
    # The declared properties are the complete parameter list...
    with pytest.raises(ToolContractError, match='"unknown" is not a parameter'):
        dispatch("ext_open", {"unknown": "rejected"})
    # ...unless the schema explicitly admits additional properties.
    assert dispatch("ext_any", {"unknown": "preserved"})["data"] == {
        "arguments": {"unknown": "preserved"}
    }


def test_not_ready_extension_tools_stay_registered_but_hidden_from_providers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_extension(
        root,
        "gated_ext",
        "from core.tools import tool_success\n"
        "def _handler(context, arguments):\n"
        "    return tool_success({})\n"
        "def register(api):\n"
        "    api.register_tool('ext_ready', 'desc', {'type': 'object'}, _handler, "
        "ready=lambda: True)\n"
        "    api.register_tool('ext_gated', 'desc', {'type': 'object'}, _handler, "
        "ready=lambda: False)\n",
    )
    _registry, tools = _loaded_tools(root)

    ready = tools.get("ext_ready").ready
    assert ready is not None and ready() is True
    assert {tool.name for tool in tools.list_tools()} == {"ext_ready", "ext_gated"}
    definitions = tools.provider_definitions(["ext_ready", "ext_gated"])
    assert [definition["name"] for definition in definitions] == ["ext_ready"]


def test_skipped_extension_tools_are_diagnosed_without_failing_the_extension(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    # Load order is sorted by name: alpha applies before zeta.
    write_extension(
        root,
        "alpha",
        "from core.tools import tool_success\n"
        "def _handler(context, arguments):\n"
        "    return tool_success({'marker': 'from-alpha'})\n"
        "def register(api):\n"
        "    api.register_tool('dup', 'desc', {'type': 'object'}, _handler)\n"
        "    api.register_tool('read', 'shadow', {'type': 'object'}, _handler)\n"
        "    api.register_tool('broken', 'Broken.', "
        "{'type': 'object', 'properties': {'value': {'type': 'string'}}}, _handler)\n"
        "    api.register_tool('healthy', 'Healthy.', "
        "{'type': 'object', 'additionalProperties': False}, _handler)\n",
    )
    write_extension(root, "zeta", tool_source("dup", "from-zeta"))
    registry, tools = _loaded_tools(root, "read")

    result = asyncio.run(tools.dispatch(tool_context("dup", tmp_path), {}))

    # The built-in keeps its name and the first-declaring Extension wins the rest.
    assert tools.get("read").description == "builtin read"
    assert result["data"]["marker"] == "from-alpha"
    assert {tool.name for tool in tools.list_tools()} == {"read", "dup", "healthy"}
    alpha, zeta = record(registry, "alpha"), record(registry, "zeta")
    assert any("zeta" in message for message in alpha.capability_errors)
    assert any("read" in message and "built-in" in message for message in alpha.capability_errors)
    assert any(
        "broken" in message and "registration failed" in message
        for message in alpha.capability_errors
    )
    assert any("alpha" in message and "skipped" in message for message in zeta.capability_errors)
    assert (alpha.status, zeta.status) == ("loaded", "loaded")
    assert registry.diagnostics() == []


@pytest.mark.asyncio
async def test_extension_opt_in_is_enforced_in_definitions_and_dispatch(tmp_path):
    from core.tools.availability import ToolAccess, resolve_tool_access
    from core.tools.tools import ToolNotAllowedError

    root = tmp_path / "extensions"
    source = tool_source("computer_test", "called")
    source = source.replace("}, _handler)", "}, _handler, requires_opt_in=True)")
    write_extension(root, "opt_in", source)
    _registry, tools = _loaded_tools(root)
    tool = tools.get("computer_test")
    assert tool.requires_opt_in
    context = tool_context(tool.name, tmp_path)
    for policy in (ToolAccess(), ToolAccess(mode="selected", allowed=(tool.name,))):
        allowed = resolve_tool_access(policy, tools.list_tools(), "off").allowed_tools
        assert tools.provider_definitions(allowed_tools=allowed) == []
        assert tools.prompt_definitions(allowed_tools=allowed) == []
        with pytest.raises(ToolNotAllowedError):
            await tools.dispatch(context, {}, allowed_tools=allowed)
    assert tools.provider_definitions() == []
    with pytest.raises(ToolNotAllowedError):
        await tools.dispatch(context, {})
    allowed = resolve_tool_access(
        ToolAccess(granted=(tool.name,)), tools.list_tools(), "off"
    ).allowed_tools
    assert [
        definition["name"] for definition in tools.provider_definitions(allowed_tools=allowed)
    ] == [tool.name]
    assert (await tools.dispatch(context, {}, allowed_tools=allowed))["data"]["marker"] == "called"


# --- Commands ----------------------------------------------------------------


def test_extension_commands_dispatch_and_skipped_commands_are_diagnosed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_extension(root, "a_first", command_source("workflow", "first"))
    write_extension(root, "b_second", command_source("workflow", "second"))
    write_extension(
        root,
        "c_invalid",
        "def _handler(context, argument):\n"
        "    return None\n"
        "def register(api):\n"
        "    api.register_command('help', 'shadow', _handler)\n"
        "    api.register_command('Bad Name', 'bad', _handler)\n"
        "    api.register_command(['listed'], 'unhashable', _handler)\n",
    )
    registry = ExtensionRegistry.load(root)
    dispatcher = CommandDispatcher(ChatRunManager())
    registry.apply_commands(dispatcher)

    prepared = dispatcher.prepare("/workflow inspect")
    assert prepared is not None
    result = asyncio.run(
        dispatcher.execute(
            prepared,
            CommandExecutionContext(
                agent_id="a",
                session_id="s",
                project_id=None,
                reply_surface=ReplySurface.webui(),
            ),
        )
    )

    assert isinstance(result, CommandOutcome)
    assert result.feedback is not None
    assert result.feedback.text == "first:inspect"
    assert dispatcher.extension_command_owner("workflow") == "a_first"
    assert any("b_second" in message for message in record(registry, "a_first").capability_errors)
    assert any("a_first" in message for message in record(registry, "b_second").capability_errors)
    invalid = record(registry, "c_invalid")
    assert invalid.status == "loaded"
    assert dispatcher.extension_command_owner("help") is None
    assert dispatcher.extension_command_owner("Bad Name") is None
    assert any("Built-in Command" in message for message in invalid.capability_errors)
    assert any("lowercase" in message for message in invalid.capability_errors)
    assert any("name must be a string" in message for message in invalid.capability_errors)


# --- Recall backends ---------------------------------------------------------


def test_extension_recall_backends_become_selectable_unless_taken_or_invalid(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_extension(
        root,
        "recall_ext",
        "class ExtBackend:\n"
        "    def __init__(self, context):\n"
        "        self.context = context\n"
        "    def search_capabilities(self):\n"
        "        from core.recall import RecallSearchCapabilities\n"
        "        return RecallSearchCapabilities('message', 'Extension search.')\n"
        "    async def search_page(self, request):\n"
        "        from core.recall import RecallSearchPage\n"
        "        return RecallSearchPage((), 'message', 'extension', 'snapshot', False, 0)\n"
        "def register(api):\n"
        "    for name in ('my_backend', 'canonical_scan', 'Bad_Name'):\n"
        "        api.register_recall_backend(name, ExtBackend)\n",
    )
    registry = ExtensionRegistry.load(root)
    recall_registry = RecallBackendRegistry.with_builtins()

    registry.apply_recall_backends(recall_registry)

    backend = recall_registry.create("my_backend", _recall_context(tmp_path))
    assert asyncio.run(backend.search_page(cast(Any, object()))).ranking == "extension"
    assert "Bad_Name" not in recall_registry.names()
    errors = record(registry, "recall_ext").capability_errors
    # The built-in canonical_scan keeps its name; both skipped names are diagnosed.
    assert len(errors) == 2
    assert any("canonical_scan" in message for message in errors)
    assert any("Bad_Name" in message and "snake_case" in message for message in errors)


# --- Interaction prefixes ----------------------------------------------------


def test_interaction_prefixes_route_to_the_first_declarer_and_never_claim_reserved_ones(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    # Load order is sorted by name: alpha applies before zeta.
    write_extension(root, "alpha", interaction_source("chk", "alpha"))
    write_extension(root, "hijacker", interaction_source("run", "hijacker"))
    write_extension(root, "zeta", interaction_source("chk", "zeta"))
    registry = ExtensionRegistry.load(root)

    chk, run = RecordingResponder(), RecordingResponder()
    assert asyncio.run(registry.dispatch_channel_interaction(tap("chk:milk"), chk)) is True
    # The runtime routes reserved prefixes itself; no Extension handler is wired.
    assert asyncio.run(registry.dispatch_channel_interaction(tap("run:go"), run)) is False

    assert (chk.answers, run.answers) == (["alpha"], [])
    alpha, zeta = record(registry, "alpha"), record(registry, "zeta")
    assert any("zeta" in message for message in alpha.capability_errors)
    assert any("alpha" in message and "skipped" in message for message in zeta.capability_errors)
    hijacker = record(registry, "hijacker")
    # Registration stays permissive: the declaration lands but is never applied.
    assert [item.prefix for item in hijacker.declarations.interaction_handlers] == ["run"]
    assert any("reserved" in message for message in hijacker.capability_errors)
    assert registry.diagnostics() == []


# --- Prompt blocks -----------------------------------------------------------


def test_prompt_blocks_become_definitions_from_loaded_extensions_only(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    write_extension(
        root,
        "ext_a",
        "def register(api):\n"
        "    api.register_prompt_block('static', default_text='Hello.')\n"
        "    api.register_prompt_block('dynamic', render=lambda ctx: 'Rendered.')\n"
        "    api.register_prompt_block('shared', default_text='A wins.')\n"
        "    api.register_prompt_block('family', default_text='Guide.', requires_tool='fam_*')\n",
    )
    write_extension(
        root,
        "ext_b",
        "def register(api):\n    api.register_prompt_block('shared', default_text='B loses.')\n",
    )
    write_extension(
        root,
        "ext_failed",
        "def register(api):\n"
        "    api.register_prompt_block('failed', default_text='Hidden.')\n"
        "    raise RuntimeError('register boom')\n",
    )
    write_extension(
        root,
        "ext_disabled",
        "def register(api):\n    api.register_prompt_block('disabled', default_text='Hidden.')\n",
    )
    registry = ExtensionRegistry.load(root, disabled={"ext_disabled"})

    definitions = {definition.id: definition for definition in registry.prompt_block_declarations()}

    assert set(definitions) == {
        "extension:static",
        "extension:dynamic",
        "extension:shared",
        "extension:family",
    }
    # requires_tool swaps the loaded-extension owner for the Tool-list gate.
    assert definitions["extension:family"].owner == "tool:fam_*"
    static, dynamic = definitions["extension:static"], definitions["extension:dynamic"]
    assert (static.owner, static.default_text, static.editable) == (
        "extension:ext_a",
        "Hello.",
        True,
    )
    assert (dynamic.owner, dynamic.render is not None, dynamic.editable) == (
        "extension:ext_a",
        True,
        False,
    )
    # A shared slug belongs to the first-loaded Extension; both sides are diagnosed.
    shared = definitions["extension:shared"]
    assert (shared.owner, shared.default_text) == ("extension:ext_a", "A wins.")
    assert any(
        "also declared" in message for message in record(registry, "ext_a").capability_errors
    )
    assert any("skipped" in message for message in record(registry, "ext_b").capability_errors)
    assert registry.loaded_extension_names() == {"ext_a", "ext_b"}

    api = ExtensionAPI("ext", ExtensionDeclarations(), config={}, logger=None)
    with pytest.raises(ValueError, match="exactly one"):
        api.register_prompt_block("both", default_text="x", render=lambda ctx: "y")
    with pytest.raises(ValueError, match="exactly one"):
        api.register_prompt_block("neither")
    for pattern in ("", "mcp_*x", "1mcp", "mcp:x"):
        with pytest.raises(ValueError, match="requires_tool"):
            api.register_prompt_block("gated", default_text="x", requires_tool=pattern)
