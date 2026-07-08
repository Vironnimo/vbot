"""Tests for live extension deactivation (disable applied without a restart).

Covers ``ExtensionRegistry.deactivate``: a currently
``loaded`` extension's hooks stop firing, its applied tools are unregistered
(and vanish from the model-facing provider definitions), its shutdown handlers
fire once, and its record flips to ``disabled`` with cleared declarations — while
a name that is unknown / already disabled / not loaded is a clean no-op. A tool
whose name was skipped on a collision is never yanked from its real owner.
Extensions are loaded through the real filesystem loader so the whole
declare → apply → deactivate path is exercised.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.extensions import ExtensionRegistry, HookContext, InteractionEvent
from core.tools import ToolContext, ToolRegistry
from core.tools.tools import ToolNotFoundError


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace after each test."""
    yield
    for module_name in list(sys.modules):
        if module_name == "vbot_ext" or module_name.startswith("vbot_ext."):
            del sys.modules[module_name]


def _write_single_file(root: Path, name: str, source: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.py").write_text(source, encoding="utf-8")


def _record(registry: ExtensionRegistry, name: str):
    return next(record for record in registry.records() if record.name == name)


def _hook_extension_source(marker: Path) -> str:
    """Extension whose run_start hook records that it fired to *marker*."""
    return (
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "def _handler(ctx, **payload):\n"
        "    with _MARKER.open('a', encoding='utf-8') as fh:\n"
        "        fh.write('fired\\n')\n"
        "def register(api):\n"
        "    api.on('run_start', _handler)\n"
    )


def _tool_extension_source(tool_name: str) -> str:
    return (
        "from core.tools import tool_success\n"
        "def _handler(context, arguments):\n"
        "    return tool_success({'value': arguments.get('value')})\n"
        "def register(api):\n"
        f"    api.register_tool({tool_name!r}, 'desc', {{'type': 'object'}}, _handler)\n"
    )


def _shutdown_extension_source(marker: Path) -> str:
    return (
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "def register(api):\n"
        "    def _shutdown():\n"
        "        with _MARKER.open('a', encoding='utf-8') as fh:\n"
        "            fh.write('shutdown\\n')\n"
        "    api.on_shutdown(_shutdown)\n"
    )


def _tool_context(tool_name: str, tmp_path: Path) -> ToolContext:
    return ToolContext(
        agent_id="a",
        session_id="s",
        run_id="r",
        tool_call_id="c1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=tmp_path,
        app_root=tmp_path,
        data_root=tmp_path,
    )


def test_deactivate_stops_hooks_from_firing(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "hooks.txt"
    _write_single_file(root, "hooky", _hook_extension_source(marker))

    registry = ExtensionRegistry.load(root)
    ctx = HookContext(session_id="s", agent_id="a", run_id="r")

    asyncio.run(registry.dispatch_run_start(ctx, session_id="s", agent_id="a"))
    assert marker.read_text(encoding="utf-8").split() == ["fired"]

    assert asyncio.run(registry.deactivate("hooky")) is True

    # After deactivation the hook no longer fires.
    asyncio.run(registry.dispatch_run_start(ctx, session_id="s", agent_id="a"))
    assert marker.read_text(encoding="utf-8").split() == ["fired"]


def test_deactivate_unregisters_tools_and_hides_from_provider_definitions(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "tooly", _tool_extension_source("ext_echo"))

    registry = ExtensionRegistry.load(root)
    tool_registry = ToolRegistry()
    registry.apply_tools(tool_registry)

    assert [tool.name for tool in tool_registry.list_tools()] == ["ext_echo"]
    assert tool_registry.provider_definitions(["ext_echo"]) != []

    assert asyncio.run(registry.deactivate("tooly", tool_registry)) is True

    assert [tool.name for tool in tool_registry.list_tools()] == []
    assert tool_registry.provider_definitions(["ext_echo"]) == []


def test_deactivate_dispatch_of_removed_tool_degrades_cleanly(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "tooly", _tool_extension_source("ext_echo"))

    registry = ExtensionRegistry.load(root)
    tool_registry = ToolRegistry()
    registry.apply_tools(tool_registry)
    asyncio.run(registry.deactivate("tooly", tool_registry))

    # A run mid-flight that still references the just-unregistered tool hits the
    # registry's normal unknown-tool path (ToolNotFoundError), which the chat
    # executor maps to a clean ``tool_not_found`` envelope — no partial state, no
    # crash into the run.
    context = _tool_context("ext_echo", tmp_path)
    with pytest.raises(ToolNotFoundError):
        asyncio.run(tool_registry.dispatch(context, {"value": "hi"}))


def test_deactivate_fires_shutdown_once(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "shutdown.txt"
    _write_single_file(root, "closer", _shutdown_extension_source(marker))

    registry = ExtensionRegistry.load(root)

    assert asyncio.run(registry.deactivate("closer")) is True
    assert marker.read_text(encoding="utf-8").split() == ["shutdown"]

    # A second deactivate is a no-op: shutdown does not fire again.
    assert asyncio.run(registry.deactivate("closer")) is False
    assert marker.read_text(encoding="utf-8").split() == ["shutdown"]


def test_deactivate_marks_record_disabled_and_clears_declarations(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "tooly", _tool_extension_source("ext_echo"))

    registry = ExtensionRegistry.load(root)
    tool_registry = ToolRegistry()
    registry.apply_tools(tool_registry)
    asyncio.run(registry.deactivate("tooly", tool_registry))

    record = _record(registry, "tooly")
    assert record.status == "disabled"
    assert record.declarations.tools == []
    assert record.declarations.hooks == {}
    # No longer counted as a loaded extension (gate 2 / prompt refresh drop it).
    assert "tooly" not in registry.loaded_extension_names()


def test_deactivate_unknown_name_is_noop(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "tooly", _tool_extension_source("ext_echo"))

    registry = ExtensionRegistry.load(root)

    assert asyncio.run(registry.deactivate("does_not_exist")) is False


def test_deactivate_already_disabled_is_noop(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "tooly", _tool_extension_source("ext_echo"))

    registry = ExtensionRegistry.load(root, disabled={"tooly"})

    # A boot-disabled extension was never imported; deactivating it does nothing.
    assert asyncio.run(registry.deactivate("tooly")) is False
    assert _record(registry, "tooly").status == "disabled"


class _RecordingResponder:
    """Minimal ``InteractionResponder`` that records whether it was answered."""

    def __init__(self) -> None:
        self.answered = False

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        self.answered = True

    async def edit(self, *, text: str | None = None, buttons: object = None) -> None:
        return None


def _tap_event(data: str) -> InteractionEvent:
    return InteractionEvent(
        platform="telegram",
        channel_id="chan",
        chat_id="1",
        user_id="2",
        message_id="3",
        data=data,
        buttons=(),
    )


def _interaction_extension_source(prefix: str) -> str:
    return (
        "async def _handler(event, responder):\n"
        "    await responder.answer()\n"
        "def register(api):\n"
        f"    api.register_interaction_handler({prefix!r}, _handler)\n"
    )


def test_deactivate_drops_interaction_prefixes(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    _write_single_file(root, "interactive", _interaction_extension_source("chk"))

    registry = ExtensionRegistry.load(root)

    before = _RecordingResponder()
    assert (
        asyncio.run(registry.dispatch_channel_interaction(_tap_event("chk:milk"), before)) is True
    )
    assert before.answered is True

    assert asyncio.run(registry.deactivate("interactive")) is True

    # After deactivation the prefix routes to no handler: unhandled, never answered.
    after = _RecordingResponder()
    assert (
        asyncio.run(registry.dispatch_channel_interaction(_tap_event("chk:milk"), after)) is False
    )
    assert after.answered is False


def test_deactivate_leaves_colliding_tool_with_its_real_owner(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    # This extension declares a tool named "read" that a built-in already owns, so
    # the extension's copy is skipped on apply. Deactivating the extension must not
    # unregister the built-in's "read".
    _write_single_file(root, "shadow", _tool_extension_source("read"))

    registry = ExtensionRegistry.load(root)
    tool_registry = ToolRegistry()

    def _builtin_read(context, arguments):
        return {"ok": True, "error": None, "data": {}, "artifacts": []}

    tool_registry.register("read", "builtin read", {"type": "object"}, _builtin_read)
    registry.apply_tools(tool_registry)

    asyncio.run(registry.deactivate("shadow", tool_registry))

    # The built-in "read" survives, still owned by the built-in handler.
    assert tool_registry.get("read").handler is _builtin_read
