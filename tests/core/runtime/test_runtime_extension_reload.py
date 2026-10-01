"""Extension reload rebuilds the layer like a restart and serializes with live changes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.extensions import InteractionButton, InteractionEvent
from core.extensions.extensions import ExtensionRegistry
from core.runtime.runtime import Runtime
from core.sessions import SessionAddress
from core.utils.config import Config
from tests.core.runtime.runtime_test_support import (
    CAPABILITY_EXT_SOURCE,
    command_extension_source,
    dispatch_tool,
    dispatch_workflow_command,
    extension_record,
    extension_record_names,
    lifecycle_extension_source,
    marker_lines,
    rewrite_source,
    tool_extension_source,
    tool_names,
    write_extension,
    write_settings,
)

INTERACTION_EXT_SOURCE = (
    "async def _toggle(event, responder):\n"
    "    await responder.answer()\n"
    "def register(api):\n"
    "    api.register_interaction_handler('chk', _toggle)\n"
)


def _prompt_block_source(block_id: str, text: str) -> str:
    return (
        f"def register(api):\n    api.register_prompt_block({block_id!r}, default_text={text!r})\n"
    )


def _write_package_extension(data_dir: Path, name: str, helper_value: str) -> Path:
    """Write a package Extension whose Tool returns a value from its submodule."""
    ext_dir = data_dir / "extensions" / name
    ext_dir.mkdir(parents=True, exist_ok=True)
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


def _tap(runtime: Runtime) -> bool:
    """Deliver a channel button tap through the dispatcher the Runtime injects."""
    responder = _RecordingResponder()
    event = InteractionEvent(
        platform="telegram",
        channel_id="ch",
        chat_id="1",
        user_id="2",
        message_id="3",
        data="chk:milk",
        buttons=(),
    )
    handled = asyncio.run(
        runtime._dispatch_channel_interaction(event, responder)  # noqa: SLF001
    )
    assert handled is responder.answered
    return handled


def test_reload_rebuilds_the_extension_layer_like_a_restart(config: Config, tmp_path: Path) -> None:
    data_dir = config.data_dir
    extensions = data_dir / "extensions"
    marker = tmp_path / "lifecycle.txt"
    write_extension(data_dir, "swap", tool_extension_source("swap_echo", "v1"))
    package = _write_package_extension(data_dir, "pkgext", "v1")
    write_extension(data_dir, "toggler", tool_extension_source("toggler_echo"))
    write_extension(data_dir, "fixme", "raise RuntimeError('boom at import')\n")
    write_extension(data_dir, "gone", tool_extension_source("gone_echo"))
    write_extension(data_dir, "workflow_ext", command_extension_source("v1"))
    write_extension(data_dir, "blockone", _prompt_block_source("b_one", "One block text."))
    write_extension(data_dir, "lifecycle_ext", lifecycle_extension_source(marker))
    write_extension(data_dir, "interactive", INTERACTION_EXT_SOURCE)
    write_extension(data_dir, "capabilities_ext", CAPABILITY_EXT_SOURCE)
    write_settings(
        data_dir, {"extensions": {"disabled": ["toggler"]}, "recall": {"backend": "ext_recall"}}
    )
    runtime = Runtime(config)
    # Before start no registry exists; a channel still acknowledges taps itself.
    assert _tap(runtime) is False

    runtime.start()
    try:
        agent = runtime.agents.get("main")
        dispatcher = runtime.command_dispatcher
        # Startup handlers wait for the serving lifespan, not for Runtime start.
        assert marker_lines(marker) == []
        asyncio.run(runtime.fire_extension_startup())
        assert dispatch_tool(runtime, "swap_echo", data_dir)["data"] == {"version": "v1"}
        assert dispatch_tool(runtime, "pkg_echo", data_dir)["data"] == {"value": "v1"}
        assert extension_record(runtime, "toggler").status == "disabled"
        assert "toggler_echo" not in tool_names(runtime)
        assert extension_record(runtime, "fixme").status == "failed"
        assert "gone_echo" in tool_names(runtime)
        assert dispatch_workflow_command(runtime) == "v1"
        assert "One block text." in runtime.system_prompts.build_system_prompt(agent)
        assert type(runtime.recall_backend).__name__ == "ExtBackend"
        # Channels receive the Runtime's live-reading dispatcher once.
        channel_dispatcher = runtime.channel_service._interaction_dispatcher  # noqa: SLF001
        assert channel_dispatcher == runtime._dispatch_channel_interaction  # noqa: SLF001
        assert _tap(runtime) is True
        assert marker_lines(marker) == ["startup"]

        # Edit a single-file Extension and a package submodule (the reload purges
        # the cached ``vbot_ext`` modules), enable one disabled at boot, fix a
        # failed one, delete one, and swap prompt blocks.
        rewrite_source(extensions / "swap.py", tool_extension_source("swap_echo", "v2"))
        rewrite_source(package / "helper.py", "VALUE = 'v2'\n")
        runtime.storage.update_settings_sections({"extensions": {"disabled": [], "config": {}}})
        rewrite_source(extensions / "fixme.py", tool_extension_source("fixme_echo"))
        (extensions / "gone.py").unlink()
        rewrite_source(extensions / "workflow_ext.py", command_extension_source("v2"))
        (extensions / "blockone.py").unlink()
        write_extension(data_dir, "blocktwo", _prompt_block_source("b_two", "Two block text."))
        asyncio.run(runtime.reload_extensions())

        assert dispatch_tool(runtime, "swap_echo", data_dir)["data"] == {"version": "v2"}
        assert dispatch_tool(runtime, "pkg_echo", data_dir)["data"] == {"value": "v2"}
        for name, tool in (("toggler", "toggler_echo"), ("fixme", "fixme_echo")):
            assert extension_record(runtime, name).status == "loaded"
            assert tool in tool_names(runtime)
        assert "gone_echo" not in tool_names(runtime)
        assert "gone" not in extension_record_names(runtime)
        assert runtime.command_dispatcher is dispatcher
        assert dispatch_workflow_command(runtime) == "v2"
        prompt = runtime.system_prompts.build_system_prompt(agent)
        assert "One block text." not in prompt
        assert "Two block text." in prompt
        # The old layer shuts down before the new one starts.
        assert marker_lines(marker) == ["startup", "shutdown", "startup"]
        assert type(runtime.recall_backend).__name__ == "ExtBackend"
        assert "ext_recall" in runtime.available_recall_backends()
        assert _tap(runtime) is True

        # Removing the providers of the active Recall backend and of a command.
        (extensions / "capabilities_ext.py").unlink()
        (extensions / "workflow_ext.py").unlink()
        asyncio.run(runtime.reload_extensions())

        assert runtime.command_dispatcher.prepare("/workflow") is None
        assert "workflow_ext" not in extension_record_names(runtime)
        # Recall falls back without rewriting the persisted selection.
        assert type(runtime.recall_backend).__name__ != "ExtBackend"
        assert "ext_recall" not in runtime.available_recall_backends()
        assert runtime.storage.load_recall_settings()["backend"] == "ext_recall"
        assert marker_lines(marker) == ["startup", "shutdown"] * 2 + ["startup"]
    finally:
        runtime.stop()
    # Shutdown handlers follow Runtime stop.
    assert marker_lines(marker) == ["startup", "shutdown"] * 3


def test_removed_extensions_leave_their_owner_managed_sessions_archived(
    config: Config, tmp_path: Path
) -> None:
    data_dir = config.data_dir
    extra = tmp_path / "extra"
    extra.mkdir()
    helper = write_extension(data_dir, "helper", tool_extension_source("helper_echo"))
    resting = write_extension(data_dir, "resting", tool_extension_source("resting_echo"))
    write_settings(
        data_dir,
        {"extension_directories": [str(extra)], "extensions": {"disabled": ["resting"]}},
    )
    runtime = Runtime(config)
    runtime.start()
    try:
        sessions = runtime.chat_sessions
        for owner in ("helper", "resting", "gone"):
            sessions.create_bound_temporary_session(
                SessionAddress("vbot", f"tmp_{owner}", "ses_participant"),
                owner_name=owner,
                group_id="group",
                participant_id="peer",
                config={},
            )

        def owners() -> list[str]:
            return asyncio.run(sessions.temporary_owners_async())

        asyncio.run(runtime.fire_extension_startup())
        # Every installed owner keeps its Sessions, a disabled one included.
        assert owners() == ["helper", "resting"]

        helper.unlink()
        asyncio.run(runtime.reload_extensions())
        assert owners() == ["resting"]

        # A missing Extension directory proves nothing about what is installed.
        resting.unlink()
        extra.rmdir()
        asyncio.run(runtime.reload_extensions())
        assert owners() == ["resting"]
    finally:
        runtime.stop()


def test_reload_and_disable_never_interleave(config: Config) -> None:
    # A live disable queued behind a reload runs entirely after the reload's swap
    # and re-apply, so it deactivates the rebuilt Extension. Were the two to
    # interleave, the reload would re-add the Tool the disable removed.
    write_extension(config.data_dir, "target", tool_extension_source("target_echo"))
    runtime = Runtime(config)
    runtime.start()

    async def race(*, cancel_disable: bool) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_shutdown() -> None:
            entered.set()
            await release.wait()

        extension_record(runtime, "target").declarations.shutdown.append(blocking_shutdown)
        reload_task = asyncio.create_task(runtime.reload_extensions())
        await asyncio.wait_for(entered.wait(), timeout=5)
        disable_task = asyncio.create_task(runtime.apply_extension_disabled_change({"target"}))
        await asyncio.sleep(0)
        assert not disable_task.done()
        if cancel_disable:
            disable_task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await disable_task
            await reload_task
        else:
            release.set()
            await asyncio.gather(reload_task, disable_task)

    async def exercise() -> None:
        # A cancelled queued disable changes nothing; the reloaded Extension stays.
        await race(cancel_disable=True)
        assert "target_echo" in tool_names(runtime)
        assert extension_record(runtime, "target").status == "loaded"
        await race(cancel_disable=False)
        assert "target_echo" not in tool_names(runtime)
        assert extension_record(runtime, "target").status == "disabled"

    try:
        asyncio.run(exercise())
    finally:
        runtime.stop()


def test_cancelled_extension_lifecycle_finishes_admitted_cleanup(config: Config) -> None:
    # One Runtime goes through every lifecycle operation in turn; each caller is
    # cancelled while an Extension handler it admitted is still running.
    write_extension(config.data_dir, "target", tool_extension_source("target_echo"))
    write_extension(config.data_dir, "closing", tool_extension_source("closing_echo"))
    runtime = Runtime(config)
    runtime.start()

    async def cancel_while_admitted(operation: str) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        finished = False
        calls = 0

        async def handler() -> None:
            nonlocal finished, calls
            calls += 1
            entered.set()
            await release.wait()
            finished = True

        owner = "closing" if operation == "close" else "target"
        declarations = extension_record(runtime, owner).declarations
        (declarations.startup if operation == "startup" else declarations.shutdown).append(handler)
        action = {
            "reload": runtime.reload_extensions,
            "disable": lambda: runtime.apply_extension_disabled_change({"target"}),
            "startup": runtime.fire_extension_startup,
            "close": runtime.aclose,
        }[operation]
        pending = asyncio.create_task(action())
        await asyncio.wait_for(entered.wait(), timeout=5)
        another_close = asyncio.create_task(runtime.aclose()) if operation == "close" else None
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        if another_close is not None:
            await another_close

        assert finished, operation
        assert calls == 1, operation

    async def exercise() -> None:
        await cancel_while_admitted("startup")
        assert "target_echo" in tool_names(runtime)
        await cancel_while_admitted("reload")
        assert "target_echo" in tool_names(runtime)
        await cancel_while_admitted("disable")
        assert "target_echo" not in tool_names(runtime)
        assert extension_record(runtime, "target").status == "disabled"
        await cancel_while_admitted("close")
        assert runtime.extensions is None
        with pytest.raises(RuntimeError, match="Runtime not started"):
            runtime.chat_sessions  # noqa: B018 - the close completed.

    try:
        asyncio.run(exercise())
    finally:
        runtime.stop()


def test_close_drains_inflight_reload_before_clearing_services(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_extension(config.data_dir, "target", tool_extension_source("target_echo"))
    runtime = Runtime(config)
    runtime.start()
    original_load = ExtensionRegistry.aload

    async def exercise() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_load(*args: Any, **kwargs: Any) -> ExtensionRegistry:
            entered.set()
            await release.wait()
            return await original_load(*args, **kwargs)

        monkeypatch.setattr(ExtensionRegistry, "aload", delayed_load)
        reload_task = asyncio.create_task(runtime.reload_extensions())
        await asyncio.wait_for(entered.wait(), timeout=5)
        close_task = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)
        assert not close_task.done()
        release.set()
        # The reload finished against live services before the close cleared them.
        assert await asyncio.gather(reload_task, close_task, return_exceptions=True) == [
            None,
            None,
        ]
        assert runtime.extensions is None
        with pytest.raises(RuntimeError, match="Runtime not started"):
            runtime.tools  # noqa: B018

    try:
        asyncio.run(exercise())
    finally:
        runtime.stop()
