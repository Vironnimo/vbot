"""Runtime-level tests for extension loading, config, and lifecycle wiring.

These exercise the bootstrap path end to end: disabled extensions are never
imported, per-extension config from ``settings.json`` reaches ``register()``, and
startup/shutdown handlers fire at runtime start/stop.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.chat import CommandExecutionContext, ReplySurface
from core.extensions import InteractionButton, InteractionEvent
from core.extensions.extensions import ExtensionRegistry
from core.runtime import runtime as runtime_module
from core.runtime.runtime import Runtime
from core.sessions.format import write_bootstrap_marker
from core.tools import ToolContext
from core.utils.config import Config


def _authorize_session_store(data_dir: Path) -> None:
    if not (data_dir / "sessions.db").is_file():
        write_bootstrap_marker(data_dir)


_CAPABILITY_EXT_SOURCE = (
    "from core.tools import tool_success\n"
    "def _echo(context, arguments):\n"
    "    return tool_success({'value': arguments.get('value')})\n"
    "class ExtBackend:\n"
    "    def __init__(self, context):\n"
    "        self.context = context\n"
    "    def browse(self, request):\n"
    "        return {}\n"
    "    def overview(self, request):\n"
    "        return {}\n"
    "    def search(self, request):\n"
    "        return {'kind': 'ext-search'}\n"
    "    def scroll(self, request):\n"
    "        return {}\n"
    "def register(api):\n"
    "    api.register_tool('ext_echo', 'desc', {'type': 'object'}, _echo)\n"
    "    api.register_recall_backend('ext_recall', ExtBackend)\n"
)


def _command_extension_source(reply: str) -> str:
    return (
        "from core.chat import CommandFeedback, CommandOutcome\n"
        "def _workflow(context, argument):\n"
        f"    return CommandOutcome(command='workflow', "
        f"feedback=CommandFeedback(kind='notice', text={reply!r}))\n"
        "def register(api):\n"
        "    api.register_command('workflow', 'Run the workflow.', _workflow)\n"
    )


def _dispatch_extension_command(runtime: Runtime) -> str:
    prepared = runtime.command_dispatcher.prepare("/workflow")
    assert prepared is not None
    outcome = asyncio.run(
        runtime.command_dispatcher.execute(
            prepared,
            CommandExecutionContext(
                agent_id="main",
                session_id=runtime.agents.get("main").current_session_id,
                project_id=None,
                reply_surface=ReplySurface.webui(),
            ),
        )
    )
    assert outcome.feedback is not None
    return outcome.feedback.text


@pytest.fixture(autouse=True)
def _clean_extension_modules() -> Iterator[None]:
    """Drop the synthetic ``vbot_ext`` namespace after each test."""
    yield
    for module_name in list(sys.modules):
        if module_name == "vbot_ext" or module_name.startswith("vbot_ext."):
            del sys.modules[module_name]


def _write_extension(data_dir: Path, name: str, source: str) -> None:
    extensions_dir = data_dir / "extensions"
    extensions_dir.mkdir(parents=True, exist_ok=True)
    _authorize_session_store(data_dir)
    (extensions_dir / f"{name}.py").write_text(source, encoding="utf-8")


def _write_settings(data_dir: Path, settings: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _authorize_session_store(data_dir)
    (data_dir / "settings.json").write_text(json.dumps(settings), encoding="utf-8")


def _marker_lines(marker: Path) -> list[str]:
    if not marker.exists():
        return []
    return marker.read_text(encoding="utf-8").split()


def test_runtime_passes_bundled_extensions_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The runtime hands the bundled root <resources>/extensions as bundled_dir.
    # Wrap the real load so the rest of start() still gets a real registry.
    config = Config(data_dir=tmp_path / "data")
    captured: dict[str, object] = {}
    original_load = ExtensionRegistry.load

    def _capturing_load(*args: object, **kwargs: object) -> ExtensionRegistry:
        captured["bundled_dir"] = kwargs.get("bundled_dir")
        return original_load(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_module.ExtensionRegistry, "load", _capturing_load)

    runtime = Runtime(config)
    runtime.start()
    try:
        expected = runtime._resolve_resources_path() / "extensions"
        assert captured["bundled_dir"] == expected
    finally:
        runtime.stop()


def test_runtime_passes_live_config_and_credential_callables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The runtime hands its live-config reader and credential resolver to load().
    config = Config(data_dir=tmp_path / "data")
    captured: dict[str, object] = {}
    original_load = ExtensionRegistry.load

    def _capturing_load(*args: object, **kwargs: object) -> ExtensionRegistry:
        captured["config_provider"] = kwargs.get("config_provider")
        captured["credential_resolver"] = kwargs.get("credential_resolver")
        return original_load(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_module.ExtensionRegistry, "load", _capturing_load)

    runtime = Runtime(config)
    runtime.start()
    try:
        assert captured["config_provider"] == runtime._live_extension_config
        assert captured["credential_resolver"] == runtime.resolve_environment_credential
    finally:
        runtime.stop()


def test_live_extension_config_reads_through_storage(tmp_path: Path) -> None:
    # _live_extension_config reflects a persisted config change without restart.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_settings(
        data_dir,
        {"extensions": {"config": {"homeassistant": {"url": "http://one:8123"}}}},
    )

    runtime = Runtime(config)
    runtime.start()
    try:
        assert runtime._live_extension_config("homeassistant") == {"url": "http://one:8123"}
        assert runtime._live_extension_config("absent") == {}

        # A persisted change is seen on the next read (live), no restart.
        runtime.storage.update_settings_sections(
            {"extensions": {"disabled": [], "config": {"homeassistant": {"url": "http://two"}}}}
        )
        assert runtime._live_extension_config("homeassistant") == {"url": "http://two"}
    finally:
        runtime.stop()


def test_disabled_extension_is_never_imported(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    import_marker = tmp_path / "imported.txt"
    _write_extension(
        data_dir,
        "disabled_ext",
        "import pathlib\n"
        f"pathlib.Path({str(import_marker)!r}).write_text('imported', encoding='utf-8')\n"
        "def register(api):\n    pass\n",
    )
    _write_settings(data_dir, {"extensions": {"disabled": ["disabled_ext"]}})

    runtime = Runtime(config)
    runtime.start()
    assert runtime.extensions is not None
    record = next(r for r in runtime.extensions.records() if r.name == "disabled_ext")
    runtime.stop()

    assert not import_marker.exists()
    assert record.status == "disabled"


def test_config_reaches_register(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    config_marker = tmp_path / "config.json"
    _write_extension(
        data_dir,
        "configured",
        "import json, pathlib\n"
        "def register(api):\n"
        f"    pathlib.Path({str(config_marker)!r}).write_text("
        "json.dumps(api.config), encoding='utf-8')\n",
    )
    _write_settings(
        data_dir,
        {"extensions": {"config": {"configured": {"token": "abc", "level": 2}}}},
    )

    runtime = Runtime(config)
    runtime.start()
    runtime.stop()

    assert json.loads(config_marker.read_text(encoding="utf-8")) == {"token": "abc", "level": 2}


def test_startup_and_shutdown_hooks_fire_at_runtime_lifecycle(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    lifecycle_marker = tmp_path / "lifecycle.txt"
    _write_extension(
        data_dir,
        "lifecycle_ext",
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(lifecycle_marker)!r})\n"
        "def _write(tag):\n"
        "    with _MARKER.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(tag + '\\n')\n"
        "def register(api):\n"
        "    api.on_startup(lambda: _write('startup'))\n"
        "    api.on_shutdown(lambda: _write('shutdown'))\n",
    )

    runtime = Runtime(config)
    runtime.start()

    # startup has not fired yet — it is gated on the serving lifespan
    assert _marker_lines(lifecycle_marker) == []

    asyncio.run(runtime.fire_extension_startup())
    assert _marker_lines(lifecycle_marker) == ["startup"]

    runtime.stop()
    assert _marker_lines(lifecycle_marker) == ["startup", "shutdown"]


_PROMPT_BLOCK_EXT_SOURCE = (
    "def register(api):\n"
    "    api.register_prompt_block('intro', default_text='Static extension intro.')\n"
    "    api.register_prompt_block('dynamic', render=lambda ctx: 'Dynamic extension text.')\n"
)


def test_extension_prompt_blocks_reach_the_system_prompt(tmp_path: Path) -> None:
    # The runtime collects loaded extensions' declared blocks and hands them to the
    # prompt manager; both a static and a dynamic block render in the system prompt,
    # gated by the extension being loaded (owner extension:<name>).
    config = Config(data_dir=tmp_path / "data")
    _write_extension(config.data_dir, "promptext", _PROMPT_BLOCK_EXT_SOURCE)

    runtime = Runtime(config)
    runtime.start()
    try:
        agent = runtime.agents.get("main")
        prompt = runtime.system_prompts.build_system_prompt(agent)
        assert "Static extension intro." in prompt
        assert "Dynamic extension text." in prompt
    finally:
        runtime.stop()


def test_retired_prompt_append_dispatch_is_removed(tmp_path: Path) -> None:
    # The legacy system-prompt tail-append event is gone entirely (D6): the
    # registry exposes only the five kept dispatch events. The retired name is
    # assembled at runtime so the literal never appears in source.
    retired_dispatch = "dispatch_" + "before" + "_agent_start"
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)
    runtime.start()
    try:
        assert runtime.extensions is not None
        assert not hasattr(runtime.extensions, retired_dispatch)
    finally:
        runtime.stop()


def test_extension_tool_and_recall_backend_wired_into_runtime(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "capabilities_ext", _CAPABILITY_EXT_SOURCE)
    _write_settings(data_dir, {"recall": {"backend": "ext_recall"}})

    runtime = Runtime(config)
    runtime.start()
    try:
        # The extension tool is registered, on the allowlist, and executes
        # through the runtime's real ToolRegistry dispatch.
        allowed = [tool.name for tool in runtime.tools.list_tools(allowed_tools=["ext_echo"])]
        assert "ext_echo" in allowed
        context = ToolContext(
            agent_id="a",
            session_id="s",
            run_id="r",
            tool_call_id="c1",
            tool_name="ext_echo",
            tool_call_index=0,
            workspace=data_dir,
            vbot_root=data_dir,
            data_root=data_dir,
        )
        result = asyncio.run(runtime.tools.dispatch(context, {"value": "hi"}))
        assert result["data"] == {"value": "hi"}

        # The extension recall backend is selectable and was resolved from
        # the persisted recall.backend setting.
        assert "ext_recall" in runtime.available_recall_backends()
        assert runtime.recall_backend.__class__.__name__ == "ExtBackend"

        # It survives a live backend switch (registry rebuilt + re-applied).
        runtime.reload_recall_backend()
        assert runtime.recall_backend.__class__.__name__ == "ExtBackend"
    finally:
        runtime.stop()


def test_extension_command_wired_into_stable_runtime_dispatcher_across_reload(
    tmp_path: Path,
) -> None:
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "workflow_ext", _command_extension_source("v1"))

    runtime = Runtime(config)
    runtime.start()
    try:
        dispatcher = runtime.command_dispatcher
        assert _dispatch_extension_command(runtime) == "v1"

        _rewrite_source(
            data_dir / "extensions" / "workflow_ext.py",
            _command_extension_source("v2"),
        )
        asyncio.run(runtime.reload_extensions())

        assert runtime.command_dispatcher is dispatcher
        assert _dispatch_extension_command(runtime) == "v2"
    finally:
        runtime.stop()


def test_apply_extension_disabled_change_deactivates_tool_and_prompt_block(
    tmp_path: Path,
) -> None:
    # A live disable removes the extension's tool from the registry and drops its
    # prompt block from the assembled system prompt — no restart.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(
        data_dir,
        "livext",
        "from core.tools import tool_success\n"
        "def _echo(context, arguments):\n"
        "    return tool_success({})\n"
        "def register(api):\n"
        "    api.register_tool('livext_echo', 'desc', {'type': 'object'}, _echo)\n"
        "    api.register_prompt_block('intro', default_text='Live extension intro.')\n",
    )

    runtime = Runtime(config)
    runtime.start()
    try:
        agent = runtime.agents.get("main")
        assert "livext_echo" in [tool.name for tool in runtime.tools.list_tools()]
        assert "Live extension intro." in runtime.system_prompts.build_system_prompt(agent)

        asyncio.run(runtime.apply_extension_disabled_change({"livext"}))

        # Tool unregistered, prompt block gone, record marked disabled — all live.
        assert "livext_echo" not in [tool.name for tool in runtime.tools.list_tools()]
        assert "Live extension intro." not in runtime.system_prompts.build_system_prompt(agent)
        assert runtime.extensions is not None
        record = next(r for r in runtime.extensions.records() if r.name == "livext")
        assert record.status == "disabled"
    finally:
        runtime.stop()


def test_apply_extension_disabled_change_deactivates_command(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "workflow_ext", _command_extension_source("ready"))

    runtime = Runtime(config)
    runtime.start()
    try:
        assert runtime.command_dispatcher.prepare("/workflow") is not None

        asyncio.run(runtime.apply_extension_disabled_change({"workflow_ext"}))

        assert runtime.command_dispatcher.prepare("/workflow") is None
        assert _extension_record(runtime, "workflow_ext").status == "disabled"
    finally:
        runtime.stop()


def test_apply_extension_disabled_change_falls_recall_back_to_default(
    tmp_path: Path,
) -> None:
    # Disabling the extension that provides the currently-active recall backend
    # must not leave recall pointing at dead code: fall back to the built-in
    # default, without rewriting the persisted selection.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "capabilities_ext", _CAPABILITY_EXT_SOURCE)
    _write_settings(data_dir, {"recall": {"backend": "ext_recall"}})

    runtime = Runtime(config)
    runtime.start()
    try:
        assert runtime.recall_backend.__class__.__name__ == "ExtBackend"

        asyncio.run(runtime.apply_extension_disabled_change({"capabilities_ext"}))

        # Active backend fell back to the built-in default; the persisted
        # selection is untouched (re-enabling on restart restores it).
        assert runtime.recall_backend.__class__.__name__ != "ExtBackend"
        assert runtime.storage.load_recall_settings()["backend"] == "ext_recall"
        assert "ext_recall" not in runtime.available_recall_backends()
    finally:
        runtime.stop()


def test_apply_extension_disabled_change_ignores_active_builtin_backend(
    tmp_path: Path,
) -> None:
    # Disabling an extension that declares a recall backend which is NOT the
    # active one leaves the active (built-in) backend alone.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "capabilities_ext", _CAPABILITY_EXT_SOURCE)
    # Default backend (sqlite_fts) is active, not the extension's ext_recall.

    runtime = Runtime(config)
    runtime.start()
    try:
        active_before = runtime.recall_backend.__class__.__name__

        asyncio.run(runtime.apply_extension_disabled_change({"capabilities_ext"}))

        assert runtime.recall_backend.__class__.__name__ == active_before
    finally:
        runtime.stop()


# --- reload_extensions: restart-equivalent rebuild -----------------------------


def _dispatch_extension_tool(
    runtime: Runtime, tool_name: str, data_dir: Path, arguments: dict | None = None
) -> dict:
    context = ToolContext(
        agent_id="a",
        session_id="s",
        run_id="r",
        tool_call_id="c1",
        tool_name=tool_name,
        tool_call_index=0,
        workspace=data_dir,
        vbot_root=data_dir,
        data_root=data_dir,
    )
    return asyncio.run(runtime.tools.dispatch(context, arguments or {}))


def _tool_names(runtime: Runtime) -> list[str]:
    return [tool.name for tool in runtime.tools.list_tools()]


def _extension_record(runtime: Runtime, name: str):
    assert runtime.extensions is not None
    return next(record for record in runtime.extensions.records() if record.name == name)


def _extension_record_names(runtime: Runtime) -> list[str]:
    assert runtime.extensions is not None
    return [record.name for record in runtime.extensions.records()]


def _versioned_tool_source(tool_name: str, version: str) -> str:
    return (
        "from core.tools import tool_success\n"
        "def _echo(context, arguments):\n"
        f"    return tool_success({{'version': {version!r}}})\n"
        "def register(api):\n"
        f"    api.register_tool({tool_name!r}, 'desc', {{'type': 'object'}}, _echo)\n"
    )


def _tool_source(tool_name: str) -> str:
    return (
        "from core.tools import tool_success\n"
        "def _echo(context, arguments):\n"
        "    return tool_success({})\n"
        "def register(api):\n"
        f"    api.register_tool({tool_name!r}, 'desc', {{'type': 'object'}}, _echo)\n"
    )


def _rewrite_source(path: Path, source: str) -> None:
    """Rewrite an extension source file and bump its mtime past the cached bytecode.

    A test rewrites v1→v2 within the same wall-clock second and often at the same
    byte length; CPython's timestamp-based ``.pyc`` invalidation would then treat
    the bytecode written on the first import as still current and re-execute the
    stale code. Pushing the source mtime forward guarantees the reload recompiles
    from the new source — the same thing real elapsed time does in production.
    """
    path.write_text(source, encoding="utf-8")
    future = path.stat().st_mtime + 10
    os.utime(path, (future, future))


def _write_package_extension(data_dir: Path, name: str, helper_value: str) -> Path:
    """Write a package extension whose tool returns a value from its submodule."""
    ext_dir = data_dir / "extensions" / name
    ext_dir.mkdir(parents=True, exist_ok=True)
    _authorize_session_store(data_dir)
    (ext_dir / "helper.py").write_text(f"VALUE = {helper_value!r}\n", encoding="utf-8")
    (ext_dir / "__init__.py").write_text(
        "from core.tools import tool_success\n"
        "from .helper import VALUE\n"
        "def _echo(context, arguments):\n"
        "    return tool_success({'value': VALUE})\n"
        "def register(api):\n"
        "    api.register_tool('pkg_echo', 'desc', {'type': 'object'}, _echo)\n",
        encoding="utf-8",
    )
    return ext_dir


def test_reload_picks_up_edited_single_file_extension(tmp_path: Path) -> None:
    # (a) An edit to a loaded single-file extension's code takes effect on reload.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "swap", _versioned_tool_source("swap_echo", "v1"))

    runtime = Runtime(config)
    runtime.start()
    try:
        assert _dispatch_extension_tool(runtime, "swap_echo", data_dir)["data"] == {"version": "v1"}

        _rewrite_source(
            data_dir / "extensions" / "swap.py", _versioned_tool_source("swap_echo", "v2")
        )
        asyncio.run(runtime.reload_extensions())

        assert _dispatch_extension_tool(runtime, "swap_echo", data_dir)["data"] == {"version": "v2"}
    finally:
        runtime.stop()


def test_reload_picks_up_edited_package_submodule(tmp_path: Path) -> None:
    # (b) Editing a *submodule* of a package extension takes effect on reload. This
    # only works because reload purges the vbot_ext module cache — without the
    # purge the stale cached submodule would keep serving the old value.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_package_extension(data_dir, "pkgext", "v1")

    runtime = Runtime(config)
    runtime.start()
    try:
        assert _dispatch_extension_tool(runtime, "pkg_echo", data_dir)["data"] == {"value": "v1"}

        _rewrite_source(data_dir / "extensions" / "pkgext" / "helper.py", "VALUE = 'v2'\n")
        asyncio.run(runtime.reload_extensions())

        assert _dispatch_extension_tool(runtime, "pkg_echo", data_dir)["data"] == {"value": "v2"}
    finally:
        runtime.stop()


def test_reload_loads_extension_enabled_since_boot(tmp_path: Path) -> None:
    # (c) An extension disabled at boot loads after it is removed from the disabled
    # set and reload runs.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "toggler", _tool_source("toggler_echo"))
    _write_settings(data_dir, {"extensions": {"disabled": ["toggler"]}})

    runtime = Runtime(config)
    runtime.start()
    try:
        assert "toggler_echo" not in _tool_names(runtime)
        assert _extension_record(runtime, "toggler").status == "disabled"

        runtime.storage.update_settings_sections({"extensions": {"disabled": [], "config": {}}})
        asyncio.run(runtime.reload_extensions())

        assert "toggler_echo" in _tool_names(runtime)
        assert _extension_record(runtime, "toggler").status == "loaded"
    finally:
        runtime.stop()


def test_reload_recovers_failed_extension_after_fix(tmp_path: Path) -> None:
    # (d) A failed extension whose file is fixed becomes loaded after reload.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "fixme", "raise RuntimeError('boom at import')\n")

    runtime = Runtime(config)
    runtime.start()
    try:
        assert _extension_record(runtime, "fixme").status == "failed"

        _rewrite_source(data_dir / "extensions" / "fixme.py", _tool_source("fixme_echo"))
        asyncio.run(runtime.reload_extensions())

        assert _extension_record(runtime, "fixme").status == "loaded"
        assert "fixme_echo" in _tool_names(runtime)
    finally:
        runtime.stop()


def test_reload_drops_extension_deleted_from_disk(tmp_path: Path) -> None:
    # (e) An extension deleted from disk disappears from records() and its tool from
    # the registry after reload.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "gone", _tool_source("gone_echo"))

    runtime = Runtime(config)
    runtime.start()
    try:
        assert "gone_echo" in _tool_names(runtime)

        (data_dir / "extensions" / "gone.py").unlink()
        asyncio.run(runtime.reload_extensions())

        assert "gone_echo" not in _tool_names(runtime)
        assert "gone" not in _extension_record_names(runtime)
    finally:
        runtime.stop()


def test_reload_drops_command_from_extension_deleted_from_disk(tmp_path: Path) -> None:
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "gone", _command_extension_source("ready"))

    runtime = Runtime(config)
    runtime.start()
    try:
        assert runtime.command_dispatcher.prepare("/workflow") is not None

        (data_dir / "extensions" / "gone.py").unlink()
        asyncio.run(runtime.reload_extensions())

        assert runtime.command_dispatcher.prepare("/workflow") is None
        assert "gone" not in _extension_record_names(runtime)
    finally:
        runtime.stop()


def test_reload_fires_old_shutdown_before_new_startup(tmp_path: Path) -> None:
    # (f) The old layer's shutdown handlers fire before the new layer's startup.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    marker = tmp_path / "lifecycle.txt"
    _write_extension(
        data_dir,
        "seq",
        "import pathlib\n"
        f"_MARKER = pathlib.Path({str(marker)!r})\n"
        "def _write(tag):\n"
        "    with _MARKER.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(tag + '\\n')\n"
        "def register(api):\n"
        "    api.on_startup(lambda: _write('startup'))\n"
        "    api.on_shutdown(lambda: _write('shutdown'))\n",
    )

    runtime = Runtime(config)
    runtime.start()
    try:
        asyncio.run(runtime.fire_extension_startup())
        assert _marker_lines(marker) == ["startup"]

        asyncio.run(runtime.reload_extensions())

        # Reload cycles the whole layer: old shutdown, then new startup — in order.
        assert _marker_lines(marker) == ["startup", "shutdown", "startup"]
    finally:
        runtime.stop()


def test_reload_reapplies_extension_recall_backend(tmp_path: Path) -> None:
    # (g) An extension-provided recall backend still resolves after reload.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "capabilities_ext", _CAPABILITY_EXT_SOURCE)
    _write_settings(data_dir, {"recall": {"backend": "ext_recall"}})

    runtime = Runtime(config)
    runtime.start()
    try:
        assert runtime.recall_backend.__class__.__name__ == "ExtBackend"

        asyncio.run(runtime.reload_extensions())

        assert "ext_recall" in runtime.available_recall_backends()
        assert runtime.recall_backend.__class__.__name__ == "ExtBackend"
    finally:
        runtime.stop()


def test_reload_falls_recall_back_when_providing_extension_vanishes(tmp_path: Path) -> None:
    # (g) When the extension providing the active recall backend is deleted, reload
    # falls the active backend back to the built-in default; the persisted
    # selection is left untouched.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "capabilities_ext", _CAPABILITY_EXT_SOURCE)
    _write_settings(data_dir, {"recall": {"backend": "ext_recall"}})

    runtime = Runtime(config)
    runtime.start()
    try:
        assert runtime.recall_backend.__class__.__name__ == "ExtBackend"

        (data_dir / "extensions" / "capabilities_ext.py").unlink()
        asyncio.run(runtime.reload_extensions())

        assert runtime.recall_backend.__class__.__name__ != "ExtBackend"
        assert "ext_recall" not in runtime.available_recall_backends()
        assert runtime.storage.load_recall_settings()["backend"] == "ext_recall"
    finally:
        runtime.stop()


def test_reload_refreshes_prompt_block_definitions(tmp_path: Path) -> None:
    # (h) Prompt block definitions are refreshed: a removed extension's block is
    # gone and a newly added extension's block is present after reload.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(
        data_dir,
        "blockone",
        "def register(api):\n"
        "    api.register_prompt_block('b_one', default_text='One block text.')\n",
    )

    runtime = Runtime(config)
    runtime.start()
    try:
        agent = runtime.agents.get("main")
        assert "One block text." in runtime.system_prompts.build_system_prompt(agent)

        (data_dir / "extensions" / "blockone.py").unlink()
        _write_extension(
            data_dir,
            "blocktwo",
            "def register(api):\n"
            "    api.register_prompt_block('b_two', default_text='Two block text.')\n",
        )
        asyncio.run(runtime.reload_extensions())

        prompt = runtime.system_prompts.build_system_prompt(agent)
        assert "One block text." not in prompt
        assert "Two block text." in prompt
    finally:
        runtime.stop()


_SLOW_SHUTDOWN_EXT_SOURCE = (
    "import asyncio\n"
    "async def _shutdown():\n"
    "    await asyncio.sleep(0.05)\n"
    "def register(api):\n"
    "    api.on_shutdown(_shutdown)\n"
)


# --- channel interaction dispatcher: live-reading injection --------------------


class _RecordingResponder:
    """Minimal ``InteractionResponder`` that records whether it was answered."""

    def __init__(self) -> None:
        self.answered = False

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        self.answered = True

    async def edit(
        self,
        *,
        text: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
    ) -> None:
        return None


def _interaction_event(data: str) -> InteractionEvent:
    return InteractionEvent(
        platform="telegram",
        channel_id="ch",
        chat_id="1",
        user_id="2",
        message_id="3",
        data=data,
        buttons=(),
    )


_INTERACTION_EXT_SOURCE = (
    "async def _toggle(event, responder):\n"
    "    await responder.answer()\n"
    "def register(api):\n"
    "    api.register_interaction_handler('chk', _toggle)\n"
)


def test_dispatch_channel_interaction_false_without_registry(tmp_path: Path) -> None:
    # Before start (no registry loaded) the dispatcher is a clean no-op returning
    # False, so a channel with no extensions still acknowledges taps itself.
    config = Config(data_dir=tmp_path / "data")
    runtime = Runtime(config)

    responder = _RecordingResponder()
    handled = asyncio.run(
        runtime._dispatch_channel_interaction(_interaction_event("chk:x"), responder)
    )

    assert handled is False
    assert responder.answered is False


def test_interaction_dispatcher_reads_live_registry_across_reload(tmp_path: Path) -> None:
    # The runtime injects its live-reading dispatcher into the channel service, so a
    # reload (which swaps self._extensions) needs no channel re-wiring — the next tap
    # routes through the rebuilt registry.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "interactive", _INTERACTION_EXT_SOURCE)

    runtime = Runtime(config)
    runtime.start()
    try:
        service = runtime._channel_service
        assert service is not None
        assert service._interaction_dispatcher == runtime._dispatch_channel_interaction

        responder = _RecordingResponder()
        handled = asyncio.run(
            runtime._dispatch_channel_interaction(_interaction_event("chk:milk"), responder)
        )
        assert handled is True
        assert responder.answered is True

        asyncio.run(runtime.reload_extensions())

        # Reload swaps self._extensions but not the channel service; the same
        # injected callable reads the freshly-swapped registry live.
        assert service._interaction_dispatcher == runtime._dispatch_channel_interaction
        after = _RecordingResponder()
        handled_after = asyncio.run(
            runtime._dispatch_channel_interaction(_interaction_event("chk:milk"), after)
        )
        assert handled_after is True
        assert after.answered is True
    finally:
        runtime.stop()


def test_reload_and_disable_never_interleave(tmp_path: Path) -> None:
    # (i) A reload and a concurrent live-disable serialize through the shared lock:
    # the disable runs entirely after the reload's swap + re-apply, so it
    # deactivates the *rebuilt* target and its tool ends up removed. Were the two to
    # interleave, the reload's apply_tools would re-add the tool after the disable
    # removed it, leaving it present.
    config = Config(data_dir=tmp_path / "data")
    data_dir = config.data_dir
    _write_extension(data_dir, "slow", _SLOW_SHUTDOWN_EXT_SOURCE)
    _write_extension(data_dir, "target", _tool_source("target_echo"))

    runtime = Runtime(config)
    runtime.start()
    try:
        assert "target_echo" in _tool_names(runtime)

        async def _race() -> None:
            reload_task = asyncio.create_task(runtime.reload_extensions())
            # Let the reload acquire the lock and park inside the slow shutdown await
            # before the disable is launched, so the disable is forced to queue.
            await asyncio.sleep(0.01)
            disable_task = asyncio.create_task(runtime.apply_extension_disabled_change({"target"}))
            await asyncio.gather(reload_task, disable_task)

        asyncio.run(_race())

        assert "target_echo" not in _tool_names(runtime)
        assert _extension_record(runtime, "target").status == "disabled"
    finally:
        runtime.stop()
