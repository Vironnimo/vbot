"""``ExtensionRuntime``: serialized reload, disable and shutdown of the Extension layer.

Reload drains and retires the old registry before its capabilities are detached,
keeps it unreachable while the replacement loads, and installs the new layer.
Mutations stay serialized when their caller is cancelled and still report a late
failure exactly once; a closing runtime refuses new mutations.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from core.extensions.extensions import ExtensionRegistry, ExtensionUnavailableError
from core.extensions.operations import ExtensionHost
from core.extensions.runtime import ExtensionRuntime
from core.tools import ToolRegistry
from tests.core.extensions.extension_test_support import (
    RecordingResponder,
    hook_context,
    marker_lines,
    record,
    tap,
    write_extension,
    write_package,
)

_LOGGER_NAME = "vbot.extensions.runtime"


def _runtime(
    data_dir: Path,
    holder: dict[str, ExtensionRegistry],
    *,
    tools: ToolRegistry | None = None,
    reload_skills: Callable[[], Awaitable[None]] | None = None,
    make_host: Callable[[], ExtensionHost] | None = None,
    recover_recall: Callable[[set[str]], None] = lambda _names: None,
) -> ExtensionRuntime:
    return ExtensionRuntime(
        storage=cast(Any, SimpleNamespace(data_dir=data_dir, load_settings=lambda: {})),
        resources_path=data_dir / "resources",
        tools=tools or ToolRegistry(),
        get_registry=lambda: holder["registry"],
        set_registry=lambda registry: holder.__setitem__("registry", registry),
        get_command_dispatcher=lambda: None,
        extra_directories=lambda _settings: [],
        load_options=lambda _settings: (set(), {}),
        live_config=lambda _name: {},
        resolve_credential=lambda _key: "",
        reload_recall=lambda: None,
        refresh_prompts=lambda: None,
        reload_skills=reload_skills or AsyncMock(),
        recover_recall=recover_recall,
        logger=logging.getLogger(_LOGGER_NAME),
        make_host=make_host,
    )


def _owner_source(marker: Path) -> str:
    """A session owner with a Tool, hook, interaction prefix, page and lifecycle."""
    return (
        "import asyncio\n"
        "import pathlib\n"
        "from core.tools import tool_success\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "calls = []\n"
        "entered = asyncio.Event()\n"
        "release = asyncio.Event()\n"
        "def _write(tag):\n"
        "    with _MARKER.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(tag + '\\n')\n"
        "async def _quiesce():\n"
        "    entered.set()\n"
        "    await release.wait()\n"
        "def _tool(context, arguments):\n"
        "    return tool_success({})\n"
        "def register(api):\n"
        "    api.on('run_start', lambda ctx, **payload: calls.append('hook'))\n"
        "    api.register_interaction_handler(\n"
        "        'owned', lambda event, responder: calls.append('tap')\n"
        "    )\n"
        "    api.register_page('board', 'Board', 'index.html')\n"
        "    api.register_tool('ext_echo', 'desc', {'type': 'object'}, _tool)\n"
        # A built-in already owns "read": this copy is skipped and must stay unowned.
        "    api.register_tool('read', 'shadow', {'type': 'object'}, _tool)\n"
        "    api.register_session_tool('owned_tool', 'test-sentinel', {'type': 'object'},"
        " lambda *_: {})\n"
        "    api.register_session_runtime(\n"
        "        before_request=lambda *_: None,\n"
        "        run_finished=lambda *_args, **_kwargs: None,\n"
        "        quiesce=_quiesce,\n"
        "    )\n"
        "    api.on_startup(lambda: _write('startup'))\n"
        "    api.on_shutdown(lambda: _write('shutdown'))\n"
    )


@pytest.mark.asyncio
async def test_reload_retires_and_detaches_the_old_layer_before_installing_the_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    marker = tmp_path / "lifecycle.txt"
    package = write_package(data_dir / "extensions", "owned", _owner_source(marker))
    (package / "index.html").write_text("<!doctype html>", encoding="utf-8")
    tools = ToolRegistry()

    def builtin_read(context, arguments):
        return {"ok": True, "error": None, "data": {}, "artifacts": []}

    tools.register("read", "builtin read", {"type": "object"}, builtin_read)
    old = ExtensionRegistry.load(data_dir / "extensions")
    old.apply_tools(tools)
    hosts: list[object] = []

    def make_host() -> ExtensionHost:
        hosts.append(SimpleNamespace(for_owner=None, release_owner=None))
        return cast(ExtensionHost, hosts[-1])

    old.bind_host(make_host())
    identity = old.registration_identity("owned")
    old.host_for(identity)
    assert old.page_declarations() != []
    assert old.current_page(identity, "board") is not None
    old_module = sys.modules["vbot_ext.owned"]
    real_aload = ExtensionRegistry.aload
    loading, finish_loading = asyncio.Event(), asyncio.Event()

    async def gated_aload(cls: type[ExtensionRegistry], *args: Any, **kwargs: Any):
        loading.set()
        await finish_loading.wait()
        return await real_aload(*args, **kwargs)

    monkeypatch.setattr(ExtensionRegistry, "aload", classmethod(gated_aload))
    holder = {"registry": old}
    runtime = _runtime(data_dir, holder, tools=tools, make_host=make_host)

    reload_task = asyncio.create_task(runtime.reload())
    try:
        await old_module.entered.wait()
        # The owner is drained before any of its capabilities is detached.
        assert tools.get("owned_tool").name == "owned_tool"
        old_module.release.set()
        await loading.wait()

        # The old registry stays installed while the replacement loads, but no
        # longer reaches its Extension.
        assert holder["registry"] is old
        await old.dispatch_run_start(hook_context(), session_id="s", agent_id="a")
        assert (
            await old.dispatch_channel_interaction(tap("owned:go"), RecordingResponder()) is False
        )
        assert old_module.calls == []
        with pytest.raises(ValueError):
            old.host_for(identity)
        with pytest.raises(ValueError):
            old.management("owned")
        assert old.page_declarations() == []
        assert old.current_page(identity, "board") is None
        # Only the Tools it registered are detached; its record is left as it was.
        assert [tool.name for tool in tools.list_tools()] == ["read"]
        assert tools.get("read").handler is builtin_read
        assert record(old, "owned").status == "loaded"
        assert marker_lines(marker) == ["shutdown"]
        # Its modules are purged so the replacement imports fresh code.
        assert "vbot_ext.owned" not in sys.modules
    finally:
        old_module.release.set()
        finish_loading.set()
        await reload_task

    new = holder["registry"]
    assert new is not old
    assert record(new, "owned").status == "loaded"
    assert tools.get("ext_echo").handler is sys.modules["vbot_ext.owned"]._tool
    assert marker_lines(marker) == ["shutdown", "startup"]
    assert len(hosts) == 2


@pytest.mark.asyncio
async def test_disable_deactivates_extensions_and_recovers_their_recall_backends(
    tmp_path: Path,
) -> None:
    write_extension(
        tmp_path / "extensions",
        "recall_ext",
        "from core.tools import tool_success\n"
        "def register(api):\n"
        "    api.register_recall_backend('ext_backend', object)\n"
        "    api.register_tool('ext_tool', 'desc', {'type': 'object'},"
        " lambda context, arguments: tool_success({}))\n",
    )
    registry = ExtensionRegistry.load(tmp_path / "extensions")
    tools = ToolRegistry()
    registry.apply_tools(tools)
    recovered: list[set[str]] = []
    reload_skills = AsyncMock()
    runtime = _runtime(
        tmp_path,
        {"registry": registry},
        tools=tools,
        reload_skills=reload_skills,
        recover_recall=recovered.append,
    )

    await runtime.apply_disabled_change(set())
    reload_skills.assert_not_awaited()

    await runtime.apply_disabled_change({"recall_ext"})

    assert record(registry, "recall_ext").status == "disabled"
    assert tools.list_tools() == []
    reload_skills.assert_awaited_once()
    # Recall falls back when the selected backend belonged to a disabled Extension.
    assert recovered == [{"ext_backend"}]


def _stub_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reload_skills: Callable[[], Awaitable[None]],
) -> ExtensionRuntime:
    """A runtime over an empty registry whose mutations end in *reload_skills*."""
    registry = ExtensionRegistry()

    async def load(*_args, **_kwargs) -> ExtensionRegistry:
        return registry

    monkeypatch.setattr(ExtensionRegistry, "aload", load)
    return _runtime(tmp_path, {"registry": registry}, reload_skills=reload_skills)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "outcome"),
    [
        ("reload", "success"),
        ("reload", "failure"),
        ("reload", "cancelled"),
        ("disable", "failure"),
    ],
)
async def test_cancelled_mutation_reports_late_failure_once_after_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
    outcome: str,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    failure = RuntimeError("test-sentinel")

    async def refresh() -> None:
        entered.set()
        await release.wait()
        if outcome == "failure":
            raise failure
        if outcome == "cancelled":
            raise asyncio.CancelledError

    runtime = _stub_runtime(tmp_path, monkeypatch, refresh)
    caller = asyncio.create_task(
        runtime.reload() if operation == "reload" else runtime.apply_disabled_change({"fixture"})
    )
    queued: asyncio.Task[None] | None = None
    try:
        await entered.wait()
        for _ in range(2):
            caller.cancel()
            await asyncio.sleep(0)
            assert not caller.done()
        queued = asyncio.create_task(runtime.startup())
        await asyncio.sleep(0)
        assert not queued.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await caller
        await queued

        records = [entry for entry in caplog.records if entry.name == _LOGGER_NAME]
        assert len(records) == (1 if outcome == "failure" else 0)
        if records:
            assert records[0].levelno == logging.ERROR
            assert records[0].exc_info is not None
            assert records[0].exc_info[1] is failure
        assert not [entry for entry in caplog.records if entry.name == "asyncio"]
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        if queued is not None:
            await queued


@pytest.mark.asyncio
async def test_uncancelled_mutation_failures_propagate_and_closed_runtime_refuses_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    failure = RuntimeError("test-sentinel")

    async def refresh() -> None:
        raise failure

    runtime = _stub_runtime(tmp_path, monkeypatch, refresh)
    mutations = (runtime.reload, lambda: runtime.apply_disabled_change({"fixture"}))

    for mutation in mutations:
        with pytest.raises(RuntimeError) as caught:
            await mutation()
        assert caught.value is failure
    # The caller receives the failure; it is not logged a second time.
    assert not [entry for entry in caplog.records if entry.name == _LOGGER_NAME]

    await runtime.aclose()
    for mutation in mutations:
        with pytest.raises(ExtensionUnavailableError):
            await mutation()
