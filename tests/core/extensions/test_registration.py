"""Extension registration: async ``register()``, the ``api`` surface and lifecycle.

Covers how ``load`` (blocking, bounded worker) and ``aload`` (serving loop) drive
async ``register()``: awaited before declarations apply, bounded by a deadline,
cancellation isolated to one Extension and the serving loop kept responsive. Also
covers what ``register(api)`` reads and declares (config snapshot and live config,
credentials, pages, settings schema, interaction handlers, Commands) and
startup/shutdown lifecycle firing.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from pathlib import Path

import pytest

import core.extensions._callbacks as extension_callbacks
import core.extensions._loading as extension_loading
from core.extensions import ExtensionRegistry
from core.extensions.extensions import ExtensionAPI, ExtensionDeclarations
from tests.core.extensions.extension_test_support import (
    hook_context,
    marker_lines,
    marker_source,
    record,
    write_extension,
    write_package,
)

_LOADERS = ["load", "aload"]

# Suppresses its timeout cancellation until released, recording that it was asked.
_HANGING_SOURCE = (
    "import asyncio\n"
    "import threading\n"
    "release = threading.Event()\n"
    "cancelled = threading.Event()\n"
    "finished = threading.Event()\n"
    "async def register(api):\n"
    "    try:\n"
    "        while not release.is_set():\n"
    "            try:\n"
    "                await asyncio.sleep(0.01)\n"
    "            except asyncio.CancelledError:\n"
    "                cancelled.set()\n"
    "    finally:\n"
    "        finished.set()\n"
)


async def _load(loader: str, root: Path) -> ExtensionRegistry:
    """Load *root*; the blocking variant runs on a live loop like a server lifespan."""
    if loader == "aload":
        return await ExtensionRegistry.aload(root)
    return ExtensionRegistry.load(root)


@pytest.mark.asyncio
@pytest.mark.parametrize("loader", _LOADERS)
async def test_async_register_completes_before_declarations_apply(
    tmp_path: Path, loader: str
) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    write_extension(
        root,
        "a_cancelled",
        "import asyncio\nasync def register(api):\n    raise asyncio.CancelledError()\n",
    )
    write_extension(root, "async_ext", marker_source(marker, "async_ext", asynchronous=True))

    registry = await _load(loader, root)

    cancelled = record(registry, "a_cancelled")
    assert cancelled.status == "failed"
    assert cancelled.error == "async register() raised: CancelledError"
    assert record(registry, "async_ext").status == "loaded"
    await registry.dispatch_run_start(hook_context(), session_id="s", agent_id="a")
    assert marker_lines(marker) == ["async_ext"]


@pytest.mark.asyncio
@pytest.mark.parametrize("loader", _LOADERS)
async def test_register_timeout_fails_only_that_extension_and_detaches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loader: str
) -> None:
    monkeypatch.setattr(extension_loading, "_ASYNC_REGISTER_TIMEOUT_SECONDS", 0.05)
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    write_extension(root, "hanging", _HANGING_SOURCE)
    write_extension(root, "healthy", marker_source(marker, "healthy"))

    registry = await _load(loader, root)
    module = sys.modules["vbot_ext.hanging"]
    try:
        hanging = record(registry, "hanging")
        assert hanging.status == "failed"
        assert "timed out" in (hanging.error or "")
        # The deadline requested cancellation; a coroutine that suppresses it keeps
        # running detached instead of holding the load.
        assert await asyncio.to_thread(module.cancelled.wait, 1)
        assert not module.finished.is_set()
        await registry.dispatch_run_start(hook_context(), session_id="s", agent_id="a")
        assert marker_lines(marker) == ["healthy"]
    finally:
        module.release.set()
    assert await asyncio.to_thread(module.finished.wait, 1)


@pytest.mark.asyncio
async def test_timed_out_registration_logs_late_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "extensions"
    monkeypatch.setattr(extension_loading, "_ASYNC_REGISTER_TIMEOUT_SECONDS", 0.01)
    write_extension(
        root,
        "late_failure",
        "import asyncio\n"
        "release = asyncio.Event()\n"
        "async def register(api):\n"
        "    global task\n"
        "    task = asyncio.current_task()\n"
        "    try:\n"
        "        await asyncio.Event().wait()\n"
        "    except asyncio.CancelledError:\n"
        "        await release.wait()\n"
        "        raise ValueError('late failure sentinel')\n",
    )

    registry = await ExtensionRegistry.aload(root)
    assert record(registry, "late_failure").status == "failed"
    module = sys.modules["vbot_ext.late_failure"]
    module.release.set()
    with pytest.raises(ValueError):
        await module.task
    await asyncio.sleep(0)

    assert any(
        entry.name == "vbot.extensions"
        and entry.exc_info
        and isinstance(entry.exc_info[1], ValueError)
        for entry in caplog.records
    )


@pytest.mark.asyncio
async def test_aload_keeps_event_loop_responsive_during_async_register(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    marker = tmp_path / "marker.txt"
    write_extension(
        root,
        "slow_async",
        "import asyncio\n"
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "async def register(api):\n"
        "    await asyncio.sleep(0.02)\n"
        "    def handler(ctx, **payload):\n"
        "        _MARKER.write_text('fired', encoding='utf-8')\n"
        "    api.on('run_start', handler)\n",
    )
    heartbeat_ticks = 0
    heartbeat_done = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal heartbeat_ticks
        while not heartbeat_done.is_set():
            await asyncio.sleep(0)
            heartbeat_ticks += 1

    heartbeat_task = asyncio.create_task(heartbeat())
    registry = await ExtensionRegistry.aload(root)
    heartbeat_done.set()
    await heartbeat_task

    # The loop serviced other tasks while register() was pending.
    assert heartbeat_ticks > 0
    assert record(registry, "slow_async").status == "loaded"
    await registry.dispatch_run_start(hook_context(), session_id="s", agent_id="a")
    assert marker.read_text(encoding="utf-8") == "fired"


@pytest.mark.asyncio
async def test_cancelling_aload_cancels_active_and_closes_pending_registrations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extensions"
    write_extension(
        root,
        "a_active",
        "import asyncio\n"
        "started = asyncio.Event()\n"
        "finished = asyncio.Event()\n"
        "async def register(api):\n"
        "    started.set()\n"
        "    try:\n"
        "        await asyncio.Event().wait()\n"
        "    finally:\n"
        "        finished.set()\n",
    )
    write_extension(
        root,
        "z_pending",
        "async def _register(api):\n"
        "    raise AssertionError('cancelled load must not start pending registration')\n"
        "def register(api):\n"
        "    global pending\n"
        "    pending = _register(api)\n"
        "    return pending\n",
    )
    loading = asyncio.create_task(ExtensionRegistry.aload(root))
    await asyncio.sleep(0)
    active = sys.modules["vbot_ext.a_active"]
    pending = sys.modules["vbot_ext.z_pending"].pending
    await active.started.wait()
    loading.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await loading
        await asyncio.sleep(0)
        assert active.finished.is_set()
        assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED
    finally:
        pending.close()
        for task in tuple(extension_loading._detached_register_tasks):
            task.cancel()
        await asyncio.sleep(0)


def test_register_api_reads_a_config_snapshot_live_config_and_credentials() -> None:
    live: dict[str, object] = {"url": "http://one"}
    wired = ExtensionAPI(
        "ext",
        ExtensionDeclarations(),
        config={"url": "http://snapshot"},
        logger=None,
        config_provider=lambda: dict(live),
        credential_resolver=lambda key: f"value-for-{key}",
    )

    assert wired.get_config() == {"url": "http://one"}
    live["url"] = "http://two"
    assert wired.get_config() == {"url": "http://two"}
    # The register-time snapshot never changes.
    assert wired.config == {"url": "http://snapshot"}
    assert wired.resolve_credential("HASS_TOKEN") == "value-for-HASS_TOKEN"

    standalone = ExtensionAPI(
        "ext", ExtensionDeclarations(), config={"url": "http://snapshot"}, logger=None
    )
    snapshot = standalone.get_config()
    assert snapshot == {"url": "http://snapshot"}
    assert snapshot is not standalone.config
    assert standalone.resolve_credential("HASS_TOKEN") == ""


def test_declarations_land_on_the_loaded_record(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    write_extension(
        root,
        "declaring",
        "async def _tap(event, responder):\n"
        "    return None\n"
        "def _workflow(context, argument):\n"
        "    return None\n"
        "def register(api):\n"
        "    api.register_settings([\n"
        "        {'key': 'url', 'type': 'text', 'label': 'URL'},\n"
        "        {'key': 'token', 'type': 'secret', 'label': 'Token', 'env_key': 'HASS_TOKEN'},\n"
        "    ])\n"
        "    api.register_interaction_handler('chk', _tap)\n"
        "    api.register_command(\n"
        "        'workflow', 'Start the workflow.', _workflow, argument='required',\n"
        "        catalog_result='state_change', execution_mode='serialized',\n"
        "        unavailable_surfaces={'channel'},\n"
        "    )\n",
    )

    registry = ExtensionRegistry.load(root)

    declared = record(registry, "declaring")
    module = sys.modules["vbot_ext.declaring"]
    assert declared.status == "loaded"
    schema = declared.declarations.settings_schema
    assert schema is not None
    assert [(field.key, field.env_key) for field in schema] == [
        ("url", None),
        ("token", "HASS_TOKEN"),
    ]
    assert [
        (declaration.prefix, declaration.handler)
        for declaration in declared.declarations.interaction_handlers
    ] == [("chk", module._tap)]
    assert [
        (
            command.name,
            command.description,
            command.handler,
            command.argument,
            command.catalog_result,
            command.execution_mode,
            command.unavailable_surfaces,
        )
        for command in declared.declarations.commands
    ] == [
        (
            "workflow",
            "Start the workflow.",
            module._workflow,
            "required",
            "state_change",
            "serialized",
            frozenset({"channel"}),
        )
    ]


def test_page_declarations_are_checked_and_scoped_to_the_live_registry_epoch(
    tmp_path: Path,
) -> None:
    package = write_package(
        tmp_path / "extensions",
        "page_owner",
        "def register(api):\n    api.register_page('board', 'Board', 'ui/index.html')\n",
    )
    entry = package / "ui" / "index.html"
    entry.parent.mkdir()
    entry.write_text("<!doctype html>", encoding="utf-8")

    registry = ExtensionRegistry.load(tmp_path / "extensions")

    identity, page, path = registry.page_declarations()[0]
    assert (identity.name, page.page_id, path) == ("page_owner", "board", entry.resolve())
    assert registry.is_registration_current(identity)
    assert registry.registration_identity("page_owner") == identity
    assert registry.current_page(identity, "board") == (page, path)
    assert registry.current_page(identity, "other") is None
    # The declaration is registration-bound; only listings require the entry file.
    entry.unlink()
    assert registry.page_declarations() == []
    assert registry.current_page(identity, "board") == (page, path)
    replacement = ExtensionRegistry.load(tmp_path / "extensions")
    assert not replacement.is_registration_current(identity)
    assert replacement.current_page(identity, "board") is None

    api = ExtensionAPI("example", ExtensionDeclarations(), config={}, logger=None)
    for unsafe_entry in ("../index.html", "/index.html", "index.js"):
        with pytest.raises(ValueError, match="relative HTML asset path"):
            api.register_page("board", "Board", unsafe_entry)


def _lifecycle_source(name: str, marker: Path, *, startup_boom: bool = False) -> str:
    boom = "        raise RuntimeError('startup boom')\n" if startup_boom else ""
    return (
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "def _write(tag):\n"
        "    with _MARKER.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(tag + '\\n')\n"
        "def register(api):\n"
        "    def _startup():\n"
        f"        _write({name!r} + ':startup')\n"
        f"{boom}"
        "    def _shutdown():\n"
        f"        _write({name!r} + ':shutdown')\n"
        "    api.on_startup(_startup)\n"
        "    api.on_shutdown(_shutdown)\n"
    )


def test_lifecycle_handlers_fire_in_load_order_and_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(extension_callbacks, "_SLOW_EXTENSION_HANDLER_SECONDS", 0)
    caplog.set_level(logging.WARNING, logger="vbot.extensions")
    root = tmp_path / "extensions"
    marker = tmp_path / "lifecycle.txt"
    write_extension(root, "alpha", _lifecycle_source("alpha", marker, startup_boom=True))
    write_extension(root, "dormant", _lifecycle_source("dormant", marker))
    write_extension(root, "zeta", _lifecycle_source("zeta", marker))

    registry = ExtensionRegistry.load(root, disabled={"dormant"})
    asyncio.run(registry.fire_startup())
    asyncio.run(registry.fire_shutdown())
    registry.fire_shutdown_blocking()

    # alpha's failing startup does not stop zeta's; the disabled Extension never fires.
    assert marker_lines(marker) == [
        "alpha:startup",
        "zeta:startup",
        "alpha:shutdown",
        "zeta:shutdown",
        "alpha:shutdown",
        "zeta:shutdown",
    ]
    # Only the failed startup is logged, with its traceback; lifecycle handlers are
    # never reported as slow.
    assert [
        (entry.levelno, entry.exc_info is not None)
        for entry in caplog.records
        if entry.name == "vbot.extensions"
    ] == [(logging.ERROR, True)]
