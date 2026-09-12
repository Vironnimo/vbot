"""Tests for runtime extensions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.chat import CommandExecutionContext, ReplySurface
from core.extensions.extensions import ExtensionRegistry
from core.runtime import runtime as runtime_module
from core.runtime._configuration import _resolve_resources_path
from core.runtime.runtime import Runtime
from core.tools import ToolContext
from core.utils.config import Config
from tests.core.runtime.runtime_extensions_test_support import (
    _CAPABILITY_EXT_SOURCE,
    _command_extension_source,
    _extension_record,
    _marker_lines,
    _rewrite_source,
    _write_extension,
    _write_settings,
)
from tests.core.runtime.runtime_extensions_test_support import (
    _clean_extension_modules as _clean_extension_modules,
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
        expected = _resolve_resources_path(runtime.config) / "extensions"
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
