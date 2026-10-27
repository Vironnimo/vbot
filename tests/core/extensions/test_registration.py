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
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

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
@pytest.mark.parametrize("phase", ["discovery", "import", "register", "async_register", "pages"])
async def test_aload_keeps_event_loop_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    root = tmp_path / "extensions"
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    entered = asyncio.Event()
    release = threading.Event()
    async_release = asyncio.Event()
    fired: list[str] = []

    def block(at: str) -> None:
        if at != phase:
            return
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(1), "serving loop did not release the loader worker"

    async def register_async(api: ExtensionAPI) -> None:
        assert asyncio.get_running_loop() is loop
        if phase == "async_register":
            entered.set()
            await async_release.wait()

        async def handler(ctx, **payload):
            assert asyncio.get_running_loop() is loop
            fired.append("fired")

        api.on("run_start", handler)
        api.register_page("board", "Board", "page.html")

    probe = ModuleType("registration_probe")
    probe.__dict__.update(block=block, register_async=register_async)
    monkeypatch.setitem(sys.modules, probe.__name__, probe)
    package = write_package(
        root,
        "slow",
        "from registration_probe import block, register_async\n"
        "block('import')\n"
        "def register(api):\n"
        "    block('register')\n"
        "    return register_async(api)\n",
    )
    page = package / "page.html"
    page.write_text("<p>Board</p>", encoding="utf-8")
    original_iterdir, original_resolve = Path.iterdir, Path.resolve

    def iterdir(path: Path) -> Iterator[Path]:
        if path == root:
            block("discovery")
        return original_iterdir(path)

    def resolve(path: Path, strict: bool = False) -> Path:
        if path == page:
            block("pages")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "resolve", resolve)
    loading = asyncio.create_task(ExtensionRegistry.aload(root))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        # This task must run while the loader is held inside the selected phase.
        assert not loading.done()
    finally:
        release.set()
        async_release.set()
        await asyncio.gather(loading, return_exceptions=True)
    registry = await loading
    assert record(registry, "slow").status == "loaded"
    await registry.dispatch_run_start(hook_context(), session_id="s", agent_id="a")
    assert fired == ["fired"]
    assert registry.current_page(registry.registration_identity("slow"), "board") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["scan", "async_register"])
async def test_cancelling_aload_cancels_active_and_closes_pending_registrations(
    tmp_path: Path,
    phase: str,
) -> None:
    root = tmp_path / "extensions"
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()

    def block_scan() -> None:
        if phase == "scan":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(1), "serving loop did not release the cancelled scan"

    write_extension(
        root,
        "a_active",
        "import asyncio\n"
        "started = asyncio.Event()\n"
        "finished = asyncio.Event()\n"
        "async def register(api):\n"
        "    started.set()\n"
        "    api.get_config()['entered'].set()\n"
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
        "    api.get_config()['block_scan']()\n"
        "    return pending\n",
    )
    loading = asyncio.create_task(
        ExtensionRegistry.aload(
            root, config_provider=lambda _: {"entered": entered, "block_scan": block_scan}
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        active = sys.modules["vbot_ext.a_active"]
        pending = sys.modules["vbot_ext.z_pending"].pending
        loading.cancel()
        if phase == "scan":
            await asyncio.sleep(0)
            assert not loading.done()
            loading.cancel()
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await loading
        await asyncio.sleep(0)
        assert active.finished.is_set() == (phase == "async_register")
        assert inspect.getcoroutinestate(pending) == inspect.CORO_CLOSED
    finally:
        release.set()
        if not loading.done():
            loading.cancel()
            await asyncio.gather(loading, return_exceptions=True)
        for task in tuple(extension_callbacks._detached_tasks):
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
        "    api.register_interaction_handler('poll', _tap)\n"
        "    api.register_command(\n"
        "        'workflow', 'Start the workflow.', _workflow, argument='required',\n"
        "        catalog_result='state_change', execution_mode='serialized',\n"
        "        unavailable_surfaces={'channel'}, argument_hint='what the workflow should do',\n"
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
    ] == [("poll", module._tap)]
    assert [
        (
            command.name,
            command.description,
            command.handler,
            command.argument,
            command.catalog_result,
            command.execution_mode,
            command.unavailable_surfaces,
            command.argument_hint,
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
            "what the workflow should do",
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


def _lifecycle_source(
    name: str, marker: Path, *, failure: str | None = None, hang: bool = False
) -> str:
    boom = f"        raise {failure}('handler failure')\n" if failure else ""
    # Hanging handlers never finish on their own; only the deadline moves on.
    define = "    async def" if hang else "    def"
    wait = (
        "        api.get_config()['expire_deadline']()\n        await asyncio.Event().wait()\n"
        if hang
        else ""
    )
    return (
        "import asyncio\n"
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "def _write(tag):\n"
        "    with _MARKER.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(tag + '\\n')\n"
        "def register(api):\n"
        f"{define} _startup():\n"
        f"        _write({name!r} + ':startup')\n"
        f"{boom}{wait}"
        f"{define} _shutdown():\n"
        f"        _write({name!r} + ':shutdown')\n"
        f"{boom if failure == 'asyncio.CancelledError' else ''}{wait}"
        "    api.on_startup(_startup)\n"
        "    api.on_shutdown(_shutdown)\n"
    )


@pytest.mark.parametrize("failure", ["RuntimeError", "asyncio.CancelledError"])
def test_lifecycle_handlers_fire_in_load_order_and_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    monkeypatch.setattr(extension_callbacks, "_SLOW_EXTENSION_HANDLER_SECONDS", 0)
    deadline: asyncio.Event | None = None

    def expire_deadline() -> None:
        assert deadline is not None
        deadline.set()

    async def wait_for_callback(
        tasks: Iterable[asyncio.Task[Any]], *, timeout: float | None = None
    ) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]:
        nonlocal deadline
        assert timeout == extension_callbacks._LIFECYCLE_HANDLER_TIMEOUT_SECONDS
        deadline = asyncio.Event()
        waiting = set(tasks)
        expired = asyncio.create_task(deadline.wait())
        try:
            # Only the deliberately hanging handler expires its deadline. Healthy
            # worker completion must not depend on scheduling within 50 ms.
            done, _ = await asyncio.wait(waiting | {expired}, return_when=asyncio.FIRST_COMPLETED)
            return done & waiting, waiting - done
        finally:
            expired.cancel()
            await asyncio.gather(expired, return_exceptions=True)

    monkeypatch.setattr(extension_callbacks, "_wait", wait_for_callback)
    caplog.set_level(logging.WARNING, logger="vbot.extensions")
    root = tmp_path / "extensions"
    marker = tmp_path / "lifecycle.txt"
    write_extension(root, "alpha", _lifecycle_source("alpha", marker, failure=failure))
    write_extension(root, "dormant", _lifecycle_source("dormant", marker))
    write_extension(root, "stuck", _lifecycle_source("stuck", marker, hang=True))
    write_extension(root, "zeta", _lifecycle_source("zeta", marker))

    registry = ExtensionRegistry.load(
        root,
        disabled={"dormant"},
        config_provider=lambda _: {"expire_deadline": expire_deadline},
    )
    asyncio.run(registry.fire_startup())
    asyncio.run(registry.fire_shutdown())
    registry.fire_shutdown_blocking()

    # alpha's failing startup and stuck's hanging handlers do not stop zeta's;
    # the disabled Extension never fires.
    assert marker_lines(marker) == [
        "alpha:startup",
        "stuck:startup",
        "zeta:startup",
        "alpha:shutdown",
        "stuck:shutdown",
        "zeta:shutdown",
        "alpha:shutdown",
        "stuck:shutdown",
        "zeta:shutdown",
    ]
    # The failed startup is logged with its traceback and each missed deadline
    # once; lifecycle handlers are never reported as slow.
    assert [
        (entry.levelno, entry.exc_info is not None, "timed out" in entry.getMessage())
        for entry in caplog.records
        if entry.name == "vbot.extensions"
    ] == (
        [(logging.ERROR, True, False), (logging.ERROR, False, True)] * 3
        if failure == "asyncio.CancelledError"
        else [(logging.ERROR, True, False)] + [(logging.ERROR, False, True)] * 3
    )


@pytest.mark.asyncio
async def test_cancelling_lifecycle_caller_cancels_the_active_callback(tmp_path: Path) -> None:
    root = tmp_path / "extensions"
    write_extension(
        root,
        "cancellable",
        "import asyncio\n"
        "entered = asyncio.Event()\n"
        "finished = asyncio.Event()\n"
        "async def startup():\n"
        "    entered.set()\n"
        "    try:\n"
        "        await asyncio.Event().wait()\n"
        "    finally:\n"
        "        finished.set()\n"
        "def register(api):\n"
        "    api.on_startup(startup)\n",
    )
    registry = await ExtensionRegistry.aload(root)
    active = sys.modules["vbot_ext.cancellable"]
    firing = asyncio.create_task(registry.fire_startup())
    await active.entered.wait()
    firing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await firing
    await active.finished.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("synchronous", [False, True])
async def test_timed_out_callback_rejects_owner_until_its_work_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, synchronous: bool
) -> None:
    root = tmp_path / "extensions"
    body = (
        "def startup():\n    loop.call_soon_threadsafe(entered.set)\n    release.wait()\n"
        if synchronous
        else "async def startup():\n"
        "    entered.set()\n"
        "    while not release.is_set():\n"
        "        try:\n"
        "            await release.wait()\n"
        "        except asyncio.CancelledError:\n"
        "            pass\n"
    )
    write_extension(
        root,
        "stuck_owner",
        "import asyncio, threading\n"
        "loop = None\n"
        "entered = asyncio.Event()\n"
        f"release = {'threading' if synchronous else 'asyncio'}.Event()\n"
        "calls = []\n"
        f"{body}"
        "def hook(ctx, **payload):\n"
        "    calls.append('hook')\n"
        "def register(api):\n"
        "    api.on_startup(startup)\n"
        "    api.on('run_start', hook)\n",
    )
    registry = await ExtensionRegistry.aload(root)
    module = sys.modules["vbot_ext.stuck_owner"]
    module.__dict__["loop"] = asyncio.get_running_loop()

    async def wait_for_callback(
        tasks: Iterable[asyncio.Task[Any]], *, timeout: float | None = None
    ) -> tuple[set[asyncio.Task[Any]], set[asyncio.Task[Any]]]:
        if timeout is not None:
            # Expire only after the handler entered; thread startup speed does
            # not decide whether this test exercises detachment or admission.
            await module.entered.wait()
            timeout = 0
        return await asyncio.wait(tasks, timeout=timeout)

    monkeypatch.setattr(extension_callbacks, "_wait", wait_for_callback)
    before = set(extension_callbacks._detached_tasks)
    startup = asyncio.create_task(registry.fire_startup())
    try:
        await module.entered.wait()
        await startup
        await registry.dispatch_run_start(hook_context(), session_id="s", agent_id="a")
        assert module.calls == []
    finally:
        module.release.set()
        await startup
        detached = extension_callbacks._detached_tasks - before
        await asyncio.gather(*detached, return_exceptions=True)
    await registry.dispatch_run_start(hook_context(), session_id="s", agent_id="a")
    assert module.calls == ["hook"]
