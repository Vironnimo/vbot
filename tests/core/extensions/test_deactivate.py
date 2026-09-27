"""Live Extension deactivation: disable applied without a restart.

Covers ``ExtensionRegistry.deactivate``: a loaded Extension's hooks, Tools,
Commands and interaction prefixes stop working, its shutdown handlers fire once
and its record reads as disabled, while a name that is unknown or already
disabled is a clean no-op. Session owners are quiesced before capabilities go,
and a retired registration cannot hand out stale Session work.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

from core.chat import CommandDispatcher
from core.extensions import ExtensionRegistry
from core.extensions.extensions import SessionCapabilityExpiredError, SessionRequestContext
from core.extensions.operations import ExtensionHost
from core.runs import ChatRunManager
from core.tools import ToolRegistry
from core.tools.tools import ToolNotFoundError
from tests.core.extensions.extension_test_support import (
    RecordingResponder,
    fire_run_start,
    marker_lines,
    record,
    tap,
    tool_context,
    write_extension,
)

# A session-scoped owner whose quiesce and before_request wait on module-level gates.
_SESSION_OWNER_SOURCE = (
    "import asyncio\n"
    "entered = asyncio.Event()\n"
    "release = asyncio.Event()\n"
    "async def _gate():\n"
    "    entered.set()\n"
    "    await release.wait()\n"
    "async def _before_request(*_args):\n"
    "    from core.extensions.extensions import PreparedSessionDelivery\n"
    "    await _gate()\n"
    "    return PreparedSessionDelivery('receipt', 'hash', ('entry',), '1')\n"
    "def register(api):\n"
    "    api.register_session_tool('owned_tool', 'test-sentinel', {'type': 'object'},"
    " lambda *_: {})\n"
    "    api.register_session_runtime(\n"
    "        before_request=_before_request,\n"
    "        run_finished=lambda *_args, **_kwargs: None,\n"
    "        quiesce=_gate,\n"
    "    )\n"
)


def _live_extension_source(marker: Path) -> str:
    """Extension using every live effect deactivation must stop."""
    return (
        "import pathlib\n"
        "from core.chat import CommandOutcome\n"
        "from core.tools import tool_success\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "def _write(tag):\n"
        "    with _MARKER.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(tag + '\\n')\n"
        "def _tool(context, arguments):\n"
        "    return tool_success({})\n"
        "async def _tap(event, responder):\n"
        "    await responder.answer('live')\n"
        "def register(api):\n"
        "    api.on('run_start', lambda ctx, **payload: _write('fired'))\n"
        "    api.register_tool('ext_echo', 'desc', {'type': 'object'}, _tool)\n"
        # A built-in already owns "read": this copy is skipped and must stay unowned.
        "    api.register_tool('read', 'shadow', {'type': 'object'}, _tool)\n"
        "    api.register_command('workflow', 'desc', lambda context, argument: "
        "CommandOutcome(command='workflow'))\n"
        "    api.register_interaction_handler('chk', _tap)\n"
        "    api.on_shutdown(lambda: _write('shutdown'))\n"
    )


def test_deactivate_stops_every_live_effect_exactly_once(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "live.txt"
    write_extension(root, "live", _live_extension_source(marker))
    write_extension(root, "dormant", _live_extension_source(tmp_path / "dormant.txt"))
    registry = ExtensionRegistry.load(root, disabled={"dormant"})
    tools = ToolRegistry()

    def builtin_read(context, arguments):
        return {"ok": True, "error": None, "data": {}, "artifacts": []}

    tools.register("read", "builtin read", {"type": "object"}, builtin_read)
    registry.apply_tools(tools)
    dispatcher = CommandDispatcher(ChatRunManager())
    registry.apply_commands(dispatcher)
    before = RecordingResponder()
    fire_run_start(registry)
    assert asyncio.run(registry.dispatch_channel_interaction(tap("chk:milk"), before)) is True
    assert before.answers == ["live"]
    assert tools.provider_definitions(["ext_echo"]) != []
    assert dispatcher.prepare("/workflow") is not None

    assert asyncio.run(registry.deactivate("live", tools, dispatcher)) is True

    fire_run_start(registry)
    after = RecordingResponder()
    assert asyncio.run(registry.dispatch_channel_interaction(tap("chk:milk"), after)) is False
    assert after.answers == []
    assert [tool.name for tool in tools.list_tools()] == ["read"]
    assert tools.get("read").handler is builtin_read
    assert tools.provider_definitions(["ext_echo"]) == []
    # A run still referencing the removed Tool hits the normal unknown-Tool path.
    with pytest.raises(ToolNotFoundError):
        asyncio.run(tools.dispatch(tool_context("ext_echo", tmp_path), {}))
    assert dispatcher.prepare("/workflow") is None
    live = record(registry, "live")
    assert live.status == "disabled"
    assert (live.declarations.tools, live.declarations.hooks) == ([], {})
    assert "live" not in registry.loaded_extension_names()
    # Deactivating again, an unknown name or a boot-disabled one changes nothing.
    assert asyncio.run(registry.deactivate("live")) is False
    assert asyncio.run(registry.deactivate("does_not_exist")) is False
    assert asyncio.run(registry.deactivate("dormant")) is False
    assert record(registry, "dormant").status == "disabled"
    assert marker_lines(marker) == ["fired", "shutdown"]


@pytest.mark.asyncio
async def test_deactivate_quiesces_before_removing_capabilities_and_retires_host(
    tmp_path: Path,
) -> None:
    write_extension(tmp_path / "extensions", "owned", _SESSION_OWNER_SOURCE)
    registry = ExtensionRegistry.load(tmp_path / "extensions")
    tools = ToolRegistry()
    registry.apply_tools(tools)
    gates = sys.modules["vbot_ext.owned"]

    async def sample(*_args: object) -> dict[str, object]:
        return {}

    def host(**owner: Any) -> ExtensionHost:
        return ExtensionHost(
            data_dir=tmp_path,
            sample=sample,
            resolve_agent=lambda *_: None,
            resolve_tool_agent=lambda context: None,
            store_attachment=lambda *_: None,
            resolve_credential=lambda _: "",
            set_credential=lambda *_: None,
            **owner,
        )

    registry.bind_host(host(for_owner=lambda identity: host()))
    identity = registry.registration_identity("owned")
    registry.host_for(identity)

    deactivation = asyncio.create_task(registry.deactivate("owned", tools))
    await gates.entered.wait()
    assert tools.get("owned_tool").name == "owned_tool"

    gates.release.set()
    assert await deactivation is True
    with pytest.raises(ValueError, match="no longer current"):
        registry.host_for(identity)
    assert registry._owner_hosts == {}  # noqa: SLF001 - the cached owner host must be dropped
    with pytest.raises(ToolNotFoundError):
        tools.get("owned_tool")


@pytest.mark.asyncio
async def test_retired_session_before_request_cannot_return_stale_delivery(
    tmp_path: Path,
) -> None:
    write_extension(tmp_path / "extensions", "owned", _SESSION_OWNER_SOURCE)
    registry = ExtensionRegistry.load(tmp_path / "extensions")
    tools = ToolRegistry()
    registry.apply_tools(tools)
    gates = sys.modules["vbot_ext.owned"]
    binding = type(
        "Binding", (), {"owner_name": "owned", "config": {"tool_access": {"mode": "all"}}}
    )()
    context = SessionRequestContext(binding, "run", "agent", "session")

    pending = asyncio.create_task(registry.dispatch_session_before_request(binding, tools, context))
    await gates.entered.wait()
    registry.retire_registration()
    gates.release.set()

    with pytest.raises(SessionCapabilityExpiredError):
        await pending
    with pytest.raises(SessionCapabilityExpiredError):
        await registry.dispatch_session_before_request(binding, tools, context)
    with pytest.raises(SessionCapabilityExpiredError):
        await registry.reconcile_session_tool_batch(binding, tools, context, (), (), True)
    with pytest.raises(SessionCapabilityExpiredError):
        await registry.dispatch_session_run_finished(binding, tools, context, "success")
