"""Tests for runtime extension reload."""

from __future__ import annotations

import asyncio
from pathlib import Path

from core.extensions import InteractionButton, InteractionEvent
from core.runtime.runtime import Runtime
from core.tools import ToolContext
from core.utils.config import Config
from tests.core.runtime.runtime_extensions_test_support import (
    _CAPABILITY_EXT_SOURCE,
    _authorize_session_store,
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
